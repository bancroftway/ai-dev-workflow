"""R -- the reusable "clean & rebuild" node (plan's Part B, section R).

No LLM at all in the happy path: `make_rebuild_node` runs a stack-appropriate clean+build command
and gates on its exit code. Only on failure does an LLM-backed fix node run, and even then with a
fix_scope-restricted prompt (see RebuildSpec.fix_scope's docstring) rather than unrestricted access
-- R's gate is "does it build," never "do tests pass," and two different placements in the
pipeline need two very different answers to "what is the fix node allowed to touch."

Kept as its own module (not folded into graph.py) since RebuildSpec/RebuildState are a genuinely
different node shape from StageSpec's draft->audit->gate template -- graph.py's build_graph()
wires make_rebuild_node/make_fix_node in at each of R's several placements (after P4, after P6,
after quality-remediation, after security-remediation, after audit-cluster), each with a different fix_scope and next_node.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from .prompt_loader import load_prompt_pair, render_prompt

from . import config, git_ops, model_config, preflight_nodes, repo_files, run_event_store, run_event_stream, run_failure, stack_runner, tech_stack_signals, test_results, workflow_persistence
from .text_truncate import truncate_middle
from .gates.checks import Check, CheckLog
from .run_events import RunEvent, RunEventType, encode_io_text
from .chat_model import close_session, get_chat_model_for_thread, lap_role
from .infra_retry import call_with_infra_retry
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .schemas import StageReport

FixScope = Literal["scaffold_only", "full"]


class BuildCommand(BaseModel):
    cwd: str = Field(description="Repo-relative directory the command was run from (e.g. apps/api.Tests).")
    command: str = Field(description="The exact build command run there (e.g. `dotnet build`).")


class BuildVerifyReport(StageReport):
    """What the build-verification agent must report (prompts/rebuild_verify.md)."""

    ok: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    # The build contract: every (cwd, command) the discovery turn actually ran. Fix laps REPLAY
    # these in Python (rebuild_node) instead of asking the model again -- observed live (run
    # d16959d3): the verifier session ran `dotnet build` on lap 0 only, then answered laps 1-3 from
    # conversation memory with zero tool calls, re-reporting an error the fix agent had already
    # repaired. Same stale-artifact class as the coverage contract replay (coverage_run.md step 0).
    build_commands: list[BuildCommand] = Field(default_factory=list)


class RebuildState(TypedDict):
    status: Literal["not_started", "clean", "failed", "fixing"]
    fix_cycle_count: int
    last_stdout_tail: str
    last_stderr_tail: str
    last_exit_ok: bool
    cannot_verify: bool  # sandbox missing at run time -- the build never ran, escalate not pass
    build_commands: list[dict[str, str]]  # discovery turn's contract, replayed on fix laps
    last_red_detail: str  # previous lap's TDD-red-gate finding, to detect a stuck fix session
    last_scan_fingerprint: frozenset[str]  # previous lap's scan-delta gating findings (line-number-free)
    checks: list[dict[str, Any]]  # the latest lap's per-check rows (CheckResult.to_dict), for its gate screen
    passed_run_id: str  # the run this placement last passed in ("" = never) ...
    passed_commit: str  # ... and HEAD right after that pass's commit -- the tree proven to build
    attempt_started_at: str  # the run attempt that last ran this placement (graph.GraphState) ...
    checked_at: str  # ... and when (UTC ISO) -- the gate screen labels a result from an earlier attempt


def default_rebuild_state() -> RebuildState:
    return {
        "status": "not_started", "fix_cycle_count": 0, "last_stdout_tail": "", "last_stderr_tail": "",
        "last_exit_ok": False, "cannot_verify": False, "build_commands": [], "last_red_detail": "",
        "last_scan_fingerprint": frozenset(), "checks": [], "passed_run_id": "", "passed_commit": "",
        "attempt_started_at": "", "checked_at": "",
    }


async def stage_started(provider: Any, thread_id: str, state: dict[str, Any], stage_key: str) -> bool:
    """Whether `stage_key` has touched this workspace: a status other than "not_started" (a draft
    marks "drafting" before its first model call; an interrupted draft whose work intake set aside
    -- graph._set_aside_interrupted_work -- stays "not_started", since its tree is back to the
    pre-draft state), or its completed draft artifact on the branch, which rides
    the workspace volume across container swaps and survives intake's status resets."""
    status = ((state.get("stages") or {}).get(stage_key) or {}).get("status", "not_started")
    if status != "not_started":
        return True
    return await repo_files.read_repo_file(provider, thread_id, workflow_persistence.stage_draft_path(stage_key)) is not None


def _has_rates(coverage: dict[str, Any]) -> bool:
    return isinstance(coverage.get("line_rate"), (int, float)) and isinstance(coverage.get("branch_rate"), (int, float))


async def _remeasure_coverage(provider: Any, thread_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Fresh line/branch coverage of the tree as it stands (gates.test_coverage_gate.measure_coverage,
    the measurement Code's own verify uses). {} when it can't produce numbers -- fail-soft, the caller
    falls back to the stored value."""
    from .gates import test_coverage_gate  # local: keeps rebuild's import graph as it was

    try:
        line_rate, branch_rate, _gaps, _reason, _entries = await test_coverage_gate.measure_coverage(
            provider, thread_id, chat_provider=state["provider"], run_id=state.get("run_id", "unknown"),
        )
    except Exception:  # noqa: BLE001 -- fail-soft: never let a measurement crash read as a regression
        logger.warning("rebuild scan: coverage re-measure crashed for thread_id=%s", thread_id, exc_info=True)
        return {}
    coverage = {"line_rate": line_rate, "branch_rate": branch_rate}
    return coverage if _has_rates(coverage) else {}


async def _coverage_for_scan(provider: Any, thread_id: str, state: dict[str, Any], *, remeasure: bool) -> dict[str, Any]:
    """The coverage _scan_regression_reasons judges: a fresh measurement when asked (after a fix
    lap), else -- or when that yields no numbers -- the value promoted onto state, else the
    artifacts on disk."""
    from . import metrics_nodes  # local: metrics_nodes imports this module's package siblings at load

    coverage: dict[str, Any] = await _remeasure_coverage(provider, thread_id, state) if remeasure else {}
    if not _has_rates(coverage):
        coverage = (state.get("repo_scan") or {}).get("coverage") or {}
    if not _has_rates(coverage):
        coverage = await metrics_nodes._read_coverage_summary(provider, thread_id)  # noqa: SLF001 -- same package, one reader
    return coverage


def should_skip_rebuild(rb: dict[str, Any], run_id: str | None, next_stage_started: bool) -> bool:
    """A placement that already passed in THIS run, whose next stage has since started, is done:
    building again on a resume would judge the next stage's work (finished or interrupted) against
    this placement's contract -- session c2bbdca1's resume rebuilt ac-to-tests' check over an
    interrupted implementation draft and burned its fix laps gutting the tests. Pure."""
    return bool(
        rb.get("status") == "clean" and run_id and rb.get("passed_run_id") == run_id and next_stage_started
    )


def blame_downstream(payload: dict[str, Any], spec: "RebuildSpec", passed_commit: str, changed: list[str]) -> dict[str, Any]:
    """The escalation payload re-pointed at the stage after this placement: the check passed
    earlier this run at `passed_commit`, so a failure now comes from the files changed since --
    that stage's work, not the placement's own stage (session c2bbdca1 failed "at Tests" for code
    an interrupted implementation draft left behind). Pure."""
    shown = changed[: config.REBUILD_BLAME_FILES_PREVIEW_MAX]
    more = f" (+{len(changed) - len(shown)} more)" if len(changed) > len(shown) else ""
    note = (
        f"Not a {spec.key} regression: this check passed earlier this run at {passed_commit[:12]}, and now "
        f"fails on {len(changed)} file(s) changed since -- {spec.next_stage_key}'s work (an interrupted "
        f"{spec.next_stage_key} draft leaves exactly this): {', '.join(shown)}{more}"
    )
    return {
        **payload,
        "stage": spec.next_stage_key,
        "blamed_check": spec.key,
        "changed_since_pass": shown,
        "feedback": f"{note}\n\n{payload.get('feedback') or ''}".strip(),
    }


# Matches one gating line _scan_regression_reasons appends: "  gating: [severity] category/rule_id
# @ file[:line] -- title". Captures (category/rule_id, file) WITHOUT the line number, so a fix that
# only shuffles the vulnerable code to a different line in the same file still reads as the same
# finding -- exact-text comparison would miss that (observed live: detect-non-literal-fs-filename
# on apps/web/serve-dist.js recurring at lines 19/30 -> 29/31 -> 32/36/40/44 -> 34/39/44/49 across
# four fix laps, four different-looking strings for what was never actually fixed).
_GATING_LINE_RE = re.compile(r"gating: \[\w+\] (\S+) @ (\S+?)(?::\d+)? --")


def _scan_finding_fingerprint(scan_reasons: list[str]) -> frozenset[str]:
    """Which (rule, file) pairs a scan-delta lap's gating findings name, line-number-free. Pure."""
    return frozenset(f"{rule}@{file}" for rule, file in _GATING_LINE_RE.findall("\n".join(scan_reasons)))


async def _replay_build(provider: Any, thread_id: str, commands: list[dict[str, str]]) -> BuildVerifyReport:
    """Deterministic re-verify: run the discovery turn's exact build commands and judge on exit
    codes. No model in the loop, so the verdict can only ever describe the tree as it is NOW."""
    ok = True
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for entry in commands:
        cwd, command = entry.get("cwd") or ".", entry.get("command") or ""
        if not command:
            continue
        result = await provider.exec_in_sandbox(thread_id, f"cd {shlex.quote(cwd)} && {command}")
        ok = ok and result.ok
        label = f"[{cwd}] $ {command} (exit {result.returncode})"
        # 2000/4000 (pre-2026-09-09) truncated a multi-error compiler log to its last ~6-12 lines --
        # observed live on a 126-error `dotnet build` (angular-dotnet, apps/api.Tests, CA1859/CA1861
        # analyzer-as-error violations repeated near-identically across 9 test files): the fix agent
        # only ever saw the last handful of errors each lap, so 3 fix cycles kept whack-a-moling the
        # same trailing subset while the rest -- invisible every single lap -- never got touched.
        # 8000/16000 is still bounded (not every log gets forwarded verbatim), just wide enough for a
        # realistic multi-dozen-error build to actually reach the model that has to fix it.
        #
        # truncate_middle, not a tail-only slice (fixed alongside the Org Settings migration, root-
        # caused via that migration's own removability research): the widen 2000/4000 -> 8000/16000
        # above fixed the SIZE but not the SHAPE -- a `[-N:]` slice still drops whatever comes before
        # the tail, the exact bug this same comment credits itself with fixing. Halving each single
        # config value between head_chars/tail_chars keeps today's total budget unchanged while
        # actually keeping both ends.
        _tail_half = config.REBUILD_OUTPUT_TAIL_CHARS // 2
        stdout_parts.append(f"{label}\n{truncate_middle(result.stdout or '', _tail_half, _tail_half)}")
        stderr_parts.append(f"{label}\n{truncate_middle(result.stderr or '', _tail_half, _tail_half)}")
    _combined_half = config.REBUILD_OUTPUT_COMBINED_TAIL_CHARS // 2
    return BuildVerifyReport(
        success=ok, ok=ok,
        stdout_tail=truncate_middle("\n".join(stdout_parts), _combined_half, _combined_half),
        stderr_tail=truncate_middle("\n".join(stderr_parts), _combined_half, _combined_half),
        error=None if ok else "replayed build command(s) failed -- see stderr_tail",
        build_commands=[BuildCommand(**c) for c in commands if c.get("command")],
    )


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RebuildSpec:
    key: str
    max_fix_cycles: int
    fix_prompt_addendum: str
    fix_scope: FixScope
    next_node: str
    # Re-scan after a green build and block on anything the TERMINAL metrics gate would block on.
    #
    # Set on the LAST placement that follows a code-writing stage. The metrics gate is terminal: it
    # can fail a run but never fix one, so any defect introduced after the final remediation pass
    # surfaces an hour later as an unfixable verdict. Observed live (run 026dee4f): remediation
    # approved with health 100 and duplication 0.0%, then adversarial-compliance spent FIVE fix laps
    # rewriting scaffolding and wireframes, and the terminal gate reported duplication 10.5%, one
    # gating finding, and coverage that had gone from measured (38/38, 99/99 lines) to unmeasurable.
    # Nothing between those two points scanned anything, so nothing could act on it.
    #
    # This closes that window: the same findings now fail the rebuild that caused them, while a fix
    # loop still exists and the feedback can name what regressed.
    scan_delta_gate: bool = False
    # The real stage that runs after this placement (pipeline_layout's rebuild_placements reads it
    # too). Once that stage has started, a re-run of this check on a resume would build against
    # the next stage's work -- see should_skip_rebuild and blame_downstream.
    next_stage_key: str = ""


# The checks a rebuild placement runs, shown as rows on the gate after the stage it follows
# (gate_view) and recorded per lap into RebuildState.checks -- same Check/CheckLog vocabulary as a
# stage's own verify, so a failed rebuild check reads (and is recovered) exactly like one.
REBUILD_BUILD = Check(
    "rebuild.build", "Builds cleanly",
    "Every buildable project in the tree compiles after this stage's changes.", "blocking",
)
_RED_CONDITION = "only before any implementation exists"
RED_SUITES_START = Check(
    "rebuild.red_suites_start", "Every test suite starts",
    "Each test suite compiles, loads its config and runs its tests -- a suite that crashes before "
    "running any test proves nothing about them.", "blocking", _RED_CONDITION,
)
RED_PLANNED_FILES = Check(
    "rebuild.red_planned_files", "Every planned test file runs",
    "Each test file the approved test plan names is run by some suite, so a whole suite can't "
    "silently drop out.", "blocking", _RED_CONDITION,
)
RED_NONE_PASS = Check(
    "rebuild.red_none_pass", "No test passes before implementation",
    "Every test fails at runtime against the stub-only scaffold -- a test that already passes proves "
    "nothing about the code still to be written.", "blocking", _RED_CONDITION,
)
SCAN_DELTA = Check(
    "rebuild.scan_delta", "No new scan regressions",
    "A full re-scan finds nothing the final merge gate would refuse: duplication, gating findings, "
    "unmeasurable coverage.", "blocking", "only on the last rebuild of the run",
)
_RED_CHECKS = (RED_SUITES_START, RED_PLANNED_FILES, RED_NONE_PASS)


def rebuild_checks(spec: "RebuildSpec") -> tuple[Check, ...]:
    """The ordered checks `spec`'s placement can record."""
    return (
        REBUILD_BUILD,
        *(_RED_CHECKS if spec.fix_scope == "scaffold_only" else ()),
        *((SCAN_DELTA,) if spec.scan_delta_gate else ()),
    )


# Where the TDD-red gate's suite run tees its console output (same convention as the AC gate's
# AC_TEST_OUTPUT_PATH; separate file so the two runs never clobber each other's evidence).
_RED_GATE_OUTPUT_PATH = "agent-work/red-gate-output.txt"


def _accounted_files(suites: list[Any]) -> set[str | None]:
    """Repo-relative paths of every file the run's suites account for. A file may be listed
    repo-relative or relative to its suite's own root -- both resolve to the same path (observed
    live: `NoteServiceTests.cs` under root `apps/api.Tests`, which an exact repo-relative match
    read as "never run" for every planned file). Pure."""
    out: set[str | None] = set()
    for s in suites:
        for f in s.files:
            out.add(test_results.repo_relative(f))
            out.add(test_results.repo_relative(posixpath.normpath(posixpath.join(s.root or ".", f))))
    return out


async def _planned_test_files(provider: Any, thread_id: str) -> list[str]:
    """The approved test plan's test file paths (05-ac-to-tests.approved.json test_files[].path);
    [] when there is no readable plan -- the red gate then just has no file list to hold the run to."""
    raw = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.AC_TO_TESTS_APPROVED_PATH)
    try:
        files = (json.loads(raw) if raw else {}).get("test_files") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    return [f["path"] for f in files if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]]


