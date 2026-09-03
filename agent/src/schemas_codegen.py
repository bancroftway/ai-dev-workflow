"""P4 (AC-to-tests) content schemas -- kept separate from schemas.py (spec/plan domain) per the
plan's own module-boundary convention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import ClarifyingQuestion, PresenceList

TestKind = Literal["unit", "integration", "e2e_playwright_skeleton"]
TestFramework = Literal["xunit", "nunit", "vitest", "jest", "playwright", "pytest"]
TestCategory = Literal["happy_path", "negative", "edge", "adversarial", "validator"]


class AcceptanceCriteriaTestPlanEntry(BaseModel):
    ac_id: str = Field(description="The AC-####.# id this entry covers, exactly as given to you -- never invented.")
    us_id: str = Field(description="The parent US-#### id.")
    test_kind: TestKind
    ui_relevant: bool = Field(description="True if this AC concerns user-visible UI behavior (drives Playwright MCP use).")
    categories: list[TestCategory] = Field(
        default_factory=list,
        description=(
            "Which categories this AC's tests actually cover. Include happy_path plus any of "
            "negative/edge/adversarial/validator that were meaningful for this AC; explain any "
            "meaningful-but-skipped category in rationale."
        ),
    )
    rationale: str


class GeneratedTestFile(BaseModel):
    path: str = Field(description="Repo-relative path of the test file, following this repo's existing test-project conventions.")
    test_framework: TestFramework
    ac_ids: list[str] = Field(default_factory=list, description="Every AC-####.# id this file's tests cover.")
    test_names: list[str] = Field(default_factory=list, description="The actual test names/identifiers, each embedding its AC id per this repo's naming convention.")
    kind: TestKind
    summary: str


class SkippedAcceptanceCriterion(BaseModel):
    ac_id: str
    reason: str


class AcceptanceCriteriaTestSuite(BaseModel):
    coverage_plan: list[AcceptanceCriteriaTestPlanEntry] = Field(default_factory=list)
    test_files: list[GeneratedTestFile] = Field(default_factory=list)
    skipped_ac_ids: list[SkippedAcceptanceCriterion] = Field(default_factory=list)
    summary: str


class AcceptanceCriteriaTestsDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    test_suite: AcceptanceCriteriaTestSuite | None = Field(default=None)

    @model_validator(mode="after")
    def _ready_means_files_were_written(self) -> "AcceptanceCriteriaTestsDraftResponse":
        """readiness=True with no test files is the stage's dominant failure, and it is not a
        content problem the audit can fix -- it means no work happened at all.

        Observed live across three consecutive fresh sessions (nextjs-dotnet): the model called
        the `skill` tool exactly once, then emitted `{"readiness": true, "test_suite": null}` in
        under 5 seconds, self-reporting "no test files were written to disk yet" as the REASON
        for the empty report. The write-scope gate caught it every time, but only after the whole
        graph cycle had been spent, and six cycles of that exhausted the stage.

        Raising it here instead moves the correction inside the turn: ainvoke_structured feeds
        this message straight back to the same session, which still has its file tools and can
        write the tests and answer again -- three attempts before a graph cycle is spent at all.
        """
        if not self.readiness:
            return self  # not claiming done; clarifying_questions is the honest path
        if self.test_suite is None or not self.test_suite.test_files:
            raise ValueError(
                "readiness=true but test_files is empty. This response is metadata ABOUT test "
                "files -- it is not the files themselves. Nothing has been written to the repo "
                "yet. Use your file tools (create/apply_patch) to write the actual test files to "
                "disk NOW, then reply with this JSON listing the files you really created. If "
                "something genuinely blocks you from writing them, set readiness=false and put "
                "the blocker in clarifying_questions instead of reporting an empty suite as done."
            )
        return self
    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )


class AcToTestsAuditResponse(BaseModel):
    """Second opinion on ac-to-tests-draft's test_suite -- same audit contract as
    MinimalCodeToGreenAuditResponse below (revise the metadata, list findings; this role's session
    is read-only, so a finding that needs a real test-file edit belongs in audit_findings, not
    here)."""

    revised_test_suite: AcceptanceCriteriaTestSuite
    audit_findings: PresenceList = Field(
        description="Gaps found and fixed, or an explicit absent+reason when none were found."
    )


_AC_TO_TESTS_SUITE_EXAMPLE = AcceptanceCriteriaTestSuite(
    coverage_plan=[
        AcceptanceCriteriaTestPlanEntry(
            ac_id="US-0001.1",
            us_id="US-0001",
            test_kind="unit",
            ui_relevant=False,
            categories=["happy_path", "negative"],
            rationale="Covers reset-token issuance and rejection of an expired token.",
        ),
        AcceptanceCriteriaTestPlanEntry(
            ac_id="US-0001.2",
            us_id="US-0001",
            test_kind="e2e_playwright_skeleton",
            ui_relevant=True,
            categories=["happy_path"],
            rationale="Covers the reset-request form's user-visible confirmation state.",
        ),
    ],
    test_files=[
        GeneratedTestFile(
            path="tests/auth/test_reset_controller.py",
            test_framework="pytest",
            ac_ids=["US-0001.1"],
            test_names=[
                "test_reset_request_issues_token_US_0001_1",
                "test_reset_request_rejects_expired_token_US_0001_1",
            ],
            kind="unit",
            summary="Unit tests for reset-token issuance and expiry rejection.",
        ),
        GeneratedTestFile(
            path="tests/e2e/reset-request.spec.ts",
            test_framework="playwright",
            ac_ids=["US-0001.2"],
            test_names=["reset request form shows a confirmation message US-0001.2"],
            kind="e2e_playwright_skeleton",
            summary="Playwright skeleton for the reset-request form's success state.",
        ),
    ],
    skipped_ac_ids=[
        SkippedAcceptanceCriterion(
            ac_id="US-0001.3",
            reason="Deferred to a later phase per the approved Specification.",
        ),
    ],
    summary="Covers both non-deferred criteria for US-0001 (unit + Playwright skeleton); "
    "US-0001.3 is deferred, not skipped for any other reason.",
)

AC_TO_TESTS_DRAFT_EXAMPLE: AcceptanceCriteriaTestsDraftResponse = AcceptanceCriteriaTestsDraftResponse(
    readiness=True,
    clarifying_questions=[],
    test_suite=_AC_TO_TESTS_SUITE_EXAMPLE,
    skills_invoked=["test-driven-development"],
)
"""Fully-populated example of the ac-to-tests drafting node's structured output, echoed into the
draft prompt so the model sees a realistic instance of the current canonical shape -- same
purpose and pattern as this stage's sibling, MINIMAL_CODE_TO_GREEN_DRAFT_EXAMPLE below. Added in
the final whole-branch review's fix wave: every OTHER wired stage already had a draft_example, and
this is the stage (AcceptanceCriteriaTestsDraftResponse's own docstring documents a live
dominant-failure history, and it alone carries max_verify_cycles=6) that most needed the missing
half of the mechanism."""

AC_TO_TESTS_AUDIT_EXAMPLE: AcToTestsAuditResponse = AcToTestsAuditResponse(
    revised_test_suite=_AC_TO_TESTS_SUITE_EXAMPLE,
    audit_findings=PresenceList(
        status="present",
        values=[
            "US-0001.1's expiry test only checked a token 1 second past expiry; added a "
            "well-past-expiry case too."
        ],
    ),
)
"""Fully-populated example of the ac-to-tests adversarial-audit node's structured output.
audit_findings is a real PresenceList (Task 14), matching every other audit schema's
audit_findings. Deliberately the non-empty 'present' branch, where MINIMAL_CODE_TO_GREEN_AUDIT_
EXAMPLE below demonstrates 'absent' -- between the two wired examples, both directions of the
typed-absence shape are exercised with real status keys, not just validated by coercion."""


class ChangedFile(BaseModel):
    path: str
    change_kind: Literal["created", "modified", "deleted"]
    summary: str = Field(description="One line -- git is the source of truth for the actual diff, not this field.")
    related_ac_ids: list[str] = Field(default_factory=list)


class SubagentTaskRecord(BaseModel):
    task_id: str
    description: str
    status: Literal["completed", "failed", "skipped"]
    reviewer_notes: str = ""


class CodegenIterationResult(BaseModel):
    approach_summary: str
    changed_files: list[ChangedFile] = Field(default_factory=list)
    subagent_tasks: list[SubagentTaskRecord] = Field(default_factory=list)
    known_gaps: PresenceList = Field(
        description="Gaps deliberately left unfixed this iteration, or an explicit absent+reason "
        "when there are none."
    )
    ponytail_rejected: list[str] = Field(
        default_factory=list,
        description="Ponytail suggestions evaluated and rejected (including by subagents), each with a one-line reason.",
    )


class MinimalCodeToGreenDraftResponse(BaseModel):
    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    iteration: CodegenIterationResult | None = Field(default=None)
    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )

    @model_validator(mode="after")
    def _ready_means_files_were_changed(self) -> "MinimalCodeToGreenDraftResponse":
        """Structural twin of AcceptanceCriteriaTestsDraftResponse._ready_means_files_were_written
        (schemas_codegen.py's sibling stage schema for AC-to-tests): readiness=True with no
        changed_files is the same dominant failure mode, just for the codegen stage -- the model
        reports metadata about a fix instead of actually writing it.

        Raising it here feeds the correction straight back into the same turn: ainvoke_structured
        re-sends this message to the same session, which still has its file tools and can write the
        change and answer again, before a whole graph cycle is spent on an empty iteration.
        """
        if not self.readiness:
            return self  # not claiming done; clarifying_questions is the honest path
        if self.iteration is None or not self.iteration.changed_files:
            raise ValueError(
                "readiness=true but changed_files is empty. This response is metadata ABOUT "
                "changed files -- it is not the files themselves. Nothing has been written to the "
                "repo yet. Use your file tools (create/edit/apply_patch) to write/edit the actual "
                "files to disk NOW, then reply with this JSON listing the files you really "
                "changed. If something genuinely blocks you from making the change, set "
                "readiness=false and put the blocker in clarifying_questions instead of reporting "
                "an empty iteration as done."
            )
        return self


class MinimalCodeToGreenAuditResponse(BaseModel):
    revised_iteration: CodegenIterationResult
    audit_findings: PresenceList = Field(
        description="Gaps found and fixed, or an explicit absent+reason when none were found."
    )


_MINIMAL_CODE_TO_GREEN_ITERATION_EXAMPLE = CodegenIterationResult(
    approach_summary="Implemented plan step PS-1: added the password-reset-request endpoint and "
    "wired it to the existing email-sending module.",
    changed_files=[
        ChangedFile(
            path="src/auth/reset_controller.py",
            change_kind="created",
            summary="New POST /auth/reset-request handler: looks up the email, issues a 1-hour "
            "token, sends the reset email.",
            related_ac_ids=["US-0001.1", "US-0001.2"],
        ),
        ChangedFile(
            path="src/auth/routes.py",
            change_kind="modified",
            summary="Registered the reset-request route.",
            related_ac_ids=["US-0001.1"],
        ),
    ],
    subagent_tasks=[
        SubagentTaskRecord(
            task_id="task-1",
            description="Write the reset-token email template.",
            status="completed",
            reviewer_notes="Matches the reset-request wireframe's copy.",
        )
    ],
    known_gaps=PresenceList(
        status="absent", reason="Both criteria for this plan step are fully implemented."
    ),
    ponytail_rejected=[
        "Considered a generic notification-service abstraction; rejected as premature for a "
        "single email type."
    ],
)

MINIMAL_CODE_TO_GREEN_DRAFT_EXAMPLE: MinimalCodeToGreenDraftResponse = MinimalCodeToGreenDraftResponse(
    readiness=True,
    clarifying_questions=[],
    iteration=_MINIMAL_CODE_TO_GREEN_ITERATION_EXAMPLE,
    skills_invoked=["test-driven-development"],
)
"""Fully-populated example of the minimal-code-to-green drafting node's structured output,
echoed into the draft prompt so the model sees a realistic instance of the current canonical
(typed-absence) shape."""

MINIMAL_CODE_TO_GREEN_AUDIT_EXAMPLE: MinimalCodeToGreenAuditResponse = MinimalCodeToGreenAuditResponse(
    revised_iteration=_MINIMAL_CODE_TO_GREEN_ITERATION_EXAMPLE,
    audit_findings=PresenceList(
        status="absent",
        reason="Confirmed the reset-request endpoint matches PS-1 and both cited criteria; no "
        "divergences found.",
    ),
)
"""Fully-populated example of the minimal-code-to-green adversarial-audit node's structured
output. audit_findings is a real PresenceList (Task 14), matching SpecificationAuditResponse/
PlanAuditResponse/AcToTestsAuditResponse's audit_findings -- all four audit schemas now share the
same typed-absence shape."""


if __name__ == "__main__":  # pragma: no cover -- `cd agent && python -m src.schemas_codegen`
    from pydantic import ValidationError

    _changed_file = ChangedFile(path="src/foo.py", change_kind="modified", summary="fixed the bug")

    def _iteration(**kwargs: object) -> CodegenIterationResult:
        base = dict(
            approach_summary="fixed it",
            changed_files=[_changed_file],
            known_gaps=PresenceList(status="absent", reason="nothing left open"),
        )
        base.update(kwargs)
        return CodegenIterationResult(**base)  # type: ignore[arg-type]

    # CodegenIterationResult.known_gaps: real PresenceList now, present/absent both validate, and
    # a bare list still legacy-coerces (older sidecars/model output).
    present_gaps = _iteration(known_gaps=PresenceList(status="present", values=["needs a follow-up migration"]))
    assert present_gaps.known_gaps.values == ["needs a follow-up migration"]
    legacy_gaps = CodegenIterationResult.model_validate(
        {
            "approach_summary": "x",
            "changed_files": [_changed_file.model_dump()],
            "known_gaps": ["legacy bare gap"],
        }
    )
    assert legacy_gaps.known_gaps.status == "present"
    assert legacy_gaps.known_gaps.values == ["legacy bare gap"]

    # MinimalCodeToGreenDraftResponse._ready_means_files_were_changed: mirrors
    # AcceptanceCriteriaTestsDraftResponse._ready_means_files_were_written -- both directions.
    not_ready = MinimalCodeToGreenDraftResponse(readiness=False, iteration=None)
    assert not_ready.iteration is None  # honest "not done yet" path, never raises

    ready_with_changes = MinimalCodeToGreenDraftResponse(readiness=True, iteration=_iteration())
    assert ready_with_changes.iteration is not None and ready_with_changes.iteration.changed_files

    try:
        MinimalCodeToGreenDraftResponse(readiness=True, iteration=None)
        raise AssertionError("expected ValidationError for readiness=true with iteration=None")
    except ValidationError:
        pass
    try:
        MinimalCodeToGreenDraftResponse(readiness=True, iteration=_iteration(changed_files=[]))
        raise AssertionError("expected ValidationError for readiness=true with empty changed_files")
    except ValidationError:
        pass

    # Task 13a: MINIMAL_CODE_TO_GREEN_DRAFT_EXAMPLE/MINIMAL_CODE_TO_GREEN_AUDIT_EXAMPLE -- same
    # generic round-trip proof schemas.py's TECH_STACK_DRAFT_EXAMPLE uses, via
    # structured_output.assert_example_matches_schema.
    import json

    from .structured_output import assert_example_matches_schema

    assert_example_matches_schema(MINIMAL_CODE_TO_GREEN_DRAFT_EXAMPLE, MinimalCodeToGreenDraftResponse)
    assert_example_matches_schema(MINIMAL_CODE_TO_GREEN_AUDIT_EXAMPLE, MinimalCodeToGreenAuditResponse)

    # Final whole-branch review fix wave: ac-to-tests was the one wired stage with draft_rules/
    # audit_rules but no draft_example/audit_example -- same round-trip proof as every other
    # example in this plan, both "validates" AND "round-trips to canonical shape with real status
    # keys", not just silent-coercion-validates.
    assert_example_matches_schema(AC_TO_TESTS_DRAFT_EXAMPLE, AcceptanceCriteriaTestsDraftResponse)
    assert_example_matches_schema(AC_TO_TESTS_AUDIT_EXAMPLE, AcToTestsAuditResponse)
    _ac_to_tests_draft_dumped = json.loads(AC_TO_TESTS_DRAFT_EXAMPLE.model_dump_json())
    assert _ac_to_tests_draft_dumped["test_suite"]["test_files"], (
        "AC_TO_TESTS_DRAFT_EXAMPLE must have real test_files -- readiness=true with none is this "
        "stage's own documented dominant failure mode"
    )
    _ac_to_tests_audit_dumped = json.loads(AC_TO_TESTS_AUDIT_EXAMPLE.model_dump_json())
    assert _ac_to_tests_audit_dumped["audit_findings"] == {
        "status": "present",
        "values": [
            "US-0001.1's expiry test only checked a token 1 second past expiry; added a "
            "well-past-expiry case too."
        ],
        "reason": "",
    }, "AC_TO_TESTS_AUDIT_EXAMPLE.audit_findings must round-trip with real status/values keys"

    # known_gaps is one of three PresenceList-wrapped fields on this module's schemas -- confirm
    # it dumps a real "status" key, not a stale bare-list shape PresenceList's own before-validator
    # would silently coerce.
    _iteration_dumped = json.loads(MINIMAL_CODE_TO_GREEN_DRAFT_EXAMPLE.iteration.model_dump_json())
    assert "status" in _iteration_dumped["known_gaps"], "iteration.known_gaps missing 'status'"

    # Task 14: AcToTestsAuditResponse.audit_findings / MinimalCodeToGreenAuditResponse.
    # audit_findings -- both were bare list[str] until now, same fix already applied to
    # SpecificationAuditResponse/PlanAuditResponse.audit_findings (schemas.py). Same three-part
    # proof as every other PresenceList field in this codebase: present/absent both validate,
    # a bare list still legacy-coerces, and the wired example round-trips to the real typed shape.
    _revised_suite = AcceptanceCriteriaTestSuite(summary="all ACs covered")
    ac_to_tests_audit_present = AcToTestsAuditResponse(
        revised_test_suite=_revised_suite,
        audit_findings=PresenceList(status="present", values=["AC-0001.1 test was missing an edge case"]),
    )
    assert ac_to_tests_audit_present.audit_findings.values == ["AC-0001.1 test was missing an edge case"]
    ac_to_tests_audit_legacy = AcToTestsAuditResponse.model_validate(
        {"revised_test_suite": _revised_suite.model_dump(), "audit_findings": ["legacy bare finding"]}
    )
    assert ac_to_tests_audit_legacy.audit_findings.status == "present"
    assert ac_to_tests_audit_legacy.audit_findings.values == ["legacy bare finding"]
    ac_to_tests_audit_legacy_empty = AcToTestsAuditResponse.model_validate(
        {"revised_test_suite": _revised_suite.model_dump(), "audit_findings": []}
    )
    assert ac_to_tests_audit_legacy_empty.audit_findings.status == "absent"
    assert ac_to_tests_audit_legacy_empty.audit_findings.reason == "legacy sidecar, pre-typed-absence"

    minimal_audit_present = MinimalCodeToGreenAuditResponse(
        revised_iteration=_iteration(),
        audit_findings=PresenceList(status="present", values=["missed the second ChangedFile's related_ac_ids"]),
    )
    assert minimal_audit_present.audit_findings.values == ["missed the second ChangedFile's related_ac_ids"]
    minimal_audit_legacy = MinimalCodeToGreenAuditResponse.model_validate(
        {"revised_iteration": _iteration().model_dump(), "audit_findings": None}
    )
    assert minimal_audit_legacy.audit_findings.status == "absent"
    assert minimal_audit_legacy.audit_findings.reason == "legacy sidecar, pre-typed-absence"

    # (AcToTestsAuditResponse's own round-trip is now proven directly against the REAL wired
    # AC_TO_TESTS_AUDIT_EXAMPLE above, not a throwaway dummy instance.)
    _minimal_audit_dumped = json.loads(MINIMAL_CODE_TO_GREEN_AUDIT_EXAMPLE.model_dump_json())
    assert "status" in _minimal_audit_dumped["audit_findings"], (
        "MINIMAL_CODE_TO_GREEN_AUDIT_EXAMPLE.audit_findings missing 'status'"
    )

    print("schemas_codegen self-check: all assertions passed")
