"""Adversarial-audit schemas, used by the adversarial-compliance stage.

The dedup-simplify and license-audit schemas that shared this module went with their node cluster:
duplication is now measured by repo_scan and gated in metrics_nodes.regression_reasons, and licence
obligations surface as repo_scan `license` findings rather than a stage of their own.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion

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

    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )