"""audit-cluster's four sub-stage schemas -- kept as one module since all four are tightly scoped to audit-cluster
and none is large enough to warrant its own file (unlike schemas_codegen.py's P4/P6 split, which
covers two genuinely separate stages each with several substantial types).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion
from .schemas_codegen import ChangedFile

# --- adversarial-audit: adversarial audit -------------------------------------------------------------------


class DivergenceFinding(BaseModel):
    id: str = Field(description="Your own placeholder id for this response (e.g. DIV-1).")
    severity: Literal["critical", "major", "minor", "informational"]
    plan_reference: str = Field(description="Which Plan Step or Acceptance Criterion this diverges from.")
    description: str
    evidence: list[str] = Field(default_factory=list, description="Concrete file/line/behavior evidence, not assertion.")
    proposed_resolution: str


class AdversarialAuditReport(BaseModel):
    plan_conformance_summary: str
    divergence_findings: list[DivergenceFinding] = Field(default_factory=list)
    unresolved_risk_notes: list[str] = Field(default_factory=list)
    overall_verdict: Literal["conforms", "minor_gaps", "major_gaps", "fails_to_conform"]


class AdversarialAuditDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    report: AdversarialAuditReport | None = Field(default=None)


class AdversarialAuditAuditResponse(BaseModel):
    """The audit pass here is itself a second, differently-modeled re-probe of the adversarial
    audit -- it only reuses findings it can independently verify, per the plan's own design."""

    revised_report: AdversarialAuditReport
    audit_findings: list[str] = Field(default_factory=list)


# --- dedup-simplify: de-dup/simplify ---------------------------------------------------------------------


class DedupResult(BaseModel):
    approach_summary: str
    changed_files: list[ChangedFile] = Field(default_factory=list)
    regression_risk: Literal["none", "low", "medium", "high"] = Field(
        description="Populated by the audit pass's own read-only risk assessment; the draft pass leaves this at its default."
    )
    duplication_percent_before: float | None = Field(default=None)
    duplication_percent_after: float | None = Field(
        default=None, description="Populated by the post_audit_hook's own deterministic jscpd re-run, never by the model."
    )
    ponytail_rejected: list[str] = Field(
        default_factory=list,
        description="Ponytail proposals evaluated and rejected, each with a one-line reason.",
    )


class DedupDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    result: DedupResult | None = Field(default=None)


class DedupAuditResponse(BaseModel):
    revised_result: DedupResult
    audit_findings: list[str] = Field(default_factory=list)


# --- license-audit: license audit -----------------------------------------------------------------------

LicenseBucket = Literal["allow", "review_required", "deny", "unknown"]
LicenseConfidence = Literal["high", "medium", "low"]


class LicenseClassification(BaseModel):
    package_name: str
    ecosystem: str = Field(description="e.g. 'nuget', 'npm'.")
    declared_license: str
    detected_license: str
    bucket: LicenseBucket
    confidence: LicenseConfidence
    dual_or_exception_flag: bool = Field(
        description="True for dual-licensed or exception-carrying packages -- the single most common automated misclassification; always route to human review, never auto-accept."
    )
    rationale: str
    recommended_action: str


class LicenseAuditReport(BaseModel):
    classifications: list[LicenseClassification] = Field(default_factory=list)
    summary: str


class LicenseAuditDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    report: LicenseAuditReport | None = Field(default=None)


class LicenseAuditAuditResponse(BaseModel):
    revised_report: LicenseAuditReport
    audit_findings: list[str] = Field(default_factory=list)