def red_gate_verdict(outcomes: dict[str, str]) -> tuple[bool, list[str], int]:
    """(all_red, passing test names, failed count) over runner-reported outcomes. Pure.

    Vacuous red is a FAIL: zero parsed outcomes means the suite never demonstrably ran, and "all
    zero tests are failing" must not open the gate."""
    passed = sorted(name for name, outcome in outcomes.items() if outcome == "pass")
    failed = sum(1 for outcome in outcomes.values() if outcome == "fail")
    return (not passed and failed > 0), passed, failed


def eligible_red_verdict(outcomes: dict[str, str], eligible_ac_ids: set[str]) -> tuple[bool, list[str], int]:
    """red_gate_verdict scoped to THIS ticket's undelivered criteria. Pure.

    On a second-or-later ticket the whole-suite all-red contract is wrong by construction --
    completed criteria's regression tests are legitimately GREEN -- but the NEW criteria still
    deserve their "watch it fail" moment. A test is in scope when its runner-reported name
    attributes (test_results.ac_ids_in_name) to an eligible AC; everything else may pass freely.
    Vacuous red (no test attributes to any eligible AC) is a FAIL, same rule as red_gate_verdict.
    """
    scoped = {
        name: outcome
        for name, outcome in outcomes.items()
        if set(test_results.ac_ids_in_name(name)) & eligible_ac_ids
    }
    passed = sorted(name for name, outcome in scoped.items() if outcome == "pass")
    failed = sum(1 for outcome in scoped.values() if outcome == "fail")
    return (not passed and failed > 0), passed, failed


def _should_skip_toolchain_capture(fix_scope: str, is_greenfield: bool) -> bool:
    """True when this rebuild placement must neither READ NOR WRITE manifest.json's shared
    `toolchain.build_commands` (Task 6, requirement 3's timing correction). Pure, so the
    greenfield/brownfield gating is directly testable without a sandbox.

    Only the ONE scaffold_only placement (r_ac_to_tests) on a GENUINELY greenfield first ticket is
    excluded: scaffold_finalize_node writes no application scaffold at all for a greenfield repo,
    so that placement's own build (against its own compile-enabling stubs, not the real app
    minimal-code-to-green will later write) is not yet a build worth locking in for every later
    placement to reuse. `is_greenfield` alone is not enough to gate this -- it is a fixed per-run
    classification computed once from app_scan, still True for the rest of a fresh greenfield run
    even after minimal-code-to-green has written the real app -- so `fix_scope` narrows it to just
    the one placement this timing concern actually applies to; r_minimal_code_to_green
    (fix_scope="full", the very next placement) is where "persist whichever placement discovers it
    first" naturally lands instead, exactly as intended. A brownfield repo (or ticket 2+ on an
    already-scaffolded one, where `is_greenfield` already reads False) is never excluded at all.
    """
    return fix_scope == "scaffold_only" and is_greenfield


async def _scan_regression_reasons(
    provider: Any, thread_id: str, state: dict[str, Any], *, remeasure_coverage: bool = False
) -> list[str]:
    """What the TERMINAL metrics gate would block this tree on, evaluated now.

    Calls metrics_nodes.regression_reasons -- the same pure decision function the exit gate uses --
    rather than re-deriving "too much duplication" here. Two definitions of the same threshold drift
    apart, and a pre-gate that disagreed with the gate it front-runs would be worse than no pre-gate
    at all: it would either block work the exit gate would have passed, or pass work it will not.

    Fails OPEN (returns []) if THE SCAN cannot run. An infrastructure gap must not read as a quality
    regression -- the terminal gate still stands behind this, so nothing is waved through
    permanently; it just is not blocked HERE on evidence that was never collected.

    The try covers ONLY the scan call, deliberately. A first version wrapped the whole body, and
    when this function read `scan.summary` as an attribute instead of calling the method, the
    resulting AttributeError was swallowed and logged as "could not scan" -- a programming error
    wearing an infrastructure error's clothes, silently disabling the gate on a live run. Everything
    after the scan is pure dict work over data that already exists: if it raises, that is a bug in
    THIS function and it should be loud.
    """
    from . import metrics_nodes, repo_scan

    try:
        # org_gitleaks_allowlist() already fails open on its own (never raises), but it lives
        # inside this exact try anyway -- this function's own contract is "only the scan call is
        # covered", and the allowlist fetch is part of standing the scan up correctly.
        gitleaks_stopwords, gitleaks_allow_paths = await repo_scan.org_gitleaks_allowlist()
        scan = await repo_scan.run_repo_scan(
            provider, thread_id, profile="full",
            gitleaks_extra_stopwords=gitleaks_stopwords, gitleaks_extra_allow_paths=gitleaks_allow_paths,
        )
    except Exception:  # noqa: BLE001 -- scan execution only; see the fail-open contract above
        logger.warning(
            "scan-delta gate: scan could not run for thread %s -- not blocking on it",
            thread_id[:8], exc_info=True,
        )
        return []

    # summary() is a METHOD with keyword args, not an attribute. Called the same way metrics_nodes
    # calls it, so both gates see the same shape -- INCLUDING the known-gap exemption (Ruling 8):
    # metrics_compute passes remediation's approved `known_gaps` ids so an honestly-explained
    # finding (a transitive CVE with no fixed_version, say) doesn't gate. This pre-gate omitted
    # them, so exactly that finding passed remediation's own gate, would pass the terminal gate,
    # and still re-blocked the adversarial rebuild for its full 3 laps -> escalate.
    try:
        known_gaps = await metrics_nodes._read_known_gaps(provider, thread_id)  # noqa: SLF001 -- same package
    except Exception:  # noqa: BLE001 -- an unreadable remediation report just means no exemptions
        known_gaps = []
    known_gap_ids = metrics_nodes._known_gap_finding_keys(known_gaps, scan.findings)  # noqa: SLF001
    latest_summary = scan.summary(known_gap_ids=known_gap_ids)
    # Prefer the contract-merged coverage minimal-code-to-green's own gate promoted onto state,
    # then FALL BACK to parsing the artifacts off disk -- exactly the order metrics_nodes uses.
    #
    # The fallback is not optional. On a resume, minimal-code-to-green hydrates as approved and its
    # verify never runs, so nothing promotes coverage onto state -- and reading only the promoted
    # value reported "coverage unmeasured" while both cobertura files sat in
    # apps/{api,web}.Tests/TestResults/. That is an unfixable instruction: the gate demanded the
    # agent repair a measurement that was already correct, and it burned fix laps on it while the
    # two genuine findings beside it were cleared in one.
    #
    # After a fix lap (remeasure_coverage), the stored value is stale: it was measured BEFORE the
    # fixer changed tests, so judging the new tree on it fails every lap no matter what the fixer
    # does (session c2bbdca1: four laps of added tests, all judged on the same 87.1%/76.1% from
    # before the first lap). Measure again -- the same measurement Code's verify uses -- and fall
    # back to the stored/on-disk value only if that measurement can't produce numbers (an infra
    # gap must not read as "coverage unmeasured", per the fallback rule above).
    coverage = await _coverage_for_scan(provider, thread_id, state, remeasure=remeasure_coverage)
    baseline = (state.get("repo_scan") or {}).get("baseline_summary") or {}
    reasons = metrics_nodes.regression_reasons(
        latest_summary,
        None,  # no delta: this is an absolute check on the tree as it stands right now
        coverage,
        baseline_has_findings=bool((baseline.get("gating_count") or 0)),
        coverage_gated=metrics_nodes.coverage_threshold_gated(state),
    )
    # NAME the gating findings, never just count them. "4 gating finding(s) open" with no
    # identities is an unfixable instruction: the fix agent changed real code for four straight
    # laps against a byte-identical count and escalated a run whose every stage was approved
    # (observed live, run e890f410) -- and because this scan is in-memory only, not even a human
    # could see what the four findings WERE afterward. Same "enumerated list, not judgement"
    # rule the adversarial audit prompt already carries.
    if any("gating finding" in reason for reason in reasons):
        gating = [
            f for f in scan.findings
            if repo_scan.is_gating(
                f,
                severity_floor=latest_summary.get("severity_floor") or config.SECURITY_SEVERITY_FLOOR,
                introduced_ids=None,
                direct_dependencies=scan.direct_dependencies,
                known_gap_ids=known_gap_ids,
            )
        ]
        for f in gating[:config.REBUILD_GATING_FINDINGS_MAX]:
            reasons.append(
                f"  gating: [{f.severity}] {f.category}/{f.rule_id} @ {f.file or '?'}"
                + (f":{f.line}" if f.line else "")
                + f" -- {(f.title or f.message or '')[:config.REBUILD_FINDING_MESSAGE_CHARS]}"
            )
        if len(gating) > config.REBUILD_GATING_FINDINGS_MAX:
            reasons.append(f"  ...and {len(gating) - config.REBUILD_GATING_FINDINGS_MAX} more gating finding(s)")
    if reasons:
        logger.warning("scan-delta gate: blocking on %d reason(s): %s", len(reasons), "; ".join(reasons))
    return reasons


