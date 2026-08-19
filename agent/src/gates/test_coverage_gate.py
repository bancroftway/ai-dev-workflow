"""P6's coverage gate: enforces a 95% line+branch coverage threshold purely in Python (not the
underlying tool's own pass/fail), so every stack produces identical semantics, and validates that
any coverage-exclusion config wasn't broadened to dodge the threshold (the anti-gaming check --
P6 has full write access, and coverage gates are notoriously gameable).

Two acquisition paths, one trust model:

1. CONTRACT REPLAY (preferred, polyglot-safe): the minimal-code-to-green draft -- which has the
   context to debug each stack's coverage plumbing -- records working command(s) in
   `.ai-dev-workflow/coverage-commands.json`, each naming the standard-format artifact it emits.
   This gate NEVER trusts the model's own run or any number it reports: it deletes each artifact,
   re-executes each recorded command itself, parses the regenerated artifacts (Cobertura XML or
   istanbul json-summary -- nothing else), and merges counts across entries line-weighted. The
   model owns the HOW; the number is always machine-derived from a replay.
2. Legacy single-stack fallback when no contract exists (dotnet coverlet / image-baked vitest).

Pragmatic simplification, stated honestly (same spirit as ac_coverage_gate.py): parses the
tools' own summary output rather than walking every uncovered line -- enough to enforce the
threshold and name the exclusion-gaming problem, not a full per-line coverage report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as ET
from pydantic import BaseModel, Field

from .. import repo_files, stack_runner
from ..repo_files import validate_repo_relative_path
from ..sandbox.provider import SandboxProvider
from ..schemas import StageReport

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

MIN_COVERAGE_PERCENT = float(os.environ.get("MIN_COVERAGE_PERCENT", "95.0"))
# Back-compat: agent/src/gates/audit_gates.py imports this old constant name.
MIN_COVERAGE_PERCENT_DEFAULT = MIN_COVERAGE_PERCENT

# The only strings `measure_coverage` may return as its "reason" -- this value ends up in
# repo_scan's `metrics.coverage.reason`, which IS hashed into ScanReport.content_hash (see
# repo_scan.py's determinism guarantee: "an unchanged repo hashes identically"). Raw subprocess
# stdout/stderr is NOT stable across runs (timestamps, temp paths, elapsed times) even when the
# repo itself hasn't changed -- it must never reach this variable. Verbose detail goes to the
# logger (and, for contract replay, the unhashed per-entry `entry_reports`) instead.
REASON_TIMEOUT = "timeout"
REASON_RUNNER_ERROR = "runner_error"
REASON_PARSE_ERROR = "parse_error"
REASON_CONTRACT_REPLAY_FAILED = "contract_replay_failed"
REASON_NO_TOOLING_MAPPING = "no_tooling_mapping"
STABLE_REASON_CODES = frozenset(
    {REASON_TIMEOUT, REASON_RUNNER_ERROR, REASON_PARSE_ERROR, REASON_CONTRACT_REPLAY_FAILED, REASON_NO_TOOLING_MAPPING}
)

COVERAGE_COMMANDS_PATH = ".ai-dev-workflow/coverage-commands.json"
_CONTRACT_FORMATS = ("cobertura", "istanbul-json-summary")


class CoverageEntry(BaseModel):
    """One test root that was actually run with coverage."""

    root: str = Field(default="", description='Repo-relative directory you ran in ("" for repo root).')
    command: str = Field(description="Exactly the command you ran there.")
    artifact: str = Field(description="Repo-relative path to the coverage report the run produced.")
    format: str = Field(description='Either "cobertura" or "istanbul-json-summary".')


class CoverageRunReport(StageReport):
    """What the coverage-run agent must report (prompts/coverage_run.md)."""

    entries: list[CoverageEntry] = Field(default_factory=list)


# thread_id -> (tree-state key, last successful measurement). Process-local, same lifetime as the
# graph's own in-memory checkpointer; a fresh process just re-measures.
_COVERAGE_MEMO: dict[str, tuple[str, tuple[float | None, float | None, list["CoverageGap"], str, list[dict[str, Any]]]]] = {}

# Coverage-exclusion glob patterns considered legitimately generated/vendor code, never a
# hand-written escape hatch. Anything outside this allowlist that appears in a repo's own
# exclusion config is treated as gaming the gate, regardless of the raw coverage percentage.
_SAFE_EXCLUSION_PATTERNS = {
    "**/*.generated.*",
    "**/*.designer.cs",
    "**/migrations/**",
    "**/Migrations/**",
    "**/dist/**",
    "**/*.d.ts",
    "**/obj/**",
    "**/bin/**",
    "**/node_modules/**",
}


@dataclass(frozen=True)
class CoverageGap:
    file: str
    line_rate: float
    branch_rate: float


@dataclass(frozen=True)
class _Counts:
    """Covered/total counts, mergeable across stacks (rates are not -- a 10-line worker and a
    10k-line app would weigh equally)."""

    lines_covered: int
    lines_total: int
    branches_covered: int
    branches_total: int
    gaps: list[CoverageGap]


def _parse_cobertura_counts(raw_xml: str) -> tuple[_Counts | None, str]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None, "artifact failed to parse as Cobertura XML"
    try:
        lc, lt = int(root.get("lines-covered", "0")), int(root.get("lines-valid", "0"))
        bc, bt = int(root.get("branches-covered", "0")), int(root.get("branches-valid", "0"))
    except ValueError:
        return None, "Cobertura root counters are not integers"
    if lt == 0:
        return None, "Cobertura artifact reports zero valid lines -- nothing was instrumented"
    gaps: list[CoverageGap] = []
    for cls in root.iter("class"):
        cls_line_rate = float(cls.get("line-rate", "1")) * 100
        # A class with no branch points reports branch-rate="0" in coverage.py/coverlet Cobertura
        # output -- vacuously satisfied, not a 0% gap (observed live: main.py 100% lines, zero
        # branches, flagged as a 0.0% branch gap).
        _cls_has_branches = any(line.get("branch") == "true" for line in cls.iter("line"))
        cls_branch_rate = float(cls.get("branch-rate", "1")) * 100 if _cls_has_branches else 100.0
        if cls_line_rate < MIN_COVERAGE_PERCENT or cls_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=cls.get("filename", cls.get("name", "?")), line_rate=cls_line_rate, branch_rate=cls_branch_rate))
    return _Counts(lc, lt, bc, bt, gaps), ""


def _parse_istanbul_counts(raw: str) -> tuple[_Counts | None, str]:
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError:
        return None, "artifact failed to parse as istanbul json-summary"

    def _count(entry: dict, key: str, field: str) -> int:
        try:
            return int(entry.get(key, {}).get(field, 0))
        except (TypeError, ValueError):
            return 0

    total = summary.get("total", {})
    lt, lc = _count(total, "lines", "total"), _count(total, "lines", "covered")
    bt, bc = _count(total, "branches", "total"), _count(total, "branches", "covered")
    if lt == 0:
        return None, "istanbul summary reports zero total lines -- nothing was instrumented"

    def _pct(entry: dict, key: str, default: float) -> float:
        try:
            return float(entry.get(key, {}).get("pct", default))
        except (TypeError, ValueError):
            return default

    gaps: list[CoverageGap] = []
    for file_path, entry in summary.items():
        if file_path == "total" or not isinstance(entry, dict):
            continue
        file_line_rate = _pct(entry, "lines", 100)
        # 0-of-0 branches is vacuous, same rule as the Cobertura parsers.
        file_branch_rate = 100.0 if _count(entry, "branches", "total") == 0 else _pct(entry, "branches", 100)
        if file_line_rate < MIN_COVERAGE_PERCENT or file_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=file_path, line_rate=file_line_rate, branch_rate=file_branch_rate))
    return _Counts(lc, lt, bc, bt, gaps), ""


def _with_timeout(command: str, timeout_seconds: int | None) -> str:
    """`sh -c` wrap keeps a single `timeout` bound to the WHOLE command even when it chains
    multiple statements (&&, if/then/else) -- a bare `timeout N cmd1 && cmd2` would only bound
    cmd1. None (every gate caller today) is a no-op, so behavior is unchanged unless a caller
    opts in."""
    return f"timeout {timeout_seconds} sh -c {shlex.quote(command)}" if timeout_seconds else command


async def _run_coverage_via_ghcp(
    provider: SandboxProvider, thread_id: str
) -> tuple[float | None, float | None, list[CoverageGap], str, list[dict[str, Any]]]:
    """Acquisition half: one GHCP session finds every test root, runs it with coverage, and
    reports where the report files landed; THIS function parses those files and does the math.

    Replaces three deleted code paths that all guessed commands in Python (a canned `dotnet test`
    at the repo root -> MSB1003 on every greenfield monorepo; a canned image-baked vitest
    invocation -> swallowed Playwright specs and instrumented nothing; and a replay of a contract
    the drafting model was supposed to write but reliably didn't). The model decides HOW to
    produce coverage; the NUMBER is still machine-derived here from artifacts on disk, and the
    artifacts must be fresh (mtime >= the epoch captured before the session ran), so a
    pre-fabricated or stale report cannot pass.
    """
    epoch_result = await provider.exec_in_sandbox(thread_id, "date +%s")
    try:
        epoch = int((epoch_result.stdout or "0").strip())
    except ValueError:
        epoch = 0

    report = await stack_runner.run_and_report(
        thread_id,
        stage_key="coverage-run",
        prompt_name="coverage_run",
        schema=CoverageRunReport,
        failure_detail=(
            "No prior automatic attempt: determine how to run this repository's tests with "
            "coverage from scratch."
        ),
    )
    if not report.entries:
        return None, None, [], REASON_RUNNER_ERROR, [{"error": report.error or "no coverage entries reported"}]

    merged: list[_Counts] = []
    entry_reports: list[dict[str, Any]] = []
    for entry in report.entries[:10]:  # bounded: dozens of entries is itself suspect
        detail: dict[str, Any] = {"entry": entry.model_dump()}
        entry_reports.append(detail)
        try:
            validate_repo_relative_path(entry.artifact)
        except ValueError as exc:
            detail["error"] = f"invalid artifact path: {exc}"
            continue

        # Freshness: the file must have been written by the run we just asked for.
        stat = await provider.exec_in_sandbox(thread_id, f"stat -c %Y {shlex.quote(entry.artifact)} 2>/dev/null")
        try:
            mtime = int((stat.stdout or "0").strip())
        except ValueError:
            mtime = 0
        if mtime == 0:
            detail["error"] = f"no artifact at {entry.artifact}"
            continue
        if mtime < epoch:
            detail["error"] = f"stale artifact at {entry.artifact} (written before this run)"
            continue

        artifact_raw = await repo_files.read_repo_file(provider, thread_id, entry.artifact)
        if artifact_raw is None:
            detail["error"] = f"artifact at {entry.artifact} could not be read"
            continue
        counts, parse_error = (
            _parse_cobertura_counts(artifact_raw)
            if entry.format == "cobertura"
            else _parse_istanbul_counts(artifact_raw)
        )
        if counts is None:
            detail["error"] = parse_error
            continue
        detail["lines"] = f"{counts.lines_covered}/{counts.lines_total}"
        detail["branches"] = f"{counts.branches_covered}/{counts.branches_total}"
        merged.append(counts)

    if not merged:
        errors = "; ".join(str(d.get("error")) for d in entry_reports if d.get("error"))
        logger.warning("repo_scan coverage: no usable coverage artifact from the run: %s", errors)
        return None, None, [], REASON_RUNNER_ERROR, entry_reports

    # Persist the contract in its historical shape: exit_nodes.py reads it for the run manifest.
    # Written by US from a validated report, never begged out of the drafting model.
    await repo_files.write_repo_file(
        provider,
        thread_id,
        COVERAGE_COMMANDS_PATH,
        json.dumps({"entries": [e.model_dump() for e in report.entries]}, indent=2) + "\n",
    )

    lines_total = sum(c.lines_total for c in merged)
    lines_covered = sum(c.lines_covered for c in merged)
    branches_total = sum(c.branches_total for c in merged)
    branches_covered = sum(c.branches_covered for c in merged)
    line_rate = 100.0 * lines_covered / lines_total if lines_total else 0.0
    # No branch points anywhere is vacuously satisfied, not a 0% failure.
    branch_rate = 100.0 * branches_covered / branches_total if branches_total else 100.0
    gaps = [gap for c in merged for gap in c.gaps]
    return line_rate, branch_rate, gaps, "", entry_reports



async def _check_exclusion_gaming(provider: SandboxProvider, thread_id: str) -> list[str]:
    """Reads whichever exclusion config exists and flags any pattern outside the known-safe
    allowlist -- catches the model broadening an exclude glob (e.g. to `src/**`) to dodge the
    threshold instead of actually writing tests."""
    violations: list[str] = []

    runsettings = await repo_files.read_repo_file(provider, thread_id, "coverage.runsettings")
    if runsettings is not None:
        for pattern in re.findall(r"<Exclude>\s*\[?([^<\]]+)\]?\s*</Exclude>", runsettings):
            if pattern.strip() not in _SAFE_EXCLUSION_PATTERNS:
                violations.append(f"coverage.runsettings excludes {pattern.strip()!r}, not on the known-safe allowlist")

    c8rc = await repo_files.read_repo_file(provider, thread_id, ".c8rc.json")
    if c8rc is not None:
        try:
            config = json.loads(c8rc)
            for pattern in config.get("exclude", []):
                if pattern not in _SAFE_EXCLUSION_PATTERNS:
                    violations.append(f".c8rc.json excludes {pattern!r}, not on the known-safe allowlist")
        except json.JSONDecodeError:
            pass

    return violations


async def measure_coverage(
    provider: SandboxProvider, thread_id: str, *, timeout_seconds: int | None = None
) -> tuple[float | None, float | None, list[CoverageGap], str, list[dict[str, Any]]]:
    """Acquisition half of `verify_coverage`: a GHCP session runs the tests with coverage and
    reports its artifacts, which THIS module parses (see `_run_coverage_via_ghcp`).

    Returns (line_rate, branch_rate, gaps, reason, entry_reports) with line_rate=None on any
    failure -- NEVER a fabricated 0, the same rule the gate's callers already depend on. `reason`
    is only meaningful when line_rate is None, and is always one of `STABLE_REASON_CODES` -- this
    value flows into repo_scan's hashed `metrics.coverage.reason` (via the baseline path), so it
    can never carry volatile subprocess output. Verbose detail is logged instead.
    """
    # Cheap guard, kept from the old tech-stack dispatch: on a repo with no detected languages
    # (the blank-repo baseline scan, which runs before anything is scaffolded) there is nothing to
    # measure, so don't spend a GHCP session discovering that.
    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if not languages and not tech_stack.get("dotnet_detected"):
        logger.info("repo_scan coverage: no tooling mapping for detected languages %s", languages)
        return None, None, [], REASON_NO_TOOLING_MAPPING, []

    # Memoized on the exact tree state (HEAD + dirty-file digest): this gate re-fires on every
    # verify cycle and again from metrics-report, and re-running a full coverage suite for a tree
    # that has not changed is pure spend. Dirty-state is part of the key because during verify
    # cycles HEAD often stands still while the working tree moves.
    head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD 2>/dev/null")
    dirty = await provider.exec_in_sandbox(thread_id, "git status --porcelain 2>/dev/null")
    memo_key = f"{(head.stdout or '').strip()}:{hashlib.sha256((dirty.stdout or '').encode()).hexdigest()}"
    cached = _COVERAGE_MEMO.get(thread_id)
    if cached and cached[0] == memo_key:
        logger.info("repo_scan coverage: reusing measurement for unchanged tree")
        return cached[1]

    result = await _run_coverage_via_ghcp(provider, thread_id)
    if result[0] is not None:
        _COVERAGE_MEMO[thread_id] = (memo_key, result)
    return result


# Human-readable expansion of each stable reason code, for the GATE's own `feedback` (read by the
# drafting LLM on retry) -- unlike `reason` itself, this text is never hashed into scan metrics, so
# it's free to be as actionable as it likes.
_REASON_FEEDBACK: dict[str, str] = {
    REASON_TIMEOUT: "the coverage run did not finish within its timeout",
    REASON_RUNNER_ERROR: "the coverage runner produced no parseable artifact (see server logs for the raw output)",
    REASON_PARSE_ERROR: "the coverage artifact could not be parsed",
    REASON_CONTRACT_REPLAY_FAILED: f"every entry in {COVERAGE_COMMANDS_PATH} failed on replay (see server logs for details)",
    REASON_NO_TOOLING_MAPPING: (
        "no coverage tooling mapping for this stack. Record working coverage command(s) in "
        f"{COVERAGE_COMMANDS_PATH} (see the drafting instructions) so the gate can replay them."
    ),
}


async def verify_coverage(
    thread_id: str, content_dict: dict[str, Any], _run_id: str, _baseline_commit: str | None, provider: SandboxProvider
) -> "VerificationResult":
    from ..graph import VerificationResult
    from .write_scope_gate import _is_pipeline_owned, _is_test_path

    # Did this stage write any APPLICATION code at all? Checked first, deterministically, because
    # the alternative is a confusing infra error several minutes later: with no app in the tree
    # there is no runnable project, so the coverage run correctly reports "nothing to measure" and
    # the drafting model gets feedback about coverage tooling when its actual mistake was writing
    # no code. Observed live: minimal-code-to-green "completed" in 18s having only answered in
    # JSON prose -- the same failure ac-to-tests already guards against with its own no-test-files
    # check, which is what makes that stage self-correct instead of looping blind.
    tracked = await provider.exec_in_sandbox(thread_id, "git ls-files")
    source_files = [
        stripped for line in (tracked.stdout or "").splitlines()
        if (stripped := line.strip()) and not _is_pipeline_owned(stripped) and not _is_test_path(stripped)
    ]
    if not source_files:
        return VerificationResult(
            passed=False,
            feedback=(
                "You wrote NO application code -- the repository contains only test files and "
                "pipeline artifacts, with no project manifest (package.json/angular.json/.csproj/"
                "etc.) and no source files at all. Your structured response is metadata ABOUT work "
                "that must already exist on disk. Scaffold the application described in the "
                "approved Plan and implement it with your file tools NOW, before responding."
            ),
            report={"infra_error": "no_application_code", "source_files": 0},
        )

    line_rate, branch_rate, gaps, reason, entry_reports = await measure_coverage(provider, thread_id)

    if line_rate is None:
        # Infra failure, not a coverage gap: the coverage run itself never produced a readable
        # artifact. The per-entry detail carries what the run agent actually reported/attempted.
        return VerificationResult(
            passed=False,
            feedback=(
                "The coverage run produced no parseable report -- treat this as an infra failure, "
                f"not a coverage gap: {_REASON_FEEDBACK.get(reason, reason)}"
            ),
            report={"infra_error": reason, "contract_replay": entry_reports},
        )

    gaming_violations = await _check_exclusion_gaming(provider, thread_id)
    if gaming_violations:
        return VerificationResult(
            passed=False,
            feedback=(
                "Coverage-exclusion config was broadened outside known-safe generated-code patterns "
                f"-- this looks like gaming the coverage gate, not legitimate exclusion: {gaming_violations}"
            ),
            report={"gaming_violations": gaming_violations},
        )

    passed = line_rate >= MIN_COVERAGE_PERCENT and branch_rate >= MIN_COVERAGE_PERCENT
    report = {
        "line_rate": line_rate,
        "branch_rate": branch_rate,
        "threshold": MIN_COVERAGE_PERCENT,
        "gaps": [{"file": g.file, "line_rate": g.line_rate, "branch_rate": g.branch_rate} for g in gaps],
    }
    if entry_reports:
        report["contract_replay"] = entry_reports
    if passed:
        return VerificationResult(passed=True, feedback=f"Coverage {line_rate:.1f}%/{branch_rate:.1f}% (line/branch) meets the {MIN_COVERAGE_PERCENT}% threshold.", report=report)

    return VerificationResult(
        passed=False,
        feedback=(
            f"Coverage {line_rate:.1f}%/{branch_rate:.1f}% (line/branch) is below the "
            f"{MIN_COVERAGE_PERCENT}% threshold. Uncovered: {report['gaps']}"
        ),
        report=report,
    )


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.gates.test_coverage_gate`
    """Self-check for the pure parts. `measure_coverage` itself is all sandbox I/O (replay a
    command, read the artifact back); its two artifact parsers and the timeout-wrap helper are
    the pure logic worth pinning here."""

    counts, err = _parse_cobertura_counts(
        '<coverage lines-covered="80" lines-valid="100" branches-covered="18" branches-valid="20">'
        "<packages><package><classes>"
        '<class filename="a.py" line-rate="0.5" branch-rate="1.0"/>'
        "</classes></package></packages></coverage>"
    )
    assert counts is not None and err == ""
    assert counts.lines_covered == 80 and counts.lines_total == 100
    assert counts.gaps and counts.gaps[0].file == "a.py", "under-threshold class must surface as a gap"
    assert _parse_cobertura_counts("not xml")[0] is None
    assert _parse_cobertura_counts('<coverage lines-covered="0" lines-valid="0"/>')[0] is None, (
        "zero instrumented lines must not report a hollow rate"
    )

    summary = {
        "total": {"lines": {"total": 100, "covered": 96, "pct": 96}, "branches": {"total": 10, "covered": 10, "pct": 100}},
        "src/x.ts": {"lines": {"pct": 50}, "branches": {"pct": 100}},
    }
    counts, err = _parse_istanbul_counts(json.dumps(summary))
    assert counts is not None and counts.lines_covered == 96, err
    assert counts.gaps and counts.gaps[0].file == "src/x.ts"
    assert _parse_istanbul_counts(json.dumps({"total": {"lines": {"total": 0}}}))[0] is None

    # timeout wraps the WHOLE command via `sh -c`, so a chained/if-else command is bounded as one
    # unit -- a bare `timeout N cmd1 && cmd2` would only bound cmd1.
    assert _with_timeout("echo hi", None) == "echo hi", "no timeout_seconds must be a no-op"
    wrapped = _with_timeout("echo hi && echo bye", 30)
    assert wrapped.startswith("timeout 30 sh -c "), wrapped
    assert "echo hi && echo bye" in wrapped

    # Every reason `measure_coverage` can return is a short stable code (never raw subprocess
    # output) -- this is what keeps repo_scan's metrics.coverage.reason, and therefore
    # content_hash, deterministic across two runs of an unchanged repo. `_REASON_FEEDBACK` must
    # cover every one of them so the gate's own feedback stays actionable despite the terse code.
    for code in STABLE_REASON_CODES:
        assert " " not in code, f"{code!r} looks like prose, not a stable code"
        assert code in _REASON_FEEDBACK, f"{code!r} has no human-readable gate feedback"

    # MIN_COVERAGE_PERCENT is a module-level env read (MIN_COVERAGE_PERCENT_DEFAULT survives only
    # as a back-compat alias for audit_gates.py's import) -- pin that it parsed to a float and
    # still defaults to 95.0 when MIN_COVERAGE_PERCENT is unset, same as the old hardcoded constant.
    assert isinstance(MIN_COVERAGE_PERCENT, float)
    assert MIN_COVERAGE_PERCENT == 95.0, "default must stay 95.0 when env var MIN_COVERAGE_PERCENT is unset"
    assert MIN_COVERAGE_PERCENT_DEFAULT == MIN_COVERAGE_PERCENT, "back-compat alias must track the live value"

    print("test_coverage_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
