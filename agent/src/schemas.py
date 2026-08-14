"""Domain model (SPECIFICATION.md Section 4) as Pydantic schemas.

These are used both as LangGraph state content and as the structured-output
target for the drafting nodes' model calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClarifyingQuestion(BaseModel):
    id: str = Field(description="Short identifier, stable within the turn that produced it.")
    question: str
    suggested_choices: list[str] = Field(
        default_factory=list,
        description="Optional suggested answers, offered as guidance only.",
    )


class AcceptanceCriterion(BaseModel):
    id: str = Field(
        description="Your own placeholder id for this AC in this response (e.g. AC-1.1) -- the "
        "real, stable id is assigned deterministically by the caller from existing_ac_id/None, "
        "never by you; this field is ignored when existing_ac_id is set."
    )
    description: str = Field(description="One specific, testable condition and its expected outcome.")
    existing_ac_id: str | None = Field(
        default=None,
        description="The stable AC-####.# id (from your immediately-prior draft or the approved "
        "Specification you were given) that this AC revises, exactly as given to you -- never "
        "invented, never a retired id. None means this is a genuinely new Acceptance Criterion.",
    )


class UserStory(BaseModel):
    id: str = Field(
        description="Your own placeholder id for this story in this response (e.g. US-1) -- the "
        "real, stable id is assigned deterministically by the caller from existing_us_id/None, "
        "never by you; this field is ignored when existing_us_id is set."
    )
    title: str
    narrative: str = Field(description='"As a <role>, I want <capability>, so that <benefit>".')
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    existing_us_id: str | None = Field(
        default=None,
        description="The stable US-#### id (from your immediately-prior draft or the approved "
        "Specification you were given) that this story revises, exactly as given to you -- never "
        "invented, never a retired id. None means this is a genuinely new User Story.",
    )


class Specification(BaseModel):
    title: str
    summary: str
    user_stories: list[UserStory] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    id: str = Field(description="Stable identifier (e.g. PS-1).")
    description: str = Field(
        description="One concrete action. Reference fulfilled Acceptance Criteria ids where meaningful."
    )


class PlanDiagram(BaseModel):
    """A diagram authored as Mermaid source text, not driven interactively -- a deterministic
    post-verify step (not this model) renders it to SVG via the mermaid CLI and validates the
    syntax in the process; a render failure routes back to this draft with the exact error."""

    name: str = Field(description="Short, filename-safe name (e.g. 'password-reset-er').")
    kind: str = Field(description="One of: er, architecture, user_flow.")
    mermaid_source: str = Field(description="Complete, valid Mermaid diagram source, including its own type declaration line (e.g. 'erDiagram', 'flowchart TD').")


class ImplementationPlan(BaseModel):
    overview: str
    plan_steps: list[PlanStep] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    diagrams: list[PlanDiagram] = Field(
        default_factory=list,
        description="ER/architecture/user-flow diagrams as needed to make the plan reviewable. "
        "Empty is acceptable for a trivial change.",
    )


class SpecificationDraftResponse(BaseModel):
    """Structured output contract for the Specification drafting node."""

    readiness: bool = Field(
        description="True if this draft is complete enough to present for human review."
    )
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    specification: Specification | None = Field(
        default=None, description="Present whenever a draft was produced, ready or not."
    )


class PlanDraftResponse(BaseModel):
    """Structured output contract for the Plan drafting node."""

    readiness: bool = Field(
        description="True if this draft is complete enough to present for human review."
    )
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    plan: ImplementationPlan | None = Field(
        default=None, description="Present whenever a draft was produced, ready or not."
    )


class SpecificationAuditResponse(BaseModel):
    """Structured output contract for the Specification adversarial-audit node."""

    revised_specification: Specification
    audit_findings: list[str] = Field(
        default_factory=list, description="Gaps found and fixed. Empty if none were found."
    )


class PlanAuditResponse(BaseModel):
    """Structured output contract for the Plan adversarial-audit node."""

    revised_plan: ImplementationPlan
    audit_findings: list[str] = Field(
        default_factory=list, description="Gaps found and fixed. Empty if none were found."
    )


class TechStack(BaseModel):
    """P0 tech-stack detection content model. Deliberately full and typed, never a thin summary --
    this is what render_tech_stack_markdown writes tech-stack.md from (the sole writer, exactly
    like specification.md/plan.md), and what the tech-stack-conventions skill's analysis (invoked
    by name from the draft prompt, itself never writing files) is reported into."""

    summary: str = Field(description="One or two sentences describing the stack at a glance.")
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    testing_frameworks: list[str] = Field(default_factory=list)
    conventions: list[str] = Field(
        default_factory=list, description="Observed conventions, each with a short reason."
    )
    dotnet_detected: bool = Field(default=False, description="Any .csproj/.sln files found.")
    dotnet_solution_root: str | None = Field(
        default=None,
        description="Repo-relative path to the common ancestor of all .csproj files (where "
        "Directory.Build.props belongs), or None if not confidently determined.",
    )
    convention_roots: dict[str, str] = Field(
        default_factory=dict,
        description="Repo-relative directory where each non-.NET ecosystem's shared config file "
        "belongs, keyed by ecosystem: 'node' (the workspace root holding package.json) and "
        "'python' (the project root holding pyproject.toml/setup.cfg/requirements.txt). Use \"\" "
        "for the repository root itself. Omit a key entirely when the ecosystem isn't present or "
        "no confident common root exists -- a wrong root is worse than a missing one. .NET keeps "
        "its own dotnet_solution_root field above rather than a key here, because several "
        "pipeline stages already read that field by name.",
    )
    conventions_applied: list[str] = Field(
        default_factory=list,
        description="Which language-specific convention files were actually written this run "
        "(e.g. ['dotnet']) -- populated by the deterministic post_audit_hook after it runs, not "
        "by the model itself, since the model never writes files.",
    )


class TechStackDraftResponse(BaseModel):
    """Structured output contract for the tech-stack drafting node."""

    readiness: bool = Field(
        description="True if this draft is complete enough to present for human review."
    )
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    tech_stack: TechStack | None = Field(
        default=None, description="Present whenever a draft was produced, ready or not."
    )


class TechStackAuditResponse(BaseModel):
    """Structured output contract for the tech-stack adversarial-audit node -- re-verifies
    reported fields against the actual files on disk (catches a wrongly-placed solution root or
    a hallucinated dotnet_detected)."""

    revised_tech_stack: TechStack
    audit_findings: list[str] = Field(
        default_factory=list, description="Gaps found and fixed. Empty if none were found."
    )


class RawRequirementsDocument(BaseModel):
    """P1 content model: the single evergreen requirements document -- the ONLY human-editable
    input to the whole pipeline. Deliberately one free-form field, not a structured wishlist --
    turning prose into User Stories/Acceptance Criteria is P2's job, not P1's."""

    content: str = Field(description="The full requirements document, in Markdown.")


class RawRequirementsDraftResponse(BaseModel):
    """Structured output contract for the raw-requirements drafting node."""

    readiness: bool = Field(
        description="True if this draft is complete enough to present for human review."
    )
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    raw_requirements: RawRequirementsDocument | None = Field(
        default=None, description="Present whenever a draft was produced, ready or not."
    )


class RawRequirementsAuditResponse(BaseModel):
    """Structured output contract for the raw-requirements adversarial-audit node."""

    revised_raw_requirements: RawRequirementsDocument
    audit_findings: list[str] = Field(
        default_factory=list, description="Gaps found and fixed. Empty if none were found."
    )
