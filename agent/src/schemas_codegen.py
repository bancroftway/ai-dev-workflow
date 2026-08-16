"""P4 (AC-to-tests) content schemas -- kept separate from schemas.py (spec/plan domain) per the
plan's own module-boundary convention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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


class MinimalCodeToGreenAuditResponse(BaseModel):
    revised_iteration: CodegenIterationResult
    audit_findings: list[str] = Field(default_factory=list)
