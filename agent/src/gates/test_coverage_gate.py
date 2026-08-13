"""P6's coverage gate: enforces a 95% line+branch coverage threshold purely in Python (not the
underlying tool's own pass/fail), so both stacks produce identical semantics, and validates that
any coverage-exclusion config wasn't broadened to dodge the threshold (the anti-gaming check --
P6 has full write access, and coverage gates are notoriously gameable).

Pragmatic simplification, stated honestly (same spirit as ac_coverage_gate.py): parses the
tools' own summary output (Cobertura's root-level line-rate/branch-rate attributes for .NET, c8's
json-summary "total" object for JS/TS) rather than walking every uncovered line -- enough to
enforce the threshold and name the exclusion-gaming problem, not a full per-line coverage report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as ET

from .. import repo_files
from ..sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from ..graph import VerificationResult

MIN_COVERAGE_PERCENT_DEFAULT = 95.0

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


async def _run_dotnet_coverage(provider: SandboxProvider, thread_id: str) -> tuple[float | None, float | None, list[CoverageGap], str]:
    command = (
        "dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura "
        "/p:CoverletOutput=./TestResults/coverage.cobertura.xml 2>&1"
    )
    result = await provider.exec_in_sandbox(thread_id, command)
    raw_xml = await repo_files.read_repo_file(provider, thread_id, "TestResults/coverage.cobertura.xml")
    if raw_xml is None:
        return None, None, [], (result.stdout or result.stderr or "")[-2000:]

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None, None, [], "coverage.cobertura.xml failed to parse as XML"

    line_rate = float(root.get("line-rate", "0")) * 100
    branch_rate = float(root.get("branch-rate", "0")) * 100

    gaps: list[CoverageGap] = []
    for cls in root.iter("class"):
        cls_line_rate = float(cls.get("line-rate", "1")) * 100
        cls_branch_rate = float(cls.get("branch-rate", "1")) * 100
        if cls_line_rate < MIN_COVERAGE_PERCENT_DEFAULT or cls_branch_rate < MIN_COVERAGE_PERCENT_DEFAULT:
            gaps.append(CoverageGap(file=cls.get("filename", cls.get("name", "?")), line_rate=cls_line_rate, branch_rate=cls_branch_rate))

    return line_rate, branch_rate, gaps, ""


async def _run_js_coverage(provider: SandboxProvider, thread_id: str) -> tuple[float | None, float | None, list[CoverageGap], str]:
    command = "npx --yes c8 --reporter=json-summary --reporter=json --check-coverage=false -- npx vitest run 2>&1"
    result = await provider.exec_in_sandbox(thread_id, command)
    raw_summary = await repo_files.read_repo_file(provider, thread_id, "coverage/coverage-summary.json")
    if raw_summary is None:
        return None, None, [], (result.stdout or result.stderr or "")[-2000:]

    try:
        summary = json.loads(raw_summary)
    except json.JSONDecodeError:
        return None, None, [], "coverage-summary.json failed to parse as JSON"

    total = summary.get("total", {})
    line_rate = float(total.get("lines", {}).get("pct", 0))
    branch_rate = float(total.get("branches", {}).get("pct", 0))

    gaps: list[CoverageGap] = []
    for file_path, entry in summary.items():
        if file_path == "total" or not isinstance(entry, dict):
            continue
        file_line_rate = float(entry.get("lines", {}).get("pct", 100))
        file_branch_rate = float(entry.get("branches", {}).get("pct", 100))
        if file_line_rate < MIN_COVERAGE_PERCENT_DEFAULT or file_branch_rate < MIN_COVERAGE_PERCENT_DEFAULT:
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


async def verify_coverage(
    thread_id: str, content_dict: dict[str, Any], _run_id: str, _baseline_commit: str | None, provider: SandboxProvider
) -> "VerificationResult":
    from ..graph import VerificationResult

    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]

    if tech_stack.get("dotnet_detected"):
        line_rate, branch_rate, gaps, infra_error = await _run_dotnet_coverage(provider, thread_id)
    elif "typescript" in languages or "javascript" in languages:
        line_rate, branch_rate, gaps, infra_error = await _run_js_coverage(provider, thread_id)
    else:
        return VerificationResult(passed=False, feedback="No coverage tooling mapping for this stack.", report={})

    if line_rate is None:
        return VerificationResult(
            passed=False,
            feedback=f"Coverage run produced no parseable report -- treat as an infra failure, not a coverage gap: {infra_error}",
            report={"infra_error": infra_error},
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

    passed = line_rate >= MIN_COVERAGE_PERCENT_DEFAULT and branch_rate >= MIN_COVERAGE_PERCENT_DEFAULT
    report = {
        "line_rate": line_rate,
        "branch_rate": branch_rate,
        "threshold": MIN_COVERAGE_PERCENT_DEFAULT,
        "gaps": [{"file": g.file, "line_rate": g.line_rate, "branch_rate": g.branch_rate} for g in gaps],
    }
    if passed:
        return VerificationResult(passed=True, feedback=f"Coverage {line_rate:.1f}%/{branch_rate:.1f}% (line/branch) meets the {MIN_COVERAGE_PERCENT_DEFAULT}% threshold.", report=report)

    return VerificationResult(
        passed=False,
        feedback=(
            f"Coverage {line_rate:.1f}%/{branch_rate:.1f}% (line/branch) is below the "
            f"{MIN_COVERAGE_PERCENT_DEFAULT}% threshold. Uncovered: {report['gaps']}"
        ),
        report=report,
    )
