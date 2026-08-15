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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import repo_files
from ..sandbox.provider import SandboxProvider

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
_PIPELINE_OWNED_PREFIXES = (".ai-dev-workflow/", "spec/", "APPROVALS.md", "AGENTS.md")


def _is_pipeline_owned(path: str) -> bool:
    return path.startswith(_PIPELINE_OWNED_PREFIXES)


@dataclass(frozen=True)
class WriteScopeOutcome:
    passed: bool
    violating_paths: list[str]
    changed_paths: list[str]


async def check_write_scope(provider: SandboxProvider, thread_id: str, baseline_commit: str | None) -> WriteScopeOutcome:
    if baseline_commit is None:
        # No baseline captured (e.g. this draft never actually ran against a sandbox) -- nothing
        # to diff against, so nothing to flag. StageSpec.capture_baseline_commit guarantees this
        # is set on every real run; a None here means the stage's own precondition wasn't met,
        # which is a bug in wiring, not a content problem -- fail open rather than false-positive.
        return WriteScopeOutcome(passed=True, violating_paths=[], changed_paths=[])

    result = await provider.exec_in_sandbox(thread_id, f"git diff --name-only {baseline_commit} -- .")
    changed_paths = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    violating = [p for p in changed_paths if not _is_test_path(p) and not _is_pipeline_owned(p)]
    return WriteScopeOutcome(passed=len(violating) == 0, violating_paths=violating, changed_paths=changed_paths)


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
        return VerificationResult(
            passed=False,
            feedback=(
                "These files are outside the test-only write scope for this stage and must be "
                f"reverted: {write_scope.violating_paths}. Only test files may be created or "
                "modified here."
            ),
            report={"violating_paths": write_scope.violating_paths, "changed_paths": write_scope.changed_paths},
        )

    coverage = await check_ac_coverage(provider, thread_id, content_dict)
    return VerificationResult(
        passed=coverage.passed,
        feedback=coverage.feedback,
        report={"changed_paths": write_scope.changed_paths, **coverage.report},
    )
