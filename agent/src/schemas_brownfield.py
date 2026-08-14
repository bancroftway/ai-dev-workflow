from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion

Confidence = Literal["high", "medium", "low"]


class InferredUserStory(BaseModel):
    us_id: str = Field(description="Placeholder id, e.g. US-1 -- real id assigned on ratification, never by you.")
    title: str
    narrative: str
    origin: Literal["inferred"] = "inferred"
    source_evidence: list[str] = Field(description="Concrete file/route/schema evidence -- never speculation.")
    confidence: Confidence


class InferredAcceptanceCriterion(BaseModel):
    ac_id: str
    us_id: str
    description: str
    confidence: Confidence
    backing_test: str | None = Field(default=None, description="Path to the existing passing test this AC is derived from, if any -- None means no test backs it, so confidence must be 'low'.")


class AsBuiltSpec(BaseModel):
    user_stories: list[InferredUserStory] = Field(default_factory=list)
    acceptance_criteria: list[InferredAcceptanceCriterion] = Field(default_factory=list)
    er_diagram_mermaid: str = ""
    notes: list[str] = Field(default_factory=list)


class AsBuiltPlan(BaseModel):
    architecture_diagram_mermaid: str = ""
    file_inventory: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BrownfieldBaselineCombined(BaseModel):
    """One content_field for the StageSpec template (getattr(response, content_field) expects a
    single dumpable object, not two separate top-level fields)."""

    as_built_spec: AsBuiltSpec
    as_built_plan: AsBuiltPlan


class BrownfieldBaselineDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    baseline: BrownfieldBaselineCombined | None = Field(default=None)


class BrownfieldBaselineAuditResponse(BaseModel):
    revised_baseline: BrownfieldBaselineCombined
    audit_findings: list[str] = Field(default_factory=list)
