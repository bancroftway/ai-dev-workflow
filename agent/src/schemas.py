"""Domain model (SPECIFICATION.md Section 4) as Pydantic schemas.

These are used both as LangGraph state content and as the structured-output
target for the drafting nodes' model calls.
"""

from __future__ import annotations

import json
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


class Specification(BaseModel):
    title: NonBlankStr
    summary: NonBlankStr
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
    assumptions: PresenceList = Field(
        description="Explicit assumptions taken during drafting -- mirrors any question answered "
        "with status='assumed' (see SpecQuestion.status) -- or an explicit absent+reason when "
        "none were needed."
    )
    out_of_scope: PresenceList = Field(
        description="Things explicitly decided NOT to build for this ticket, or an explicit "
        "absent+reason when nothing was excluded."
    )
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

    @model_validator(mode="after")
    def _validate_ac_ids(self) -> "PlanStep":
        """Enforces ac_ids' own docstring rule: empty is only valid for kind='infrastructure' --
        every other step must cite at least one criterion. Infrastructure steps MAY still cite
        ac_ids (this only permits empty for them, never requires it)."""
        if self.kind != "infrastructure" and not self.ac_ids:
            raise ValueError(
                "ac_ids must be non-empty unless kind='infrastructure' -- see ac_ids' own "
                "docstring."
            )
        return self


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


class DiagramPresence(BaseModel):
    """Typed-absence wrapper for `ImplementationPlan.diagrams`, same shape/rules as `PresenceList`
    (this module) but with `values: list[PlanDiagram]` -- PresenceList's own `values` is fixed to
    `list[str]` (Task 1 deliberately avoided a `Generic[T]` wrapper), and a diagram is a structured
    object, not a string. A trivial change with no diagrams states an explicit reason instead of an
    empty list that could equally mean "never considered"."""

    status: Literal["present", "absent"]
    values: list[PlanDiagram] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_bare_list(cls, data: Any) -> Any:
        """Older sidecars/model output stored this as a bare `list[...]` (or `None` for "nothing
        found"). Mirrors PresenceList._coerce_legacy_bare_list exactly -- duplicated here rather
        than shared because the two wrappers' `values` element type differs."""
        if isinstance(data, list):
            if data:
                return {"status": "present", "values": list(data)}
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        if data is None:
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        return data

    @model_validator(mode="after")
    def _validate_presence(self) -> "DiagramPresence":
        if self.status == "present":
            if not self.values:
                raise ValueError("status='present' requires a non-empty values list.")
        else:  # absent
            if self.values:
                raise ValueError("status='absent' requires an empty values list.")
            if not self.reason.strip():
                raise ValueError("status='absent' requires a non-blank reason.")
        return self


class WireframePresence(BaseModel):
    """Typed-absence wrapper for `ImplementationPlan.wireframes` -- same rationale/shape as
    `DiagramPresence` above, `values: list[Wireframe]`."""

    status: Literal["present", "absent"]
    values: list[Wireframe] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_bare_list(cls, data: Any) -> Any:
        """Mirrors PresenceList._coerce_legacy_bare_list exactly -- duplicated here rather than
        shared because this wrapper's `values` element type differs."""
        if isinstance(data, list):
            if data:
                return {"status": "present", "values": list(data)}
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        if data is None:
            return {"status": "absent", "reason": "legacy sidecar, pre-typed-absence"}
        return data

    @model_validator(mode="after")
    def _validate_presence(self) -> "WireframePresence":
        if self.status == "present":
            if not self.values:
                raise ValueError("status='present' requires a non-empty values list.")
        else:  # absent
            if self.values:
                raise ValueError("status='absent' requires an empty values list.")
            if not self.reason.strip():
                raise ValueError("status='absent' requires a non-blank reason.")
        return self


