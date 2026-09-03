from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import ClarifyingQuestion, NonBlankStr

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

    @model_validator(mode="after")
    def _backing_test_requires_low_confidence(self) -> "InferredAcceptanceCriterion":
        """Enforces backing_test's own docstring, in the one direction it actually states:
        backing_test=None means no test backs this criterion, so confidence must be 'low'. Does
        NOT forbid the reverse (a low-confidence AC that cites a real, if weak, backing test) --
        the docstring never says that."""
        if self.backing_test is None and self.confidence != "low":
            raise ValueError(
                "backing_test=None means no test backs this criterion, so confidence must be "
                "'low' -- see backing_test's own docstring."
            )
        return self


class AsBuiltSpec(BaseModel):
    user_stories: list[InferredUserStory] = Field(default_factory=list)
    acceptance_criteria: list[InferredAcceptanceCriterion] = Field(default_factory=list)
    er_diagram_mermaid: NonBlankStr = Field(
        description="Mermaid ER diagram of the as-built data model -- a brownfield repo always "
        "has SOMETHING to diagram, so this is required, never blank."
    )
    notes: list[str] = Field(default_factory=list)


class AsBuiltPlan(BaseModel):
    architecture_diagram_mermaid: NonBlankStr = Field(
        description="Mermaid architecture diagram of the as-built system -- a brownfield repo "
        "always has SOMETHING to diagram, so this is required, never blank."
    )
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


if __name__ == "__main__":  # pragma: no cover -- `cd agent && python -m src.schemas_brownfield`
    from pydantic import ValidationError

    # InferredAcceptanceCriterion: backing_test=None requires confidence='low', both directions.
    ok_no_test = InferredAcceptanceCriterion(
        ac_id="AC-1", us_id="US-1", description="x", confidence="low", backing_test=None
    )
    assert ok_no_test.backing_test is None
    ok_with_test = InferredAcceptanceCriterion(
        ac_id="AC-1", us_id="US-1", description="x", confidence="low", backing_test="tests/x_test.py"
    )
    assert ok_with_test.confidence == "low"
    # The undocumented reverse (low confidence + a real backing test) must NOT be forbidden.
    ok_low_with_real_test = InferredAcceptanceCriterion(
        ac_id="AC-1", us_id="US-1", description="x", confidence="low", backing_test="tests/y_test.py"
    )
    assert ok_low_with_real_test.backing_test == "tests/y_test.py"
    # A higher confidence WITH a backing test is fine too -- only the None+non-low pairing is banned.
    ok_high_with_test = InferredAcceptanceCriterion(
        ac_id="AC-1", us_id="US-1", description="x", confidence="high", backing_test="tests/z_test.py"
    )
    assert ok_high_with_test.confidence == "high"
    try:
        InferredAcceptanceCriterion(
            ac_id="AC-1", us_id="US-1", description="x", confidence="medium", backing_test=None
        )
        raise AssertionError("expected ValidationError for backing_test=None with confidence!='low'")
    except ValidationError:
        pass

    # AsBuiltSpec.er_diagram_mermaid / AsBuiltPlan.architecture_diagram_mermaid: required non-blank.
    spec = AsBuiltSpec(er_diagram_mermaid="erDiagram\n  A ||--o{ B : has")
    assert spec.er_diagram_mermaid.startswith("erDiagram")
    plan = AsBuiltPlan(architecture_diagram_mermaid="flowchart TD\n  A --> B")
    assert plan.architecture_diagram_mermaid.startswith("flowchart")
    try:
        AsBuiltSpec(er_diagram_mermaid="")
        raise AssertionError("expected ValidationError for blank er_diagram_mermaid")
    except ValidationError:
        pass
    try:
        AsBuiltSpec()
        raise AssertionError("expected ValidationError for missing er_diagram_mermaid")
    except ValidationError:
        pass
    try:
        AsBuiltPlan(architecture_diagram_mermaid="   ")
        raise AssertionError("expected ValidationError for whitespace-only architecture_diagram_mermaid")
    except ValidationError:
        pass

    print("schemas_brownfield self-check: all assertions passed")

