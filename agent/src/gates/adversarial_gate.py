"""adversarial-compliance's deterministic verify: act on the audit's own verdict.

The stage this belongs to spent the whole consolidation as a stub -- a one-sentence inline prompt,
no findings passed in, a free-form `report: dict | None`, and no tools -- so it approved every run
without reading anything. Restoring the prompt and schema is only half the fix: an audit whose
findings nothing consumes is advisory, and advisory is how it came to be ignored.

Honest about its own strength: unlike the coverage or write-scope gates, this one reads a verdict the
MODEL assigned. The divergence findings carry evidence (file/line, test name, the Plan reference they
contradict), but the SEVERITY is judgement, not measurement. It is a real gate -- it blocks the run
and feeds specifics back -- but it is not machine-verified the way a parsed cobertura number is, and
should not be read as such.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

# Verdicts that block. "minor_gaps" passes deliberately: the stage is meant to surface small
# divergences for the record without deadlocking a run over cosmetic drift, and a run that cannot
# ever pass its own audit teaches people to disable the audit.
BLOCKING_VERDICTS = frozenset({"major_gaps", "fails_to_conform"})
BLOCKING_SEVERITIES = frozenset({"critical", "major"})


def evaluate_audit(report: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """(passed, reasons). Pure, so the routing logic is testable without a sandbox.

    An ABSENT or empty report fails: this stage's entire job is to produce a judgement, and "no
    report" previously sailed through as approval. Blocking on it is the difference between a gate
    and a formality.
    """
    if not report:
        return False, [
            "the adversarial audit produced no report at all -- this stage must return a "
            "plan_conformance_summary and an overall_verdict, and an empty report cannot be "
            "distinguished from an audit that never happened"
        ]

    reasons: list[str] = []
    verdict = str(report.get("overall_verdict") or "").strip()
    if not verdict:
        reasons.append("no overall_verdict was reported")
    elif verdict in BLOCKING_VERDICTS:
        reasons.append(f"overall_verdict is {verdict!r}")

    blocking = [
        finding
        for finding in (report.get("divergence_findings") or [])
        if str(finding.get("severity") or "").lower() in BLOCKING_SEVERITIES
    ]
    for finding in blocking:
        # Feedback names the Plan reference and the evidence, not just a count -- a redraft needs to
        # know WHICH criterion diverged and how it was established, the same reason the coverage gate
        # reports per-line gaps rather than a bare percentage.
        evidence = "; ".join(str(e) for e in (finding.get("evidence") or [])) or "(no evidence cited)"
        reasons.append(
            f"[{finding.get('severity')}] {finding.get('plan_reference') or 'unknown plan reference'}: "
            f"{finding.get('description') or '(no description)'} -- evidence: {evidence}"
        )
    return not reasons, reasons


async def verify_adversarial_compliance(
    _thread_id: str, content_dict: dict[str, Any], _run_id: str, _baseline_commit: str | None, _provider: Any,
    _chat_provider: str,
) -> "VerificationResult":
    # _chat_provider (StageSpec.deterministic_verify's Ruling-4 addition) is unused: this check has
    # no chat-model dispatch call of its own.
    from ..graph import VerificationResult

    passed, reasons = evaluate_audit(content_dict)
    if passed:
        findings = content_dict.get("divergence_findings") or []
        return VerificationResult(
            passed=True,
            feedback=(
                f"adversarial audit verdict {content_dict.get('overall_verdict')!r} with "
                f"{len(findings)} divergence finding(s), none critical/major"
            ),
            report={"overall_verdict": content_dict.get("overall_verdict"), "divergence_count": len(findings)},
        )

    logger.info("adversarial gate: blocking (%d reason(s))", len(reasons))
    return VerificationResult(
        passed=False,
        feedback=(
            "The adversarial audit found the implementation does NOT conform to the approved Plan "
            "and Specification. Fix the code (or, where the audit is demonstrably wrong about the "
            "Plan, say so with evidence rather than lowering the finding's severity):\n"
            + "\n".join(f"- {reason}" for reason in reasons)
        ),
        report={
            "overall_verdict": content_dict.get("overall_verdict") if content_dict else None,
            "blocking_reasons": reasons,
        },
    )


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.adversarial_gate`."""
    # An absent or empty report is the failure mode that let this stage rubber-stamp every run.
    for empty in (None, {}):
        passed, reasons = evaluate_audit(empty)
        assert not passed and "no report at all" in reasons[0], empty

    conforms = {"overall_verdict": "conforms", "divergence_findings": []}
    assert evaluate_audit(conforms) == (True, [])

    # minor_gaps passes on purpose -- see BLOCKING_VERDICTS.
    minor = {"overall_verdict": "minor_gaps", "divergence_findings": [{"severity": "minor"}]}
    assert evaluate_audit(minor)[0]

    # A blocking verdict blocks.
    for verdict in ("major_gaps", "fails_to_conform"):
        passed, reasons = evaluate_audit({"overall_verdict": verdict, "divergence_findings": []})
        assert not passed and verdict in reasons[0]

    # A critical/major finding blocks even when the model calls the whole thing "conforms" -- the
    # per-finding severity is not allowed to be contradicted by an optimistic summary verdict.
    contradictory = {
        "overall_verdict": "conforms",
        "divergence_findings": [{
            "severity": "critical", "plan_reference": "AC US-0003.2",
            "description": "reset endpoint missing", "evidence": ["apps/api/Program.cs has no /reset route"],
        }],
    }
    passed, reasons = evaluate_audit(contradictory)
    assert not passed
    assert "US-0003.2" in reasons[0] and "Program.cs" in reasons[0], reasons

    # A missing verdict is itself a failure: the stage must commit to a judgement.
    assert not evaluate_audit({"divergence_findings": []})[0]

    print("adversarial_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
