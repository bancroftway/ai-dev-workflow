"""P4 (AC-to-tests) content schemas -- kept separate from schemas.py (spec/plan domain) per the
plan's own module-boundary convention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import ClarifyingQuestion

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
        description="Exact names of skills you invoked with your `skill` tool this turn, and only "
        "those. Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )


class AcToTestsAuditResponse(BaseModel):
    """Second opinion on ac-to-tests-draft's test_suite -- same audit contract as
    MinimalCodeToGreenAuditResponse below (revise the metadata, list findings; this role's session
    is read-only, so a finding that needs a real test-file edit belongs in audit_findings, not
    here)."""

    revised_test_suite: AcceptanceCriteriaTestSuite
    audit_findings: list[str] = Field(default_factory=list)


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
    known_gaps: list[str] = Field(default_factory=list)
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
        description="Exact names of skills you invoked with your `skill` tool this turn, and only "
        "those. Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )


class MinimalCodeToGreenAuditResponse(BaseModel):
    revised_iteration: CodegenIterationResult
    audit_findings: list[str] = Field(default_factory=list)
