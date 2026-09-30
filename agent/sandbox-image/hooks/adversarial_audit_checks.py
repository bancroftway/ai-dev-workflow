"""adversarial-compliance's pure verdict logic, extracted from adversarial_gate.py (2026-09-29,
Task 10) so the sandbox's own same-turn Stop hook (sandbox-image/hooks/check-adversarial-stop.mjs)
can run the REAL check by shelling out to `python3` on this ONE file, instead of a hand-ported
JavaScript reimplementation drifting from it -- same rationale as `coverage_parsing.py`'s own
extraction from `test_coverage_gate.py`; see that module's docstring for the full argument.

`adversarial_gate.py` itself cannot be the staged copy: it imports `..schemas.presence_values`
(pydantic-backed, not installed in the sandbox) and `..graph`/`..repo_files` for its host-side
verify/snapshot logic. This module needs only the stdlib, so `_findings_from` below is a small,
deliberately duplicated copy of `schemas.presence_values`'s own body (4 lines, unlikely to drift --
see that function's own docstring for the shape it unwraps), not the whole-parser-scale
duplication risk `coverage_parsing.py`'s own docstring warns against.

`adversarial_gate.py` imports `evaluate_audit`/`BLOCKING_VERDICTS`/`BLOCKING_SEVERITIES` from here
UNCHANGED (this is a pure code-move, not a fork); its own self-check is the proof this extraction
changed no behavior.

CLI mode (`python3 adversarial_audit_checks.py --check-hook`, stdin: JSON `{"report": {...}|null}`,
stdout: JSON `{"passed": bool, "reasons": [str, ...]}`) is what the Stop hook actually invokes.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Verdicts that block. "minor_gaps" passes deliberately: the stage is meant to surface small
# divergences for the record without deadlocking a run over cosmetic drift, and a run that cannot
# ever pass its own audit teaches people to disable the audit.
BLOCKING_VERDICTS = frozenset({"major_gaps", "fails_to_conform"})
BLOCKING_SEVERITIES = frozenset({"critical", "major"})


def _findings_from(entry: Any) -> list[Any]:
    """Copy of `schemas.presence_values`'s own body (see this module's docstring for why this one
    small function is duplicated rather than imported): the `values` list of a PresenceList-shaped
    dict, tolerating a legacy bare list or a missing/None field."""
    if isinstance(entry, dict):
        return list(entry.get("values") or [])
    if isinstance(entry, list):
        return list(entry)
    return []


def evaluate_audit(report: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """(passed, reasons) -- `evaluate_audit_checks` with the check ids stripped. This is the shape
    the Stop hook's CLI and every pre-existing caller consume."""
    passed, tagged, _ran = evaluate_audit_checks(report)
    return passed, [reason for _check_id, reason in tagged]


# Sub-check ids, as plain strings: this file is stdlib-only (sandbox copy), so it cannot import
# gates/checks.py's Check. adversarial_gate.py declares the matching Check objects.
CHECK_REPORT = "adversarial.report"
CHECK_VERDICT = "adversarial.verdict"
CHECK_BLOCKING_FINDINGS = "adversarial.blocking_findings"


