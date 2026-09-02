"""P4's write-scope gate: the AC-to-tests stage only ever touches test files.

Enforced with a plain `git diff --name-only` against the baseline commit captured before the
stage's first draft attempt (graph.py's make_draft_node, StageSpec.capture_baseline_commit),
classified against a per-stack allowlist. Never trusts the model's own claim about what it
touched -- this has always been the authoritative check.

There used to be a "Layer 1" in front of it too: a `pre_tool_use_hook` (the old SDK-based Copilot
session, wired via P4's StageSpec.session_options) that fast-failed an out-of-scope tool call
before it ran. Neither current provider's CLI-exec turn has anything to translate that hook into
-- ClaudeChatModel's and CopilotChatModel's own `_agenerate_inner` just log a warning and proceed
when one is set -- so the wiring was removed from P4's StageSpec instead of firing that warning,
for no enforcement benefit, on every single ac-to-tests turn. This module was always the
authoritative layer regardless (a subagent's own tool or an MCP-exposed filesystem tool could
bypass a pre-tool-use hook even when one existed); it is simply the only layer now.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import repo_files, workflow_persistence
from ..repo_files import validate_repo_relative_path
from ..sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..graph import VerificationResult

# Each pattern is matched against a repo-relative path with re.search (not fullmatch) -- deliberately
# permissive about surrounding path segments, strict about the filename/directory shape itself.
_DOTNET_TEST_PATTERNS = [
    r"(^|/)[A-Za-z0-9_.]+\.Tests(/|$)",
    r"(^|/)[A-Za-z0-9_.]+Tests\.csproj$",
    r"(^|/)[A-Za-z0-9_.]*Tests?\.cs$",
]
_TS_TEST_PATTERNS = [
    r"\.test\.tsx?$",
    r"\.spec\.tsx?$",
    r"(^|/)(tests|__tests__|test|e2e)/",
    # [cm]?[jt]s covers the ESM/CJS flavours (`.mts`, `.mjs`, `.cts`, `.cjs`, plain `.js`) a
    # package's own "type" field can REQUIRE -- observed live: legitimate `vitest.config.mts`
    # files at apps/jobs and packages/db were silently reverted because only `.ts(x)` matched,
    # while _E2E_PATH_RE below already accepted `playwright.config.[jt]sx?`. Configs the stage is
    # explicitly told to write must never be deleted over their extension.
    r"(^|/)playwright\.config\.[cm]?[jt]sx?$",
    r"(^|/)vitest\.config\.[cm]?[jt]sx?$",
    # The vitest/Angular setup file its config points at (setupFiles: ["src/test-setup.ts"]) --
    # observed live (2026-08-30): quarantined every lap because the hyphenated name matches none
    # of the patterns above, and the model (never told) rewrote it every lap.
    r"(^|/)test-setup\.[cm]?[jt]s$",
]
_PY_TEST_PATTERNS = [
    r"(^|/)test_[A-Za-z0-9_]+\.py$",
    r"(^|/)[A-Za-z0-9_]+_test\.py$",
    r"(^|/)tests?/",
    r"(^|/)conftest\.py$",
]

_ALL_PATTERNS = [re.compile(p) for p in _DOTNET_TEST_PATTERNS + _TS_TEST_PATTERNS + _PY_TEST_PATTERNS]


def _is_test_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in _ALL_PATTERNS)


# Paths the PIPELINE ITSELF writes and commits between the baseline commit and this gate's diff
# (stage artifacts, the action ledger, the spec id ledger). Observed live: every ac-to-tests
# verify cycle flagged `.ai-dev-workflow/ac-to-tests.draft.json` etc. as scope violations the
# model could never fix -- it didn't write them, workflow persistence did -- deadlocking the
# stage at the verify cap. The scope rule is about the MODEL's writes only.
_PIPELINE_OWNED_PREFIXES = (".ai-dev-workflow/", "APPROVALS.md", "AGENTS.md")


# Artifacts the coverage gate's own test run produces (runner reports and Playwright's failure
# dumps) at whatever depth the test roots live. They are the GATE's requested evidence, not model
# writes -- quarantining them each lap deleted the very reports ac_coverage_gate prefers (observed
# live 2026-08-30: apps/web/ac-run-playwright.json + test-results/ reverted every lap). The
# coverage gate deletes them itself before each fresh run, so staleness is handled there.
_RUNNER_ARTIFACT_RE = re.compile(r"(^|/)(ac-run-[^/]*\.json$|test-results/|TestResults/)|\.trx$")


def _is_pipeline_owned(path: str) -> bool:
    return path.startswith(_PIPELINE_OWNED_PREFIXES) or bool(_RUNNER_ARTIFACT_RE.search(path))


# A Playwright end-to-end spec: either it sits in an e2e directory, or it's the playwright config
# itself. Matched by LOCATION rather than by reading imports -- `tests/e2e/` is the convention this
# stage's own prompt mandates, and the coverage gate relies on the same split to exclude browser
# specs from a unit run.
_E2E_PATH_RE = re.compile(r"(^|/)e2e(/|$)|(^|/)playwright\.config\.[jt]sx?$|\.e2e\.[jt]sx?$", re.IGNORECASE)


def _has_e2e_test(changed_paths: list[str]) -> bool:
    """True when a real browser-level spec was written (a playwright config alone doesn't count --
    a config with no spec runs zero tests and still yields no screenshots)."""
    return any(
        _is_test_path(p)
        and _E2E_PATH_RE.search(p)
        and not _is_pipeline_owned(p)
        and not p.endswith(("playwright.config.ts", "playwright.config.js"))
        for p in changed_paths
    )


async def _stack_has_ui(provider: SandboxProvider, thread_id: str) -> bool:
    """UI-framework signal read from the repo's own approved tech-stack record.

    Read from disk rather than from this stage's content_dict, which holds the ac-to-tests draft and
    has no tech_stack key. Uses the same `frameworks_have_ui` helper as e2e's gate and P3's wireframe
    requirement, so all three agree on what "has a UI" means. Fails OPEN (False) when the record is
    unreadable -- an unreadable artifact must not be reported as a missing browser test.
    """
    from ..tech_stack_signals import frameworks_have_ui, presence_values

    for path in (workflow_persistence.TECH_STACK_APPROVED_PATH, workflow_persistence.TECH_STACK_DRAFT_PATH):
        raw = await repo_files.read_repo_file(provider, thread_id, path)
        if raw is None:
            continue
        try:
            tech_stack = json.loads(raw)
        except json.JSONDecodeError:
            continue
        frameworks = presence_values(tech_stack, "frameworks")
        if frameworks:
            return frameworks_have_ui(frameworks)
    return False


def _has_non_e2e_test(changed_paths: list[str]) -> bool:
    """True when at least one written test lives below the browser layer (unit/integration/
    subcutaneous). Pipeline artifacts never count as tests."""
    return any(
        _is_test_path(p) and not _E2E_PATH_RE.search(p) and not _is_pipeline_owned(p)
        for p in changed_paths
    )


@dataclass(frozen=True)
class WriteScopeOutcome:
    passed: bool
    violating_paths: list[str]
    changed_paths: list[str]
    reverted_paths: list[str] = field(default_factory=list)
    # Non-empty exactly when reverted_paths is: where a COPY of each reverted/deleted path's
    # content survives, for a human (or the model, next lap) to confirm a true scope violation
    # versus this gate's 3-language-family test-path regex missing a legitimate test in an
    # unrecognized language (Go, Rust, Java, Ruby, ...). See check_write_scope's docstring.
    quarantine_dir: str = ""


async def check_write_scope(
    provider: SandboxProvider, thread_id: str, baseline_commit: str | None, run_id: str = "unknown"
) -> WriteScopeOutcome:
    if baseline_commit is None:
        # No baseline captured (e.g. this draft never actually ran against a sandbox) -- nothing
        # to diff against, so nothing to flag. StageSpec.capture_baseline_commit guarantees this
        # is set on every real run; a None here means the stage's own precondition wasn't met,
        # which is a bug in wiring, not a content problem -- fail open rather than false-positive.
        return WriteScopeOutcome(passed=True, violating_paths=[], changed_paths=[], reverted_paths=[])

    # Untracked files MUST be included: the draft's brand-new test files are exactly that until
    # a later stage commits them, and `git diff <commit>` alone never lists untracked paths --
    # observed live (headless run 3): changed_paths showed only the ledger while five new test
    # files sat invisible, which both misreports the stage and would let untracked non-test
    # writes slip past the scope rule entirely.
    result = await provider.exec_in_sandbox(
        thread_id,
        f"git diff --name-only {baseline_commit} -- . && git ls-files --others --exclude-standard",
    )
    changed_paths = sorted({line.strip() for line in (result.stdout or "").splitlines() if line.strip()})
    violating = [p for p in changed_paths if not _is_test_path(p) and not _is_pipeline_owned(p)]
    if violating:
        # Retirement carve-out: deleting a test file for a RETIRED criterion is exactly what the
        # residue check demands, but on a stack _ALL_PATTERNS doesn't recognize (Go, Rust, Java
        # outside tests/) the deletion classifies as a violation and the revert below would restore
        # the file -- an infinite fight between two deterministic checks. A deleted path whose
        # baseline content names a retired AC id is therefore in-scope by definition.
        from .. import spec_ledger
        from ..test_results import ac_ids_in_name

        retired_ids = {
            e["id"]
            for e in await spec_ledger.load_ledger(provider, thread_id)
            if e.get("kind") == "acceptance_criterion" and e.get("status") == "retired"
        }
        if retired_ids:
            still_violating = []
            for p in violating:
                validate_repo_relative_path(p)
                probe = await provider.exec_in_sandbox(
                    thread_id,
                    f"if [ -e {shlex.quote(p)} ]; then echo __EXISTS__; "
                    f"else git show {shlex.quote(baseline_commit)}:{shlex.quote(p)} 2>/dev/null; fi",
                )
                out = probe.stdout or ""
                if "__EXISTS__" not in out.splitlines()[:1] and any(
                    set(ac_ids_in_name(line)) & retired_ids for line in out.splitlines()
                ):
                    logger.info("write-scope gate: allowing retirement delete of %s (thread %s)", p, thread_id)
                    continue
                still_violating.append(p)
            violating = still_violating
    if not violating:
        return WriteScopeOutcome(passed=True, violating_paths=[], changed_paths=changed_paths, reverted_paths=[])

    # Deterministic remediation instead of feedback the model cannot act on: the draft session
    # has create/edit tools but NO delete and NO bash, so "revert these files" deadlocked the
    # stage at the verify cap (observed live, headless run 5: two helper .sh scripts at the repo
    # root). The gate enforces its own contract -- untracked out-of-scope files are removed,
    # tracked ones restored to their committed state -- and the stage proceeds on what remains.
    #
    # A COPY is quarantined under .ai-dev-workflow/quarantine/ (already pipeline-owned, so it
    # never re-triggers this same check next lap) before the revert runs. This is a copy-then-
    # revert, never a plain move: a *tracked* file that was only moved (not reverted) would still
    # show up as a deletion against baseline_commit in every future git diff -- the model has no
    # way to "fix" a deletion of a file it correctly should not have touched, so that would
    # deadlock exactly like the original no-revert-at-all design did, just via a different path.
    # Reverting to baseline is still what makes the violation disappear; quarantining only stops
    # that revert from being silent, unrecoverable data loss when the "violation" is actually a
    # legitimate test file in a language this gate's regex doesn't recognize.
    for path in violating:
        validate_repo_relative_path(path)
    quoted = " ".join(shlex.quote(p) for p in violating)
    quarantine_dir = f".ai-dev-workflow/quarantine/{run_id}"
    revert = await provider.exec_in_sandbox(
        thread_id,
        f"mkdir -p {shlex.quote(quarantine_dir)} && "
        f"for p in {quoted}; do "
        f"mkdir -p \"{quarantine_dir}/$(dirname -- \"$p\")\" && cp -f -- \"$p\" \"{quarantine_dir}/$p\" 2>/dev/null; "
        f"if git ls-files --error-unmatch -- \"$p\" >/dev/null 2>&1; "
        f"then git checkout -- \"$p\"; else rm -rf -- \"$p\"; fi; done",
    )
    if not revert.ok:
        return WriteScopeOutcome(passed=False, violating_paths=violating, changed_paths=changed_paths, reverted_paths=[])
    logger.info(
        "write-scope gate quarantined+reverted out-of-scope paths for thread_id=%s: %s -> %s",
        thread_id, violating, quarantine_dir,
    )
    return WriteScopeOutcome(
        passed=True, violating_paths=[], changed_paths=changed_paths, reverted_paths=violating, quarantine_dir=quarantine_dir
    )


async def verify_ac_to_tests(
    thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None, provider: SandboxProvider,
    chat_provider: str,
) -> "VerificationResult":
    """Combines the write-scope check above with the AC-coverage check (ac_coverage_gate.py) into
    one VerificationResult, since both answer the same question -- "is P4's output acceptable" --
    not two independently useful checks. Imported lazily to avoid a module-load-time cycle with
    graph.py (which imports this module).

    `chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is threaded straight
    through to check_ac_coverage below, which needs it for its own stack_runner.run_and_report
    call -- named distinctly from `provider` (the pre-existing SandboxProvider connection object)
    to avoid colliding with it."""
    from .. import spec_ledger
    from ..graph import VerificationResult
    from .ac_coverage_gate import (
        check_ac_coverage,
        check_completed_ac_protection,
        check_deferred_ac_residue,
        check_ledger_integrity,
        check_retired_ac_residue,
    )

    write_scope = await check_write_scope(provider, thread_id, baseline_commit, run_id)
    if not write_scope.passed:
        # Only reachable when the gate's own auto-revert failed -- in-scope violations are
        # remediated deterministically (removed/restored), never bounced back to the model,
        # which has no delete tool and no bash to act on such feedback.
        return VerificationResult(
            passed=False,
            feedback=(
                "These files are outside the test-only write scope for this stage and could not "
                f"be auto-reverted: {write_scope.violating_paths}. Only test files may be created "
                "or modified here."
            ),
            report={"violating_paths": write_scope.violating_paths, "changed_paths": write_scope.changed_paths},
        )

    # Provenance protections, before any content checks: the ledger must be untampered (it is the
    # truth every check below reads), retired criteria's tests must be gone, and completed
    # criteria's tests must be untouched.
    ledger_entries = await spec_ledger.load_ledger(provider, thread_id)
    protection_problems = (
        await check_ledger_integrity(provider, thread_id)
        + await check_retired_ac_residue(provider, thread_id, ledger_entries)
        + await check_deferred_ac_residue(provider, thread_id, ledger_entries)
        + await check_completed_ac_protection(provider, thread_id, baseline_commit, ledger_entries)
    )
    if protection_problems:
        return VerificationResult(
            passed=False,
            feedback="; ".join(protection_problems),
            report={"changed_paths": write_scope.changed_paths, "protection_problems": protection_problems},
        )

    # Work-queue scoping: when every one of this ticket's own criteria is already delivered (or the
    # ticket only retires criteria), writing no new tests is CORRECT -- the wrote-nothing and
    # test-pyramid checks below would otherwise demand work the pipeline forbids re-doing.
    own_ac_ids: set[str] = set()
    raw_spec = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH)
    if raw_spec is not None:
        try:
            own_ac_ids = spec_ledger.own_ac_ids_from_specification(json.loads(raw_spec))
        except json.JSONDecodeError:
            pass
    no_eligible_work = raw_spec is not None and not spec_ledger.eligible_ac_ids(ledger_entries, own_ac_ids)

    # A draft that changed nothing but pipeline artifacts DESCRIBED tests instead of creating
    # them (observed live, run 13: three laps of readiness=true structured replies, zero file
    # writes). Name that failure exactly -- "no test found covering US-xxxx" reads to the model
    # like a naming problem, not a you-never-wrote-files problem.
    real_changes = [p for p in write_scope.changed_paths if not p.startswith((".ai-dev-workflow/", "APPROVALS.md", "AGENTS.md"))]
    if not real_changes and not no_eligible_work:
        return VerificationResult(
            passed=False,
            feedback=(
                "You created NO test files -- the working tree has no changes beyond pipeline "
                "artifacts. Your structured response is metadata ABOUT files; the files "
                "themselves must be written to disk with your file tools (create/edit) BEFORE "
                "you respond. Write the actual test files now."
            ),
            report={"changed_paths": write_scope.changed_paths},
        )

    # Test-pyramid check: a suite made only of Playwright e2e specs means the stage stopped at the
    # outermost layer. Observed live -- one `apps/web/tests/e2e/*.spec.ts` and nothing else, for
    # every AC, because a user-facing criterion always "needs a browser" under the skill's own
    # heuristic. That suite is slow, brittle, and proves no rule below the UI; it also leaves the
    # coverage gate with nothing instrumentable, since a unit runner cannot execute Playwright
    # specs. Enforced here rather than left to the prompt, which the model can silently ignore.
    if not _has_non_e2e_test(real_changes) and not no_eligible_work:
        return VerificationResult(
            passed=False,
            feedback=(
                "Every test you wrote is a Playwright end-to-end spec. A browser test cannot prove "
                "the rules beneath the UI, and a unit runner cannot execute it, so this suite is "
                "not acceptable on its own. Add tests BELOW the UI for the same criteria and keep "
                "e2e for genuine user journeys: unit tests for logic/validation/state rules, and "
                "integration or subcutaneous tests (API/service layer, no browser) for workflows. "
                "You may create these without touching any dependency manifest -- a .NET test "
                "project (e.g. apps/api.Tests/Api.Tests.csproj plus *Tests.cs) and/or JS/TS "
                "*.test.ts files with a vitest.config.ts run on the sandbox's baked runners."
            ),
            report={"changed_paths": write_scope.changed_paths, "e2e_only": True},
        )

    # The mirror of the check above: a UI stack that wrote NO browser test at all. Both directions
    # are enforced because each alone is satisfiable while dodging the other -- e2e-only stops at the
    # outermost layer, and no-e2e leaves the running app unproven and (just as concretely) leaves the
    # e2e stage with nothing to run, which is how every delivered branch ended up with zero
    # screenshots and a blocked merge.
    if not no_eligible_work and await _stack_has_ui(provider, thread_id) and not _has_e2e_test(real_changes):
        return VerificationResult(
            passed=False,
            feedback=(
                "You did NOT write a Playwright spec. Check the working tree before you answer "
                "again: previous attempts reported \"Added required Playwright e2e skeleton files "
                "beside the web app (config + spec)\" four times in a row while making no write "
                "call for either file, so the claim was false each time and this gate caught it "
                "each time. Your response is metadata about files that must already exist.\n\n"
                "This stack has a UI framework but you wrote no Playwright end-to-end spec. The "
                "running app is never exercised through a browser, and the e2e stage has nothing to "
                "run -- so the merge is blocked for missing visual evidence no matter how good the "
                "unit tests are. Add a playwright.config.ts beside the web app plus at least one "
                "spec under its tests/e2e/ covering the primary user journeys. Import from "
                "'@playwright/test' in BOTH the config and the specs (mixing that with "
                "'playwright/test' loads two runner copies and playwright refuses to run), set "
                "screenshot: 'on', take baseURL from process.env.BASE_URL, and locate elements with "
                "getByTestId. Keep the tests below the UI that you already wrote."
            ),
            report={"changed_paths": write_scope.changed_paths, "missing_e2e": True},
        )

    coverage = await check_ac_coverage(provider, thread_id, content_dict, chat_provider=chat_provider, run_id=run_id)
    report = {"changed_paths": write_scope.changed_paths, **coverage.report}
    feedback = coverage.feedback
    if write_scope.reverted_paths:
        report["reverted_out_of_scope_paths"] = write_scope.reverted_paths
        report["quarantine_dir"] = write_scope.quarantine_dir
        # Tell the MODEL, not just the report: a silent revert made the draft rewrite the same
        # out-of-scope files every lap (observed live 2026-08-30, test-setup.ts x2), because
        # nothing it could read ever said they were being deleted.
        feedback = (
            (feedback + " " if feedback else "")
            + "NOTE: these paths were out of this stage's write scope and were reverted/removed "
            f"({', '.join(write_scope.reverted_paths)}) -- do NOT write them again; only test "
            "files, test configs, and their setup files are in scope here."
        )
    return VerificationResult(passed=coverage.passed, feedback=feedback, report=report)


def _demo() -> None:
    """Self-check for the pure path classifiers. The gate's own I/O half needs a sandbox."""
    # e2e-only suites are what this stage produced live, and must be rejected
    # A UI stack must ALSO write a browser spec -- the mirror of the e2e-only rejection. A config
    # with no spec does not count: it runs zero tests and yields no screenshots.
    assert _has_e2e_test(["apps/web/tests/e2e/a.spec.ts"])
    assert not _has_e2e_test(["apps/web/playwright.config.ts"])
    assert not _has_e2e_test(["apps/api.Tests/TaskTests.cs", "apps/web/src/app/calc.test.ts"])
    assert not _has_e2e_test([".ai-dev-workflow/ledger.jsonl"])
    # Both directions together: a full, healthy suite satisfies each check.
    _full = ["apps/web/tests/e2e/a.spec.ts", "apps/api.Tests/TaskTests.cs"]
    assert _has_e2e_test(_full) and _has_non_e2e_test(_full)

    assert not _has_non_e2e_test(["apps/web/tests/e2e/task-tracker.ac.spec.ts"])
    assert not _has_non_e2e_test(["apps/web/tests/e2e/a.spec.ts", "apps/web/playwright.config.ts"])
    # a real pyramid passes
    assert _has_non_e2e_test(["apps/web/tests/e2e/a.spec.ts", "apps/api.Tests/TaskTests.cs"])
    assert _has_non_e2e_test(["apps/web/src/app/calc.test.ts"])
    assert _has_non_e2e_test(["apps/api.Tests/Api.Tests.csproj", "apps/api.Tests/TaskTests.cs"])
    assert _has_non_e2e_test(["tests/test_tasks.py"])
    # vitest/Angular setup file is in scope (was quarantined every lap, live 2026-08-30)
    assert _is_test_path("apps/web/src/test-setup.ts")
    assert _is_test_path("apps/web/src/test-setup.mjs")
    # the coverage gate's own runner artifacts are pipeline-owned, never model violations
    assert _is_pipeline_owned("apps/web/ac-run-playwright.json")
    assert _is_pipeline_owned("apps/web/test-results/foo/error-context.md")
    assert _is_pipeline_owned("apps/api.Tests/TestResults/run.trx")
    assert _is_pipeline_owned("apps/api.Tests/results.trx")
    assert not _is_pipeline_owned("apps/web/src/app/app.ts")
    # non-test and pipeline-owned files never count as tests
    assert not _has_non_e2e_test(["apps/web/src/app/page.tsx"])
    assert not _has_non_e2e_test([".ai-dev-workflow/ledger.jsonl"])

    # Task 8: prove check_write_scope's classifier is operation-agnostic BY CONSTRUCTION, not just
    # by inspection. `git diff --name-only` (the only source check_write_scope's changed_paths is
    # built from) prints a bare path for an added, edited, OR deleted file alike -- no status
    # letter ever reaches this module. _is_test_path, the function that decides violating-vs-not,
    # takes only that path string, so a DELETED test path and an EDITED test path are, and always
    # were, indistinguishable to this gate: both are in-scope. A deleted non-test path is flagged
    # and reverted (git checkout -- restores it, undoing the delete) exactly like an edited one
    # already was. No gate change was needed to support test-retirement deletes; this assertion
    # just makes that fact a standing check instead of a one-time reading of the source.
    assert _is_test_path("apps/api.Tests/TaskTests.cs")  # true whether this path was added, edited, or deleted
    assert not _is_test_path("apps/api/Startup.cs")  # same regardless of operation -- always reverted if changed
    print("write_scope_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
