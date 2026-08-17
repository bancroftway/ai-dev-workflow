"""Minimal consolidated schema for stage 7 (adversarial-compliance: adversarial-audit + test-hardening + e2e)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion


class AdversarialComplianceDraftResponse(BaseModel):
    """Consolidated compliance response: adversarial-audit + test-hardening + e2e + wireframe checks."""
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    # Consolidated audit report (minimal)
    report: dict | None = Field(default=None, description="Audit findings and compliance status")
