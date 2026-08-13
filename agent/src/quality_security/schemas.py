"""P8/P10 triage schemas -- the LLM's structured-output contract for deciding fix-vs-suppress per
finding. Kept separate from schemas.py/schemas_codegen.py per the established one-domain-per-module
convention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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


class TriageResponse(BaseModel):
    decisions: list[TriageDecision] = Field(default_factory=list)
