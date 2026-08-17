"""Minimal consolidated schema for stage 6 (remediation: quality+security+dedup+license)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion


class RemediationDraftResponse(BaseModel):
    """Consolidated remediation response: quality + security + dedup + license findings."""
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    # Consolidated finding report (minimal)
    remediation_summary: str = ""
