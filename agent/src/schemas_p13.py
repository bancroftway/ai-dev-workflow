from __future__ import annotations

from pydantic import BaseModel, Field


class FlakeTriageDecision(BaseModel):
    test_name: str = Field(description="Exactly as given to you -- never invented.")
    likely_duplicate_of: str | None = Field(default=None, description="An existing US-#### id this flake is already tracked under, if any.")
    new_ticket_title: str = Field(default="", description="Only if likely_duplicate_of is None: a short title for the new ticket.")
    new_ticket_narrative: str = Field(default="", description="Only if likely_duplicate_of is None: what makes this test flaky, from the actual evidence (not speculation).")


class FlakeTriageResponse(BaseModel):
    decisions: list[FlakeTriageDecision] = Field(default_factory=list)
