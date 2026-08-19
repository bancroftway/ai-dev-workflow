"""Consolidated schema for stage 6 (remediation: quality+security+dedup+license)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion


class RemediationDraftResponse(BaseModel):
    """What the remediation stage must report (prompts/remediation_draft.md).

    The fields below exist so the stage's output is CHECKABLE rather than prose: a summary alone
    cannot be compared against the scanner's own finding ids, and the previous version of this stage
    returned exactly that -- one free-text string, from a prompt that was handed no findings at all.
    It therefore fixed nothing, and 43 gating findings (including a critical pre-auth RCE in the
    pinned Next.js version, with a fixed_version published) sailed through to the metrics gate.
    """

    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    remediation_summary: str = Field(
        default="",
        description="What was actually changed, grouped by kind: dependencies upgraded, code "
        "findings fixed, findings deliberately left alone.",
    )
    findings_addressed: list[str] = Field(
        default_factory=list,
        description="The `id` of each finding fixed, copied verbatim from repo-scan-latest.json.",
    )
    dependencies_upgraded: list[str] = Field(
        default_factory=list,
        description='One entry per package moved, as "name: old -> new" (e.g. "next: 15.4.6 -> 15.4.9").',
    )
    known_gaps: list[str] = Field(
        default_factory=list,
        description="Every finding deliberately NOT fixed, each with its real reason. An honest "
        "gap is a valid outcome; a silently skipped finding is not.",
    )
