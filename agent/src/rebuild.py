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

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from .prompt_loader import load_prompt_pair, render_prompt

from . import git_ops, model_config, repo_files, run_failure, stack_runner, test_results, workflow_persistence
from .chat_model import get_chat_model_for_thread
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


def default_rebuild_state() -> RebuildState:
    return {
        "status": "not_started", "fix_cycle_count": 0, "last_stdout_tail": "", "last_stderr_tail": "",
        "last_exit_ok": False, "cannot_verify": False, "build_commands": [],
    }


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
        stdout_parts.append(f"{label}\n{(result.stdout or '')[-2000:]}")
        stderr_parts.append(f"{label}\n{(result.stderr or '')[-2000:]}")
    return BuildVerifyReport(
        success=ok, ok=ok,
        stdout_tail="\n".join(stdout_parts)[-4000:],
        stderr_tail="\n".join(stderr_parts)[-4000:],
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


# Where the TDD-red gate's suite run tees its console output (same convention as the AC gate's
# AC_TEST_OUTPUT_PATH; separate file so the two runs never clobber each other's evidence).
_RED_GATE_OUTPUT_PATH = "agent-work/red-gate-output.txt"


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


async def _scan_regression_reasons(provider: Any, thread_id: str, state: dict[str, Any]) -> list[str]:
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
        scan = await repo_scan.run_repo_scan(provider, thread_id, profile="full")
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
    coverage = (state.get("repo_scan") or {}).get("coverage") or {}
    if not (isinstance(coverage.get("line_rate"), (int, float)) and isinstance(coverage.get("branch_rate"), (int, float))):
        coverage = await metrics_nodes._read_coverage_summary(provider, thread_id)  # noqa: SLF001 -- same package, one reader
    baseline = (state.get("repo_scan") or {}).get("baseline_summary") or {}
    reasons = metrics_nodes.regression_reasons(
        latest_summary,
        None,  # no delta: this is an absolute check on the tree as it stands right now
        coverage,
        baseline_has_findings=bool((baseline.get("gating_count") or 0)),
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
                severity_floor=latest_summary.get("severity_floor") or repo_scan.SECURITY_SEVERITY_FLOOR,
                introduced_ids=None,
                direct_dependencies=scan.direct_dependencies,
                known_gap_ids=known_gap_ids,
            )
        ]
        for f in gating[:10]:
            reasons.append(
                f"  gating: [{f.severity}] {f.category}/{f.rule_id} @ {f.file or '?'}"
                + (f":{f.line}" if f.line else "")
                + f" -- {(f.title or f.message or '')[:110]}"
            )
        if len(gating) > 10:
            reasons.append(f"  ...and {len(gating) - 10} more gating finding(s)")
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