async def _eligible_ac_ids_for_run(provider: Any, thread_id: str, run_id: str, *, new_or_modified_only: bool = False) -> set[str]:
    """This ticket's undelivered AC ids, from the persisted spec + ledger (this node has no
    stage content of its own). Empty set on any read/parse failure -- callers treat empty as
    "nothing to scope to" and skip their check, the fail-open posture every rebuild check keeps.

    `new_or_modified_only` narrows to ids this RUN itself introduced or reworded
    (spec_ledger.change_status in ("new","modified")) -- for the TDD-red gate specifically, never
    for ac-coverage's own (unfiltered) work-queue scoping. Carried-over debt (an AC an EARLIER run
    left undelivered, untouched by this run's own citations) has no business being held to "must
    currently fail": its wording may describe a property that is already true as an emergent
    consequence of other, already-shipped code (observed live: 'no separate mechanism needed to
    discard out-of-order responses' -- a criterion whose own text says nothing new is required).
    Demanding a red proof for that is unsatisfiable without breaking correct, delivered behavior.
    Carried-over debt still owes coverage (ac-coverage's own gate still requires it); it just does
    not owe a fresh watch-it-fail moment on someone else's incomplete work.
    """
    import json

    from . import spec_ledger

    raw_spec = await repo_files.read_repo_file(
        provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH
    )
    if raw_spec is None:
        return set()
    try:
        own = spec_ledger.own_ac_ids_from_specification(json.loads(raw_spec))
    except json.JSONDecodeError:
        return set()
    entries = await spec_ledger.load_ledger(provider, thread_id)
    eligible = set(spec_ledger.eligible_ac_ids(entries, own))
    if not new_or_modified_only:
        return eligible
    by_id = {e.get("id"): e for e in entries}
    return {
        ac_id for ac_id in eligible
        if ac_id in by_id and spec_ledger.change_status(by_id[ac_id], run_id) in ("new", "modified")
    }


async def _provenance_reasons(provider: Any, thread_id: str, state: dict[str, Any]) -> list[str]:
    """Re-run of the provenance protections at the LAST gate before metrics: every stage after
    ac-to-tests (codegen, rebuild fixes, remediation, test-hardening, e2e, adversarial) can write
    test files, and none of their own checks read AC status or the ledger -- without this re-check
    a fix lap could delete a completed criterion's regression test or resurrect a retired one with
    nothing noticing until (or past) the terminal gate. Fails OPEN on infra errors, same contract
    as _scan_regression_reasons."""
    from . import spec_ledger
    from .gates.ac_coverage_gate import (
        check_completed_ac_protection,
        check_deferred_ac_residue,
        check_ledger_integrity,
        check_retired_ac_residue,
    )

    try:
        entries = await spec_ledger.load_ledger(provider, thread_id)
        baseline = ((state.get("stages") or {}).get("ac-to-tests") or {}).get("baseline_commit")
        return (
            await check_ledger_integrity(provider, thread_id)
            + await check_retired_ac_residue(provider, thread_id, entries)
            + await check_deferred_ac_residue(provider, thread_id, entries)
            + await check_completed_ac_protection(provider, thread_id, baseline, entries)
        )
    except Exception:  # noqa: BLE001 -- fail-open, mirrors _scan_regression_reasons
        logger.warning(
            "provenance re-check could not run for thread %s -- not blocking on it",
            thread_id[:8], exc_info=True,
        )
        return []


def _red_outcome_verdict(
    outcomes: dict[str, str], eligible_only: set[str] | None, report_error: str | None,
) -> tuple[bool, str]:
    """The TDD-red verdict on the parsed outcomes: nothing (in scope) passed, something failed. Pure."""
    if not outcomes:
        return False, (
            "TDD-red gate: could not verify a single test outcome -- the suite run produced no "
            f"parseable runner report ({report_error or 'no result_artifacts reported'}). Re-run "
            "the suite with a machine-readable reporter (.trx / vitest-json / playwright-json); "
            "the pipeline does not proceed until every test demonstrably FAILS."
        )
    if eligible_only is not None:
        # Ticket-mode red: only this ticket's undelivered criteria must fail RED; completed
        # criteria's regression tests are legitimately green and must NOT be stripped to stubs.
        red_ok, passed, failed = eligible_red_verdict(outcomes, eligible_only)
        if red_ok:
            return True, f"TDD-red verified for this ticket's criteria: 0 passed / {failed} failed."
        if not passed:
            return False, (
                "TDD-red gate (ticket scope): no test in the suite attributes to this ticket's "
                f"undelivered criteria ({', '.join(sorted(eligible_only))}) -- the RED tests for "
                "them either were not written or do not name their criterion ids."
            )
        names = ", ".join(passed[:config.REBUILD_PASSED_TESTS_PREVIEW_MAX]) + (
            f", and {len(passed) - config.REBUILD_PASSED_TESTS_PREVIEW_MAX} more"
            if len(passed) > config.REBUILD_PASSED_TESTS_PREVIEW_MAX else ""
        )
        return False, (
            f"TDD-red gate (ticket scope): {len(passed)} test(s) for this ticket's undelivered "
            f"criteria PASSED after scaffolding ({failed} failed): {names}. Strip only THOSE code "
            "paths back to stubs so they fail at runtime -- leave earlier tickets' passing "
            "regression tests untouched."
        )
    all_red, passed, failed = red_gate_verdict(outcomes)
    if not all_red:
        names = ", ".join(passed[:config.REBUILD_PASSED_TESTS_PREVIEW_MAX]) + (
            f", and {len(passed) - config.REBUILD_PASSED_TESTS_PREVIEW_MAX} more"
            if len(passed) > config.REBUILD_PASSED_TESTS_PREVIEW_MAX else ""
        )
        return False, (
            f"TDD-red gate: {len(passed)} test(s) PASSED after scaffolding ({failed} failed): "
            f"{names}. Scaffolding must not implement behavior -- strip those code paths back to "
            "NotImplementedException-style stubs so every test fails at runtime; a test that "
            "passes before the implementation stage proves nothing."
        )
    return True, f"TDD-red verified: 0 passed / {failed} failed."


async def _verify_all_red(
    thread_id: str, chat_provider: str, spec_key: str, run_id: str = "unknown",
    eligible_only: set[str] | None = None, lap: int = 0, log: CheckLog | None = None,
) -> tuple[bool, str]:
    """Deterministic TDD-red gate: run the suite, parse the runners' own structured reports, and
    require zero passing tests (and at least one failing). The scaffold fix node is INSTRUCTED to
    keep tests failing at runtime; this is the check that stops an over-implemented scaffold --
    an accidental green here means a test that will never have its "watch it fail" moment.

    `eligible_only` switches to the ticket-mode contract (eligible_red_verdict): on a
    second-or-later ticket, only tests attributing to those undelivered criteria must be red --
    the earlier tickets' regression suite is legitimately green.

    `chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is threaded straight
    through to stack_runner.run_and_report below, which now requires it itself. `run_id` (Phase E
    known-bugs fix) is threaded the same way, defaulting to "unknown" -- this function has no
    `state` of its own, same reasoning as chat_provider.

    `spec_key` (Overview-tab rebuild-row fix, 2026-09-22): only caller today is
    make_rebuild_node, gated on `spec.fix_scope == "scaffold_only"` (only r_ac_to_tests uses this
    gate), but this turn's own events were still tagged with the bare, placement-blind
    `stage_key="red-gate"` literal -- indistinguishable from any other placement that might reuse
    this gate later. Placement-specific now (`f"red-gate-{spec_key}"`), matching the pattern
    already established at make_fix_node's `f"rebuild-{spec.key}"`. model_name is resolved
    explicitly from the "ac-test-run" model_config.Stage key (Task 6 naming fix) -- this turn runs
    the identical `ac_test_run` prompt ac_coverage_gate.py's own ac-test-run stage does, so reusing
    its already-registered Stage entry is the correct fit, not a new "red-gate" Stage literal for a
    key nothing else needs. Previously resolved from the bare string "red-gate", which was never a
    registered Stage (model_config.get_model_name silently returned None for it every time) and
    fell through to the "stack-run" fallback below unconditionally -- "ac-test-run" resolves to the
    exact same models.yaml tier (gpt-5.4/haiku) as that fallback already gave, so this is a pure
    naming fix with no behavior change. stack_runner.run_and_report's own internal fallback
    (`model_config.get_model_name(stage_key, ...)`) is still not what resolves this: passing
    model_name explicitly here means it never re-resolves against the new placement-specific
    `stage_key` (absent from models.yaml) instead."""
    from .gates.ac_coverage_gate import (  # local: avoids import at module load
        NO_PLANNED_TEST_FILES, AcTestRunReport, clear_runner_artifacts, run_resolved_test_command,
    )

    log = log if log is not None else CheckLog(f"red-gate-{spec_key}", _RED_CHECKS)
    provider = get_sandbox_provider()
    await clear_runner_artifacts(provider, thread_id, _RED_GATE_OUTPUT_PATH)

    # resolve_test_command() first, deterministically, before any LLM turn (Task 6): only when it
    # produces nothing usable does a GHCP discovery session run at all. The correctness guard right
    # below (`if not outcomes:`) is unchanged and now fires for EITHER path -- a resolved command
    # that parses to zero outcomes falls straight through to the same discovery turn a stack with no
    # resolver answer always used, rather than being silently read as "0 failed" (Task 6 requirement).
    # Every test file the approved test plan names must run (and fail) here. The run agent
    # accounts for each one under the suite that ran it; the check below holds it to that list.
    # With a plan, the resolved single-command shortcut is skipped: one runner command can't say
    # which planned files it covered, so on a mixed stack a whole suite could silently not run.
    planned = await _planned_test_files(provider, thread_id)
    tech_stack = await workflow_persistence.read_tech_stack_json(provider, thread_id)
    report = None if planned else await run_resolved_test_command(
        provider, thread_id, tech_stack, output_path_base="agent-work/red-gate-resolved"
    )
    agent_run = report is None
    # The tee was deleted before this run (clear_runner_artifacts): an agent run that leaves it
    # empty ran nothing, so its suites/artifacts can't describe the tree. That is the run agent's
    # failure, not the code's -- re-run it (each call is a fresh session, stack_runner) within the
    # verify infra budget instead of handing a fixer a verdict about nothing.
    silent = False
    for _attempt in range(1 + config.VERIFY_INFRA_RETRY_CAP if agent_run else 0):
        report = await stack_runner.run_and_report(
            thread_id,
            stage_key=f"red-gate-{spec_key}",
            prompt_name="ac_test_run",
            schema=AcTestRunReport,
            provider=chat_provider,
            run_id=run_id,
            lap=lap,
            output_path=_RED_GATE_OUTPUT_PATH,
            model_name=model_config.get_model_name("ac-test-run", "draft", chat_provider) or model_config.get_model_name("stack-run", "draft", chat_provider),
            planned_test_files="\n".join(f"   - {f}" for f in planned) or NO_PLANNED_TEST_FILES,
        )
        silent = not await repo_files.read_repo_file(provider, thread_id, _RED_GATE_OUTPUT_PATH)
        if not silent:
            break
    assert report is not None
    # After scaffolding every suite must at least LOAD: red means each test fails at runtime. A
    # root whose runner stopped before running any test proves nothing about its tests, and its
    # absence from result_artifacts would otherwise let the other roots' results pass for the
    # whole suite. Stack-agnostic -- the run's own per-root report, no runner/error-text matching.
    if silent:
        log.infra(RED_SUITES_START, f"the test run captured no output ({_RED_GATE_OUTPUT_PATH}) -- nothing was run")
        return False, (
            f"TDD-red gate: the test run captured no output this lap ({_RED_GATE_OUTPUT_PATH} is empty or "
            "missing), even after re-running it, so no suite was actually run and its report can't be "
            "trusted. Nothing in the code needs to change for this -- the next check runs the suites again."
        )
    not_run = [s for s in report.suites if not s.ran]
    if not_run:
        listed = "; ".join(f"{s.root}: {s.reason}" for s in not_run)
        log.failed(RED_SUITES_START, listed)
        return False, (
            f"TDD-red gate: {len(not_run)} test suite(s) never ran a single test after "
            f"scaffolding -- {listed}. Scaffolding must leave every suite compiling and loading so "
            "each test fails at RUNTIME; fix whatever stops the suite from starting (full runner "
            f"output: {_RED_GATE_OUTPUT_PATH})."
        )
    # ...and no planned file may go unaccounted for: a suite the run agent left out entirely
    # shows up here as its files, by path -- a set check, no runner or report-format knowledge.
    # ponytail: a file listed under a root that ran is taken at its word (.trx records no source
    # paths, so there is no format-agnostic way to confirm each file's own tests executed).
    log.passed(RED_SUITES_START)
    accounted = _accounted_files(report.suites)
    missing = [f for f in planned if test_results.repo_relative(f) not in accounted]
    if missing:
        log.failed(RED_PLANNED_FILES, ", ".join(missing))
        return False, (
            f"TDD-red gate: {len(missing)} planned test file(s) were never run -- no test suite in "
            f"the run covered them: {', '.join(missing)}. Every test file in the approved test "
            "plan must run (and fail at runtime) before implementation starts; make sure each "
            "one's suite builds, loads and actually picks the file up (full runner output: "
            f"{_RED_GATE_OUTPUT_PATH})."
        )
    if planned:
        log.passed(RED_PLANNED_FILES)
    else:
        log.skipped(RED_PLANNED_FILES, "no approved test plan to check the run against")
    outcomes: dict[str, str] = {}
    for artifact in report.result_artifacts or []:
        rel = test_results.repo_relative(artifact)
        contents = await repo_files.read_repo_file(provider, thread_id, rel) if rel else None
        if not contents:
            continue
        parsed = (
            test_results.parse_trx(contents)
            or test_results.parse_vitest_json(contents)
            or test_results.playwright_outcomes(contents)
        )
        outcomes = test_results.merge_outcomes(outcomes, parsed)

    ok, detail = _red_outcome_verdict(outcomes, eligible_only, report.error)
    (log.passed if ok else log.failed)(RED_NONE_PASS, detail)
    return ok, detail


