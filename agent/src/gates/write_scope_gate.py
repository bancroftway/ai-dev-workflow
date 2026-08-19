"""P4's write-scope gate: Layer 2 of the two-layer enforcement (plan's Part B, P4 section) that
the AC-to-tests stage only ever touches test files.

Layer 1 (agent/src/copilot_chat_model.py's pre_tool_use_hook, wired via P4's StageSpec.
session_options) is best-effort and SDK-level -- fast-fails a tool call before it runs, but a
subagent's own tool or an MCP-exposed filesystem tool could bypass it. This module is Layer 2, the
authoritative one: a plain `git diff --name-only` against the baseline commit captured before the
stage's first draft attempt (graph.py's make_draft_node, StageSpec.capture_baseline_commit),
classified against a per-stack allowlist. Never trusts the model's own claim about what it
touched.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import repo_files
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
    r"(^|/)playwright\.config\.tsx?$",
    r"(^|/)vitest\.config\.tsx?$",
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


def _is_pipeline_owned(path: str) -> bool:
    return path.startswith(_PIPELINE_OWNED_PREFIXES)


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
    from ..tech_stack_signals import frameworks_have_ui

    for path in (".ai-dev-workflow/tech-stack.approved.json", ".ai-dev-workflow/tech-stack.draft.json"):
        raw = await repo_files.read_repo_file(provider, thread_id, path)
        if raw is None:
            continue
        try:
            frameworks = json.loads(raw).get("frameworks") or []
        except json.JSONDecodeError:
            continue
        if frameworks:
            return frameworks_have_ui([str(f) for f in frameworks])
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


async def check_write_scope(provider: SandboxProvider, thread_id: str, baseline_commit: str | None) -> WriteScopeOutcome:
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
    if not violating:
        return WriteScopeOutcome(passed=True, violating_paths=[], changed_paths=changed_paths, reverted_paths=[])

    # Deterministic remediation instead of feedback the model cannot act on: the draft session
    # has create/edit tools but NO delete and NO bash, so "revert these files" deadlocked the
    # stage at the verify cap (observed live, headless run 5: two helper .sh scripts at the repo
    # root). The gate enforces its own contract -- untracked out-of-scope files are removed,
    # tracked ones restored to their committed state -- and the stage proceeds on what remains.
    for path in violating:
        validate_repo_relative_path(path)
    quoted = " ".join(shlex.quote(p) for p in violating)
    revert = await provider.exec_in_sandbox(
        thread_id,
        f"for p in {quoted}; do if git ls-files --error-unmatch -- \"$p\" >/dev/null 2>&1; "
        f"then git checkout -- \"$p\"; else rm -rf -- \"$p\"; fi; done",
    )
    if not revert.ok:
        return WriteScopeOutcome(passed=False, violating_paths=violating, changed_paths=changed_paths, reverted_paths=[])
    logger.info("write-scope gate reverted out-of-scope paths for thread_id=%s: %s", thread_id, violating)
    return WriteScopeOutcome(passed=True, violating_paths=[], changed_paths=changed_paths, reverted_paths=violating)


_WRITE_TOOL_NAMES = {"builtin:create", "builtin:edit", "builtin:apply_patch"}
_PATH_ARG_KEYS = ("path", "file_path", "filePath", "target_file", "filename")


def _extract_candidate_paths(tool_args: Any) -> list[str]:
    """Best-effort extraction of path-like strings from a tool call's args -- checks common key
    names first, falls back to any string value that looks path-shaped (contains a `/` or ends in
    a recognizable extension). NOT verified against the real Copilot CLI builtin tools' actual
    arg shapes (Phase A0's spike confirmed the tool *names* exist and are reachable, not their
    exact arg schema) -- Layer 2 (check_write_scope, a plain git diff) is what's actually
    authoritative; this is a fast, best-effort first line of defense only.
    """
    candidates: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, val in value.items():
                if key in _PATH_ARG_KEYS and isinstance(val, str):
                    candidates.append(val)
                else:
                    _walk(val)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, str) and ("/" in value or re.search(r"\.[A-Za-z0-9]{1,5}$", value)):
            candidates.append(value)

    _walk(tool_args)
    return candidates


async def pre_tool_use_write_scope_hook(hook_input: dict[str, Any], _extra: dict[str, str]) -> dict[str, Any] | None:
    """StageSpec.session_options' pre_tool_use_hook for P4 -- Layer 1 (fast-fail, best-effort; see
    module docstring). Denies a write-capable tool call whose path argument(s) don't look like a
    test file, with a reason explaining the constraint so the model can self-correct within the
    same turn. Returns None (no opinion) for every non-write tool and for a call with no
    extractable path (avoiding a false-positive block on an args shape this doesn't recognize)."""
    tool_name = hook_input.get("toolName")
    if tool_name not in _WRITE_TOOL_NAMES:
        return None

    paths = _extract_candidate_paths(hook_input.get("toolArgs"))
    if not paths:
        return None

    violating = [p for p in paths if not _is_test_path(p)]
    if not violating:
        return None

    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"This stage may only create/modify test files. {violating} does not look like a "
            "test file path. If this really is a test file, use a path/naming convention this "
            "repo's test projects already use."
        ),
    }


async def verify_ac_to_tests(
    thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None, provider: SandboxProvider
) -> "VerificationResult":
    """Combines the write-scope check above with the AC-coverage check (ac_coverage_gate.py) into
    one VerificationResult, since both answer the same question -- "is P4's output acceptable" --
    not two independently useful checks. Imported lazily to avoid a module-load-time cycle with
    graph.py (which imports this module)."""
    from ..graph import VerificationResult
    from .ac_coverage_gate import check_ac_coverage

    write_scope = await check_write_scope(provider, thread_id, baseline_commit)
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

    # A draft that changed nothing but pipeline artifacts DESCRIBED tests instead of creating
    # them (observed live, run 13: three laps of readiness=true structured replies, zero file
    # writes). Name that failure exactly -- "no test found covering US-xxxx" reads to the model
    # like a naming problem, not a you-never-wrote-files problem.
    real_changes = [p for p in write_scope.changed_paths if not p.startswith((".ai-dev-workflow/", "APPROVALS.md", "AGENTS.md"))]
    if not real_changes:
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
    if not _has_non_e2e_test(real_changes):
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
    if await _stack_has_ui(provider, thread_id) and not _has_e2e_test(real_changes):
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

    coverage = await check_ac_coverage(provider, thread_id, content_dict)
    report = {"changed_paths": write_scope.changed_paths, **coverage.report}
    if write_scope.reverted_paths:
        report["reverted_out_of_scope_paths"] = write_scope.reverted_paths
    return VerificationResult(passed=coverage.passed, feedback=coverage.feedback, report=report)


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
    # non-test and pipeline-owned files never count as tests
    assert not _has_non_e2e_test(["apps/web/src/app/page.tsx"])
    assert not _has_non_e2e_test([".ai-dev-workflow/ledger.jsonl"])
    print("write_scope_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