async def _verify_all_red(
    thread_id: str, chat_provider: str, run_id: str = "unknown", eligible_only: set[str] | None = None
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
    `state` of its own, same reasoning as chat_provider."""
    from .gates.ac_coverage_gate import AcTestRunReport  # local: avoids import at module load

    provider = get_sandbox_provider()
    await provider.exec_in_sandbox(thread_id, f"rm -f {shlex.quote(_RED_GATE_OUTPUT_PATH)}")
    report = await stack_runner.run_and_report(
        thread_id,
        stage_key="red-gate",
        prompt_name="ac_test_run",
        schema=AcTestRunReport,
        provider=chat_provider,
        run_id=run_id,
        output_path=_RED_GATE_OUTPUT_PATH,
    )
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

    if not outcomes:
        return False, (
            "TDD-red gate: could not verify a single test outcome -- the suite run produced no "
            f"parseable runner report ({report.error or 'no result_artifacts reported'}). Re-run "
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
        names = ", ".join(passed[:10]) + (f", and {len(passed) - 10} more" if len(passed) > 10 else "")
        return False, (
            f"TDD-red gate (ticket scope): {len(passed)} test(s) for this ticket's undelivered "
            f"criteria PASSED after scaffolding ({failed} failed): {names}. Strip only THOSE code "
            "paths back to stubs so they fail at runtime -- leave earlier tickets' passing "
            "regression tests untouched."
        )
    all_red, passed, failed = red_gate_verdict(outcomes)
    if not all_red:
        names = ", ".join(passed[:10]) + (f", and {len(passed) - 10} more" if len(passed) > 10 else "")
        return False, (
            f"TDD-red gate: {len(passed)} test(s) PASSED after scaffolding ({failed} failed): "
            f"{names}. Scaffolding must not implement behavior -- strip those code paths back to "
            "NotImplementedException-style stubs so every test fails at runtime; a test that "
            "passes before the implementation stage proves nothing."
        )
    return True, f"TDD-red verified: 0 passed / {failed} failed."


def make_rebuild_node(spec: RebuildSpec):
    async def rebuild_node(state: dict[str, Any], config) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rb = rebuild.get(spec.key, default_rebuild_state())

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

        # GHCP finds every buildable project and builds it from the right directory, then reports
        # through a schema-validated terminal tool. Replaces "an audit model guesses a build
        # command + root, Python runs `cd {root} && {command}` blindly" -- that guess was wrong on
        # every headless run (a greenfield monorepo has nothing buildable at the repo root, so
        # `dotnet build` died with MSB1003 in ~2s and this node silently escalated every time).
        replayed = bool(rb["fix_cycle_count"] > 0 and rb.get("build_commands"))
        if replayed:
            # Fix laps re-run the contract the discovery turn established; the model is never
            # asked "does it build?" twice in one placement (see BuildVerifyReport.build_commands).
            report = await _replay_build(provider, thread_id, rb["build_commands"])
        else:
            report = await stack_runner.run_and_report(
                thread_id,
                stage_key="rebuild",
                prompt_name="rebuild_verify",
                schema=BuildVerifyReport,
                provider=state["provider"],
                run_id=state.get("run_id", "unknown"),
                addendum=spec.fix_prompt_addendum or "",
            )
            rb["build_commands"] = [c.model_dump() for c in (report.build_commands or [])]
            if not rb["build_commands"]:
                logger.warning("rebuild %s: discovery reported no build_commands -- fix laps will fall back to the model", spec.key)
        build_ok = report.success and report.ok

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
        mctg_status = ((state.get("stages") or {}).get("minimal-code-to-green") or {}).get("status", "not_started")
        mctg_never_ran = (
            await repo_files.read_repo_file(provider, thread_id, workflow_persistence.MINIMAL_CODE_TO_GREEN_DRAFT_PATH) is None
            and mctg_status == "not_started"
        )
        if build_ok and spec.fix_scope == "scaffold_only" and mctg_never_ran:
            red_ok, red_detail = await _verify_all_red(thread_id, state["provider"], run_id=state.get("run_id", "unknown"))
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
                red_ok, red_detail = await _verify_all_red(
                    thread_id, state["provider"], run_id=run_id, eligible_only=eligible
                )
                if not red_ok:
                    build_ok = False
                    red_failed = True

        # Scan-delta gate: same question the terminal metrics gate asks, asked here where it is
        # still actionable. See RebuildSpec.scan_delta_gate for why this placement exists.
        scan_detail = ""
        if build_ok and spec.scan_delta_gate:
            scan_reasons = await _scan_regression_reasons(provider, thread_id, state)
            scan_reasons += await _provenance_reasons(provider, thread_id, state)
            if scan_reasons:
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

        rb["status"] = "clean" if build_ok else "failed"
        rb["last_exit_ok"] = build_ok
        rb["last_stdout_tail"] = (report.stdout_tail or "")[-4000:]
        # A red-gate violation replaces the (green) build's stderr as the fix node's feedback --
        # the passing test names are the actionable part, not a clean compiler log.
        rb["last_stderr_tail"] = (
            red_detail if red_failed
            else scan_detail if scan_detail
            else (report.stderr_tail or report.error or "")
        )[-4000:]
        rebuild[spec.key] = rb

        ledger_entry: dict[str, Any] = {
            "stage": spec.key, "node": "rebuild", "ok": build_ok, "cycle": rb["fix_cycle_count"],
            "verify": "replay" if replayed else "discovery",
        }
        if red_detail:
            # 1500, not 300: this is the DURABLE record of why the red gate blocked, and the detail
            # is a LIST of the tests that wrongly passed. 300 characters stopped inside the first
            # entry ("41 test(s) PASSED after scaffolding (16 failed): [US-0001.1] displays the
            # value..."), so the ledger recorded that the gate fired without recording what it
            # found -- the same truncation that made an adversarial-compliance rejection
            # unreadable in the run log.
            ledger_entry["red_gate"] = red_detail[:1500]
        await repo_files.append_ledger_entry(provider, thread_id, ledger_entry)
        if build_ok:
            # A green build is the checkpoint where the code-writing sessions' source changes
            # (codegen, fixes) become worth keeping -- the artifact-only commit sites never stage
            # source, so without this the pushed work branch would carry no code at all.
            await git_ops.commit_all(provider, thread_id, f"ai-dev-workflow: {spec.key} source changes (build green)")
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
    "your change -- only the COMPILER's complaints are your job here. If a test fails to compile "
    "because it references a symbol that doesn't exist yet, add the minimal stub; do not make the "
    "test pass."
)


def make_fix_node(spec: RebuildSpec):
    async def fix_node(state: dict[str, Any], config) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
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
            "draft",
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
        try:
            await call_with_infra_retry(
                lambda: model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)]),
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
            rb["last_stderr_tail"] = f"[infra failure, fix lap not attempted] {exc}"[-4000:]

        rb["fix_cycle_count"] = rb["fix_cycle_count"] + 1
        rb["status"] = "fixing"
        rebuild[spec.key] = rb
        return {"rebuild": rebuild}

    return fix_node


def make_escalate_node(spec: RebuildSpec):
    async def escalate_node(state: dict[str, Any], config) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
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
            "feedback": (rb["last_stderr_tail"] or rb["last_stdout_tail"] or "")[-1000:],
        }
        payload = await run_failure.record_run_failure_and_reset(
            thread_id, state.get("run_id"),
            payload=payload,
            detail_for_classification=f"{rb['last_stdout_tail']} {rb['last_stderr_tail']}",
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

        async def exec_in_sandbox(self, _thread_id: str, command: str) -> _Result:
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
    print("rebuild red-gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
