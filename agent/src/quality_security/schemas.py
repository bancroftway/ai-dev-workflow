"""quality-remediation/security-remediation triage schemas -- the LLM's structured-output contract for deciding fix-vs-suppress per
finding. Kept separate from schemas.py/schemas_codegen.py per the established one-domain-per-module
convention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class TriageDecision(BaseModel):
    finding_key: str = Field(description="The finding_key exactly as given to you -- never invented.")
    decision: Literal["fix", "suppress"]
    justification: str = Field(
        description="Rule-specific reasoning, at least a full sentence. A justification under "
        "~15 words with no rule-specific reasoning is a rubber stamp, not a real decision."
    )
    suppression_marker: str = Field(
        default="", description="For decision=suppress only: the exact suppression comment/pragma text to insert, minus the ref: token (added deterministically after triage)."
    )

    @model_validator(mode="after")
    def _suppress_requires_marker(self) -> "TriageDecision":
        """Enforces suppression_marker's own docstring: for decision=suppress only. A fix decision
        needing no marker is unaffected -- this only requires one be present when suppressing."""
        if self.decision == "suppress" and not self.suppression_marker.strip():
            raise ValueError(
                "decision='suppress' requires a non-blank suppression_marker -- see its own "
                "docstring."
            )
        return self


class TriageResponse(BaseModel):
    decisions: list[TriageDecision] = Field(default_factory=list)


if __name__ == "__main__":  # pragma: no cover -- `cd agent && python -m src.quality_security.schemas`
    from pydantic import ValidationError

    # decision='suppress' requires a non-blank suppression_marker.
    suppressed = TriageDecision(
        finding_key="abc123",
        decision="suppress",
        justification="This finding is a false positive because the input is validated upstream.",
        suppression_marker="// nosemgrep: rule-id",
    )
    assert suppressed.suppression_marker == "// nosemgrep: rule-id"

    # decision='fix' needs no marker -- blank is fine.
    fixed = TriageDecision(
        finding_key="abc123",
        decision="fix",
        justification="This is a real SQL injection risk; parameterizing the query fixes it.",
    )
    assert fixed.suppression_marker == ""

    try:
        TriageDecision(
            finding_key="abc123",
            decision="suppress",
            justification="This finding is a false positive because the input is validated upstream.",
        )
        raise AssertionError("expected ValidationError for decision='suppress' with a blank suppression_marker")
    except ValidationError:
        pass
    try:
        TriageDecision(
            finding_key="abc123",
            decision="suppress",
            justification="This finding is a false positive because the input is validated upstream.",
            suppression_marker="   ",
        )
        raise AssertionError("expected ValidationError for decision='suppress' with a whitespace-only suppression_marker")
    except ValidationError:
        pass

    print("quality_security.schemas self-check: all assertions passed")