def evaluate_audit_checks(report: dict[str, Any] | None) -> tuple[bool, list[tuple[str, str]], list[str]]:
    """(passed, [(check_id, reason), ...], ran_check_ids). Pure, so the routing logic is testable
    without a sandbox. `ran_check_ids` lists every sub-check actually evaluated, in order.

    An ABSENT or empty report fails: this stage's entire job is to produce a judgement, and "no
    report" previously sailed through as approval. Blocking on it is the difference between a gate
    and a formality.
    """
    if not report:
        return False, [(
            CHECK_REPORT,
            (
                "the adversarial audit produced no report at all -- this stage must return a "
                "plan_conformance_summary and an overall_verdict, and an empty report cannot be "
                "distinguished from an audit that never happened"
            ),
        )], [CHECK_REPORT]

    reasons: list[tuple[str, str]] = []
    verdict = str(report.get("overall_verdict") or "").strip()
    if not verdict:
        reasons.append((CHECK_VERDICT, "no overall_verdict was reported"))
    elif verdict in BLOCKING_VERDICTS:
        reasons.append((CHECK_VERDICT, f"overall_verdict is {verdict!r}"))

    blocking = [
        finding
        for finding in _findings_from(report.get("divergence_findings"))
        if str(finding.get("severity") or "").lower() in BLOCKING_SEVERITIES
    ]
    for finding in blocking:
        # Feedback names the Plan reference and the evidence, not just a count -- a redraft needs to
        # know WHICH criterion diverged and how it was established, the same reason the coverage gate
        # reports per-line gaps rather than a bare percentage.
        evidence = "; ".join(str(e) for e in (finding.get("evidence") or [])) or "(no evidence cited)"
        reasons.append((
            CHECK_BLOCKING_FINDINGS,
            (
                f"[{finding.get('severity')}] {finding.get('plan_reference') or 'unknown plan reference'}: "
                f"{finding.get('description') or '(no description)'} -- evidence: {evidence}"
            ),
        ))
    return not reasons, reasons, [CHECK_REPORT, CHECK_VERDICT, CHECK_BLOCKING_FINDINGS]


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.adversarial_audit_checks`."""
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "adversarial_audit_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            "sandbox-image/hooks/adversarial_audit_checks.py has drifted from "
            "src/gates/adversarial_audit_checks.py -- re-sync with: cp "
            "src/gates/adversarial_audit_checks.py sandbox-image/hooks/adversarial_audit_checks.py"
        )

    # An absent or empty report is the failure mode that let this stage rubber-stamp every run.
    for empty in (None, {}):
        passed, reasons = evaluate_audit(empty)
        assert not passed and "no report at all" in reasons[0], empty

    # divergence_findings is DivergenceFindingPresence-wrapped (schemas_audit.py) -- a bare list is
    # only the legacy shape _findings_from tolerates, not what a current content_dict carries.
    absent_findings = {"status": "absent", "values": [], "reason": "no divergences found"}
    conforms = {"overall_verdict": "conforms", "divergence_findings": absent_findings}
    assert evaluate_audit(conforms) == (True, [])

    # minor_gaps passes on purpose -- see BLOCKING_VERDICTS.
    minor = {
        "overall_verdict": "minor_gaps",
        "divergence_findings": {"status": "present", "values": [{"severity": "minor"}]},
    }
    assert evaluate_audit(minor)[0]

    # A blocking verdict blocks.
    for verdict in ("major_gaps", "fails_to_conform"):
        passed, reasons = evaluate_audit({"overall_verdict": verdict, "divergence_findings": absent_findings})
        assert not passed and verdict in reasons[0]

    # A critical/major finding blocks even when the model calls the whole thing "conforms" -- the
    # per-finding severity is not allowed to be contradicted by an optimistic summary verdict.
    contradictory = {
        "overall_verdict": "conforms",
        "divergence_findings": {
            "status": "present",
            "values": [{
                "severity": "critical", "plan_reference": "AC US-0003.2",
                "description": "reset endpoint missing", "evidence": ["apps/api/Program.cs has no /reset route"],
            }],
        },
    }
    passed, reasons = evaluate_audit(contradictory)
    assert not passed
    assert "US-0003.2" in reasons[0] and "Program.cs" in reasons[0], reasons

    # A missing verdict is itself a failure: the stage must commit to a judgement.
    assert not evaluate_audit({"divergence_findings": absent_findings})[0]

    # _findings_from also tolerates a legacy bare list (pre-wrapper on-disk content), never crashes.
    assert _findings_from([{"severity": "minor"}]) == [{"severity": "minor"}]
    assert _findings_from(None) == []
    assert _findings_from({"status": "absent", "values": [], "reason": "x"}) == []

    # Tagged variant: each reason carries the sub-check that raised it.
    assert evaluate_audit_checks(None)[1][0][0] == CHECK_REPORT and evaluate_audit_checks({})[2] == [CHECK_REPORT]
    all_ran = [CHECK_REPORT, CHECK_VERDICT, CHECK_BLOCKING_FINDINGS]
    assert evaluate_audit_checks(conforms) == (True, [], all_ran)
    passed, tagged, ran = evaluate_audit_checks({**contradictory, "overall_verdict": "major_gaps"})
    assert not passed and [t[0] for t in tagged] == [CHECK_VERDICT, CHECK_BLOCKING_FINDINGS] and ran == all_ran
    assert evaluate_audit_checks({"divergence_findings": absent_findings})[1] == [
        (CHECK_VERDICT, "no overall_verdict was reported")
    ]

    print("adversarial_audit_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"report": ...} on stdin, JSON result on stdout.
        # Deliberately the ONLY thing this branch does -- no sandbox access beyond what the hook
        # already read from the transcript and handed over as data.
        payload = json.loads(sys.stdin.read())
        passed, reasons = evaluate_audit(payload.get("report"))
        json.dump({"passed": passed, "reasons": reasons}, sys.stdout)
    else:
        _demo()
