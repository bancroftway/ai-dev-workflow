"""Adversarial-audit schemas, used by the adversarial-compliance stage.

The dedup-simplify and license-audit schemas that shared this module went with their node cluster:
duplication is now measured by repo_scan and gated in metrics_nodes.regression_reasons, and licence
obligations surface as repo_scan `license` findings rather than a stage of their own.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .schemas import ClarifyingQuestion, NonBlankStr, PresenceList

# --- adversarial-audit: adversarial audit -------------------------------------------------------------------


class DivergenceFinding(BaseModel):
    id: str = Field(description="Your own placeholder id for this response (e.g. DIV-1).")
    severity: Literal["critical", "major", "minor", "informational"]
    plan_reference: str = Field(description="Which Plan Step or Acceptance Criterion this diverges from.")
    description: str
    evidence: list[str] = Field(default_factory=list, description="Concrete file/line/behavior evidence, not assertion.")
    proposed_resolution: str


class DivergenceFindingPresence(BaseModel):
    """Typed-absence wrapper for `divergence_findings`, same shape/rules as `PresenceList`
    (schemas.py) but with `values: list[DivergenceFinding]` -- PresenceList's own `values` is fixed
    to `list[str]` (Task 1 deliberately avoided a `Generic[T]` wrapper), and a divergence finding
    is a structured object, not a string. A genuinely-clean audit states an explicit reason instead
    of an empty list that could equally mean "never checked".
    """

    status: Literal["present", "absent"]
    values: list[DivergenceFinding] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_bare_list(cls, data: Any) -> Any:
        """Older sidecars/model output stored this as a bare `list[...]` (or `None` for "nothing
        found"). Mirrors PresenceList._coerce_legacy_bare_list (schemas.py) exactly -- duplicated
        here rather than shared because the two wrappers' `values` element type differs."""
        if isinstance(data, list):
            if data:
                return {"status": "present", "values": list(data)}
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        if data is None:
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        return data

    @model_validator(mode="after")
    def _validate_presence(self) -> "DivergenceFindingPresence":
        if self.status == "present":
            if not self.values:
                raise ValueError("status='present' requires a non-empty values list.")
        else:  # absent
            if self.values:
                raise ValueError("status='absent' requires an empty values list.")
            if not self.reason.strip():
                raise ValueError("status='absent' requires a non-blank reason.")
        return self


class AdversarialAuditReport(BaseModel):
    plan_conformance_summary: NonBlankStr
    divergence_findings: DivergenceFindingPresence = Field(
        description="Divergences found, or an explicit absent+reason when the implementation "
        "conforms. Do NOT infer this from overall_verdict -- a 'conforms' verdict may still carry "
        "minor/informational findings; only wrap for typed-absence, never cross-validate against "
        "the verdict (see gates/adversarial_gate.py's own self-check, the `contradictory` case)."
    )
    unresolved_risk_notes: PresenceList = Field(
        description="Risks the audit flagged that remain open, or an explicit absent+reason when "
        "none -- a genuinely risk-free audit says so instead of returning an empty list."
    )
    overall_verdict: Literal["conforms", "minor_gaps", "major_gaps", "fails_to_conform"]


class AdversarialAuditDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    report: AdversarialAuditReport | None = Field(default=None)

    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.schemas_audit`
    _finding = DivergenceFinding(
        id="DIV-1", severity="critical", plan_reference="US-0001.1",
        description="reset endpoint missing", proposed_resolution="add the route",
    )

    # DivergenceFindingPresence: present/absent round-trip.
    present = DivergenceFindingPresence(status="present", values=[_finding])
    assert present.values == [_finding]
    absent = DivergenceFindingPresence(status="absent", reason="no divergences found")
    assert absent.values == []

    # DivergenceFindingPresence: legacy bare-list/None coercion.
    coerced_present = DivergenceFindingPresence.model_validate([_finding.model_dump()])
    assert coerced_present.status == "present" and coerced_present.values == [_finding]
    for legacy_absent in ([], None):
        coerced = DivergenceFindingPresence.model_validate(legacy_absent)
        assert coerced.status == "absent"
        assert coerced.values == []
        assert coerced.reason == "legacy sidecar, pre-typed-absence"

    try:
        DivergenceFindingPresence(status="present", values=[])
        raise AssertionError("expected ValidationError for present with empty values")
    except ValidationError:
        pass
    try:
        DivergenceFindingPresence(status="absent", values=[_finding])
        raise AssertionError("expected ValidationError for absent with non-empty values")
    except ValidationError:
        pass
    try:
        DivergenceFindingPresence(status="absent")
        raise AssertionError("expected ValidationError for absent with blank reason")
    except ValidationError:
        pass

    # AdversarialAuditReport: the "conforms" + non-empty critical finding shape (adversarial_gate.py's
    # own `contradictory` self-check case) must validate -- severity/verdict coupling is the GATE's
    # job (evaluate_audit), never this schema's.
    contradictory = AdversarialAuditReport(
        plan_conformance_summary="Implementation mostly matches the Plan.",
        divergence_findings=DivergenceFindingPresence(status="present", values=[_finding]),
        unresolved_risk_notes=PresenceList(status="absent", reason="no open risks"),
        overall_verdict="conforms",
    )
    assert contradictory.overall_verdict == "conforms"
    assert contradictory.divergence_findings.values[0].severity == "critical"

    # NonBlankStr field: plan_conformance_summary rejects blank/whitespace-only.
    try:
        AdversarialAuditReport(
            plan_conformance_summary="   ",
            divergence_findings=DivergenceFindingPresence(status="absent", reason="none found"),
            unresolved_risk_notes=PresenceList(status="absent", reason="none found"),
            overall_verdict="conforms",
        )
        raise AssertionError("expected ValidationError for blank plan_conformance_summary")
    except ValidationError:
        pass

    print("schemas_audit self-check: all assertions passed")