def make_rebuild_node(spec: RebuildSpec):
    async def rebuild_node(state: dict[str, Any], run_config) -> dict[str, Any]:
        # Named run_config, not config -- config.py's module import above is used throughout this
        # function (config.REBUILD_OUTPUT_COMBINED_TAIL_CHARS etc.); a same-named parameter here
        # silently shadows it for the rest of the function body, resolving every config.CONSTANT
        # read to this RunnableConfig dict instead and crashing with AttributeError (root-caused
        # 2026-09-11, introduced by the output-truncation/config refactor before this fix).
        thread_id = run_config["configurable"]["thread_id"]
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rb = rebuild.get(spec.key, default_rebuild_state())
        # Stamped on entry, so every path below (no sandbox, skip, pass, fail) records which attempt
        # this result belongs to; the fix/escalate nodes carry it forward unchanged.
        rb["attempt_started_at"] = state.get("attempt_started_at") or ""
        rb["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if sandbox_registry.get(thread_id) is None:
            # No sandbox means the build never ran. Escalate rather than declare it clean
            # (route reads cannot_verify).
            rb["status"] = "failed"
            rb["last_exit_ok"] = False
            rb["cannot_verify"] = True
            rebuild[spec.key] = rb
            return {"rebuild": rebuild}

        provider = get_sandbox_provider()
        # Clear the sticky no-sandbox flag: it survives END-terminated runs in the checkpoint,
        # and the router checks it FIRST -- without this a healthy resubmit insta-fails.
        rb["cannot_verify"] = False
        log = CheckLog(spec.key, rebuild_checks(spec))

        # Durable + live NODE_STARTED (root-caused 2026-09-11): a real build/red-gate check here
        # can run for minutes, but until now this node emitted no run_event at all, so
        # BuildView.tsx's RebuildConnector had NOTHING to show for a placement's first-ever
        # attempt (state.rebuild[spec.key] only exists after this function RETURNS) -- read live
        # as "two stages active at once" the first time it happened to overlap with the FOLLOWING
        # stage's own draft event arriving before this node's slower state-snapshot-based check
        # caught up. Same two-call pattern (append_event then emit_live) as every other RunEvent
        # site in this file; only fires on the path that can reach this node's own NODE_FINISHED
        # below (the no-sandbox early return above never touches either event).
        run_id = state.get("run_id", "unknown")
        start_event = RunEvent(
            run_id=run_id, session_id=thread_id, type=RunEventType.NODE_STARTED,
            stage=spec.key, node="rebuild", summary="rebuild check started",
        )
        start_event = await run_event_store.append_event(start_event)
        await run_event_stream.emit_live(start_event, run_config)

        # Already passed this run and the next stage has started: done (should_skip_rebuild).
        # intake set aside any interrupted work first (graph._set_aside_interrupted_work), so the
        # tree is clean either way.
        next_started = bool(spec.next_stage_key) and rb.get("status") == "clean" and await stage_started(
            provider, thread_id, state, spec.next_stage_key
        )
        if should_skip_rebuild(rb, state.get("run_id"), next_started):
            summary = f"skipped: already passed this run; {spec.next_stage_key} has since started"
            logger.info("rebuild %s %s", spec.key, summary)
            skip_event = RunEvent(
                run_id=run_id, session_id=thread_id, type=RunEventType.NODE_FINISHED,
                stage=spec.key, node="rebuild", summary=summary,
                payload={"passed": True, "cycle": rb["fix_cycle_count"], "skipped": True},
            )
            skip_event = await run_event_store.append_event(skip_event)
            await run_event_stream.emit_live(skip_event, run_config)
            rebuild[spec.key] = rb
            return {"rebuild": rebuild}

        # GHCP finds every buildable project and builds it from the right directory, then reports
        # through a schema-validated terminal tool. Replaces "an audit model guesses a build
        # command + root, Python runs `cd {root} && {command}` blindly" -- that guess was wrong on
        # every headless run (a greenfield monorepo has nothing buildable at the repo root, so
        # `dotnet build` died with MSB1003 in ~2s and this node silently escalated every time).
        #
        # Toolchain reuse (Task 6, requirement 2/3): unlike the test command, the BUILD command has
        # no resolver table at all -- persist whichever placement discovers it first into
        # manifest.json's shared `toolchain` section (preflight_nodes.persist_toolchain_value, same
        # `update_manifest` read-modify-write record_toolchain already uses), so later placements
        # read it instead of independently re-asking the model. Timing correction (greenfield vs.
        # brownfield): see _should_skip_toolchain_capture's own docstring.
        skip_toolchain_capture = _should_skip_toolchain_capture(spec.fix_scope, tech_stack_signals.is_greenfield_repo(state))
        used_persisted_build_command = False
        if rb["fix_cycle_count"] == 0 and not rb.get("build_commands") and not skip_toolchain_capture:
            persisted_build_commands = await preflight_nodes.read_toolchain_value(provider, thread_id, "build_commands")
            if isinstance(persisted_build_commands, list) and persisted_build_commands:
                rb["build_commands"] = persisted_build_commands
                used_persisted_build_command = True

        replayed = bool(rb["fix_cycle_count"] > 0 and rb.get("build_commands")) or used_persisted_build_command
        if replayed:
            # Fix laps re-run the contract the discovery turn established; the model is never
            # asked "does it build?" twice in one placement (see BuildVerifyReport.build_commands).
            report = await _replay_build(provider, thread_id, rb["build_commands"])
        else:
            # Placement-specific stage_key (Overview-tab rebuild-row fix, 2026-09-22): was the
            # bare literal "rebuild", shared by every non-ac-to-tests placement's discovery turn
            # -- indistinguishable in run_events, and (worse) an identical stack_runner
            # ChatModel._session_key across every placement's first-ever discovery turn within one
            # run (stage+role were the only two components, and role defaults to lap 0 here).
            # model_name is resolved explicitly from the OLD literal "rebuild" key so
            # run_and_report's own internal model_config.get_model_name(stage_key, ...) fallback
            # doesn't silently re-resolve against the new key instead (absent from models.yaml,
            # which would fall through to the "stack-run" default model instead of "rebuild"'s).
            report = await stack_runner.run_and_report(
                thread_id,
                stage_key=f"rebuild-{spec.key}",
                prompt_name="rebuild_verify",
                schema=BuildVerifyReport,
                provider=state["provider"],
                run_id=state.get("run_id", "unknown"),
                addendum=spec.fix_prompt_addendum or "",
                model_name=model_config.get_model_name("rebuild", "draft", state["provider"]) or model_config.get_model_name("stack-run", "draft", state["provider"]),
            )
            rb["build_commands"] = [c.model_dump() for c in (report.build_commands or [])]
            if not rb["build_commands"]:
                logger.warning("rebuild %s: discovery reported no build_commands -- fix laps will fall back to the model", spec.key)
            else:
                # Deterministic re-verify (root-caused 2026-09-13, observed live thread 8242ea6d):
                # every LATER fix lap already re-runs these exact commands and judges on exit code
                # alone (_replay_build, this module's own docstring: "No LLM at all in the happy
                # path... gates on its exit code") -- the discovery turn alone skipped that and
                # trusted the model's own self-reported ok/success instead. A model can get spooked
                # by benign stderr noise unrelated to the actual result: observed live, `dotnet
                # build` printed "Build succeeded. 0 Warning(s) 0 Error(s)" (exit 0) plus an
                # unrelated SDK advisory ("An issue was encountered verifying workloads") on
                # stderr, and the model self-reported ok=false anyway -- permanently blocking a run
                # at its very first rebuild gate for a diagnostic that never affected the build.
                # Re-running here costs one extra build, the same cost every subsequent fix lap
                # already pays for the identical guarantee.
                report = await _replay_build(provider, thread_id, rb["build_commands"])
        build_ok = report.success and report.ok
        (log.passed if build_ok else log.failed)(
            REBUILD_BUILD, None if build_ok else (report.error or "the build failed -- see its output")
        )
        built = build_ok
        red_ran = False

        # Persist a FRESH discovery's build_commands (never a replay of an already-persisted one,
        # and never at the greenfield scaffold-only placement -- see skip_toolchain_capture above)
        # once it is actually proven green, so the NEXT rebuild placement this run -- or a future
        # run's first placement, on a resumed/brownfield repo -- reads it instead of discovering it
        # again (Task 6, requirement 2/3).
        if build_ok and not skip_toolchain_capture and not used_persisted_build_command and rb["build_commands"]:
            await preflight_nodes.persist_toolchain_value(provider, thread_id, "build_commands", rb["build_commands"])

        # Deliberate asymmetry vs. e2e_nodes.py's start_command (Task 6 review, Important item):
        # a persisted build_commands set that later FAILS a replay here is never invalidated. This
        # is intentional, not an oversight: a build failure's own fix loop (make_fix_node, below)
        # is written to FIX THE CODE so the SAME command succeeds -- that is what this whole
        # placement's replay-then-fix cycle already did BEFORE Task 6 touched this file, persisted
        # or not. A start_command, by contrast, can be genuinely WRONG in a way no code fix
        # resolves (the app moved directories, needs a different port/flag) -- that asymmetry is
        # why e2e's cache needs active forgetting and this one doesn't. The one real risk this
        # accepts: a persisted build_commands set that becomes stale for a reason no code fix can
        # repair (e.g. a later stage restructures the repo layout) will still burn a placement's
        # fix cycles before escalating, same as a bad FRESH discovery already could pre-Task-6 --
        # not a new failure mode, just not actively short-circuited either.

        # TDD-red gate, scaffold placement only: a green build is necessary but NOT sufficient --
        # the suite must also RUN with zero passing tests before the implementation stage may
        # start. A red-gate violation re-enters the same bounded fix loop (the fix prompt gets the
        # passing test names via last_stderr_tail); at the cap the run ENDs with run_failure.
        red_detail = ""
        red_failed = False
        # Only while the implementation stage has NOT yet run: on a resumed thread where
        # minimal-code-to-green has already produced code, the suite is legitimately GREEN here --
        # observed live (s04 run 7): the red gate on a resume stripped the finished implementation
        # back to stubs to satisfy all-red, mctg was hydrated-skipped, and test-hardening flagged
        # the wreckage as a stable regression.
        #
        # The guard tests "has mctg run at all", NOT "is mctg approved". On a fresh run this node
        # always precedes the implementation stage, so the status is "not_started" and the red gate
        # fires normally. Any other value means that stage has already written code into this
        # workspace, and demanding all-red again asks the fix node to DELETE it. `approved` alone
        # missed the case that actually bit (run 026dee4f): the codegen turn wrote a full
        # implementation, committed it green, then died on a provider quota outage leaving mctg at
        # `needs_clarification` -- so the resume walked straight into the red gate reporting
        # "56 test(s) PASSED after scaffolding" against code it should have been protecting.
        # Asked of the WORKSPACE, not of stage bookkeeping. `status` cannot answer this on a
        # resume: intake's hydration reset (graph.py) puts every unapproved stage back to
        # "not_started", which is indistinguishable from "codegen has never run", and a killed run
        # persists the same value. A tree scan ("is there app source?") cannot answer it either --
        # scaffolding creates Program.cs/App.razor long before this node. The draft ARTIFACT is
        # written only when the implementation stage actually produced a draft, is committed to the
        # branch, and rides the workspace volume across container swaps, so it is the one signal
        # that survives everything above.
        # Two proofs codegen touched this workspace: the completed draft artifact, or a stage
        # status other than "not_started" -- make_draft_node persists "drafting" to state.json
        # before its first model call and intake keeps it across resumes, so a draft killed
        # mid-turn (run d16959d3: three 40-minute timeouts, 200 tests already passing) no longer
        # reads as "codegen never ran" and gets its implementation stubbed back to red.
        mctg_never_ran = not await stage_started(provider, thread_id, state, "minimal-code-to-green")
        if build_ok and spec.fix_scope == "scaffold_only" and mctg_never_ran:
            red_ran = True
            red_ok, red_detail = await _verify_all_red(
                thread_id, state["provider"], spec.key, run_id=state.get("run_id", "unknown"), log=log,
            )
            if not red_ok:
                build_ok = False
                red_failed = True
        elif build_ok and spec.fix_scope == "scaffold_only" and not mctg_never_ran:
            # Second-or-later ticket on a workspace that already carries delivered code: the
            # whole-suite red contract is wrong (regression tests are green), but this ticket's own
            # NEW criteria still get their "watch it fail" moment -- scoped to tests attributing to
            # the eligible set. Guarded on mctg not having run THIS run (fresh-run reset put it at
            # "not_started"); any other status means a resume where implementation already exists,
            # and demanding red then would strip finished work (observed live, s04 run 7).
            mctg_status = ((state.get("stages") or {}).get("minimal-code-to-green") or {}).get("status")
            run_id = state.get("run_id", "unknown")
            eligible = await _eligible_ac_ids_for_run(provider, thread_id, run_id, new_or_modified_only=True)
            if mctg_status == "not_started" and eligible:
                red_ran = True
                red_ok, red_detail = await _verify_all_red(
                    thread_id, state["provider"], spec.key, run_id=run_id, eligible_only=eligible, log=log,
                )
                if not red_ok:
                    build_ok = False
                    red_failed = True

        if built and spec.fix_scope == "scaffold_only" and not red_ran:
            for check in _RED_CHECKS:
                log.skipped(check, "not needed this run: the implementation already exists, or there are no new criteria")

        # Stuck-fixer detection: originally written when the fix session (f"rebuild-{spec.key}"/
        # "draft") was resumed across every fix cycle, so a fixer repeating the SAME red-gate
        # mistake byte-for-byte would never see fresh context. make_fix_node's session key is now
        # lap-numbered (session-poisoning fix, mirrors ac-test-run/e2e_fix's own fresh-per-lap
        # sessions), so every fix cycle already starts a genuinely fresh session. The
        # close_session calls below target that lap-numbered key via chat_model.lap_role
        # (fix_cycle_count - 1: the fix session that produced THIS build -- make_fix_node
        # increments the counter after its turn), so the reset really evicts the stuck session.
        # Until 2026-09-19 they passed the bare "draft" role, a key nothing populates -- a silent
        # no-op, the same key-mismatch class as graph.py's _lap_role_keys incident. Kept because the
        # repeated-identical-finding detection itself is still useful diagnostic signal regardless
        # of session mechanics -- a fixer repeating a finding even across fresh sessions means
        # something else is stuck (misread instructions, a fix that doesn't address the real cause).
        if red_failed and red_detail and red_detail == rb.get("last_red_detail"):
            logger.warning(
                "rebuild %s: TDD-red gate repeated the identical finding -- resetting the stuck fix session",
                spec.key,
            )
            await close_session(
                thread_id, f"rebuild-{spec.key}",
                lap_role("draft", state.get("run_id", "unknown"), rb["fix_cycle_count"] - 1),
                provider=state["provider"],
            )
        rb["last_red_detail"] = red_detail if red_failed else ""

        # Scan-delta gate: same question the terminal metrics gate asks, asked here where it is
        # still actionable. See RebuildSpec.scan_delta_gate for why this placement exists.
        scan_detail = ""
        if build_ok and spec.scan_delta_gate:
            # Re-measure coverage only when it can matter: a fix lap changed the tree since the
            # stored value was taken, and this mode enforces the threshold (YOLO doesn't).
            from . import metrics_nodes  # local, same as _scan_regression_reasons

            scan_reasons = await _scan_regression_reasons(
                provider, thread_id, state,
                remeasure_coverage=rb["fix_cycle_count"] > 0 and metrics_nodes.coverage_threshold_gated(state),
            )
            scan_reasons += await _provenance_reasons(provider, thread_id, state)
            if scan_reasons:
                log.failed(SCAN_DELTA, "\n".join(scan_reasons))
                build_ok = False
                scan_detail = (
                    "The build is green, but a full re-scan of the tree you just modified reports "
                    "problems the FINAL metrics gate will refuse to merge on. Fix them now, while "
                    "this loop can still act on them:\n"
                    + "\n".join(f"- {reason}" for reason in scan_reasons)
                    + "\n\nThese are regressions introduced by the fix work in this stage: the "
                    "remediation stage earlier in this run left the tree clean. Duplication usually "
                    "means the same edit was pasted across components -- extract it. 'coverage "
                    "unmeasured' means the coverage command itself no longer runs, which is a "
                    "broken build/test configuration, not a missing test."
                )
                scan_fingerprint = _scan_finding_fingerprint(scan_reasons)
                if scan_fingerprint and scan_fingerprint == rb.get("last_scan_fingerprint"):
                    logger.warning(
                        "rebuild %s: scan-delta gate repeated the identical finding(s) %s -- "
                        "resetting the stuck fix session",
                        spec.key, sorted(scan_fingerprint),
                    )
                    await close_session(
                        thread_id, f"rebuild-{spec.key}",
                        lap_role("draft", state.get("run_id", "unknown"), rb["fix_cycle_count"] - 1),
                        provider=state["provider"],
                    )
                rb["last_scan_fingerprint"] = scan_fingerprint
            else:
                log.passed(SCAN_DELTA)
                rb["last_scan_fingerprint"] = frozenset()

        rb["status"] = "clean" if build_ok else "failed"
        rb["last_exit_ok"] = build_ok
        rb["checks"] = [r.to_dict() for r in log.results()]
        # 16000 total, matching _replay_build's own cap above -- this used to re-truncate to 4000 on
        # top of that, which quietly threw away most of what the wider cap just preserved (the
        # fix prompt below reads exactly these two fields, so THIS slice, not _replay_build's, is
        # what actually reaches the model). truncate_middle, not a tail-only slice, for the same
        # keep-both-ends reason as _replay_build's own fix above.
        _combined_half = config.REBUILD_OUTPUT_COMBINED_TAIL_CHARS // 2
        rb["last_stdout_tail"] = truncate_middle(report.stdout_tail or "", _combined_half, _combined_half)
        # A red-gate violation replaces the (green) build's stderr as the fix node's feedback --
        # the passing test names are the actionable part, not a clean compiler log.
        rb["last_stderr_tail"] = truncate_middle(
            red_detail if red_failed
            else scan_detail if scan_detail
            else (report.stderr_tail or report.error or ""),
            _combined_half, _combined_half,
        )
        rebuild[spec.key] = rb

        ledger_entry: dict[str, Any] = {
            "stage": spec.key, "node": "rebuild", "ok": build_ok, "cycle": rb["fix_cycle_count"],
            "verify": "replay" if replayed else "discovery",
        }
        if red_detail:
            ledger_entry["red_gate"] = red_detail[:config.REBUILD_LEDGER_DETAIL_CHARS]
        await repo_files.append_ledger_entry(provider, thread_id, ledger_entry)
        if build_ok:
            # A green build is the checkpoint where the code-writing sessions' source changes
            # (codegen, fixes) become worth keeping -- the artifact-only commit sites never stage
            # source, so without this the pushed work branch would carry no code at all.
            await git_ops.commit_all(provider, thread_id, f"ai-dev-workflow: {spec.key} source changes (build green)")
            # What should_skip_rebuild / blame_downstream compare against later in this run.
            rb["passed_run_id"] = state.get("run_id") or ""
            rb["passed_commit"] = await git_ops.head_commit(provider, thread_id) or ""

        # Durable + live NODE_FINISHED, closing the NODE_STARTED span above.
        finish_event = RunEvent(
            run_id=run_id, session_id=thread_id, type=RunEventType.NODE_FINISHED,
            stage=spec.key, node="rebuild", summary=f"rebuild check {'passed' if build_ok else 'failed'}",
            payload={"passed": build_ok, "cycle": rb["fix_cycle_count"]},
        )
        finish_event = await run_event_store.append_event(finish_event)
        await run_event_stream.emit_live(finish_event, run_config)
        return {"rebuild": rebuild}

    return rebuild_node


def make_route_after_rebuild(spec: RebuildSpec) -> Callable[[dict[str, Any]], str]:
    def route(state: dict[str, Any]) -> str:
        rb = (state.get("rebuild") or {}).get(spec.key, default_rebuild_state())
        if rb.get("cannot_verify"):
            return "escalate"  # no sandbox -- the build never ran; a human must see it
        if rb["last_exit_ok"]:
            return "next"
        if rb["fix_cycle_count"] < spec.max_fix_cycles:
            return "fix"
        return "escalate"

    return route


def route_after_escalate(state: dict[str, Any]) -> str:
    """Post-escalate routing shared by all POST_STAGE_REBUILD placements: a sandbox-alive escalate
    ("exit") continues into metrics-exit_draft so the run still gets its exit report, manifest and
    session close; cannot_verify ("end") means the sandbox is GONE and metrics-exit's own
    draft/verify/finalize all execute in the sandbox -- routing there would only crash-escalate
    again. Reads run_failure["type"], NOT rebuild[key]["cannot_verify"]: make_escalate_node resets
    that flag in the same super-step it records the failure."""
    return "end" if (state.get("run_failure") or {}).get("type") == "cannot_verify" else "exit"


_SCAFFOLD_ONLY_ADDENDUM = (
    "You may ONLY add minimal compile-enabling scaffolding: signatures, classes, and interfaces "
    "that don't yet exist, each throwing NotImplementedException (or the stack's equivalent) in "
    "every method body. Do NOT implement real behavior. Tests must remain failing at RUNTIME after "
    "your change -- only what stops a suite from compiling or starting (missing symbols, project or "
    "test-runner config wiring, dependencies) is your job here. If a test fails to compile "
    "because it references a symbol that doesn't exist yet, add the minimal stub; do not make the "
    "test pass."
)


async def _snapshot_files(provider: Any, thread_id: str, paths: list[str]) -> dict[str, str]:
    """Current content of each existing file in `paths`."""
    snap = {}
    for path in paths:
        content = await repo_files.read_repo_file(provider, thread_id, path)
        if content is not None:
            snap[path] = content
    return snap


async def _restore_changed(provider: Any, thread_id: str, snapshot: dict[str, str]) -> list[str]:
    """Rewrites every snapshotted file whose content changed (or that was deleted) since the
    snapshot; returns those paths."""
    restored = []
    for path, content in snapshot.items():
        if await repo_files.read_repo_file(provider, thread_id, path) != content:
            await repo_files.write_repo_file(provider, thread_id, path, content)
            restored.append(path)
    return restored


def make_fix_node(spec: RebuildSpec):
    async def fix_node(state: dict[str, Any], run_config) -> dict[str, Any]:
        # See rebuild_node's own comment: named run_config to avoid shadowing the config module.
        thread_id = run_config["configurable"]["thread_id"]
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rb = rebuild.get(spec.key, default_rebuild_state())

        addendum = _SCAFFOLD_ONLY_ADDENDUM if spec.fix_scope == "scaffold_only" else spec.fix_prompt_addendum
        system, template = load_prompt_pair("rebuild_build_fix")
        prompt = render_prompt(
            template,
            addendum=addendum,
            stdout_tail=rb["last_stdout_tail"],
            stderr_tail=rb["last_stderr_tail"],
        )

        # Own session key per placement (rebuild-<key>:draft), not plan:draft -- sharing plan's key
        # returned its cached read-only session so this autopilot fixer silently couldn't write, and
        # bled plan's conversation across every R placement. Dedicated "rebuild" model entry
        # (falling back to plan's) -- the fixer needs codegen-tier capability regardless of how
        # cheap the drafting roster is.
        #
        # No custom_agents: confirmed live (twice -- ac-to-tests, then minimal-code-to-green) that
        # a session created with custom_agents silently loses part of the agent's own declared
        # `tools:` list, leaving a "fixer" that cannot edit anything and answers in prose instead.
        # available_tools IS honored, so the tool set is declared here.
        model = get_chat_model_for_thread(
            thread_id,
            f"rebuild-{spec.key}",
            # Fresh session per retry lap (mirrors e2e_fix_node/graph.py's draft/audit/fix nodes): a
            # static "draft" role let --resume replay every prior retry's full transcript into each
            # new one. fix_cycle_count is this placement's own existing retry counter, already
            # incremented per attempt and capped by spec.max_fix_cycles -- no new state.
            lap_role("draft", state.get("run_id", "unknown"), rb["fix_cycle_count"]),
            provider=state["provider"],
            # Task 3b (Part 2 Ruling 10) fix-round-3 -- same mechanism/fix as every other
            # graph-node call site in this task.
            run_id=state.get("run_id", "unknown"),
            model_name=model_config.get_model_name("rebuild", "draft", state["provider"]) or model_config.get_model_name("plan", "draft", state["provider"]),
            sandbox=sandbox_registry.get(thread_id),
            available_tools=[
                "builtin:view", "builtin:grep", "builtin:glob", "builtin:bash",
                "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
            ],
            # Always autopilot -- a fix node's whole purpose requires write access.
            agent_mode="autopilot",
        )
        run_id = state.get("run_id", "unknown")
        # Durable + live NODE_STARTED/FINISHED (Overview-tab redraft history, Workstream 3): this
        # node previously emitted no run_event of its own (unlike rebuild_node's "rebuild" pair
        # just above), so a rebuild placement's own fix laps had no Overview-tab entry at all.
        # Same two-call, sandbox-guarded pattern as every other RunEvent site in this file.
        if sandbox_registry.get(thread_id) is not None:
            start_event = RunEvent(
                run_id=run_id, session_id=thread_id, type=RunEventType.NODE_STARTED,
                stage=spec.key, node="fix", summary="rebuild fix started",
            )
            start_event = await run_event_store.append_event(start_event)
            await run_event_stream.emit_live(start_event, run_config)

        # The scaffold-only fixer must never touch the approved tests -- they are the contract the
        # red gate holds the scaffold to. Enforced, not just asked: the plan's test files are
        # snapshotted before the turn and any it edited or deleted are put back after (observed
        # live, run c2bbdca1: a fixer chasing a false red-gate verdict rewrote a Playwright spec).
        test_snapshot: dict[str, str] = {}
        if spec.fix_scope == "scaffold_only" and sandbox_registry.get(thread_id) is not None:
            provider = get_sandbox_provider()
            test_snapshot = await _snapshot_files(provider, thread_id, await _planned_test_files(provider, thread_id))

        fix_messages = [SystemMessage(content=system), HumanMessage(content=prompt)]
        fix_response = None
        try:
            fix_response = await call_with_infra_retry(
                lambda: model.ainvoke(fix_messages),
                label=f"rebuild-{spec.key}:fix",
            )
        except (TimeoutError, RuntimeError) as exc:
            # A Copilot session failure that survived infra_retry's own backoff attempts. No new
            # separate counter here (unlike make_draft_node/make_verify_node): fix_node has no
            # routing decision of its own -- rebuild_node's NEXT build-check run is what decides
            # pass/fail, driven only by fix_cycle_count vs max_fix_cycles. Tagging last_stderr_tail
            # lets make_escalate_node's failure_type classification (run_failure.py) correctly
            # report infra_transient/quota_exhausted if this stage does eventually escalate,
            # instead of looking like a genuine build defect.
            logger.warning("rebuild fix infra-exhausted for %s -- counting the lap: %s", spec.key, exc)
            rb["last_stderr_tail"] = (
                f"[infra failure, fix lap not attempted] {exc}"
            )[-config.REBUILD_OUTPUT_COMBINED_TAIL_CHARS:]

        if sandbox_registry.get(thread_id) is not None:
            input_text, output_text, input_size, output_size = encode_io_text(
                fix_messages,
                str(fix_response.content) if fix_response is not None else "(infra failure -- fix lap not attempted)",
            )
            finish_event = RunEvent(
                run_id=run_id, session_id=thread_id, type=RunEventType.NODE_FINISHED,
                stage=spec.key, node="fix", summary="rebuild fix applied",
                payload={"cycle": rb["fix_cycle_count"]},
                input_text=input_text, output_text=output_text,
                input_size=input_size, output_size=output_size,
                # Overview-tab rebuild-row cost fix (2026-09-22): every other direct-ainvoke()
                # node in this codebase (graph.py's draft/audit/fix nodes, e2e_nodes.py,
                # metrics_nodes.py, test_hardening_nodes.py) attaches model._last_usage to its own
                # finish event -- this was the one exception, so a rebuild placement's fix laps
                # never had a cost anywhere. # noqa: SLF001, same private-attribute access every
                # one of those other call sites already uses.
                token_usage=model._last_usage,
            )
            finish_event = await run_event_store.append_event(finish_event)
            await run_event_stream.emit_live(finish_event, run_config)

        if test_snapshot and sandbox_registry.get(thread_id) is not None:
            restored = await _restore_changed(get_sandbox_provider(), thread_id, test_snapshot)
            if restored:
                logger.warning("rebuild %s: the fixer edited approved test files -- restored %s", spec.key, restored)
                await repo_files.append_ledger_entry(
                    get_sandbox_provider(), thread_id,
                    {"stage": spec.key, "node": "fix", "restored_test_files": restored},
                )

        rb["fix_cycle_count"] = rb["fix_cycle_count"] + 1
        rb["status"] = "fixing"
        rebuild[spec.key] = rb
        return {"rebuild": rebuild}

    return fix_node


def make_escalate_node(spec: RebuildSpec):
    async def escalate_node(state: dict[str, Any], run_config) -> dict[str, Any]:
        # See rebuild_node's own comment: named run_config to avoid shadowing the config module.
        thread_id = run_config["configurable"]["thread_id"]
        rb = (state.get("rebuild") or {}).get(spec.key, default_rebuild_state())
        # R never auto-approves past a failing build -- and never pauses for a human either: with
        # run_failure set, the run continues into metrics-exit (sandbox alive) so the exit report
        # still gets written, or ENDs (cannot_verify -- see route_after_escalate). Counters/flags
        # are reset in the same return so the checkpointed thread isn't poisoned for the next
        # resubmission.
        payload = {
            "stage": spec.key,
            "type": "cannot_verify" if rb.get("cannot_verify") else "rebuild_cap_exceeded",
            "stdout_tail": rb["last_stdout_tail"],
            "stderr_tail": rb["last_stderr_tail"],
            # session_store._build_failure reads only feedback/report for failure_message -- without
            # this the DB row's message is empty and the support/UI surfaces show a bare type.
            "feedback": (rb["last_stderr_tail"] or rb["last_stdout_tail"] or "")[-config.REBUILD_ESCALATE_FEEDBACK_CHARS:],
        }
        # Passed earlier THIS run, failing now: the files changed since that pass broke it, and
        # those belong to the stage after it (blame_downstream). An earlier run's pass proves
        # nothing about this run's own redraft of the stage before it.
        passed_commit = rb.get("passed_commit") or ""
        if (
            not rb.get("cannot_verify") and spec.next_stage_key and passed_commit
            and rb.get("passed_run_id") and rb.get("passed_run_id") == state.get("run_id")
        ):
            changed = await git_ops.changed_since(get_sandbox_provider(), thread_id, passed_commit)
            if changed:
                payload = blame_downstream(payload, spec, passed_commit, changed)
        payload = await run_failure.record_run_failure_and_reset(
            thread_id, state.get("run_id"),
            payload=payload,
            detail_for_classification=f"{rb['last_stdout_tail']} {rb['last_stderr_tail']}",
            # route_after_escalate sends every non-cannot_verify type on into metrics-exit_draft
            # in this SAME sandbox -- tearing the container down here raced that node and lost.
            keep_sandbox=not rb.get("cannot_verify"),
        )
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rebuild.setdefault(spec.key, default_rebuild_state())
        rebuild[spec.key]["fix_cycle_count"] = 0
        rebuild[spec.key]["cannot_verify"] = False
        return {"rebuild": rebuild, "run_failure": payload}

    return escalate_node


def _demo() -> None:
    """`cd agent && uv run python -m src.rebuild`."""
    # Vacuous red is a FAIL: no parsed outcomes must not open the gate.
    ok, passed, failed = red_gate_verdict({})
    assert not ok and passed == [] and failed == 0
    ok, _, failed = red_gate_verdict({"a": "fail", "b": "fail"})
    assert ok and failed == 2
    ok, passed, _ = red_gate_verdict({"a": "fail", "b": "pass"})
    assert not ok and passed == ["b"]

    # Ticket-scoped red (eligible_red_verdict): completed criteria's green regression tests are
    # exempt; only tests attributing to the eligible set must be red; vacuous scope is a FAIL.
    outcomes = {
        "[US-0001.1] old feature still works": "pass",   # completed -- may pass
        "[US-0003.1] new feature does X": "fail",        # eligible -- correctly red
    }
    ok, passed, failed = eligible_red_verdict(outcomes, {"US-0003.1"})
    assert ok and passed == [] and failed == 1
    ok, passed, _ = eligible_red_verdict(
        {**outcomes, "[US-0003.1] new feature already green": "pass"}, {"US-0003.1"}
    )
    assert not ok and passed == ["[US-0003.1] new feature already green"]
    ok, passed, failed = eligible_red_verdict(outcomes, {"US-0009.9"})
    assert not ok and passed == [] and failed == 0, "no test names the eligible AC -- vacuous is a FAIL"
    # Build-contract replay: the verdict is the real exit code of the discovery turn's commands,
    # run again NOW -- a stale model answer cannot happen by construction.
    import asyncio

    class _Result:
        def __init__(self, rc: int, out: str = "", err: str = "") -> None:
            self.returncode, self.stdout, self.stderr = rc, out, err

        @property
        def ok(self) -> bool:
            return self.returncode == 0

    class _StubProvider:
        def __init__(self, rc: int) -> None:
            self.rc, self.commands = rc, []

        async def exec_in_sandbox(self, _thread_id: str, command: str, **_kw: Any) -> _Result:
            self.commands.append(command)
            return _Result(self.rc, "built", "" if self.rc == 0 else "error CS0001")

    contract = [{"cwd": "apps/api.Tests", "command": "dotnet build"}, {"cwd": "apps/web", "command": "npm run build"}]
    green = _StubProvider(0)
    rep = asyncio.run(_replay_build(green, "t", contract))
    assert rep.ok and rep.success and len(green.commands) == 2 and green.commands[0] == "cd apps/api.Tests && dotnet build", green.commands
    red = _StubProvider(1)
    rep = asyncio.run(_replay_build(red, "t", contract))
    assert not rep.ok and "CS0001" in rep.stderr_tail and "exit 1" in rep.stderr_tail, rep.stderr_tail
    assert default_rebuild_state()["build_commands"] == []

    # Scan-delta stall fingerprint: line numbers must not defeat the same-finding comparison
    # (observed live: detect-non-literal-fs-filename on apps/web/serve-dist.js recurring at four
    # different line numbers across four fix laps -- byte-identical text never matched, so the
    # stuck fix session was never reset until the placement's cap was exhausted).
    lap_a = [
        "  gating: [medium] sast/security/detect-non-literal-fs-filename @ apps/web/serve-dist.js:19 -- Found existsSync",
        "  gating: [medium] sast/security/detect-object-injection @ apps/web/serve-dist.js:30 -- Generic Object Injection Sink",
    ]
    lap_b = [
        "  gating: [medium] sast/security/detect-non-literal-fs-filename @ apps/web/serve-dist.js:34 -- Found existsSync",
        "  gating: [medium] sast/security/detect-object-injection @ apps/web/serve-dist.js:36 -- Generic Object Injection Sink",
    ]
    assert _scan_finding_fingerprint(lap_a) == _scan_finding_fingerprint(lap_b), "same (rule, file) at a different line must still count as the same finding"
    lap_c = ["  gating: [medium] sast/security/detect-non-literal-fs-filename @ apps/web/serve-dist.js:34 -- Found existsSync"]
    assert _scan_finding_fingerprint(lap_a) != _scan_finding_fingerprint(lap_c), "a finding that actually disappeared must change the fingerprint"
    assert _scan_finding_fingerprint([]) == frozenset()

    # Regression for the config/run_config shadowing bug (root-caused 2026-09-11): rebuild_node,
    # fix_node and escalate_node all take a LangGraph RunnableConfig as their second positional
    # arg -- naming it `config` used to silently shadow this module's own `from . import config`
    # for the rest of the function body, so every config.SOME_CONSTANT read inside resolved to the
    # RunnableConfig dict instead and crashed with AttributeError the first time any of these three
    # functions actually reached one (observed live: r_ac_to_tests_rebuild, mid-run, on a resumed
    # thread). Two levels of guard: an end-to-end call through rebuild_node's replay path (the
    # exact branch that crashed) proving config.REBUILD_OUTPUT_COMBINED_TAIL_CHARS resolves
    # correctly end to end, plus a cheap signature check on all three so the parameter can never be
    # renamed back to `config` without this self-check catching it immediately.
    import inspect

    global get_sandbox_provider

    demo_spec = RebuildSpec(
        key="rebuild-selfcheck", max_fix_cycles=3, fix_prompt_addendum="", fix_scope="full",
        next_node="next", scan_delta_gate=False,
    )
    for fn in (make_fix_node(demo_spec), make_escalate_node(demo_spec)):
        params = list(inspect.signature(fn).parameters)
        assert params[1] != "config", (
            f"{fn.__name__}'s second parameter must never be literally named `config` -- it "
            f"shadows this module's own config import for the whole function body, got {params}"
        )

    thread_id = "t-rebuild-selfcheck"
    sandbox_registry.set(thread_id, object())  # rebuild_node only checks presence, not shape
    original_get_sandbox_provider = get_sandbox_provider
    get_sandbox_provider = lambda: _StubProvider(0)  # noqa: E731
    try:
        rebuild_state = {
            **default_rebuild_state(),
            "fix_cycle_count": 1,  # >0 with build_commands -> _replay_build path, no LLM call
            "build_commands": [{"cwd": ".", "command": "true"}],
        }
        result = asyncio.run(make_rebuild_node(demo_spec)(
            {"provider": "claude", "run_id": "r1", "stages": {}, "rebuild": {demo_spec.key: rebuild_state}},
            {"configurable": {"thread_id": thread_id}},
        ))
        rb_after = result["rebuild"][demo_spec.key]
        assert rb_after["last_exit_ok"] is True, rb_after
        assert rb_after["last_stdout_tail"], "config.REBUILD_OUTPUT_COMBINED_TAIL_CHARS must have resolved to a real int, not crashed"
    finally:
        get_sandbox_provider = original_get_sandbox_provider
        sandbox_registry.pop(thread_id)

    # Regression for the discovery-turn self-report bug (root-caused 2026-09-13, observed live
    # thread 8242ea6d): the FIRST rebuild check (fix_cycle_count == 0) used to trust the model's
    # own self-reported ok/success verbatim instead of deterministically replaying its own
    # build_commands the way every later fix lap already does -- a model that gets spooked by
    # benign stderr noise on an otherwise exit-0 build could self-report ok=false and permanently
    # block a run at its very first rebuild gate. Stub stack_runner.run_and_report to return
    # exactly that shape (self-reported ok=False, but a real command that actually exits 0) and
    # confirm the deterministic replay overrides it.
    global stack_runner

    async def _fake_run_and_report(*_args: Any, **_kwargs: Any) -> BuildVerifyReport:
        return BuildVerifyReport(
            success=True, ok=False, error=None,
            stdout_tail="Build succeeded. 0 Warning(s) 0 Error(s)",
            stderr_tail="An issue was encountered verifying workloads.",
            build_commands=[BuildCommand(cwd=".", command="true")],
        )

    class _FakeStackRunner:
        run_and_report = staticmethod(_fake_run_and_report)

    thread_id = "t-rebuild-discovery-selfcheck"
    sandbox_registry.set(thread_id, object())
    original_get_sandbox_provider = get_sandbox_provider
    original_stack_runner = stack_runner
    get_sandbox_provider = lambda: _StubProvider(0)  # noqa: E731
    stack_runner = _FakeStackRunner()
    try:
        result = asyncio.run(make_rebuild_node(demo_spec)(
            {"provider": "claude", "run_id": "r1", "stages": {}, "rebuild": {demo_spec.key: default_rebuild_state()}},
            {"configurable": {"thread_id": thread_id}},
        ))
        rb_after = result["rebuild"][demo_spec.key]
        assert rb_after["last_exit_ok"] is True, (
            "the discovery turn's own self-reported ok=False must be overridden by a deterministic "
            f"replay of its build_commands (which actually exit 0): {rb_after}"
        )
    finally:
        get_sandbox_provider = original_get_sandbox_provider
        stack_runner = original_stack_runner
        sandbox_registry.pop(thread_id)

    # Task 6, requirement 3: greenfield/brownfield toolchain-capture timing. Pure, directly
    # testable without a sandbox.
    assert _should_skip_toolchain_capture("scaffold_only", True) is True, (
        "the ONE scaffold_only placement, on a genuinely greenfield first ticket, must be excluded"
    )
    assert _should_skip_toolchain_capture("scaffold_only", False) is False, (
        "brownfield (or ticket 2+, already-scaffolded) -- capturing as early as the first "
        "placement that runs this session is fine"
    )
    assert _should_skip_toolchain_capture("full", True) is False, (
        "r_minimal_code_to_green (fix_scope='full') is where capture is meant to naturally land "
        "on a fresh greenfield run -- greenfield-ness alone must never exclude it"
    )
    assert _should_skip_toolchain_capture("full", False) is False

    # Task 6, requirement 1: resolve_test_command() wired in as the FIRST choice at the TDD-red
    # gate, before any LLM discovery turn -- and the existing zero-outcomes guard just above
    # ("could not verify a single test outcome") must fire identically regardless of which path
    # produced the parse.
    from .gates import ac_coverage_gate as _acg
    from .gates.ac_coverage_gate import AcTestRunReport as _AcTestRunReport

    trx_all_red = (
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">'
        '<Results><UnitTestResult testName="T1" outcome="Failed" /></Results></TestRun>'
    )

    red_gate_commands: list[str] = []

    class _RedGateProvider:
        async def exec_in_sandbox(self, _thread_id: str, command: str, **_kw: Any) -> _Result:
            red_gate_commands.append(command)
            return _Result(0)

    original_get_sandbox_provider = get_sandbox_provider
    original_read_repo_file = repo_files.read_repo_file
    original_resolved_fn = _acg.run_resolved_test_command
    original_stack_runner = stack_runner
    discovery_calls: list[Any] = []

    tee_written = ["[apps/x] $ test run output"]  # an agent run's captured console output

    async def _fake_read_repo_file(_provider: Any, _thread_id: str, path: str) -> str | None:
        if path == _RED_GATE_OUTPUT_PATH:
            return tee_written[0]
        return trx_all_red if path == "agent-work/red-gate-resolved.trx" else None

    async def _fake_run_resolved_ok(*_args: Any, **_kwargs: Any) -> _AcTestRunReport:
        return _AcTestRunReport(
            success=True, ready_for_next_stage=True, exit_ok=True,
            result_artifacts=["agent-work/red-gate-resolved.trx"],
        )

    class _FakeStackRunnerTracks:
        @staticmethod
        async def run_and_report(*args: Any, **kwargs: Any) -> Any:
            discovery_calls.append((args, kwargs))
            raise AssertionError("resolve_test_command()'s own resolved path succeeded -- the LLM discovery turn must not run")

    get_sandbox_provider = lambda: _RedGateProvider()  # noqa: E731
    repo_files.read_repo_file = _fake_read_repo_file
    _acg.run_resolved_test_command = _fake_run_resolved_ok
    stack_runner = _FakeStackRunnerTracks()
    try:
        red_ok, detail = asyncio.run(_verify_all_red("t-red-gate-resolved-selfcheck", "claude", "selfcheck"))
        assert red_ok is True, detail
        assert not discovery_calls, "resolve_test_command()'s own resolved path must skip the LLM turn entirely"
        # Stale-evidence sweep runs first: a previous lap's *.trx must never be re-read as this lap's.
        assert "'*.trx'" in red_gate_commands[0] and _RED_GATE_OUTPUT_PATH in red_gate_commands[0], red_gate_commands
    finally:
        get_sandbox_provider = original_get_sandbox_provider
        repo_files.read_repo_file = original_read_repo_file
        _acg.run_resolved_test_command = original_resolved_fn
        stack_runner = original_stack_runner

    # Same call, but the resolved path finds nothing usable -- must fall through to the LLM
    # discovery turn exactly as it always did, and the SAME zero-outcomes guard must still fire if
    # THAT also produces nothing (never silently read as "0 failed").
    async def _fake_run_resolved_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _fake_run_and_report_empty(*_args: Any, **_kwargs: Any) -> Any:
        return _AcTestRunReport(
            success=True, ready_for_next_stage=False, exit_ok=False,
            result_artifacts=[], error="no result_artifacts reported",
        )

    class _FakeStackRunnerEmpty:
        run_and_report = staticmethod(_fake_run_and_report_empty)

    get_sandbox_provider = lambda: _RedGateProvider()  # noqa: E731
    _acg.run_resolved_test_command = _fake_run_resolved_none
    stack_runner = _FakeStackRunnerEmpty()
    repo_files.read_repo_file = _fake_read_repo_file  # the run captured output, but no parseable report
    try:
        red_ok2, detail2 = asyncio.run(_verify_all_red("t-red-gate-fallback-selfcheck", "claude", "selfcheck"))
        assert red_ok2 is False, "zero outcomes from EITHER path must never read as '0 failed'"
        assert "could not verify a single test outcome" in detail2, detail2

        # A root that never ran a test fails the gate even though another root's results are all
        # red -- quoting the run's own root and reason, whatever the stack.
        async def _fake_run_and_report_partial(*_args: Any, **_kwargs: Any) -> Any:
            return _AcTestRunReport(
                success=True, ready_for_next_stage=True, exit_ok=False,
                result_artifacts=["agent-work/red-gate-resolved.trx"],
                suites=[_acg.SuiteRun(root="apps/web", ran=False, reason="failed to load config")],
            )

        class _FakeStackRunnerPartial:
            run_and_report = staticmethod(_fake_run_and_report_partial)

        stack_runner = _FakeStackRunnerPartial()
        repo_files.read_repo_file = _fake_read_repo_file  # the other root's report: all red
        red_ok3, detail3 = asyncio.run(_verify_all_red("t-red-gate-partial-selfcheck", "claude", "selfcheck"))
        assert red_ok3 is False and "apps/web: failed to load config" in detail3, detail3

        # A suite the run agent leaves out ENTIRELY: the approved plan names a file no suite
        # accounts for -- the gate names it even though every reported result is red. A plan also
        # skips the resolved single-command shortcut (it can't account for files).
        plan = json.dumps({"test_files": [{"path": "apps/api.Tests/A.cs"}, {"path": "apps/web/b.spec.ts"}]})

        async def _read_with_plan(_provider: Any, _thread_id: str, path: str) -> str | None:
            if path == workflow_persistence.AC_TO_TESTS_APPROVED_PATH:
                return plan
            return await _fake_read_repo_file(_provider, _thread_id, path)

        agent_runs: list[int] = []

        async def _fake_run_and_report_omits(*_args: Any, **kwargs: Any) -> Any:
            agent_runs.append(1)
            assert "apps/web/b.spec.ts" in kwargs["planned_test_files"], kwargs
            return _AcTestRunReport(
                success=True, ready_for_next_stage=True, exit_ok=False,
                result_artifacts=["agent-work/red-gate-resolved.trx"],
                suites=[_acg.SuiteRun(root="apps/api.Tests", files=["/workspace/repo/apps/api.Tests/A.cs"])],
            )

        class _FakeStackRunnerOmits:
            run_and_report = staticmethod(_fake_run_and_report_omits)

        async def _resolved_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("with a test plan the resolved single-command path must be skipped")

        stack_runner = _FakeStackRunnerOmits()
        repo_files.read_repo_file = _read_with_plan
        _acg.run_resolved_test_command = _resolved_must_not_run
        red_log = CheckLog("selfcheck", _RED_CHECKS, strict=True)
        red_ok4, detail4 = asyncio.run(_verify_all_red("t-red-gate-omitted-selfcheck", "claude", "selfcheck", log=red_log))
        assert red_ok4 is False and "apps/web/b.spec.ts" in detail4 and "A.cs" not in detail4, detail4
        # ...and the gate screen's rows say exactly which check that was.
        assert [(r.id, r.status) for r in red_log.results()] == [
            (RED_SUITES_START.id, "passed"), (RED_PLANNED_FILES.id, "failed"),
        ], red_log.results()
        # An agent run that captured no output ran nothing this lap: its report is never acted on
        # (observed live: a resumed session re-reported a stale failure with zero tool calls).
        tee_written[0] = ""
        agent_runs.clear()
        silent_log = CheckLog("selfcheck", _RED_CHECKS, strict=True)
        red_ok5, detail5 = asyncio.run(_verify_all_red("t-red-gate-silent-selfcheck", "claude", "selfcheck", log=silent_log))
        assert red_ok5 is False and "captured no output" in detail5, detail5
        assert len(agent_runs) == 1 + config.VERIFY_INFRA_RETRY_CAP, "a silent run is re-run before the verdict"
        assert [(r.id, r.status) for r in silent_log.results()] == [(RED_SUITES_START.id, "infra")]
        tee_written[0] = "[apps/x] $ test run output"
        # Files may be listed repo-relative OR relative to their suite's root (incl. an absolute root).
        suites = [
            _acg.SuiteRun(root="apps/api.Tests", files=["NoteServiceTests.cs"]),
            _acg.SuiteRun(root="/workspace/repo/apps/web", files=["src/a.spec.ts", "./tests/e2e/b.spec.ts"]),
            _acg.SuiteRun(root="apps/web", files=["apps/web/src/c.spec.ts"]),
        ]
        assert {"apps/api.Tests/NoteServiceTests.cs", "apps/web/src/a.spec.ts", "apps/web/tests/e2e/b.spec.ts",
                "apps/web/src/c.spec.ts"} <= _accounted_files(suites), _accounted_files(suites)
        assert "apps/web/src/other.spec.ts" not in _accounted_files(suites)
        # The approved tests are restored after a scaffold-only fix lap edits or deletes them.
        disk = {"a.spec.ts": "contract", "b.cs": "contract-b"}

        async def _disk_read(_p: Any, _t: str, path: str) -> str | None:
            return disk.get(path)

        async def _disk_write(_p: Any, _t: str, path: str, content: str) -> None:
            disk[path] = content

        real_write = repo_files.write_repo_file
        repo_files.read_repo_file, repo_files.write_repo_file = _disk_read, _disk_write
        try:
            snap = asyncio.run(_snapshot_files(None, "t", ["a.spec.ts", "b.cs", "missing.ts"]))
            disk["a.spec.ts"] = "edited by the fixer"
            del disk["b.cs"]
            assert asyncio.run(_restore_changed(None, "t", snap)) == ["a.spec.ts", "b.cs"]
            assert disk == {"a.spec.ts": "contract", "b.cs": "contract-b"}, disk
        finally:
            repo_files.write_repo_file = real_write
            repo_files.read_repo_file = _read_with_plan
        assert [c.id for c in rebuild_checks(RebuildSpec("r", 1, "", "scaffold_only", "n"))] == [
            REBUILD_BUILD.id, *(c.id for c in _RED_CHECKS)
        ]
        assert [c.id for c in rebuild_checks(RebuildSpec("r", 1, "", "full", "n", scan_delta_gate=True))] == [
            REBUILD_BUILD.id, SCAN_DELTA.id
        ]
    finally:
        repo_files.read_repo_file = original_read_repo_file
        get_sandbox_provider = original_get_sandbox_provider
        _acg.run_resolved_test_command = original_resolved_fn
        stack_runner = original_stack_runner

    print("rebuild red-gate self-check: all assertions passed")
    _demo_resume()


def _demo_resume() -> None:
    """Resume after an interrupted draft (session c2bbdca1): a placement that already passed this
    run is skipped once the next stage has started, and a failure on files changed since its pass
    is blamed on that next stage -- the pure rules, then both nodes end to end with stubs."""
    import asyncio
    import sys
    from contextlib import ExitStack
    from unittest.mock import patch

    spec = RebuildSpec(
        "r_ac_to_tests", 3, "", "scaffold_only", "minimal-code-to-green_draft", next_stage_key="minimal-code-to-green",
    )
    sha = "3b9a9d7" + "0" * 33
    passed = {**default_rebuild_state(), "status": "clean", "last_exit_ok": True, "passed_run_id": "r1", "passed_commit": sha}
    assert should_skip_rebuild(passed, "r1", True)
    assert not should_skip_rebuild(passed, "r1", False), "the next stage never started: just run it"
    assert not should_skip_rebuild(passed, "r2", True), "an earlier run's pass proves nothing about this run"
    assert not should_skip_rebuild({**passed, "status": "failed"}, "r1", True), "a failed check always re-runs"
    assert not should_skip_rebuild(default_rebuild_state(), "", True)

    changed = [f"apps/web/src/f{i}.ts" for i in range(12)]
    with patch.object(config, "REBUILD_BLAME_FILES_PREVIEW_MAX", 10):
        blamed = blame_downstream(
            {"stage": "r_ac_to_tests", "type": "rebuild_cap_exceeded", "feedback": "TS2307"}, spec, sha, changed,
        )
    assert blamed["stage"] == "minimal-code-to-green" and blamed["blamed_check"] == "r_ac_to_tests", blamed
    assert blamed["type"] == "rebuild_cap_exceeded", "WHICH ceiling was hit is unchanged -- only who caused it"
    assert len(blamed["changed_since_pass"]) == 10 and "(+2 more)" in blamed["feedback"], blamed["feedback"]
    assert sha[:12] in blamed["feedback"] and blamed["feedback"].endswith("TS2307"), "keeps the build's own error"

    me = sys.modules[__name__]
    events: list[RunEvent] = []

    async def _append(event: RunEvent) -> RunEvent:
        events.append(event)
        return event

    async def _emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    class _NoExecProvider:
        async def exec_in_sandbox(self, _thread_id: str, command: str, **_kwargs: Any) -> Any:
            raise AssertionError(f"a skipped check must not touch the sandbox: {command}")

    thread_id = "t-rebuild-resume-selfcheck"
    run_config = {"configurable": {"thread_id": thread_id}}
    sandbox_registry.set(thread_id, object())
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(me, "get_sandbox_provider", lambda: _NoExecProvider()))
            stack.enter_context(patch.object(run_event_store, "append_event", _append))
            stack.enter_context(patch.object(run_event_stream, "emit_live", _emit))
            state = {
                "provider": "claude", "run_id": "r1", "rebuild": {spec.key: passed},
                # intake re-marked the interrupted draft (graph._set_aside_interrupted_work)
                "stages": {"minimal-code-to-green": {"status": "drafting"}},
            }
            result = asyncio.run(make_rebuild_node(spec)(state, run_config))
            _stamps = ("attempt_started_at", "checked_at")
            assert {k: v for k, v in result["rebuild"][spec.key].items() if k not in _stamps} == {
                k: v for k, v in passed.items() if k not in _stamps
            }, "skipping keeps the pass as it was (only the attempt stamp moves on)"
            assert make_route_after_rebuild(spec)({"rebuild": result["rebuild"]}) == "next"
            assert events[-1].summary == "skipped: already passed this run; minimal-code-to-green has since started"
            assert events[-1].payload == {"passed": True, "cycle": 0, "skipped": True}, events[-1].payload

        # Escalation after the check passed this run and then failed: blamed on the next stage.
        changed_calls: list[str] = []

        async def _changed(_provider: Any, _thread_id: str, commit: str) -> list[str]:
            changed_calls.append(commit)
            return ["apps/web/src/main.ts", "apps/web/angular.json"]

        async def _record(_thread_id: str, _run_id: Any, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            return payload

        failed = {**passed, "status": "failed", "last_exit_ok": False, "fix_cycle_count": 3, "last_stderr_tail": "TS2307"}
        with ExitStack() as stack:
            stack.enter_context(patch.object(me, "get_sandbox_provider", lambda: _NoExecProvider()))
            stack.enter_context(patch.object(git_ops, "changed_since", _changed))
            stack.enter_context(patch.object(run_failure, "record_run_failure_and_reset", _record))
            out = asyncio.run(make_escalate_node(spec)({"run_id": "r1", "rebuild": {spec.key: failed}}, run_config))
            assert changed_calls == [sha] and out["run_failure"]["stage"] == "minimal-code-to-green", out["run_failure"]
            assert "apps/web/src/main.ts" in out["run_failure"]["feedback"]
            # Passed only in an EARLIER run: this run's own redraft may have caused it -- no blame.
            out = asyncio.run(make_escalate_node(spec)({"run_id": "r2", "rebuild": {spec.key: failed}}, run_config))
            assert out["run_failure"]["stage"] == "r_ac_to_tests" and changed_calls == [sha], out["run_failure"]
    finally:
        sandbox_registry.pop(thread_id)

    # Coverage the re-scan judges: after a fix lap, a fresh measurement beats the stored value (which
    # predates the fix -- session c2bbdca1 judged four laps of added tests on the same stale number);
    # a measurement that can't produce numbers falls back to the stored value, never "unmeasured".
    from unittest.mock import patch as _patch

    from . import metrics_nodes

    stored = {"repo_scan": {"coverage": {"line_rate": 87.1, "branch_rate": 76.1}}}

    async def _fresh(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"line_rate": 96.0, "branch_rate": 95.5}

    async def _no_numbers(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    async def _disk(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("the stored value is numeric -- disk must not be consulted")

    with _patch.object(metrics_nodes, "_read_coverage_summary", _disk):
        with _patch(f"{__name__}._remeasure_coverage", _fresh):
            assert asyncio.run(_coverage_for_scan(None, "t", stored, remeasure=True))["line_rate"] == 96.0
            assert asyncio.run(_coverage_for_scan(None, "t", stored, remeasure=False))["line_rate"] == 87.1, "no fix lap: keep the stored value"
        with _patch(f"{__name__}._remeasure_coverage", _no_numbers):
            assert asyncio.run(_coverage_for_scan(None, "t", stored, remeasure=True))["line_rate"] == 87.1
    print("rebuild resume self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
