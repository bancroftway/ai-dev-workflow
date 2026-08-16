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

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as ET

from .. import repo_files
from ..repo_files import validate_repo_relative_path
from ..sandbox.provider import SandboxProvider

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
        cls_branch_rate = float(cls.get("branch-rate", "1")) * 100
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
        file_branch_rate = _pct(entry, "branches", 100)
        if file_line_rate < MIN_COVERAGE_PERCENT or file_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=file_path, line_rate=file_line_rate, branch_rate=file_branch_rate))
    return _Counts(lc, lt, bc, bt, gaps), ""


def _with_timeout(command: str, timeout_seconds: int | None) -> str:
    """`sh -c` wrap keeps a single `timeout` bound to the WHOLE command even when it chains
    multiple statements (&&, if/then/else) -- a bare `timeout N cmd1 && cmd2` would only bound
    cmd1. None (every gate caller today) is a no-op, so behavior is unchanged unless a caller
    opts in."""
    return f"timeout {timeout_seconds} sh -c {shlex.quote(command)}" if timeout_seconds else command


async def _run_contract_coverage(
    provider: SandboxProvider, thread_id: str, *, timeout_seconds: int | None = None
) -> tuple[float | None, float | None, list[CoverageGap], str, list[dict[str, Any]]] | None:
    """Replays `.ai-dev-workflow/coverage-commands.json`. Returns None when no contract exists
    (caller falls back to the legacy single-stack path); otherwise (line, branch, gaps,
    infra_error, per_entry_report) with line=None when every entry failed.

    Trust model: the artifact is DELETED before the replay, so only files regenerated by THIS
    gate's own execution of the recorded command are ever parsed -- a pre-fabricated artifact
    cannot survive, and no model-reported number is read from anywhere.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, COVERAGE_COMMANDS_PATH)
    if raw is None:
        return None
    try:
        entries = json.loads(raw).get("entries", [])
    except json.JSONDecodeError:
        logger.warning("repo_scan coverage: %s is not valid JSON", COVERAGE_COMMANDS_PATH)
        return None, None, [], REASON_CONTRACT_REPLAY_FAILED, []
    if not isinstance(entries, list) or not entries:
        logger.warning("repo_scan coverage: %s has no entries", COVERAGE_COMMANDS_PATH)
        return None, None, [], REASON_CONTRACT_REPLAY_FAILED, []

    merged: list[_Counts] = []
    entry_reports: list[dict[str, Any]] = []
    for entry in entries[:10]:  # bounded: a contract with dozens of entries is itself suspect
        report: dict[str, Any] = {"entry": entry}
        entry_reports.append(report)
        command = entry.get("command") if isinstance(entry, dict) else None
        artifact = entry.get("artifact") if isinstance(entry, dict) else None
        fmt = entry.get("format") if isinstance(entry, dict) else None
        root = (entry.get("root") or "") if isinstance(entry, dict) else ""
        if not command or not artifact or fmt not in _CONTRACT_FORMATS:
            report["error"] = f"entry needs command, artifact, and format in {_CONTRACT_FORMATS}"
            continue
        try:
            validate_repo_relative_path(artifact)
            if root:
                validate_repo_relative_path(root)
        except ValueError as exc:
            report["error"] = f"invalid path: {exc}"
            continue

        prefix = f"cd {shlex.quote(root)} && " if root else ""
        replay = await provider.exec_in_sandbox(
            thread_id, f"rm -f {shlex.quote(artifact)} && {_with_timeout(f'{prefix}{command}', timeout_seconds)} 2>&1"
        )
        report["replay_exit_ok"] = replay.ok
        report["replay_returncode"] = replay.returncode
        artifact_raw = await repo_files.read_repo_file(provider, thread_id, artifact)
        if artifact_raw is None:
            report["error"] = f"replay produced no artifact at {artifact}: {(replay.stdout or replay.stderr or '')[-800:]}"
            continue
        counts, parse_error = (
            _parse_cobertura_counts(artifact_raw) if fmt == "cobertura" else _parse_istanbul_counts(artifact_raw)
        )
        if counts is None:
            report["error"] = parse_error
            continue
        report["lines"] = f"{counts.lines_covered}/{counts.lines_total}"
        report["branches"] = f"{counts.branches_covered}/{counts.branches_total}"
        merged.append(counts)

    if not merged:
        errors = "; ".join(str(r.get("error")) for r in entry_reports if r.get("error"))
        logger.warning("repo_scan coverage: every coverage-commands entry failed on replay: %s", errors)
        timed_out = any(r.get("replay_returncode") == 124 for r in entry_reports)
        return None, None, [], (REASON_TIMEOUT if timed_out else REASON_CONTRACT_REPLAY_FAILED), entry_reports

    lines_total = sum(c.lines_total for c in merged)
    lines_covered = sum(c.lines_covered for c in merged)
    branches_total = sum(c.branches_total for c in merged)
    branches_covered = sum(c.branches_covered for c in merged)
    line_rate = 100.0 * lines_covered / lines_total if lines_total else 0.0
    # No branch points anywhere is vacuously satisfied, not a 0% failure.
    branch_rate = 100.0 * branches_covered / branches_total if branches_total else 100.0
    gaps = [gap for c in merged for gap in c.gaps]
    return line_rate, branch_rate, gaps, "", entry_reports


async def _run_dotnet_coverage(
    provider: SandboxProvider, thread_id: str, *, timeout_seconds: int | None = None
) -> tuple[float | None, float | None, list[CoverageGap], str]:
    command = _with_timeout(
        "dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura "
        "/p:CoverletOutput=./TestResults/coverage.cobertura.xml",
        timeout_seconds,
    ) + " 2>&1"
    result = await provider.exec_in_sandbox(thread_id, command)
    raw_xml = await repo_files.read_repo_file(provider, thread_id, "TestResults/coverage.cobertura.xml")
    if raw_xml is None:
        logger.warning(
            "repo_scan coverage: dotnet coverage run produced no artifact: %s",
            (result.stdout or result.stderr or "")[-2000:],
        )
        return None, None, [], REASON_TIMEOUT if result.returncode == 124 else REASON_RUNNER_ERROR

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None, None, [], REASON_PARSE_ERROR

    line_rate = float(root.get("line-rate", "0")) * 100
    branch_rate = float(root.get("branch-rate", "0")) * 100
    if float(root.get("branches-valid", "0") or 0) == 0:
        # No branch points anywhere is vacuously satisfied, not a 0% failure -- same rule as the
        # contract-replay path's merged counts above.
        branch_rate = 100.0

    gaps: list[CoverageGap] = []
    for cls in root.iter("class"):
        cls_line_rate = float(cls.get("line-rate", "1")) * 100
        cls_branch_rate = float(cls.get("branch-rate", "1")) * 100
        if cls_line_rate < MIN_COVERAGE_PERCENT or cls_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=cls.get("filename", cls.get("name", "?")), line_rate=cls_line_rate, branch_rate=cls_branch_rate))

    return line_rate, branch_rate, gaps, ""


async def _run_js_coverage(
    provider: SandboxProvider, thread_id: str, *, timeout_seconds: int | None = None
) -> tuple[float | None, float | None, list[CoverageGap], str]:
    # The IMAGE-BAKED vitest (+@vitest/coverage-v8 sibling in the same node_modules) measures
    # coverage without installing anything into the target repo. Both wrapper alternatives were
    # tried live and measure a hard 0.0%: `c8 -- vitest run` (pool workers invisible to the
    # wrapper) and NODE_V8_COVERAGE + `c8 report` (vitest workers never write the dumps).
    command = _with_timeout(
        "rm -rf coverage && "
        "if [ -x /opt/aidw/test/node_modules/.bin/vitest ]; then "
        "/opt/aidw/test/node_modules/.bin/vitest run --coverage --coverage.provider=v8 "
        "--coverage.reporter=json-summary --coverage.reporter=json "
        "--coverage.reportsDirectory=coverage 2>&1; "
        "else npx --yes vitest run --coverage --coverage.reporter=json-summary 2>&1; fi",
        timeout_seconds,
    )
    result = await provider.exec_in_sandbox(thread_id, command)
    raw_summary = await repo_files.read_repo_file(provider, thread_id, "coverage/coverage-summary.json")
    if raw_summary is None:
        logger.warning(
            "repo_scan coverage: js coverage run produced no artifact: %s",
            (result.stdout or result.stderr or "")[-2000:],
        )
        return None, None, [], REASON_TIMEOUT if result.returncode == 124 else REASON_RUNNER_ERROR

    try:
        summary = json.loads(raw_summary)
    except json.JSONDecodeError:
        return None, None, [], REASON_PARSE_ERROR

    # istanbul writes the literal string "Unknown" for pct when a metric has zero instrumented
    # entities (0 of 0 -- observed live, it crashed float()). Vacuous coverage is treated
    # per-metric: total LINES unknown means nothing was instrumented at all (real fail, 0);
    # total BRANCHES unknown just means the code has no branch points (vacuously satisfied,
    # 100 -- else a branchless repo fails the gate forever).
    def _pct(entry: dict, key: str, default: float) -> float:
        try:
            return float(entry.get(key, {}).get("pct", default))
        except (TypeError, ValueError):
            return default

    total = summary.get("total", {})
    line_rate = _pct(total, "lines", 0)
    branch_rate = _pct(total, "branches", 100)

    gaps: list[CoverageGap] = []
    for file_path, entry in summary.items():
        if file_path == "total" or not isinstance(entry, dict):
            continue
        # Per-file default 100: an unmeasured metric on one file must not fabricate a gap.
        file_line_rate = _pct(entry, "lines", 100)
        file_branch_rate = _pct(entry, "branches", 100)
        if file_line_rate < MIN_COVERAGE_PERCENT or file_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=file_path, line_rate=file_line_rate, branch_rate=file_branch_rate))

    return line_rate, branch_rate, gaps, ""


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
    """Acquisition half of `verify_coverage`: contract replay (coverage-commands.json) first, then
    the tech-stack fallback (.NET coverlet / JS vitest) -- no third "direct detection" leg, since
    by the time anything other than the gate itself calls this (the repo-scan baseline node),
    `.ai-dev-workflow/tech-stack.approved.json` already exists (tech-stack runs first).

    Every path returns (line_rate, branch_rate, gaps, reason, entry_reports) with line_rate=None
    on any failure -- NEVER a fabricated 0, the same rule the gate's callers already depend on.
    `reason` is only meaningful when line_rate is None, and is always one of
    `STABLE_REASON_CODES` -- this value flows into repo_scan's hashed `metrics.coverage.reason`
    (via the baseline path), so it can never carry volatile subprocess output. Verbose detail is
    logged at the point of failure instead.
    """
    contract = await _run_contract_coverage(provider, thread_id, timeout_seconds=timeout_seconds)
    if contract is not None:
        return contract

    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]

    if tech_stack.get("dotnet_detected"):
        line_rate, branch_rate, gaps, reason = await _run_dotnet_coverage(provider, thread_id, timeout_seconds=timeout_seconds)
    elif "typescript" in languages or "javascript" in languages:
        line_rate, branch_rate, gaps, reason = await _run_js_coverage(provider, thread_id, timeout_seconds=timeout_seconds)
    else:
        logger.info("repo_scan coverage: no tooling mapping for detected languages %s", languages)
        return None, None, [], REASON_NO_TOOLING_MAPPING, []

    return line_rate, branch_rate, gaps, reason, []


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

    line_rate, branch_rate, gaps, reason, entry_reports = await measure_coverage(provider, thread_id)

    if line_rate is None:
        return VerificationResult(
            passed=False,
            feedback=(
                "Coverage run produced no parseable report -- treat as an infra failure, not a "
                f"coverage gap: {_REASON_FEEDBACK.get(reason, reason)}"
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
