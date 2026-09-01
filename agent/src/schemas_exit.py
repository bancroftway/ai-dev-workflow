from __future__ import annotations

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion


class MergeReadinessReport(BaseModel):
    merge_ready: bool
    blocking_reasons: list[str] = Field(default_factory=list, description="Empty if merge_ready=True.")
    pr_title: str
    pr_description_markdown: str
    risk_notes: list[str] = Field(default_factory=list)
    suggested_reviewers_note: str = ""


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


