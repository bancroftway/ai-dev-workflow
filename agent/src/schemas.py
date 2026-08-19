"""Domain model (SPECIFICATION.md Section 4) as Pydantic schemas.

These are used both as LangGraph state content and as the structured-output
target for the drafting nodes' model calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StageReport(BaseModel):
    """Universal contract: EVERY stage reports through this before it can exit -- gate or no gate.

    LLM-backed stages report it by calling the `report_stage_output` tool that src/stack_runner.py
    registers (a schema-valid call ends the turn; an invalid one is rejected client-side with
    field-level Pydantic errors and the model retries in-session). Deterministic stages construct
    it in Python from what they measured. Either way stack_runner ledgers it before the stage
    returns, so a stage can FAIL but can never go silent -- the failure mode that made 25+ headless
    runs unreadable.

    Stage-specific schemas subclass this and add their own fields (build check: stdout_tail;
    coverage: entries; e2e: port/results_artifact; ...). Fields carry defaults so the legacy
    stage schemas (specification/plan/ac-to-tests, which already have their own proven response
    shapes) can be rebased onto this base without invalidating outputs their models produce today.
    """

    success: bool = Field(default=True, description="Did the work complete as instructed?")
    ready_for_next_stage: bool = Field(
        default=True,
        description="Your own claim that the pipeline can proceed. Telemetry only -- the "
        "deterministic gates, never this field, decide what actually happens next.",
    )
    error: str | None = Field(
        default=None, description="What went wrong. REQUIRED whenever success is false."
    )
    summary: str = Field(default="", description="Short plain account of what you actually did.")
    artifacts: list[str] = Field(
        default_factory=list, description="Repo-relative paths this stage produced or changed."
    )
    skills_invoked: list[str] = Field(
        default_factory=list,
        description=(
            "Every skill you invoked this turn, by name (e.g. 'test-driven-development'). Report "
            "what you ACTUALLY invoked, not what you were asked to -- this is cross-checked "
            "against the session's own skill-invocation log, and a mismatch is treated as a "
            "false report."
        ),
    )


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
    mermaid_source: str = Field(
        description="Complete, valid Mermaid diagram source, including its own type declaration "
        "line (e.g. 'erDiagram', 'flowchart TD'). Node/edge labels containing special characters "
        "(/ ( ) : [ ] { } < > & | , ; #) MUST be double-quoted, e.g. Node[\"/tickers route\"] and "
        "A -->|\"GET /api\"| B -- a bare [/...] is a trapezoid-shape lexical error. Mermaid has "
        "NO backslash escapes: never write \\\" inside a label; for a literal double quote use "
        "#quot; instead."
    )


class Wireframe(BaseModel):
    """A high-fidelity, fully self-contained HTML wireframe for one screen. A deterministic
    post-verify step (gates/diagram_gate.py) rejects external references, scripts, and oversize
    sources, then writes each one to .ai-dev-workflow/plan/wireframes/<screen>.html."""

    screen: str = Field(description="Short, filename-safe screen name (e.g. 'login', 'dashboard').")
    html_source: str = Field(
        description="One complete self-contained HTML page: inline CSS only, system font stack, "
        "CSS shapes/gradients for imagery. NO <script>, no external URLs (no CDN css/js, no web "
        "fonts, no remote images). Keep it under 30 KB."
    )


class ImplementationPlan(BaseModel):
    overview: str
    plan_steps: list[PlanStep] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    diagrams: list[PlanDiagram] = Field(
        default_factory=list,
        description="ER/architecture/user-flow diagrams as needed to make the plan reviewable. "
        "Empty is acceptable for a trivial change.",
    )
    wireframes: list[Wireframe] = Field(
        default_factory=list,
        description="One self-contained high-fidelity HTML wireframe per new or changed screen. "
        "Empty for plans with no user-interface work. At most 6 screens.",
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
    """brownfield-baseline tech-stack detection content model. Deliberately full and typed, never a thin summary --
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


# Raw requirements have no schemas: the human's text is recorded verbatim by the deterministic
# record_raw_requirements_node in graph.py -- no draft, no audit, no structured output.