class ImplementationPlan(BaseModel):
    overview: str
    plan_steps: list[PlanStep] = Field(default_factory=list)
    risk_notes: PresenceList = Field(
        description="Risks/tradeoffs called out during planning, or an explicit absent+reason "
        "when none apply."
    )
    diagrams: DiagramPresence = Field(
        description="ER/architecture/user-flow diagrams as needed to make the plan reviewable, "
        "or an explicit absent+reason for a trivial change where one wouldn't add value."
    )
    wireframes: WireframePresence = Field(
        description="One self-contained high-fidelity HTML wireframe per new or changed screen "
        "(at most 6 screens), or an explicit absent+reason for a plan with no user-interface work."
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
    audit_findings: PresenceList = Field(
        description="Gaps found and fixed, or an explicit absent+reason when none were found."
    )


class PlanAuditResponse(BaseModel):
    """Structured output contract for the Plan adversarial-audit node."""

    revised_plan: ImplementationPlan
    audit_findings: PresenceList = Field(
        description="Gaps found and fixed, or an explicit absent+reason when none were found."
    )


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

    root="" is a legitimate `status="present"` value -- it means the repo root itself (same
    convention as the old dict shape and as `_join_root` in preflight_nodes.py, which special-
    cases a falsy root as "join nothing, just use the bare filename"). Unambiguous here in a way
    an omitted dict key never was: `status` itself already says "checked, found present" -- this
    is not "never checked" wearing a blank string.
    """

    ecosystem: Literal["node", "python"]
    status: Literal["present", "absent"]
    root: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _validate_ecosystem_root(self) -> "EcosystemRoot":
        if self.status == "absent":
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

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_shape(cls, data: Any) -> Any:
        """Older on-disk sidecars/model output predate this task's field changes and crash without
        this: `dotnet_detected`/`dotnet_solution_root` (no `dotnet` key existed at all) and
        `convention_roots` as a bare `dict[str, str]` (not `list[EcosystemRoot]`).

        Unlike the six `PresenceList` fields -- whose key NAME never changed, so `PresenceList`'s
        own before-validator fires automatically on old bare-list data under that same key --
        `dotnet` and `convention_roots` have a different key name/container shape in old data, so
        nothing bridges the gap on its own. Reshape here, before Pydantic construction, then let
        `DotnetStatus`'s own before-validator (below) do the actual legacy-pair coercion so the
        mapping isn't duplicated.
        """
        if not isinstance(data, dict):
            return data
        if "dotnet" not in data and (
            "dotnet_detected" in data or "dotnet_solution_root" in data
        ):
            data = {
                **data,
                "dotnet": {
                    "dotnet_detected": data.get("dotnet_detected", False),
                    "dotnet_solution_root": data.get("dotnet_solution_root"),
                },
            }
        roots = data.get("convention_roots")
        if isinstance(roots, dict):
            # Old dict shape never recorded an "absent" ecosystem at all -- it just omitted the
            # key -- so there's nothing to backfill an absent entry from. Only emit what's there.
            data = {
                **data,
                "convention_roots": [
                    {"ecosystem": eco, "status": "present", "root": root, "reason": ""}
                    for eco, root in roots.items()
                ],
            }
        return data

    summary: str = Field(description="One or two sentences describing the stack at a glance.")
    languages: PresenceList = Field(description="Programming languages found evidence for.")
    frameworks: PresenceList = Field(description="Frameworks found evidence for.")
    package_managers: PresenceList = Field(description="Package managers found evidence for.")
    testing_frameworks: PresenceList = Field(description="Testing frameworks found evidence for.")
    conventions: PresenceList = Field(
        description="Observed conventions, each with a short reason."
    )
    dotnet: DotnetStatus = Field(
        description="Whether any .csproj/.sln files were found and, if so, the repo-relative "
        "solution root (where Directory.Build.props belongs), or a reason it couldn't be "
        "confidently determined."
    )
    convention_roots: list[EcosystemRoot] = Field(
        default_factory=list,
        description="One entry per non-.NET ecosystem actually checked -- 'node' (the workspace "
        "root holding package.json) and 'python' (the project root holding "
        "pyproject.toml/setup.cfg/requirements.txt). root is the repo-relative directory where "
        "that ecosystem's shared config file belongs (\"\" for the repository root itself) when "
        "status='present'; a reason when status='absent' (not present, or no single confident "
        "common root). .NET keeps its own top-level `dotnet` field rather than an entry here, "
        "because several pipeline stages already read that field by name.",
    )
    conventions_applied: list[str] = Field(
        default_factory=list,
        description="Which language-specific convention files were actually written this run "
        "(e.g. ['dotnet']) -- populated by the deterministic post_approve_hook after it runs, not "
        "by the model itself, since the model never writes files.",
    )
    auth_kind: Literal["entra", "google", "generic-oidc", "custom", "none"] = Field(
        default="none",
        description="How the app authenticates users: 'entra' (Microsoft Entra ID / "
        "Microsoft.Identity.Web / MSAL), 'google', 'generic-oidc' (any other OpenID Connect "
        "provider), 'custom' (the app checks credentials itself -- ASP.NET Identity, a login form "
        "issuing its own cookie/JWT, a Credentials provider), or 'none' (no sign-in). Drives "
        "whether e2e uses a fake OIDC identity provider (OIDC kinds) or seeded users + the real "
        "login form (custom), and whether the auth-enforcement gate arms.",
    )
    config_inventory: PresenceList = Field(
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


TECH_STACK_DRAFT_EXAMPLE: TechStackDraftResponse = TechStackDraftResponse(
    readiness=True,
    clarifying_questions=[],
    tech_stack=TechStack(
        summary="Small Node/TypeScript API service using Express, tested with Jest; no .NET or "
        "Python components detected.",
        languages=PresenceList(status="present", values=["TypeScript", "JavaScript"]),
        frameworks=PresenceList(status="present", values=["Express"]),
        package_managers=PresenceList(status="present", values=["npm"]),
        testing_frameworks=PresenceList(status="present", values=["Jest"]),
        conventions=PresenceList(
            status="present",
            values=[
                "ESLint + Prettier enforced via a pre-commit hook",
                "Path aliases configured in tsconfig.json",
            ],
        ),
        dotnet=DotnetStatus(
            status="not_detected", reason="No .csproj or .sln files found in the repository."
        ),
        convention_roots=[
            EcosystemRoot(ecosystem="node", status="present", root=""),
            EcosystemRoot(
                ecosystem="python",
                status="absent",
                reason="No pyproject.toml, setup.cfg, or requirements.txt found.",
            ),
        ],
        conventions_applied=[],
        auth_kind="none",
        config_inventory=PresenceList(status="present", values=["PORT", "DATABASE_URL"]),
    ),
)
"""Fully-populated example of the tech-stack drafting node's structured output, echoed into the
draft prompt (Task 7) so the model sees a realistic instance of the current canonical shape."""


TECH_STACK_EXTRACT_EXAMPLE: TechStack = TechStack(
    summary="ASP.NET Core web API secured with Microsoft Entra ID, with a small set of Python "
    "data-migration scripts alongside the .NET solution.",
    languages=PresenceList(status="present", values=["C#", "Python"]),
    frameworks=PresenceList(status="present", values=["ASP.NET Core", "Entity Framework Core"]),
    package_managers=PresenceList(status="present", values=["NuGet", "pip"]),
    testing_frameworks=PresenceList(status="present", values=["xUnit"]),
    conventions=PresenceList(
        status="present",
        values=[
            "Repository pattern for data access",
            "Feature-folder organization under src/Api",
        ],
    ),
    dotnet=DotnetStatus(status="detected", solution_root="src"),
    convention_roots=[
        EcosystemRoot(
            ecosystem="node",
            status="absent",
            reason="No package.json found anywhere in the repository.",
        ),
        EcosystemRoot(ecosystem="python", status="present", root="scripts"),
    ],
    conventions_applied=[],
    auth_kind="entra",
    config_inventory=PresenceList(
        status="present",
        values=["AzureAd:TenantId", "AzureAd:ClientId", "ConnectionStrings:Db"],
    ),
)
"""Fully-populated example of the plain TechStack shape (no draft wrapper), for the
extraction-only prompt path that reads approved markdown back into this same schema."""


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

    # present + root="" is the repo-root-itself case (same convention as the old dict shape and
    # _join_root in preflight_nodes.py) -- unambiguous once status itself says "checked, present",
    # so this must validate, not raise.
    root_level_present = EcosystemRoot(ecosystem="node", status="present", root="")
    assert root_level_present.root == ""
    root_level_present_with_reason = EcosystemRoot(
        ecosystem="node", status="present", root="", reason="workspace root is the repo root"
    )
    assert root_level_present_with_reason.root == ""

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

    # TechStack examples: validate, AND actually use the current canonical (typed) shape --
    # "validates" alone doesn't prove this, since PresenceList's own before-validator would
    # silently coerce a stale bare-list/dict example back into the typed shape.
    _presence_fields = (
        "languages",
        "frameworks",
        "package_managers",
        "testing_frameworks",
        "conventions",
        "config_inventory",
    )
    for _label, _stack in (
        ("TECH_STACK_DRAFT_EXAMPLE.tech_stack", TECH_STACK_DRAFT_EXAMPLE.tech_stack),
        ("TECH_STACK_EXTRACT_EXAMPLE", TECH_STACK_EXTRACT_EXAMPLE),
    ):
        assert _stack is not None, f"{_label} is missing a tech_stack"
        # Round-trip through JSON to prove the example is really a validated instance of the
        # CURRENT schema, not just a Python object built to look like one.
        _reloaded = TechStack.model_validate_json(_stack.model_dump_json())
        assert _reloaded == _stack, f"{_label} did not round-trip through model_validate_json"

        _dumped = json.loads(_stack.model_dump_json())
        for _field in _presence_fields:
            assert "status" in _dumped[_field], f"{_label}.{_field} missing 'status' key"
        assert "status" in _dumped["dotnet"], f"{_label}.dotnet missing 'status' key"
        assert _dumped["convention_roots"], f"{_label}.convention_roots is empty"
        for _root in _dumped["convention_roots"]:
            assert "status" in _root, f"{_label}.convention_roots entry missing 'status' key"

    assert TechStackDraftResponse.model_validate_json(
        TECH_STACK_DRAFT_EXAMPLE.model_dump_json()
    ) == TECH_STACK_DRAFT_EXAMPLE

    # Nested $defs/$refs: TechStack's schema now nests PresenceList/DotnetStatus/EcosystemRoot
    # instead of flat arrays -- confirm the schema actually reflects that (a regression guard,
    # not a live end-to-end check of the CLI/model consumers -- see task-2-report.md).
    _ts_schema = TechStack.model_json_schema()
    assert "$defs" in _ts_schema, "TechStack.model_json_schema() lost its nested $defs"
    assert json.dumps(_ts_schema), "TechStack.model_json_schema() failed to json.dumps"

    # Full legacy on-disk TechStack shape (both the old dotnet_detected/dotnet_solution_root pair
    # AND a dict-shaped convention_roots, together) must not crash -- preflight_nodes.py:595 loads
    # exactly this shape from real cached JSON today. The six PresenceList-wrapped fields already
    # coerce for free (key name unchanged), so only those two fields are exercised here.
    _legacy_stack = TechStack.model_validate(
        {
            "summary": "legacy sidecar",
            "languages": ["Python"],
            "frameworks": [],
            "package_managers": ["pip"],
            "testing_frameworks": [],
            "conventions": [],
            "dotnet_detected": False,
            "dotnet_solution_root": None,
            "convention_roots": {"python": "src"},
            "conventions_applied": [],
            "auth_kind": "none",
            "config_inventory": [],
        }
    )
    assert _legacy_stack.dotnet.status == "not_detected"
    assert _legacy_stack.dotnet.solution_root is None
    assert [r.model_dump() for r in _legacy_stack.convention_roots] == [
        {"ecosystem": "python", "status": "present", "root": "src", "reason": ""}
    ]

    _legacy_stack_dotnet_detected = TechStack.model_validate(
        {
            "summary": "legacy sidecar, dotnet detected",
            "languages": [],
            "frameworks": [],
            "package_managers": [],
            "testing_frameworks": [],
            "conventions": [],
            "dotnet_detected": True,
            "dotnet_solution_root": "src/Api",
            "convention_roots": {},
            "conventions_applied": [],
            "auth_kind": "none",
            "config_inventory": [],
        }
    )
    assert _legacy_stack_dotnet_detected.dotnet.status == "detected"
    assert _legacy_stack_dotnet_detected.dotnet.solution_root == "src/Api"
    assert _legacy_stack_dotnet_detected.convention_roots == []

    # NonBlankStr on Specification.title/summary: rejects blank/whitespace-only.
    _ok_spec_kwargs: dict[str, Any] = dict(
        title="A title",
        summary="A summary",
        assumptions=PresenceList(status="absent", reason="no assumptions were needed"),
        out_of_scope=PresenceList(status="absent", reason="nothing was excluded"),
    )
    assert Specification(**_ok_spec_kwargs).title == "A title"
    for _blank_field in ("title", "summary"):
        try:
            Specification(**{**_ok_spec_kwargs, _blank_field: "   "})
            raise AssertionError(f"expected ValidationError for blank Specification.{_blank_field}")
        except ValidationError:
            pass

    # Specification.assumptions/out_of_scope: real PresenceList fields -- present/absent both
    # validate, and a bare list still coerces (legacy sidecars/model output).
    _spec_present = Specification(
        title="T", summary="S",
        assumptions=PresenceList(status="present", values=["assume X"]),
        out_of_scope=["Y is out of scope"],  # legacy bare-list coercion
    )
    assert _spec_present.assumptions.values == ["assume X"]
    assert _spec_present.out_of_scope.status == "present"
    assert _spec_present.out_of_scope.values == ["Y is out of scope"]
    try:
        Specification(title="T", summary="S", assumptions=[], out_of_scope=[])
        # A bare empty list legacy-coerces to status="absent" with a synthesized reason -- this
        # must NOT raise, unlike an explicit PresenceList(status="absent") with no reason.
    except ValidationError:
        raise AssertionError("expected a bare empty list to legacy-coerce, not raise")

    # PlanStep.ac_ids: the model_validator(mode="after") enforcing "empty only valid when
    # kind='infrastructure'" -- both branches.
    assert PlanStep(id="PS-1", description="wire CI", ac_ids=[], kind="infrastructure").ac_ids == []
    # infrastructure steps MAY still cite ac_ids (permitted, never required).
    assert PlanStep(id="PS-1", description="x", ac_ids=["US-0001.1"], kind="infrastructure").ac_ids == ["US-0001.1"]
    try:
        PlanStep(id="PS-1", description="build it", ac_ids=[], kind="feature")
        raise AssertionError("expected ValidationError for empty ac_ids on a non-infrastructure step")
    except ValidationError:
        pass
    # kind defaults to 'feature' -- the default-kind path must reject empty ac_ids too.
    try:
        PlanStep(id="PS-1", description="build it", ac_ids=[])
        raise AssertionError("expected ValidationError for empty ac_ids with default kind")
    except ValidationError:
        pass

    # DiagramPresence/WireframePresence: same present/absent/legacy-coercion contract as
    # PresenceList, but values are structured PlanDiagram/Wireframe objects.
    _diagram = PlanDiagram(name="core-er", kind="er", mermaid_source="erDiagram\n  A ||--o{ B : has")
    _wireframe = Wireframe(screen="login", html_source="<html><body><div>Login</div></body></html>")

    present_diagrams = DiagramPresence(status="present", values=[_diagram])
    assert present_diagrams.values == [_diagram]
    absent_diagrams = DiagramPresence(status="absent", reason="trivial change, no diagram needed")
    assert absent_diagrams.values == []

    coerced_diagrams = DiagramPresence.model_validate([_diagram.model_dump()])
    assert coerced_diagrams.status == "present" and coerced_diagrams.values == [_diagram]
    for _legacy_absent in ([], None):
        _coerced = DiagramPresence.model_validate(_legacy_absent)
        assert _coerced.status == "absent"
        assert _coerced.values == []
        assert _coerced.reason == "legacy sidecar, pre-typed-absence"
    try:
        DiagramPresence(status="present", values=[])
        raise AssertionError("expected ValidationError for present with empty values")
    except ValidationError:
        pass
    try:
        DiagramPresence(status="absent", values=[_diagram])
        raise AssertionError("expected ValidationError for absent with non-empty values")
    except ValidationError:
        pass
    try:
        DiagramPresence(status="absent")
        raise AssertionError("expected ValidationError for absent with blank reason")
    except ValidationError:
        pass

    present_wireframes = WireframePresence(status="present", values=[_wireframe])
    assert present_wireframes.values == [_wireframe]
    absent_wireframes = WireframePresence(status="absent", reason="no UI work in this plan")
    assert absent_wireframes.values == []

    coerced_wireframes = WireframePresence.model_validate([_wireframe.model_dump()])
    assert coerced_wireframes.status == "present" and coerced_wireframes.values == [_wireframe]
    for _legacy_absent in ([], None):
        _coerced = WireframePresence.model_validate(_legacy_absent)
        assert _coerced.status == "absent"
        assert _coerced.values == []
        assert _coerced.reason == "legacy sidecar, pre-typed-absence"
    try:
        WireframePresence(status="present", values=[])
        raise AssertionError("expected ValidationError for present with empty values")
    except ValidationError:
        pass
    try:
        WireframePresence(status="absent", values=[_wireframe])
        raise AssertionError("expected ValidationError for absent with non-empty values")
    except ValidationError:
        pass
    try:
        WireframePresence(status="absent")
        raise AssertionError("expected ValidationError for absent with blank reason")
    except ValidationError:
        pass

    # ImplementationPlan.risk_notes/diagrams/wireframes: a fully-populated, present-everywhere
    # instance round-trips, and an empty-everywhere (absent+reason) instance validates too.
    _plan_present = ImplementationPlan(
        overview="Add password reset.",
        plan_steps=[PlanStep(id="PS-1", description="build it", ac_ids=["US-0001.1"])],
        risk_notes=PresenceList(status="present", values=["email deliverability is unverified"]),
        diagrams=DiagramPresence(status="present", values=[_diagram]),
        wireframes=WireframePresence(status="present", values=[_wireframe]),
    )
    assert _plan_present.risk_notes.values == ["email deliverability is unverified"]
    assert _plan_present.diagrams.values == [_diagram]
    assert _plan_present.wireframes.values == [_wireframe]
    _reloaded_plan = ImplementationPlan.model_validate_json(_plan_present.model_dump_json())
    assert _reloaded_plan == _plan_present

    _plan_absent = ImplementationPlan(
        overview="Trivial copy fix.",
        plan_steps=[PlanStep(id="PS-1", description="fix copy", ac_ids=[], kind="infrastructure")],
        risk_notes=PresenceList(status="absent", reason="no risks identified"),
        diagrams=DiagramPresence(status="absent", reason="trivial change, no diagram needed"),
        wireframes=WireframePresence(status="absent", reason="no UI work in this plan"),
    )
    assert _plan_absent.diagrams.status == "absent"

    # SpecificationAuditResponse/PlanAuditResponse.audit_findings: now a real PresenceList, not a
    # bare list[str] -- present/absent both validate.
    _spec_for_audit = Specification(
        title="T", summary="S",
        assumptions=PresenceList(status="absent", reason="none needed"),
        out_of_scope=PresenceList(status="absent", reason="none"),
    )
    spec_audit_clean = SpecificationAuditResponse(
        revised_specification=_spec_for_audit,
        audit_findings=PresenceList(status="absent", reason="no gaps found"),
    )
    assert spec_audit_clean.audit_findings.values == []
    spec_audit_findings = SpecificationAuditResponse(
        revised_specification=_spec_for_audit,
        audit_findings=PresenceList(status="present", values=["AC US-0001.1 was untestable as written"]),
    )
    assert spec_audit_findings.audit_findings.status == "present"

    plan_audit_clean = PlanAuditResponse(
        revised_plan=_plan_absent,
        audit_findings=PresenceList(status="absent", reason="no gaps found"),
    )
    assert plan_audit_clean.audit_findings.values == []
    # Legacy bare-list audit_findings must still coerce (older sidecars/model output).
    plan_audit_legacy = PlanAuditResponse.model_validate(
        {"revised_plan": _plan_absent.model_dump(), "audit_findings": ["fixed a missing AC citation"]}
    )
    assert plan_audit_legacy.audit_findings.status == "present"
    assert plan_audit_legacy.audit_findings.values == ["fixed a missing AC citation"]

    print("schemas self-check: all assertions passed")
