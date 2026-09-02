"""Domain model (SPECIFICATION.md Section 4) as Pydantic schemas.

These are used both as LangGraph state content and as the structured-output
target for the drafting nodes' model calls.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator


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
            "Every skill you invoked this turn, by name (e.g. 'test-driven-development'; a plugin "
            "slash command counts -- report its bare name), plus any subagents you launched, "
            "reported as 'agent:<name>'. Report what you ACTUALLY invoked, not what you were "
            "asked to -- this is cross-checked against the session's own skill-invocation log, "
            "and a mismatch is treated as a false report."
        ),
    )


class ClarifyingQuestion(BaseModel):
    id: str = Field(description="Short identifier, stable within the turn that produced it.")
    question: str
    suggested_choices: list[str] = Field(
        default_factory=list,
        description="Optional suggested answers, offered as guidance only.",
    )


class SpecQuestion(BaseModel):
    """One entry in the Specification's durable question ledger (user requirement 2026-08-31:
    every question ever raised is tracked with its resolution, so provenance always traces back
    to the requirements document). The FULL history is re-emitted on every draft -- answered and
    assumed entries included, never dropped."""

    id: str = Field(
        description="Stable question id you assign once (e.g. 'q-empty-title') and copy "
        "character-for-character on every later draft -- never renumber or re-slug a question "
        "that already exists in the ledger."
    )
    question: str
    status: Literal["open", "answered", "assumed"] = Field(
        description="'open' = only the human can decide (forces the clarification pause); "
        "'answered' = the requirements document now answers it; 'assumed' = you chose an "
        "explicit assumption instead (also recorded in `assumptions`)."
    )
    answer: str = Field(
        default="",
        description="For 'answered': the answer, citing the requirements wording that settles "
        "it. For 'assumed': the assumption taken. Empty only while 'open'.",
    )
    suggested_choices: list[str] = Field(default_factory=list)


class AcceptanceCriterion(BaseModel):
    id: str = Field(
        description="Your own placeholder id for this AC in this response (e.g. 'ac-a') -- the "
        "real, stable id is assigned deterministically by the caller from existing_ac_id/None, "
        "never by you; this field is ignored when existing_ac_id is set. Never write a "
        "real-looking id here (US-####.#) unless it is copied from existing_ac_id -- a "
        "placeholder that merely LOOKS real is treated as an attempted renumbering and rejected."
    )
    description: str = Field(description="One specific, testable condition and its expected outcome.")
    existing_ac_id: str | None = Field(
        default=None,
        description="The stable id of the Acceptance Criterion this revises -- copied "
        "CHARACTER-FOR-CHARACTER from your immediately-prior draft or the approved Specification "
        "you were given, never reformatted, never re-derived. The real format is the parent "
        "story's own id plus '.' plus a number, e.g. 'US-0001.1' for the 1st criterion of story "
        "US-0001 -- ALWAYS a 'US-' prefix (never 'AC-'), always the parent story's full "
        "zero-padded number (never 'US-1.1' for 'US-0001.1'). Copy it, do not reconstruct it. "
        "None means this is a genuinely new Acceptance Criterion.",
    )
    deferred: bool = Field(
        default=False,
        description="True when the requirements mark this criterion for a LATER phase ('deferred', "
        "'later', 'do not build yet'): specified and reviewed now, but excluded from this ticket's "
        "build/test scope. Deferral is NOT retirement -- the item stays in the document, parked. "
        "A criterion inside a deferred story is deferred automatically.",
    )
    ui_related: bool = Field(
        default=False,
        description="True when satisfying this criterion involves something the user sees or "
        "interacts with (a screen, a component, layout, client-side behavior) -- False for pure "
        "backend/API/data logic with no visible surface. A deterministic Plan gate demands at "
        "least one wireframe (Wireframe.ac_ids) cite every live ui_related criterion -- set this "
        "honestly, not defensively; marking a backend-only criterion true forces an unneeded "
        "wireframe, and marking a real UI criterion false lets it slip through unreviewed.",
    )


class UserStory(BaseModel):
    id: str = Field(
        description="Your own placeholder id for this story in this response (e.g. 'story-a') -- "
        "the real, stable id is assigned deterministically by the caller from existing_us_id/None, "
        "never by you; this field is ignored when existing_us_id is set. Never write a "
        "real-looking id here (US-####) unless it is copied from existing_us_id -- a placeholder "
        "that merely LOOKS real is treated as an attempted renumbering and rejected."
    )
    title: str
    narrative: str = Field(description='"As a <role>, I want <capability>, so that <benefit>".')
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    existing_us_id: str | None = Field(
        default=None,
        description="The stable id of the User Story this revises -- copied CHARACTER-FOR-"
        "CHARACTER from your immediately-prior draft or the approved Specification you were "
        "given, never reformatted, never re-derived. The real format is always 4-digit "
        "zero-padded, e.g. 'US-0001' (never 'US-1'). Copy it, do not reconstruct it -- never "
        "invented, never a retired id. None means this is a genuinely new User Story. Citing a "
        "currently-DEFERRED story id is how you promote it when the requirements move it into "
        "the build-now scope.",
    )
    deferred: bool = Field(
        default=False,
        description="True when the requirements mark this story for a LATER phase ('deferred', "
        "'later', 'do not build yet', a 'Later' section): still fully specified -- title, "
        "narrative, acceptance criteria -- and shown to the reviewer, but parked: no tests or "
        "code are demanded for it in this ticket, and all of its criteria defer with it. "
        "Deferral is NOT retirement: never put a merely-deferred item in retired_us_ids. When a "
        "later revision moves it into the build-now scope, re-emit it citing its existing id "
        "with deferred=false.",
    )


class Specification(BaseModel):
    title: str
    summary: str
    work_kind: Literal["bug", "feature"] = Field(
        default="feature",
        description="What this ticket's raw requirements actually ask for: 'bug' when the text "
        "reports something broken/regressed/failing in EXISTING behavior (error reports, 'X "
        "stopped working', wrong output); 'feature' for new or changed capability, including "
        "enhancements and chores. Classify from the requirements text itself -- downstream "
        "stages gate a reproduce-first debugging discipline on 'bug', so a misclassified "
        "feature costs a pointless debugging pass and a misclassified bug skips the discipline "
        "the fix depends on.",
    )
    user_stories: list[UserStory] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    questions: list[SpecQuestion] = Field(
        default_factory=list,
        description="The COMPLETE question ledger -- every question ever raised across all "
        "drafts of this ticket with its current status, answered and assumed entries included. "
        "Any 'open' entry routes the draft to the clarification pause instead of the human "
        "gate.",
    )
    attachment_notes: list[str] = Field(
        default_factory=list,
        description="Your own distillation of what each provided attachment (screenshot, "
        "document, etc.) actually showed and how it informed this specification -- one entry per "
        "attachment, in the order given. Empty when no attachments were provided; never invent an "
        "entry for a ticket that had none.",
    )
    retired_ac_ids: list[str] = Field(
        default_factory=list,
        description="Stable ids of Acceptance Criteria (from the ledger/approved Specification you "
        "were given -- copied CHARACTER-FOR-CHARACTER, e.g. 'US-0001.1', always a 'US-' prefix, "
        "never 'AC-') that no longer belong in this specification -- cut, descoped, or superseded. "
        "Name them here explicitly; simply leaving an old AC out of this draft does NOT retire it "
        "(a deterministic gate only ever retires an id you name). Never list an id you're also "
        "revising via existing_ac_id in this same response -- revise or retire, not both.",
    )
    retired_us_ids: list[str] = Field(
        default_factory=list,
        description="Same as retired_ac_ids, for stable User Story ids (e.g. 'US-0001', copied "
        "character-for-character). Retiring a story also retires its still-active acceptance "
        "criteria automatically -- you don't need to repeat them in retired_ac_ids too, though "
        "you may.",
    )


class PlanStep(BaseModel):
    id: str = Field(description="Stable identifier (e.g. PS-1).")
    description: str = Field(description="One concrete action.")
    ac_ids: list[str] = Field(
        default_factory=list,
        description="The ledger Acceptance Criterion ids (US-####.#) this step fulfils, copied "
        "exactly from the approved Specification -- never invented, never a retired id. Empty is "
        "only valid when kind='infrastructure'. A deterministic gate enforces this in both "
        "directions: every step cites live criteria (or is infrastructure), and every criterion "
        "still awaiting delivery is cited by at least one step.",
    )
    kind: Literal["feature", "infrastructure"] = Field(
        default="feature",
        description="'infrastructure' marks scaffolding/tooling/config steps that fulfil no "
        "single criterion; every other step is 'feature' and must cite ac_ids.",
    )
    ui_related: bool = Field(
        default=False,
        description="True when this step changes what the user sees or interacts with (a screen, "
        "a component, layout, styling, client-side behavior) -- False for pure backend/API/data/"
        "infrastructure work with no visible surface. Lets the review UI separate UI-facing work "
        "from the rest at a glance; not gate-enforced against wireframe coverage.",
    )
    removes_ids: list[str] = Field(
        default_factory=list,
        description="Stable ids of RETIRED stories/criteria (from the approved Specification's "
        "retired lists -- copied exactly, never invented, never a live id) whose already-delivered "
        "artifacts this step removes: implementation code, UI screens, navigation links, config. "
        "Only for features that were actually BUILT before being removed -- a feature retired "
        "before any code existed needs no removal step at all. A pure-removal step is "
        "kind='infrastructure' with empty ac_ids; a step may also both fulfil live criteria and "
        "remove retired ones. A deterministic gate rejects a live id here, and demands that every "
        "criterion delivered by an earlier run and retired this round is named by some step's "
        "removes_ids.",
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
    ac_ids: list[str] = Field(
        default_factory=list,
        description="The ledger Acceptance Criterion ids (US-####.#) this screen fulfils, copied "
        "exactly from the approved Specification -- same convention as PlanStep.ac_ids, never "
        "invented, never a retired or deferred id. A reviewer must be able to tell which "
        "requirements this wireframe is evidence for at a glance.",
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
    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
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
    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
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


NonBlankStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
"""A str that Pydantic rejects if empty or all-whitespace after stripping. Use in place of a bare
`str` field anywhere blank is a silent way to mean "I have nothing to say" -- reasons, roots,
anything meant to be read by a human or another gate."""


class PresenceList(BaseModel):
    """Typed replacement for a bare `list[str]` field whenever an empty list is ambiguous: did the
    detector look and find nothing, or did it never look? `status` makes that explicit instead of
    forcing every reader to guess from an empty list alone.
    """

    status: Literal["present", "absent"]
    values: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_bare_list(cls, data: Any) -> Any:
        """Older sidecars/model output stored this as a bare `list[str]` (or `None` for "nothing
        found"). Coerce that shape into the typed one so existing producers aren't broken by this
        field going from a list to an object."""
        if isinstance(data, list):
            if data:
                return {"status": "present", "values": list(data)}
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        if data is None:
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        return data

    @model_validator(mode="after")
    def _validate_presence(self) -> "PresenceList":
        if self.status == "present":
            if not self.values:
                raise ValueError("status='present' requires a non-empty values list.")
        else:  # absent
            if self.values:
                raise ValueError("status='absent' requires an empty values list.")
            if not self.reason.strip():
                raise ValueError("status='absent' requires a non-blank reason.")
        return self


class DotnetStatus(BaseModel):
    """Typed replacement for the `dotnet_detected: bool` / `dotnet_solution_root: str | None` field
    pair. Folds in the low-confidence case `tech_stack_draft.md:9-11` already describes in prose --
    detected but the solution root couldn't be confidently located -- as a first-class state
    instead of an unexplained `None`.
    """

    status: Literal["detected", "not_detected"]
    solution_root: str | None = None
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_dotnet_pair(cls, data: Any) -> Any:
        """Older sidecars/model output stored this as the separate `dotnet_detected` /
        `dotnet_solution_root` fields. Coerce that pair into the typed shape."""
        if isinstance(data, dict) and "dotnet_detected" in data and "status" not in data:
            if data.get("dotnet_detected"):
                root = data.get("dotnet_solution_root")
                return {
                    "status": "detected",
                    "solution_root": root,
                    # A blank root on old data IS the legacy low-confidence case
                    # (TechStack.dotnet_solution_root's own docstring: None means "not confidently
                    # determined") -- no reason was ever recorded for it, so synthesize the same
                    # legacy marker used everywhere else, rather than leaving reason="" and letting
                    # the after-validator reject data that used to load fine.
                    "reason": "" if (root or "").strip() else "legacy sidecar, pre-typed-absence",
                }
            return {
                "status": "not_detected",
                "solution_root": None,
                "reason": "legacy sidecar, pre-typed-absence",
            }
        return data

    @model_validator(mode="after")
    def _validate_dotnet_status(self) -> "DotnetStatus":
        if self.status == "detected":
            if not (self.solution_root or "").strip() and not self.reason.strip():
                raise ValueError(
                    "status='detected' requires either a non-blank solution_root or a non-blank "
                    "reason explaining why the root couldn't be confidently located."
                )
        else:  # not_detected
            if not self.reason.strip():
                raise ValueError("status='not_detected' requires a non-blank reason.")
            if self.solution_root is not None:
                raise ValueError("status='not_detected' requires solution_root=None.")
        return self


class EcosystemRoot(BaseModel):
    """Typed replacement for one entry of the `convention_roots: dict[str, str]` map -- makes
    "this ecosystem isn't present" a first-class state instead of an omitted dict key that every
    reader has to know to check for.
    """

    ecosystem: Literal["node", "python"]
    status: Literal["present", "absent"]
    root: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _validate_ecosystem_root(self) -> "EcosystemRoot":
        if self.status == "present":
            if not self.root.strip():
                raise ValueError("status='present' requires a non-blank root.")
        else:  # absent
            if self.root.strip():
                raise ValueError("status='absent' requires a blank root.")
            if not self.reason.strip():
                raise ValueError("status='absent' requires a non-blank reason.")
        return self


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
    auth_kind: str = Field(
        default="none",
        description="How the app authenticates users, one of: 'entra' (Microsoft Entra ID / "
        "Microsoft.Identity.Web / MSAL), 'google', 'generic-oidc' (any other OpenID Connect "
        "provider), 'custom' (the app checks credentials itself -- ASP.NET Identity, a login form "
        "issuing its own cookie/JWT, a Credentials provider), or 'none' (no sign-in). Drives "
        "whether e2e uses a fake OIDC identity provider (OIDC kinds) or seeded users + the real "
        "login form (custom), and whether the auth-enforcement gate arms.",
    )
    config_inventory: list[str] = Field(
        default_factory=list,
        description="Config keys the app reads that a tester may need to supply values for -- "
        "appsettings section paths ('Section:Key') and code-read keys "
        "(Configuration[...], GetSection, process.env.X). Unioned with a deterministic scan; "
        "the human reviews them and supplies test values on the repo settings page.",
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


if __name__ == "__main__":  # pragma: no cover -- `cd agent && python -m src.schemas`
    # NonBlankStr: rejects blank/whitespace-only, strips what it keeps.
    class _NonBlankStrProbe(BaseModel):
        value: NonBlankStr

    assert _NonBlankStrProbe(value="  hi  ").value == "hi"
    try:
        _NonBlankStrProbe(value="   ")
        raise AssertionError("expected ValidationError for a blank NonBlankStr")
    except ValidationError:
        pass

    # PresenceList: new shape round-trips as given.
    present = PresenceList(status="present", values=["a", "b"])
    assert present.model_dump() == {"status": "present", "values": ["a", "b"], "reason": ""}
    absent = PresenceList(status="absent", reason="checked, none found")
    assert absent.values == []

    # PresenceList: legacy bare-list/None coercion.
    assert PresenceList.model_validate(["x", "y"]).model_dump() == {
        "status": "present",
        "values": ["x", "y"],
        "reason": "",
    }
    for legacy_absent in ([], None):
        coerced = PresenceList.model_validate(legacy_absent)
        assert coerced.status == "absent"
        assert coerced.values == []
        assert coerced.reason == "legacy sidecar, pre-typed-absence"

    try:
        PresenceList(status="present", values=[])
        raise AssertionError("expected ValidationError for present with empty values")
    except ValidationError:
        pass
    try:
        PresenceList(status="absent", values=["x"])
        raise AssertionError("expected ValidationError for absent with non-empty values")
    except ValidationError:
        pass
    try:
        PresenceList(status="absent")
        raise AssertionError("expected ValidationError for absent with blank reason")
    except ValidationError:
        pass

    # DotnetStatus: legacy dotnet_detected/dotnet_solution_root pair coercion, both directions.
    legacy_not_detected = DotnetStatus.model_validate(
        {"dotnet_detected": False, "dotnet_solution_root": None}
    )
    assert legacy_not_detected.status == "not_detected"
    assert legacy_not_detected.solution_root is None
    assert legacy_not_detected.reason == "legacy sidecar, pre-typed-absence"

    legacy_detected = DotnetStatus.model_validate(
        {"dotnet_detected": True, "dotnet_solution_root": "src/Api"}
    )
    assert legacy_detected.status == "detected"
    assert legacy_detected.solution_root == "src/Api"
    assert legacy_detected.reason == ""

    # Real legacy shape: detected=True but root=None (TechStack.dotnet_solution_root's own
    # docstring says None means "not confidently determined") -- must coerce, not crash.
    legacy_detected_no_root = DotnetStatus.model_validate(
        {"dotnet_detected": True, "dotnet_solution_root": None}
    )
    assert legacy_detected_no_root.status == "detected"
    assert legacy_detected_no_root.solution_root is None
    assert legacy_detected_no_root.reason == "legacy sidecar, pre-typed-absence"

    # DotnetStatus: new shape covers the low-confidence case (detected, no root, but a reason).
    low_confidence = DotnetStatus(status="detected", reason="two unrelated .csproj roots found")
    assert low_confidence.solution_root is None

    try:
        DotnetStatus(status="detected")
        raise AssertionError("expected ValidationError for detected with no root and no reason")
    except ValidationError:
        pass
    try:
        DotnetStatus(status="not_detected")
        raise AssertionError("expected ValidationError for not_detected with a blank reason")
    except ValidationError:
        pass
    try:
        DotnetStatus(status="not_detected", solution_root="src", reason="stray root")
        raise AssertionError("expected ValidationError for not_detected with a solution_root")
    except ValidationError:
        pass

    # EcosystemRoot: same present/absent pattern, no legacy coercion (nothing to migrate yet).
    node_present = EcosystemRoot(ecosystem="node", status="present", root="apps/web")
    assert node_present.root == "apps/web"
    python_absent = EcosystemRoot(ecosystem="python", status="absent", reason="no pyproject.toml")
    assert python_absent.root == ""

    try:
        EcosystemRoot(ecosystem="node", status="present", root="")
        raise AssertionError("expected ValidationError for present with a blank root")
    except ValidationError:
        pass
    try:
        EcosystemRoot(ecosystem="python", status="absent", root="apps/api")
        raise AssertionError("expected ValidationError for absent with a non-blank root")
    except ValidationError:
        pass
    try:
        EcosystemRoot(ecosystem="python", status="absent")
        raise AssertionError("expected ValidationError for absent with a blank reason")
    except ValidationError:
        pass

    print("schemas self-check: all assertions passed")
