from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError, model_validator

from .schemas import ClarifyingQuestion, NonBlankStr, PresenceList


class MergeReadinessReport(BaseModel):
    merge_ready: bool
    blocking_reasons: PresenceList = Field(
        description="Reasons blocking merge, or an explicit absent+reason when merge_ready=True -- "
        "see _validate_merge_consistency below, which ties status to merge_ready."
    )
    pr_title: NonBlankStr
    pr_description_markdown: NonBlankStr
    risk_notes: PresenceList = Field(
        description="Risks noted for this merge, or an explicit absent+reason when none -- a "
        "genuinely risk-free merge says so instead of returning an empty list."
    )
    suggested_reviewers_note: str = ""

    @model_validator(mode="after")
    def _validate_merge_consistency(self) -> "MergeReadinessReport":
        """merge_ready and blocking_reasons.status must agree -- unlike divergence_findings/
        overall_verdict (adversarial_gate.py: severity is judged independent of the summary verdict,
        so no cross-field validator there), merge readiness and its own blocking reasons have no
        such precedent: True with open blockers, or False with none, is never a legitimate shape.

        Fires only at initial LLM-output parse (structured_output.py) -- exit_nodes.py never
        reconstructs this model from the mutated content_dict afterward, so this constrains what the
        model may originally claim; it does not re-enforce consistency after verify_exit_readiness's
        and exit_finalize_node's own later dict mutations (which keep the two fields consistent
        themselves, see their read-modify-rewrite of the blocking_reasons wrapper)."""
        if self.merge_ready and self.blocking_reasons.status != "absent":
            raise ValueError("merge_ready=True requires blocking_reasons.status='absent'.")
        if not self.merge_ready and self.blocking_reasons.status != "present":
            raise ValueError("merge_ready=False requires blocking_reasons.status='present'.")
        return self


class ExitDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    report: MergeReadinessReport | None = Field(default=None)
    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )


EXIT_DRAFT_EXAMPLE: ExitDraftResponse = ExitDraftResponse(
    readiness=True,
    clarifying_questions=[],
    report=MergeReadinessReport(
        merge_ready=True,
        blocking_reasons=PresenceList(
            status="absent",
            reason="Coverage, adversarial audit, and remediation all passed with no open findings.",
        ),
        pr_title="Add password reset via email",
        pr_description_markdown="Implements US-0001 (password reset via emailed token).\n\n"
        "- Adds POST /auth/reset-request and /auth/reset-confirm\n"
        "- 96% branch coverage on new code\n"
        "- Adversarial audit: minor_gaps (missing password-strength meter), fixed",
        risk_notes=PresenceList(
            status="present",
            values=[
                "Email deliverability depends on the third-party SMTP provider staying within "
                "its rate limit."
            ],
        ),
        suggested_reviewers_note="Auth module owner recommended.",
    ),
    skills_invoked=[],
)
"""Fully-populated example of the metrics-exit drafting node's structured output, echoed into
the draft prompt so the model sees a realistic instance of the current canonical (typed-absence)
shape. No separate audit pass for this stage -- draft-only, per StageSpec.audit_response_schema."""


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.schemas_exit`
    # merge_ready=True requires blocking_reasons absent.
    ready = MergeReadinessReport(
        merge_ready=True,
        blocking_reasons=PresenceList(status="absent", reason="no blockers"),
        pr_title="Add the thing",
        pr_description_markdown="Adds the thing.",
        risk_notes=PresenceList(status="absent", reason="no open risks"),
    )
    assert ready.merge_ready is True

    # merge_ready=False requires blocking_reasons present.
    blocked = MergeReadinessReport(
        merge_ready=False,
        blocking_reasons=PresenceList(status="present", values=["coverage regressed"]),
        pr_title="Add the thing",
        pr_description_markdown="Adds the thing.",
        risk_notes=PresenceList(status="absent", reason="no open risks"),
    )
    assert blocked.merge_ready is False

    # merge_ready=True with a non-empty blocking_reasons must be rejected.
    try:
        MergeReadinessReport(
            merge_ready=True,
            blocking_reasons=PresenceList(status="present", values=["coverage regressed"]),
            pr_title="Add the thing",
            pr_description_markdown="Adds the thing.",
            risk_notes=PresenceList(status="absent", reason="no open risks"),
        )
        raise AssertionError("expected ValidationError for merge_ready=True with open blockers")
    except ValidationError:
        pass

    # merge_ready=False with an absent blocking_reasons must be rejected.
    try:
        MergeReadinessReport(
            merge_ready=False,
            blocking_reasons=PresenceList(status="absent", reason="no blockers"),
            pr_title="Add the thing",
            pr_description_markdown="Adds the thing.",
            risk_notes=PresenceList(status="absent", reason="no open risks"),
        )
        raise AssertionError("expected ValidationError for merge_ready=False with no blockers")
    except ValidationError:
        pass

    # Legacy bare-list blocking_reasons still coerces (PresenceList's own before-validator) and
    # then must still satisfy the merge_ready cross-check.
    legacy_ready = MergeReadinessReport.model_validate({
        "merge_ready": True,
        "blocking_reasons": [],
        "pr_title": "Add the thing",
        "pr_description_markdown": "Adds the thing.",
        "risk_notes": [],
    })
    assert legacy_ready.blocking_reasons.status == "absent"
    assert legacy_ready.risk_notes.status == "absent"

    # NonBlankStr fields: pr_title/pr_description_markdown reject blank/whitespace-only.
    for blank_field in ("pr_title", "pr_description_markdown"):
        kwargs = {
            "merge_ready": True,
            "blocking_reasons": PresenceList(status="absent", reason="no blockers"),
            "pr_title": "Add the thing",
            "pr_description_markdown": "Adds the thing.",
            "risk_notes": PresenceList(status="absent", reason="no open risks"),
        }
        kwargs[blank_field] = "   "
        try:
            MergeReadinessReport(**kwargs)
            raise AssertionError(f"expected ValidationError for blank {blank_field}")
        except ValidationError:
            pass

    # Task 13a: EXIT_DRAFT_EXAMPLE -- same generic round-trip proof schemas.py's
    # TECH_STACK_DRAFT_EXAMPLE uses, via structured_output.assert_example_matches_schema.
    import json

    from .structured_output import assert_example_matches_schema

    assert_example_matches_schema(EXIT_DRAFT_EXAMPLE, ExitDraftResponse)
    _report_dumped = json.loads(EXIT_DRAFT_EXAMPLE.report.model_dump_json())
    assert "status" in _report_dumped["blocking_reasons"], "report.blocking_reasons missing 'status'"
    assert "status" in _report_dumped["risk_notes"], "report.risk_notes missing 'status'"

    print("schemas_exit self-check: all assertions passed")
