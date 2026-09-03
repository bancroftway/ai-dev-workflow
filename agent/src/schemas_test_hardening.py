from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class FlakeTriageDecision(BaseModel):
    test_name: str = Field(description="Exactly as given to you -- never invented.")
    likely_duplicate_of: str | None = Field(default=None, description="An existing US-#### id this flake is already tracked under, if any.")
    new_ticket_title: str = Field(default="", description="Only if likely_duplicate_of is None: a short title for the new ticket.")
    new_ticket_narrative: str = Field(default="", description="Only if likely_duplicate_of is None: what makes this test flaky, from the actual evidence (not speculation).")

    @model_validator(mode="after")
    def _new_ticket_requires_title_and_narrative(self) -> "FlakeTriageDecision":
        """Enforces new_ticket_title/new_ticket_narrative's own docstrings, in the one direction
        they actually state: likely_duplicate_of=None means this IS a new ticket, so both fields
        are required non-blank. Does not forbid populating them when likely_duplicate_of is set --
        neither docstring says that."""
        if self.likely_duplicate_of is None:
            if not self.new_ticket_title.strip():
                raise ValueError(
                    "likely_duplicate_of=None means this is a new ticket, so new_ticket_title "
                    "must be non-blank -- see its own docstring."
                )
            if not self.new_ticket_narrative.strip():
                raise ValueError(
                    "likely_duplicate_of=None means this is a new ticket, so new_ticket_narrative "
                    "must be non-blank -- see its own docstring."
                )
        return self


class FlakeTriageResponse(BaseModel):
    decisions: list[FlakeTriageDecision] = Field(default_factory=list)


if __name__ == "__main__":  # pragma: no cover -- `cd agent && python -m src.schemas_test_hardening`
    from pydantic import ValidationError

    # New ticket (likely_duplicate_of=None) requires both fields non-blank.
    new_ticket = FlakeTriageDecision(
        test_name="test_login_flaky",
        likely_duplicate_of=None,
        new_ticket_title="Flaky login test",
        new_ticket_narrative="Fails intermittently on slow CI runners due to a race in setup.",
    )
    assert new_ticket.new_ticket_title == "Flaky login test"

    # Duplicate (likely_duplicate_of set) does not require either field -- both blank is fine.
    duplicate = FlakeTriageDecision(test_name="test_login_flaky", likely_duplicate_of="US-0042")
    assert duplicate.new_ticket_title == ""

    # Populating them anyway on a duplicate is not forbidden -- neither docstring bans it.
    duplicate_with_extra = FlakeTriageDecision(
        test_name="test_login_flaky",
        likely_duplicate_of="US-0042",
        new_ticket_title="ignored anyway",
        new_ticket_narrative="also ignored",
    )
    assert duplicate_with_extra.new_ticket_title == "ignored anyway"

    try:
        FlakeTriageDecision(test_name="test_login_flaky", likely_duplicate_of=None)
        raise AssertionError("expected ValidationError for a new ticket with blank title/narrative")
    except ValidationError:
        pass
    try:
        FlakeTriageDecision(
            test_name="test_login_flaky",
            likely_duplicate_of=None,
            new_ticket_title="Flaky login test",
            new_ticket_narrative="   ",
        )
        raise AssertionError("expected ValidationError for a new ticket with a whitespace-only narrative")
    except ValidationError:
        pass

    print("schemas_test_hardening self-check: all assertions passed")
