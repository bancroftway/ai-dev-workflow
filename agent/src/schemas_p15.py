from __future__ import annotations

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion


class MergeReadinessReport(BaseModel):
    merge_ready: bool
    blocking_reasons: list[str] = Field(default_factory=list, description="Empty if merge_ready=True.")
    pr_title: str
    pr_description_markdown: str
    risk_notes: list[str] = Field(default_factory=list)
    suggested_reviewers_note: str = ""


class ExitDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    report: MergeReadinessReport | None = Field(default=None)


class ExitAuditResponse(BaseModel):
    revised_report: MergeReadinessReport
    audit_findings: list[str] = Field(default_factory=list)
