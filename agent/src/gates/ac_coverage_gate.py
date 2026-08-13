"""P4's AC-coverage half of the deterministic_verify gate: every active Acceptance Criterion in
spec/ledger.json must have at least one test whose name embeds its AC id, and that test must
currently be FAILING (a passing "new" test before any implementation exists is almost certainly
tautological -- this is TDD's RED step, checked mechanically rather than trusted to the model's
own self-report).

Regex-based test-name/result extraction, not a framework-specific structured-report parser
(trx/junit-xml/playwright-json each have real schemas this could parse properly) -- a pragmatic
simplification given time constraints, noted here rather than silently passed off as more rigorous
than it is. Good enough to catch the two failure modes that matter (zero coverage, tautological
pass) as long as the stack's test runner prints AC ids and pass/fail status in its console output,
which every stack's default reporter does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .. import repo_files
from ..sandbox.provider import SandboxProvider
from ..spec_ledger import LEDGER_PATH

_AC_ID_RE = re.compile(r"AC-\d{4}\.\d+")

# One line of a test runner's console output naming a test and its outcome -- covers dotnet test's
# default console logger, vitest's default reporter, and jest/playwright's default reporters
# closely enough for this purpose (all print a pass/fail glyph or word beside the test name).
_FAIL_MARKERS = ("fail", "✗", "×", "FAILED")
_PASS_MARKERS = ("pass", "✓", "√", "PASSED", "ok ")


def _resolve_test_command(tech_stack: dict[str, Any]) -> str | None:
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if tech_stack.get("dotnet_detected"):
        return "dotnet test --logger 'console;verbosity=normal'"
    if "typescript" in languages or "javascript" in languages:
        return "npx --yes vitest run --reporter=verbose || npx --yes jest --verbose"
    if "python" in languages:
        return "python -m pytest -v"
    return None


@dataclass(frozen=True)
class AcCoverageOutcome:
    passed: bool
    feedback: str
    report: dict[str, Any]


async def check_ac_coverage(provider: SandboxProvider, thread_id: str, content_dict: dict[str, Any]) -> AcCoverageOutcome:
    raw_ledger = await repo_files.read_repo_file(provider, thread_id, LEDGER_PATH)
    active_ac_ids: list[str] = []
    if raw_ledger is not None:
        try:
            entries = json.loads(raw_ledger).get("entries", [])
            active_ac_ids = [
                e["id"] for e in entries if e.get("kind") == "acceptance_criterion" and e.get("status") in ("active", "revised")
            ]
        except json.JSONDecodeError:
            pass

    if not active_ac_ids:
        return AcCoverageOutcome(
            passed=False,
            feedback="spec/ledger.json has no active Acceptance Criteria -- P2 must be approved with real ACs before P4 can run.",
            report={},
        )

    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}
    command = _resolve_test_command(tech_stack)
    if command is None:
        return AcCoverageOutcome(
            passed=False, feedback="No test-runner command mapping for this stack -- cannot verify AC coverage.", report={}
        )

    result = await provider.exec_in_sandbox(thread_id, command)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    lines = output.splitlines()

    # Per AC id: does any line naming it look like a FAIL, a PASS, or neither (ambiguous/no match).
    ac_line_status: dict[str, str] = {}
    for line in lines:
        ac_ids_in_line = set(_AC_ID_RE.findall(line))
        if not ac_ids_in_line:
            continue
        lowered = line.lower()
        is_fail = any(marker.lower() in lowered for marker in _FAIL_MARKERS)
        is_pass = any(marker.lower() in lowered for marker in _PASS_MARKERS)
        for ac_id in ac_ids_in_line:
            if is_fail:
                ac_line_status[ac_id] = "fail"
            elif is_pass and ac_line_status.get(ac_id) != "fail":
                ac_line_status[ac_id] = "pass"
            elif ac_id not in ac_line_status:
                ac_line_status[ac_id] = "unknown"

    missing = [ac for ac in active_ac_ids if ac not in ac_line_status]
    tautological = [ac for ac in active_ac_ids if ac_line_status.get(ac) == "pass"]

    if missing or tautological:
        reasons = []
        if missing:
            reasons.append(f"no test found covering: {missing}")
        if tautological:
            reasons.append(
                f"these ACs' tests are already PASSING with no implementation yet, which almost "
                f"certainly means they're tautological (assertion-free or trivially true): {tautological}"
            )
        return AcCoverageOutcome(
            passed=False,
            feedback="; ".join(reasons),
            report={"missing": missing, "tautological": tautological, "active_ac_ids": active_ac_ids},
        )

    return AcCoverageOutcome(
        passed=True,
        feedback=f"All {len(active_ac_ids)} active AC(s) have a covering test, correctly failing pre-implementation.",
        report={"active_ac_ids": active_ac_ids},
    )
