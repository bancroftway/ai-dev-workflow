"""The LangGraph workflow graph (SPECIFICATION.md Section 3.2, Section 5).

Built from a data-driven `STAGES` list (Decision 6 / BR-7 extensibility):
appending a future third stage means adding one more `StageSpec` entry, not
restructuring the nodes/edges below. Every stage gets the same generated
graph segment: draft -> (gate | needs_clarification | auto_approve) -> next
stage's draft (or END for the last stage).

Every run (initial submission or any later revision) enters at `intake` and
unconditionally proceeds to the Specification stage's draft node (AC-6.2),
regardless of which stage/gate a prior run left paused at — a fresh
`.invoke()`/`.astream()` on the same thread simply starts a new super-step
from the entry point; any interrupt left open from a previous run is never
resumed and is abandoned by construction (BR-4's cascade).
"""

from __future__ import annotations

import inspect
import importlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError
from langgraph.graph.message import add_messages
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt

from . import app_discovery
from . import approvals
from . import config as workflow_config
from . import git_ops
from . import model_config
from . import preflight_nodes
from . import repo_files
from . import repo_scan
from . import tech_stack_signals
from . import test_hardening_nodes
from . import e2e_nodes
from . import metrics_nodes
from . import exit_nodes
from . import rebuild
from . import run_event_store
from . import run_event_stream
from .run_events import RunEvent, RunEventType
from . import run_failure
from . import session_store
from . import spec_ledger
from . import telemetry
from . import workflow_persistence
from .custom_agent_loader import load_agent_for_stage
from .gates import adversarial_gate, remediation_gate, skill_gate
from .gates.ac_coverage_gate import MAX_TEST_BODY_SIMILARITY
from .gates.diagram_gate import verify_plan_diagrams
from .gates.test_coverage_gate import verify_coverage
from .gates.write_scope_gate import verify_ac_to_tests
from .infra_retry import call_with_infra_retry
from .a2ui_tools import (
    build_ac_to_tests_envelope,
    build_adversarial_audit_envelope,
    build_exit_envelope,
    build_minimal_code_to_green_envelope,
    build_brownfield_baseline_envelope,
    build_plan_envelope,
    build_specification_envelope,
    build_tech_stack_envelope,
    present_surface_messages,
)
from . import chat_model
from .chat_model import ainvoke_structured, close_session, close_thread_session, get_chat_model_for_thread
from .markdown_render import (
    render_ac_to_tests_markdown,
    render_adversarial_audit_markdown,
    render_minimal_code_to_green_markdown,
    render_brownfield_baseline_markdown,
    render_plan_markdown,
    render_raw_requirements_markdown,
    render_specification_markdown,
    render_tech_stack_markdown,
)
from .prompt_loader import load_prompt, load_prompt_pair, render_prompt
from . import template_loader
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider, SandboxSession
from .schemas import (
    PlanAuditResponse,
    PlanDraftResponse,
    SpecificationAuditResponse,
    SpecificationDraftResponse,
    TechStack,
    TechStackDraftResponse,
)
from .schemas_codegen import (
    AcceptanceCriteriaTestsDraftResponse,
    AcToTestsAuditResponse,
    MinimalCodeToGreenAuditResponse,
    MinimalCodeToGreenDraftResponse,
)
from .schemas_audit import (
    AdversarialAuditDraftResponse,
)
from .schemas_brownfield import BrownfieldBaselineDraftResponse
from .schemas_exit import ExitDraftResponse
from .schemas_remediation import RemediationDraftResponse

logger = logging.getLogger(__name__)


StageStatus = Literal[
    "not_started", "drafting", "needs_clarification", "ready_for_review", "approved"
]


class StageState(TypedDict):
    status: StageStatus
    draft: dict[str, Any] | None
    clarifying_questions: list[dict[str, Any]]
    readiness: bool
    cycle_count: int
    approved_content: dict[str, Any] | None
    ever_ready_for_review: bool
    used_ids: list[str]
    audit_findings: list[str]
    # Independent of cycle_count (the LLM's own clarification-loop counter) -- tracks retries
    # through a StageSpec.deterministic_verify gate, when one is set (unused by specification/plan).
    verify_cycle_count: int
    last_verification: dict[str, Any] | None
    # Set by make_gate_node on a human REJECTION (Part 2 Task 10, Ruling 3) -- deliberately its
    # own field, not a reuse of last_verification/last_verify_feedback above: those two belong to
    # the deterministic_verify retry/stall machinery (make_verify_node/make_route_after_verify/
    # should_skip_draft all read them for that purpose, for specification/plan too, which already
    # have a deterministic_verify of their own), and AppShell's stageGroupDot paints a tab's dot
    # red when last_verification says failed -- a human politely asking for a revision is not the
    # same signal as a script-detected failure, and must not light that same error dot. Folded
    # into the next draft prompt (_build_tech_stack_prompt/_build_specification_prompt/
    # _build_plan_prompt) as reviewer guidance; cleared back to None the moment the stage is
    # actually approved. Only ever set for the 3 requires_human_gate=True stages.
    reviewer_feedback: str | None
    # Set once per run by make_draft_node when StageSpec.capture_baseline_commit is True (P4's
    # write-scope gate); None for every other stage.
    baseline_commit: str | None
    skills: dict[str, Any] | None
    # Draft-level infra exhaustion (make_draft_node's infra_retry attempts all lost to a Copilot
    # quota/timeout failure) -- routed by make_route_after_draft straight to make_draft_escalate_node,
    # bypassing cycle_count entirely. See infra_retry.py's module docstring.
    infra_exhausted: bool
    last_infra_error: str | None
    # make_verify_node's stall-detector: near-identical feedback / zero changed files / no
    # improvement in reported coverage across config.VERIFY_STALL_LAPS consecutive verify laps
    # resets the draft session, on top of the existing fabrication/skipped-skill triggers.
    last_verify_feedback: str | None
    last_verify_changed_paths: list[str] | None
    verify_stall_count: int
    # minimal-code-to-green never sets capture_baseline_commit, so last_verify_changed_paths is
    # always None there -- best_verify_coverage_rate is the heuristic that actually fires for that
    # stage's documented failure shape (oscillating branch coverage across laps, not "zero files
    # changed" -- files WERE changed each lap in the observed incident, just not converging).
    best_verify_coverage_rate: float | None


class GraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # Minted fresh by intake_node on every genuinely new run (module docstring's definition of a
    # "run" -- not regenerated across a gate-approval resume, since those don't re-enter intake).
    # Used as the ledger/approvals systems' run identifier (spec_ledger.py's
    # first_seen_run_id/last_revised_run_id, approvals.py's ApprovalRecord.run_id) and, later,
    # metrics-report/exit's history/<run_id>-*.json snapshot naming.
    run_id: str
    # The org-wide provider ("copilot"/"claude") this THREAD is pinned to, resolved from the live
    # org setting via `await chat_model.get_provider()` -- Ruling 2,
    # docs/superpowers/plans/part-4-org-settings-tasks.md: every later graph node reads this key
    # instead of calling get_provider() again, so an admin flipping the org-wide setting mid-run can
    # never split one pipeline execution across two providers' sessions/CLIs (a real hazard: session
    # caches, tool allowlists and CLI transcript formats all differ per provider).
    #
    # Deliberately UNLIKE run_id just above: run_id reMINTS on every intake_node execution
    # (including a same-thread "later revision" or a blank-resume reattach -- see run_id's own
    # comment), but provider must not. intake_node computes `state.get("provider") or
    # (await session_store.get_session(thread_id) or {}).get("provider") or await
    # chat_model.get_provider()` -- preserve-if-already-set, THEN prefer this session's own stored
    # row, resolve live only if genuinely neither exists -- specifically because AppShell's blank
    # runAgent()-on-mount reattach (intake_node's own is_new_submission dance; also AIDW_RESUME
    # headless resume) re-enters intake_node exactly the same as a fresh submission does, and that
    # path is by far the most common way an in-review run gets touched again. Re-resolving live
    # there would let an admin's live setting change flip the provider out from under a run that is
    # not finished, exactly the failure Ruling 2 exists to prevent -- pinning per-THREAD (not merely
    # per-intake-call) is what actually closes that gap.
    #
    # The session_store fallback (Phase E audit I-3 review, Important 1) closes a gap the original
    # two-level fallback opened for itself: after a backend restart, `state.get("provider")` is
    # ALWAYS empty (see the InMemorySaver paragraph below) for every thread's first post-restart
    # intake -- INCLUDING one that is only reattaching, not genuinely new. Falling straight to a
    # live `get_provider()` read there means intake and `sessions_api.provision_session` (which
    # itself now prefers `dbo.sessions.provider` -- see that module and session_store.create_session)
    # each resolve independently: if the org setting changed since this session was first
    # provisioned, intake would dispatch to the NEW live value while provisioning had already baked
    # the container for the OLD stored one (or vice versa, whichever runs first), reintroducing the
    # exact split-provider hazard this field exists to prevent -- just moved from "mid-run" to
    # "across a restart." Checking `session_store.get_session(thread_id).get("provider")` BEFORE the
    # live fallback makes both sides agree on the same durable value again, even with an empty
    # checkpoint: it is a plain SELECT on the same branch that was already going to hit the DB for
    # `get_provider()`'s own `org_settings` read, so this adds a second rare-path query, not a new
    # cost class on the hot path (every later intake on the same thread still short-circuits on
    # `state.get("provider")` and touches the DB not at all).
    #
    # Survives a resume exactly like every other never-touched-again GraphState field (stages,
    # run_id, StageState.baseline_commit) does: this module's compiled graph uses InMemorySaver
    # (see compile_graph below), which gives every TypedDict key its own last-value channel, merged
    # from the thread's last checkpoint into every invocation that doesn't overwrite it -- confirmed
    # against real code here, not assumed, the same way this field's own brief asked for:
    # resume_regression_check.py runs a REAL compiled graph against a REAL InMemorySaver and proves
    # this exact round-trip for consumed_message_id (also written once by intake_node and never
    # touched again downstream); make_draft_node's baseline_commit handling (~line 1462) documents
    # the identical "captured once, persists via checkpoint" reliance for a plain StageState field.
    # Nothing about a fresh top-level GraphState key makes this field any less durable than those.
    #
    # The one real wrinkle, independently documented in three other places in this codebase
    # (run_headless.py's own module docstring, repo_scan.py, sandbox/registry.py): InMemorySaver is
    # process-local. It holds nothing across a process restart, for ANY field -- provider included,
    # but also stages, run_id, everything. The only cross-restart durability this pipeline has is
    # workflow_persistence.hydrate_state(), and intake_node calls it to restore just `stages`
    # (never a bare top-level field -- manifest_exists/app_scan/run_baseline_commit are all simply
    # recomputed fresh every run instead of hydrated, same as this one will be). So
    # `state.get("provider")` comes back empty not only for a checkpoint that predates this field,
    # but for EVERY thread's first intake call after EVERY future restart, forever -- the `or await
    # chat_model.get_provider()` fallback is this field's ordinary, permanent steady-state behavior,
    # not a one-time migration shim that stops mattering once every pre-migration checkpoint is gone.
    provider: Literal["copilot", "claude"]
    # Set by scaffold_node (preflight_nodes.py) -- manifest.json absence is the canonical
    # "never onboarded before" signal, routing into brownfield-baseline's brownfield sub-flow. Read once at
    # scaffold time and routed on from state, never re-read: app_check_record_node writes to
    # manifest.json mid-run, so a fresh read at the branch point would always report "onboarded".
    manifest_exists: bool
    # `git rev-parse HEAD` captured by scaffold_node before this run writes anything.
    run_baseline_commit: str | None
    # app_discovery.py's deterministic scan output (candidates/evidence/fingerprint) -- grounds
    # the Tech Stack tab's fresh-detection draft, app_check_record_node's manifest write, and
    # e2e's app-boot rescan. "Greenfield" is now derived from this (tech_stack_signals.
    # is_greenfield_repo: no candidates found), not a separate state channel.
    app_scan: dict[str, Any]
    # Deterministic schema/migration/route grep, grounding brownfield-baseline brownfield's draft prompt.
    brownfield_context: str
    raw_requirements_text: str
    # Non-text InputContent parts (screenshots/documents) from the latest submission's
    # HumanMessage, if any -- only ever consumed by the specification stage's draft prompt
    # (BR-2: the plan stage's input is the approved Specification, never raw attachments).
    requirements_attachments: list[dict[str, Any]]
    # id of the last HumanMessage intake_node actually consumed as a fresh submission. Plain
    # last-write-wins channel (no reducer): intake_node sets it exactly once per genuinely new
    # submission and otherwise returns it unchanged. This is what lets intake_node tell "the human
    # submitted something new" apart from "the checkpoint is still replaying the same message from
    # a run that already consumed it" -- text presence alone can't: a resume click's blank
    # runAgent() call merges an EMPTY messages list via add_messages, so the checkpointed messages
    # list still ends in the ORIGINAL HumanMessage from the failed run, non-empty text and all.
    consumed_message_id: str | None
    stages: dict[str, StageState]
    # Keyed by RebuildSpec.key (rebuild.py) -- each R placement's own RebuildState. Accessed via
    # .get("rebuild") or {} everywhere (rebuild.py's nodes tolerate it being absent on a thread
    # that predates R), so this key is genuinely optional at the TypedDict level in practice.
    rebuild: dict[str, Any]
    # The quality/security/audit-cluster/finding-cluster scratch channels that sat here went with
    # their node modules. Nothing live read any of them, and each cluster's surviving behaviour has a
    # home now: never-suppress and dependency upgrades in the remediation prompt, licence findings and
    # duplication in repo_scan (the duplication THRESHOLD moved to metrics_nodes.regression_reasons --
    # jscpd measured it but nothing had gated on it since the audit cluster was switched off).
    # test-hardening's own bespoke-cluster state -- see agent/src/test_hardening_nodes.py.
    test_hardening: dict[str, Any]
    # e2e's own bespoke-cluster state (playwright execution + screenshots + gating) -- see
    # agent/src/e2e_nodes.py. None until e2e_gate_check_node's first write this run.
    e2e: dict[str, Any] | None
    # metrics-report's own metrics state -- see agent/src/metrics_nodes.py.
    metrics_report: dict[str, Any]
    # The baseline repo scan taken once at the top of the graph -- see agent/src/repo_scan.py.
    repo_scan: dict[str, Any]
    # Outcome of the most recent push to the single, repo-shared `ai-dev-workflow` work branch
    # (git_ops.push_head): {ok, error, at}. Every session/user on this repo pushes that same
    # branch via --force-with-lease (WS0's single-branch migration retired the old per-branch
    # `ai-dev-workflow/<branch>` naming). Streamed so the frontend can warn when GitHub
    # persistence is failing (e.g. the user lacks push permission) instead of silently lying.
    last_push: dict[str, Any] | None
    # Terminal failure record ({stage, type, ...detail} -- the old escalation payload shape).
    # Escalations no longer pause for a human (spec/plan approval are the only two human
    # touchpoints); an exhausted deterministic gate ENDs the run with this set, the frontend
    # shows it as a red pill, and a resubmission clears it (scaffold_node).
    run_failure: dict[str, Any] | None
    # Live token spend {input_tokens, output_tokens, cost}, re-summed from the ledger whenever a
    # background refresh scan lands (metrics_nodes.collect_live_refresh) -- feeds the metrics
    # bar's Cost chip during the run; metrics_report's token_usage_summary is the final word.
    token_usage_running: dict[str, Any] | None
    # The generated app's authentication posture for THIS run: {auth_mode, anonymous_routes,
    # secrets_present}. Seeded once by record_raw_requirements_node from repo_auth_settings'
    # per-thread store (provision wrote it) + keyvault's secret cache; read everywhere via
    # get_app_auth(state) -- NEVER a direct subscript, because every checkpoint written before
    # this field shipped lacks it, and a gate-approval resume re-enters PAST the seeding node.
    app_auth: dict[str, Any]
    # Declared test users for THIS run: [{name, email, roles}]. Seeded beside app_auth from
    # repo_test_users' per-thread store; read via state.get("test_users"). Empty when none declared.
    test_users: list[dict[str, Any]]


def default_stage_state() -> StageState:
    return {
        "status": "not_started",
        "draft": None,
        "clarifying_questions": [],
        "readiness": False,
        "cycle_count": 0,
        "approved_content": None,
        "ever_ready_for_review": False,
        "used_ids": [],
        "audit_findings": [],
        "verify_cycle_count": 0,
        "last_verification": None,
        "reviewer_feedback": None,
        "baseline_commit": None,
        # Skill evidence recorded by make_draft_node from the session's own skill.invoked events.
        "skills": None,
        "infra_exhausted": False,
        "last_infra_error": None,
        "last_verify_feedback": None,
        "last_verify_changed_paths": None,
        "verify_stall_count": 0,
        "best_verify_coverage_rate": None,
    }


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of a StageSpec.deterministic_verify check -- never LLM self-attestation, always a
    real script/parse. `feedback` is injected as extra prompt context on the next draft retry;
    `report` is the full structured detail, persisted and surfaced to the frontend even though it
    wasn't produced by an LLM call."""

    passed: bool
    feedback: str
    report: dict[str, Any]


# Paths the pipeline itself writes; a "change set" containing only these means the stage produced
# nothing of its own (see make_verify_node's fabrication check). Mirrors write_scope_gate's own
# _PIPELINE_OWNED_PREFIXES.
_PIPELINE_ARTIFACT_PREFIXES = (".ai-dev-workflow/", "APPROVALS.md", "AGENTS.md")


def _extract_ids(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, val in value.items():
            if key == "id" and isinstance(val, str):
                out.add(val)
            else:
                _extract_ids(val, out)
    elif isinstance(value, list):
        for item in value:
            _extract_ids(item, out)


SPEC_SYSTEM_PROMPT = load_prompt("specification_draft")

# Appended only when spec_ledger.hydrate_ticket_mode_context (StageSpec.
# draft_prompt_context_from_repo_file) finds a non-empty ledger -- a second-or-later ticket
# against a project that already has an approved baseline, vs. a from-scratch first pass.
SPEC_TICKET_MODE_SEGMENT = load_prompt("specification_ticket_mode_segment")

PLAN_SYSTEM_PROMPT = load_prompt("plan_draft")

PLAN_GREENFIELD_SEGMENT = load_prompt("plan_greenfield_segment")

# Appended to specification/plan/ac-to-tests/minimal-code-to-green drafts when the repo's auth
# posture requires it AND Key Vault auth secrets are actually present (an app cannot demand Entra
# sign-in with no ClientId to sign in against -- see get_app_auth/auth_enforced below).
AUTH_REQUIREMENT_SEGMENT = load_prompt("auth_requirement_segment")
TEST_USERS_SEGMENT = load_prompt("test_users_segment")

# W9: repo-durable memory (.ai-dev-workflow/memory.md -- the only store that outlives the
# container, since $HOME and its .claude transcripts die on every reprovision). Appended to the
# write-capable stages that actually learn stack quirks: ac-to-tests, minimal-code-to-green,
# remediation. Advisory by design -- no gate ever blocks on memory.
MEMORY_SEGMENT = load_prompt("memory_segment")

_APP_AUTH_DEFAULT: dict[str, Any] = {"auth_mode": "required", "anonymous_routes": [], "secrets_present": False}


def get_app_auth(state: "GraphState") -> dict[str, Any]:
    """The ONLY way to read state['app_auth']: every checkpoint written before the field shipped
    lacks it, and a gate-approval resume re-enters PAST record_raw_requirements_node's seeding --
    a direct subscript is a guaranteed KeyError on every in-flight session at deploy time. The
    default has secrets_present=False, so enforcement is inert until genuinely seeded."""
    return state.get("app_auth") or dict(_APP_AUTH_DEFAULT)


def auth_kind(state: "GraphState") -> str:
    """How the approved tech-stack says the app authenticates (entra/google/generic-oidc/custom/
    none). Read from the approved tech-stack content; 'none' for old sidecars / pre-detection runs.
    Tech-stack is always approved before any stage that reads this, so approved_content is set."""
    content = (state.get("stages", {}).get("tech-stack") or {}).get("approved_content") or {}
    if not isinstance(content, dict):
        return "none"
    return str(content.get("auth_kind") or "none")


def auth_enforced(state: "GraphState") -> bool:
    """True when the generated app must be locked down AND the run can actually satisfy that. The
    auth mode must require it, the kill-switch (AIDW_AUTH_GATE) must be on, and the run must have a
    way to exercise auth: either Key Vault auth secrets were injected (the original path) OR the
    operator declared test users (the fake-IdP / seeded-user path, which needs no vault). The same
    predicate guards prompt injection and the e2e auth gate, so the pipeline never demands what it
    cannot verify (or vice versa)."""
    if not workflow_config.AIDW_AUTH_GATE:
        return False
    app_auth = get_app_auth(state)
    if app_auth.get("auth_mode") not in ("required", "anonymous_list"):
        return False
    return bool(app_auth.get("secrets_present")) or bool(state.get("test_users"))


def _auth_segment_message(state: "GraphState") -> HumanMessage:
    routes = get_app_auth(state).get("anonymous_routes") or []
    rendered = "\n".join(f"  - {r}" for r in routes) if routes else "  (none -- every route requires sign-in)"
    return HumanMessage(content=AUTH_REQUIREMENT_SEGMENT.replace("<<anonymous_routes>>", rendered))


def _test_users_segment_message(state: "GraphState") -> HumanMessage:
    from . import repo_test_users

    table = repo_test_users.render_table(state.get("test_users") or [])
    return HumanMessage(content=TEST_USERS_SEGMENT.replace("<<test_users>>", table))

# Appended only when hydrate_plan_ticket_mode_context (StageSpec.draft_prompt_context_from_repo_file)
# finds an earlier ticket's approved Plan already on disk -- spec_ledger.hydrate_ticket_mode_context's
# own sibling for the plan stage, same reasoning, different artifact.
PLAN_TICKET_MODE_SEGMENT = load_prompt("plan_ticket_mode_segment")


async def hydrate_plan_ticket_mode_context(
    thread_id: str, _state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.draft_prompt_context_from_repo_file for the plan stage.

    Signals ticket-mode framing when workflow_persistence.PLAN_APPROVED_PATH already exists on
    disk -- an earlier ticket's approved Implementation Plan for this same project, the plan
    stage's own analogue of spec_ledger.hydrate_ticket_mode_context (same shape, same "reframe,
    never skip" contract -- see draft_prompt_context_from_repo_file's own docstring on StageSpec).

    Deliberately reads the FILE rather than state["stages"]["plan"]["draft"]/["approved_content"]:
    those are already surfaced to the prompt separately by _build_plan_prompt's own
    "Your immediately-prior draft" block below, which is what actually carries a hydrated prior
    ticket's plan content into this ticket's very first draft call (workflow_persistence.
    hydrate_state repopulates a brand-new thread's stage state from this same repo's last commit
    before intake_node's fresh-run reset -- see intake_node's own comments -- clears only the
    per-run bookkeeping, not the content fields). This hook's only job is deciding whether that
    content should be read as "your own abandoned draft" (a first ticket's ordinary retry) or "the
    project's existing architecture to extend" (a later ticket) -- exactly the ambiguity
    PLAN_TICKET_MODE_SEGMENT resolves.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.PLAN_APPROVED_PATH)
    return {"ticket_mode_baseline": True} if raw is not None else None


def _reviewer_feedback_message(stage: StageState) -> HumanMessage | None:
    """Shared by every requires_human_gate=True stage's build_prompt (tech-stack/specification/
    plan) -- Part 2 Task 10 (Ruling 3): folds a human's gate REJECTION feedback into the next
    draft call as reviewer guidance. Phrased distinctly from the ac-to-tests/minimal-code-to-green/
    adversarial-compliance "last verification attempt failed" wording those stages' own
    build_prompt functions use for a deterministic_verify failure -- a person's opinion is not a
    script's finding, and StageState.reviewer_feedback is deliberately its own field for exactly
    that reason (see its own comment). None on the overwhelmingly common path (never rejected)."""
    feedback = stage.get("reviewer_feedback")
    if not feedback:
        return None
    return HumanMessage(content=f"A human reviewer rejected your previous draft and left this feedback: {feedback}")


def _build_specification_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    requirements_text = f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"
    attachments = state.get("requirements_attachments") or []
    # Attachments (screenshots/documents) ride alongside the text as a multimodal content list.
    # As of task-13, claude_chat_model.py's own `_messages_to_prompt` forwards a real (data-
    # sourced, base64-inline) part instead of dropping it: it decodes and writes the bytes to a
    # sandbox scratch file, then references that path in the prompt text for Claude's own Read
    # tool to open -- confirmed empirically as the mechanism that actually works, NOT a --file CLI
    # flag (needs a CLAUDE_CODE_SESSION_ACCESS_TOKEN this pipeline never provisions) and NOT an
    # inline content block over --input-format stream-json (reaches the model with no visual
    # content at all). copilot_chat_model.py's own `_messages_to_prompt` still flattens this list
    # to plain text and DROPS every non-text part (logged via logger.warning) -- a disclosed,
    # deliberate asymmetry (Ruling 9), not an oversight: no confirmed Copilot CLI attachment
    # mechanism exists to restore it there. See either module's own `_messages_to_prompt`
    # docstring. Still built as a list rather than joined to plain text here either way, so
    # Claude's real mechanism and any future Copilot one both have the original structure to work
    # from instead of information already lost upstream.
    requirements_content: str | list[dict[str, Any]] = (
        [{"type": "text", "text": requirements_text}, *attachments] if attachments else requirements_text
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=SPEC_SYSTEM_PROMPT),
        HumanMessage(content=requirements_content),
    ]
    if auth_enforced(state):
        messages.append(_auth_segment_message(state))
    # Not a StageState field -- only ever present on the prompt-only stage copy make_draft_node
    # builds from StageSpec.draft_prompt_context_from_repo_file's return (see that hook's own
    # docstring and spec_ledger.hydrate_ticket_mode_context). Absent, hence falsy, on every other
    # call path (including this function's own direct callers in tests).
    if stage.get("ticket_mode_baseline"):
        messages.append(HumanMessage(content=SPEC_TICKET_MODE_SEGMENT))
    if stage["draft"] is not None:
        messages.append(
            HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}")
        )
    if stage["used_ids"]:
        messages.append(
            HumanMessage(content=f"Identifiers already used at some point, never reuse: {stage['used_ids']}")
        )
    feedback_message = _reviewer_feedback_message(stage)
    if feedback_message is not None:
        messages.append(feedback_message)
    return messages


def _build_plan_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    messages: list[BaseMessage] = [
        SystemMessage(content=PLAN_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
    ]
    if auth_enforced(state):
        messages.append(_auth_segment_message(state))
    if tech_stack_signals.is_greenfield_repo(state):
        messages.append(HumanMessage(content=PLAN_GREENFIELD_SEGMENT))
    if _tech_stack_has_ui_framework(state):
        messages.append(HumanMessage(content=IMPECCABLE_PLAN_SEGMENT))
    # Not a StageState field -- only ever present on the prompt-only stage copy make_draft_node
    # builds from StageSpec.draft_prompt_context_from_repo_file's return (see
    # hydrate_plan_ticket_mode_context's own docstring). Absent, hence falsy, on every other call
    # path (including this function's own direct callers in tests).
    if plan_stage.get("ticket_mode_baseline"):
        messages.append(HumanMessage(content=PLAN_TICKET_MODE_SEGMENT))
    if plan_stage["draft"] is not None:
        messages.append(
            HumanMessage(content=f"Your immediately-prior draft (JSON):\n{plan_stage['draft']}")
        )
    if plan_stage["used_ids"]:
        messages.append(
            HumanMessage(content=f"Identifiers already used at some point, never reuse: {plan_stage['used_ids']}")
        )
    feedback_message = _reviewer_feedback_message(plan_stage)
    if feedback_message is not None:
        messages.append(feedback_message)
    return messages


# Moved to tech_stack_signals.py (a shared leaf module) so e2e_nodes.py can use the exact same
# signal without a circular import (graph.py imports e2e_nodes to wire it in). Aliased to the old
# private name so every call site below is unchanged.
_tech_stack_has_ui_framework = tech_stack_signals.tech_stack_has_ui_framework


def _spec_work_kind(state: GraphState) -> str:
    """The approved Specification's work_kind classification ("bug"/"feature"), defaulting to
    "feature" for pre-classification specs (persisted before schemas.Specification grew the field)
    and for anything malformed -- the bug segment is an extra discipline, so absence of evidence
    means don't add it."""
    approved = state["stages"]["specification"].get("approved_content")
    if isinstance(approved, dict) and approved.get("work_kind") == "bug":
        return "bug"
    return "feature"


# Impeccable (vendored design skill, plugins/vendor/pbakaus-impeccable) -- per-stage prompt
# segments appended ONLY for UI-framework repos (same _tech_stack_has_ui_framework gate as the
# Playwright/Excalidraw MCPs), so non-UI runs never spend context on design guidance. Each segment
# is scoped to what that stage's session can actually execute: P3's read-only allowlist has no
# bash, so it gets methodology only; P6/dedup-simplify run autopilot and may run the skill's node scripts;
# adversarial-audit's default plan-mode session gets the LLM-only critique, never the detector scripts.
_IMPECCABLE_SKILL_DIR = (
    f"{workflow_config.COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/pbakaus-impeccable/impeccable/skills/impeccable"
)

IMPECCABLE_PLAN_SEGMENT = (
    "This repository has a UI framework. Before drafting any UI-touching plan steps, load the"
    " `impeccable` skill and read its `reference/shape.md` -- apply that shape methodology (plan"
    " UX/UI decisions before code) to the UI portions of the plan. Product context is the approved"
    " Specification above; do NOT run any impeccable scripts and do NOT run `init` -- this session"
    " has no shell, and the methodology is what matters here."
)

IMPECCABLE_CODEGEN_SEGMENT = (
    "This repository has a UI framework. Before writing UI code: (1) if PRODUCT.md is missing at"
    " the repo root, create it from the approved Specification above (product truth only -- never"
    " run the interactive `impeccable init` interview); (2) if DESIGN.md is missing, load the"
    " `impeccable` skill and follow its `document` command to capture the existing visual system."
    " While implementing UI code, follow the impeccable skill's design rules (its SKILL.md general"
    " rules and craft-floor reference: contrast, typography, layout, motion, absolute bans),"
    " product/Operate register. The impeccable skill lives at"
    f" {_IMPECCABLE_SKILL_DIR} -- its scripts run with plain `node`."
)

# IMPECCABLE_CRITIQUE_SEGMENT and IMPECCABLE_DEDUP_SEGMENT were deleted 2026-08-24: both lost
# their only consumers when the standalone critique/dedup-simplify stages consolidated into
# adversarial-compliance/remediation, and dead prompt segments read as wired when they aren't.
# KNOWN REGRESSION (flagged, not fixed here): adversarial-compliance no longer receives any
# impeccable-critique framing on UI repos -- re-adding it means a new segment appended in that
# stage's build_prompt, not resurrecting the old constant unwired.

FRONTEND_DESIGN_SEGMENT = (
    "This repository has a UI framework. Before writing any new UI surface, invoke the"
    " `frontend-design` skill with your Skill tool and apply its direction (typography, aesthetic"
    " intent, avoiding templated defaults) alongside the impeccable design rules above -- the two"
    " are complementary: frontend-design sets the visual direction, impeccable enforces the craft"
    " floor."
)

# Appended to minimal-code-to-green's prompt only when the approved Specification classified this
# ticket's work_kind as "bug" (schemas.SpecificationDraftResponse.work_kind) -- same
# conditional-segment shape as the ticket-mode and UI-framework segments above. Prompt-level
# mandate; systematic-debugging is deliberately NOT in REQUIRED_SKILLS_BY_STAGE because that map
# is static and this requirement only exists for bug tickets.
BUG_FIX_SEGMENT = (
    "This ticket fixes a BUG (classified in the approved Specification). Before writing the fix,"
    " invoke the `systematic-debugging` skill with your Skill tool and follow it: reproduce the"
    " failure first -- a failing test or a captured command output that goes red on this bug --"
    " then diagnose, then fix, and keep the reproduction as a regression test. The"
    " `diagnosing-bugs` skill is the complementary red-green diagnosis loop; invoke it too when"
    " the cause is not obvious from the reproduction."
)


# MCP server configs -- confirmed real and working via a live spike (mcp_servers= reaches an
# actual Copilot CLI session; tool names surface as "<server_key>-<tool_name>", e.g.
# "playwright-browser_navigate"; "mcp:*" in available_tools is required to let them through an
# allowlist-tier stage, otherwise the allowlist silently filters every MCP tool out -- caught by
# testing, not guessed). Playwright MCP itself (spawns its own browser via npx, no external
# service needed) was the one actually exercised. NO MCP server is attached to any stage today, and
# none is installed in the image -- the notes below record what each was and why it went, so the
# next person weighing one starts from evidence instead of re-running these experiments.
# ac-to-tests used to attach a Playwright MCP server here. Deleted rather than left dormant, in
# keeping with the two notes below: the stage runs in the TDD RED phase, so on a greenfield run
# there is no application to browse, and selectors are now AUTHORED rather than discovered (the
# codegen stage must put a `data-testid` on every element a test touches; specs locate by
# `getByTestId`). Two facts worth keeping if it is ever reinstated, both learned expensively:
# the installed binary is `playwright-mcp` (NOT `mcp-server-*` -- `find / -iname 'mcp-server*'`
# finds nothing), and getting that name wrong did not fail loudly: the stdio startup failed
# silently and the session lost every OTHER tool with it, leaving ac-to-tests-draft without an edit
# tool and 8+ runs escalating with zero test files. Its env must point at
# /opt/playwright-browsers, the one baked browser path. The server is no longer installed either --
# keeping an unattached MCP in the image was what forced PLAYWRIGHT_VERSION to track
# @playwright/mcp's own alpha `playwright` dependency, so removing it also frees that pin.
# Reinstating it means one npm line in the Dockerfile plus this config, for a stage that audits a
# RUNNING app -- never one that writes tests before the app exists.
# The plan stage used to attach an Excalidraw MCP server here for wireframes. Deleted rather
# than left dormant: it was never spike-tested, fetched unpinned `npx -y mcp-excalidraw` over
# the network at session time, and had no scene-to-SVG export path. Wireframes are now
# LLM-emitted self-contained HTML, validated deterministically in gates/diagram_gate.py.
# quality-remediation used to attach a SonarQube MCP server here too. Deleted rather than left dormant: it was never
# spike-tested, needed SONARQUBE_URL/SONARQUBE_TOKEN that nothing sets, fetched unpinned
# `sonarqube-mcp-server@latest` over the network into a container running untrusted repositories,
# and SonarQube is LGPL-3.0 -- the same bar that kept it out of repo_scan.py's tool set.


SPEC_AUDIT_SYSTEM_PROMPT = load_prompt("specification_audit")

PLAN_AUDIT_SYSTEM_PROMPT = load_prompt("plan_audit")


def _build_specification_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    return [
        SystemMessage(content=SPEC_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"),
        HumanMessage(content=f"Draft Specification to audit (JSON):\n{stage['draft']}"),
    ]


def _build_plan_audit_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    return [
        SystemMessage(content=PLAN_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Draft Plan to audit (JSON):\n{plan_stage['draft']}"),
    ]


TECH_STACK_SYSTEM_PROMPT = load_prompt("tech_stack_draft")


def _build_tech_stack_prompt(state: GraphState) -> list[BaseMessage]:
    """Unconditional explore-and-draft -- no greenfield branch. The greenfield case (no code to
    explore) is now handled BEFORE this LLM ever runs, via prefill_tech_stack_from_repo_file
    (tech-stack.md already has the user's picked/edited canned-stack content) or, for a genuinely
    fresh empty repo with no tech-stack.md either, this same prompt just explores an empty
    repository and honestly reports finding nothing -- the Tech Stack tab's dropdown lets the
    human pick a canned stack afterward, no prompt-level special-casing needed."""
    stage = state["stages"]["tech-stack"]
    messages: list[BaseMessage] = [SystemMessage(content=TECH_STACK_SYSTEM_PROMPT)]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    feedback_message = _reviewer_feedback_message(stage)
    if feedback_message is not None:
        messages.append(feedback_message)
    return messages


def _build_tech_stack_interrupt_extra(state: GraphState) -> dict[str, Any]:
    """Tells the Tech Stack tab (a) whether to show the canned-stack dropdown -- only when this
    draft came from the unconditional LLM explore-and-draft path (no tech-stack.md existed),
    never when prefill_tech_stack_from_repo_file already loaded the file's own content -- and
    (b) plain markdown text to populate the editor with, computed HERE (not client-side) since
    the frontend has no renderer for a raw TechStack-shaped draft, only for the {"markdown": ...}
    shape prefill produces. Both distinctions key off the same shape test preflight_nodes.
    _select_tech_stack_markdown already relies on: prefill's draft has a top-level "markdown" key,
    the LLM draft path's raw TechStack dict does not."""
    draft = state["stages"]["tech-stack"].get("draft") or {}
    if isinstance(draft, dict) and "markdown" in draft:
        return {"file_existed": True, "markdown": draft.get("markdown") or ""}
    return {"file_existed": False, "markdown": render_tech_stack_markdown(draft)}


BROWNFIELD_BASELINE_SYSTEM_PROMPT = load_prompt("brownfield_baseline_draft")


def _build_brownfield_baseline_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["brownfield-baseline"]
    messages: list[BaseMessage] = [
        SystemMessage(content=BROWNFIELD_BASELINE_SYSTEM_PROMPT),
        HumanMessage(content=state.get("brownfield_context") or "(no grounding context available)"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages



async def record_raw_requirements_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """The human's requirements text is accepted AS-IS -- no LLM draft, no audit, no gate. This
    deterministic node records it as the raw-requirements stage's approved content (so the
    frontend, persistence, hydration, and exit's content hash all keep working) and the
    specification stage does all actual processing (it has always read raw_requirements_text
    directly). An empty text this run keeps whatever hydration restored -- the blank-reattach
    path must not clobber a returning thread's document."""
    thread_id = config["configurable"]["thread_id"]
    text = (state.get("raw_requirements_text") or "").strip()
    stages = {key: dict(value) for key, value in state["stages"].items()}
    stage = stages.get("raw-requirements") or default_stage_state()
    if text:
        stage = {
            **stage,
            "status": "approved",
            "readiness": True,
            "ever_ready_for_review": True,
            "clarifying_questions": [],
            "draft": None,
            "approved_content": {"content": text},
        }
    stages["raw-requirements"] = stage
    await _persist_if_sandboxed(thread_id, state, stages, "ai-dev-workflow: raw requirements recorded")
    # Seed the run's app-auth posture ONCE, here (the one deterministic node every fresh run passes
    # through after provision): repo settings from the per-thread store sessions_api populated,
    # plus whether Key Vault actually delivered auth secrets -- auth_enforced() needs both.
    # Checkpointed like every other GraphState field; resumes re-read it from the checkpoint.
    from . import keyvault, repo_auth_settings, repo_test_users

    auth_settings = repo_auth_settings.get_for_thread(thread_id)
    app_auth = {**auth_settings, "secrets_present": bool(keyvault.get_app_secrets(thread_id))}
    return {"stages": stages, "app_auth": app_auth, "test_users": repo_test_users.get_for_thread(thread_id)}


async def _verify_specification_ledger(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: SandboxProvider,
    _chat_provider: str,
) -> VerificationResult:
    """StageSpec.deterministic_verify for the specification stage: resolves/validates every User
    Story's and Acceptance Criterion's id against spec_ledger.LEDGER_PATH (spec_ledger.py's real
    logic -- this is just the SandboxProvider-I/O wrapper) and persists the ledger on success.

    content_dict is stage["draft"] (the just-audited, revised Specification, mutated in place by
    sync_ledger to carry ledger-resolved ids) -- schemas.Specification's own `retired_ac_ids`/
    `retired_us_ids` fields (Ruling 3) live right on it, so no separate plumbing is needed to reach
    them here. `_chat_provider` is unused -- a pure ledger sync has no dispatch call of its own
    (StageSpec.deterministic_verify's own docstring).
    """
    entries = await spec_ledger.load_ledger(provider, thread_id)
    user_stories = content_dict.get("user_stories") or []
    retired_ac_ids = content_dict.get("retired_ac_ids") or []
    retired_us_ids = content_dict.get("retired_us_ids") or []
    result = spec_ledger.sync_ledger(
        entries, user_stories, run_id, retired_ac_ids=retired_ac_ids, retired_us_ids=retired_us_ids
    )
    if result.passed:
        # No explicit commit: the ledger lives under .ai-dev-workflow/, which the verify-pass
        # persistence commit sweeps up.
        await spec_ledger.save_ledger(provider, thread_id, result.updated_entries)
    return VerificationResult(
        passed=result.passed,
        feedback="; ".join(result.reasons) if result.reasons else "Ledger sync passed: every id resolved cleanly.",
        report={"reasons": result.reasons, "ledger_entry_count": len(result.updated_entries)},
    )


# playwright.config.ts's exact content is a template file (agent/src/templates/playwright/), not
# hand-duplicated prose here -- it must byte-for-byte match the sandbox image's pinned Playwright
# version (Dockerfile's PLAYWRIGHT_VERSION comment: "this literal is duplicated in the ac-to-tests
# SKILL.md and in two prompts... so all four change together"). Substituted once at module load,
# same caching lifetime as load_prompt's own lru_cache.
AC_TO_TESTS_SYSTEM_PROMPT = load_prompt("ac_to_tests_draft").replace(
    "<<playwright_config_template>>", template_loader.load_template("playwright/playwright.config.ts").strip()
)

AC_TO_TESTS_GREENFIELD_SEGMENT = load_prompt("ac_to_tests_greenfield_segment")

# Appended only when spec_ledger.hydrate_ac_to_tests_ticket_mode_context (StageSpec.
# draft_prompt_context_from_repo_file) finds ledger ACs beyond this ticket's own approved
# Specification -- a genuine multi-ticket project, not merely "the ledger has entries" (which is
# trivially true by ac-to-tests time even on a project's first-ever ticket, since specification's
# own sync_ledger run just populated it moments before).
AC_TO_TESTS_TICKET_MODE_SEGMENT = load_prompt("ac_to_tests_ticket_mode_segment")


def _build_ac_to_tests_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    stage = state["stages"]["ac-to-tests"]
    messages: list[BaseMessage] = [
        SystemMessage(content=AC_TO_TESTS_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{plan_stage['approved_content']}"),
    ]
    # The suite must KNOW about auth: without this, every positive-path AC test hits a 302-to-
    # Entra, and the cheapest way for a fix lap to go green is deleting the middleware -- which
    # then re-fails the auth gate. The segment mandates the AIDW_TEST_AUTH seam tests sign in with.
    if auth_enforced(state):
        messages.append(_auth_segment_message(state))
    if state.get("test_users"):
        messages.append(_test_users_segment_message(state))
    messages.append(HumanMessage(content=MEMORY_SEGMENT))
    if tech_stack_signals.is_greenfield_repo(state):
        messages.append(HumanMessage(content=AC_TO_TESTS_GREENFIELD_SEGMENT))
    # Not a StageState field -- see hydrate_plan_ticket_mode_context's docstring for the general
    # shape (this stage's own copy lives in spec_ledger.hydrate_ac_to_tests_ticket_mode_context).
    if stage.get("ticket_mode_baseline"):
        messages.append(HumanMessage(content=AC_TO_TESTS_TICKET_MODE_SEGMENT))
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    if stage.get("last_verification"):
        messages.append(
            HumanMessage(content=f"The last verification attempt failed with: {stage['last_verification'].get('feedback')}")
        )
    return messages


AC_TO_TESTS_AUDIT_SYSTEM_PROMPT = load_prompt("ac_to_tests_audit")


def _build_ac_to_tests_audit_prompt(state: GraphState) -> list[BaseMessage]:
    """Unlike mcg's audit (draft JSON alone is enough context there), this audit also needs the
    Approved Specification: its own job includes catching an AC the spec/ledger lists that nothing
    in the draft's test_files actually covers, which the draft JSON alone cannot reveal -- it only
    says what the draft itself chose to report. The audit session's own read-only tools (see this
    stage's session_options, role="audit") can additionally open the ledger and the real test
    files on disk, same as every other tool-writing stage's audit."""
    spec_stage = state["stages"]["specification"]
    stage = state["stages"]["ac-to-tests"]
    return [
        SystemMessage(content=AC_TO_TESTS_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Draft test suite to audit (JSON):\n{stage['draft']}"),
    ]


MINIMAL_CODE_TO_GREEN_SYSTEM_PROMPT = load_prompt("minimal_code_to_green_draft")

MINIMAL_CODE_TO_GREEN_AUDIT_SYSTEM_PROMPT = load_prompt("minimal_code_to_green_audit")

# Brownfield counterpart of PLAN_GREENFIELD_SEGMENT/AC_TO_TESTS_GREENFIELD_SEGMENT, gated on the
# SAME tech_stack_signals.is_greenfield_repo() app-scan signal those two already use (deliberately
# NOT a new StageSpec.draft_prompt_context_from_repo_file hook: that would be a second, independent
# repo scan re-answering the exact question app_discovery_pre_node already computed once per run
# for every other stage that needs it -- see is_greenfield_repo's own docstring). This stage was
# the one of the three still missing either half of the greenfield/brownfield pair: Plan and
# ac-to-tests both already frame the greenfield case, but nothing told minimal-code-to-green the
# repo already has code and conventions worth extending on a brownfield/later-ticket run.
MINIMAL_CODE_TO_GREEN_BROWNFIELD_SEGMENT = load_prompt("minimal_code_to_green_brownfield_segment")


def _build_minimal_code_to_green_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    tests_stage = state["stages"]["ac-to-tests"]
    stage = state["stages"]["minimal-code-to-green"]
    messages: list[BaseMessage] = [
        SystemMessage(content=MINIMAL_CODE_TO_GREEN_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{plan_stage['approved_content']}"),
        HumanMessage(content=f"Approved Test Suite from P4 (JSON):\n\n{tests_stage['approved_content']}"),
    ]
    if auth_enforced(state):
        messages.append(_auth_segment_message(state))
    if state.get("test_users"):
        messages.append(_test_users_segment_message(state))
    messages.append(HumanMessage(content=MEMORY_SEGMENT))
    if _tech_stack_has_ui_framework(state):
        messages.append(HumanMessage(content=IMPECCABLE_CODEGEN_SEGMENT))
        messages.append(HumanMessage(content=FRONTEND_DESIGN_SEGMENT))
    if _spec_work_kind(state) == "bug":
        messages.append(HumanMessage(content=BUG_FIX_SEGMENT))
    if not tech_stack_signals.is_greenfield_repo(state):
        messages.append(HumanMessage(content=MINIMAL_CODE_TO_GREEN_BROWNFIELD_SEGMENT))
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior iteration (JSON):\n{stage['draft']}"))
    if stage.get("last_verification"):
        messages.append(
            HumanMessage(content=f"The last coverage verification failed with: {stage['last_verification'].get('feedback')}")
        )
    return messages


def _build_minimal_code_to_green_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["minimal-code-to-green"]
    return [
        SystemMessage(content=MINIMAL_CODE_TO_GREEN_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft code-change iteration to audit (JSON):\n{stage['draft']}"),
    ]


ADVERSARIAL_AUDIT_SYSTEM_PROMPT = load_prompt("adversarial_audit_draft")


LICENSE_AUDIT_SYSTEM_PROMPT = load_prompt("license_audit_draft")


EXIT_SYSTEM_PROMPT = load_prompt("exit_draft")


def _build_exit_prompt(state: GraphState, stage_key: str = "metrics-exit") -> list[BaseMessage]:
    # The consolidated pipeline has no bare "exit" stage -- exit work lives in `metrics-exit`
    # (see STAGES). Hardcoding "exit" here raised KeyError the first time a run ever reached this
    # far, which is why it stayed latent until now.
    stage = state["stages"][stage_key]
    metrics_compute = (state.get("metrics_report") or {}).get("metrics", {})
    messages: list[BaseMessage] = [
        SystemMessage(content=EXIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{state['stages']['specification']['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{state['stages']['plan']['approved_content']}"),
        HumanMessage(content=f"metrics-report metrics summary (JSON):\n\n{_bounded_json(metrics_compute, 8000)}"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior report (JSON):\n{stage['draft']}"))
    return messages


# A real prompt file, like every other stage's -- the inline one-liner this replaces told the model
# to "recommend fixes" and handed it no findings whatsoever, so the stage produced prose while the
# scanner's gating findings went untouched all the way to the metrics gate.
REMEDIATION_SYSTEM_PROMPT, REMEDIATION_HUMAN_TEMPLATE = load_prompt_pair("remediation_draft")

# Appended only when hydrate_remediation_ticket_mode_context (StageSpec.
# draft_prompt_context_from_repo_file) finds an earlier ticket's own approved remediation report
# already on disk for this same project -- remediation's own analogue of
# hydrate_plan_ticket_mode_context, same "reframe, never skip" contract (see that hook's own
# docstring on StageSpec), different artifact.
REMEDIATION_TICKET_MODE_SEGMENT = load_prompt("remediation_ticket_mode_segment")


async def hydrate_remediation_ticket_mode_context(
    thread_id: str, _state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.draft_prompt_context_from_repo_file for the remediation stage.

    Signals ticket-mode framing when workflow_persistence.REMEDIATION_APPROVED_PATH already exists
    on disk -- an earlier ticket already ran this stage against this same project and left its own
    `known_gaps` reasoning behind. Unlike plan/spec/ac-to-tests, _build_remediation_prompt below has
    no "immediately-prior draft" block at all (every attempt re-reads the live scan from scratch by
    design -- see its own docstring), so a prior TICKET's report is otherwise completely invisible
    to this ticket's draft; reading the file is the only way to recover it.

    Without this, a still-open, already-investigated finding (e.g. "no fixed version published
    yet") forces every later ticket to rediscover and re-justify the exact same gap from scratch --
    the "re-auditing the whole project every time" cost the Spec calls out for this stage
    specifically. Never short-circuits drafting -- see draft_prompt_context_from_repo_file's own
    docstring on StageSpec; this ticket's own draft still scans and fixes whatever it can either
    way, this only tells it where not to start from zero.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.REMEDIATION_APPROVED_PATH)
    return {"ticket_mode_baseline": True} if raw is not None else None


def _build_remediation_prompt(state: GraphState) -> list[BaseMessage]:
    """Consolidated remediation stage 6.

    The findings themselves are NOT marshalled into the prompt: the agent has file tools and reads
    `.ai-dev-workflow/repo-scan-latest.json` directly, which is both the authoritative copy and the
    same offload principle the rest of this pipeline uses -- Python checks the facts afterwards
    (the metrics regression gate re-scans), the agent decides how to fix them. No "immediately-prior
    draft" block either, unlike plan/spec/ac-to-tests: max_cycles=2 and every attempt re-reads the
    live scan fresh, so there is no same-run draft worth echoing back -- only a PRIOR TICKET's own
    approved report (see hydrate_remediation_ticket_mode_context) is otherwise invisible here.
    """
    messages: list[BaseMessage] = [
        SystemMessage(content=REMEDIATION_SYSTEM_PROMPT),
        HumanMessage(content=REMEDIATION_HUMAN_TEMPLATE),
        HumanMessage(content=MEMORY_SEGMENT),
    ]
    if state["stages"]["remediation"].get("ticket_mode_baseline"):
        messages.append(HumanMessage(content=REMEDIATION_TICKET_MODE_SEGMENT))
    # The rejection feedback, same as spec/plan/coverage/adversarial-compliance already do. Without
    # it this stage's redrafts were unreachable by their own gate: a skill-gate failure resets the
    # draft session (make_verify_node), so the retry opens a FRESH transcript and was handed the
    # identical original prompt with no hint anything had been rejected. Observed live (run
    # 026dee4f): three laps in a row launched `general-purpose` instead of the required
    # code-simplifier agent, each shorter than the last, because the model was never told. The
    # docstring above rules out echoing a prior DRAFT (the scan is re-read live each attempt) --
    # that reasoning never applied to the gate's own verdict, which exists nowhere else.
    if state["stages"]["remediation"].get("last_verification"):
        messages.append(
            HumanMessage(
                content="Your previous attempt was rejected by the deterministic gate:\n\n"
                f"{state['stages']['remediation']['last_verification'].get('feedback')}"
            )
        )
    return messages


def _bounded_json(payload: Any, limit: int) -> str:
    """Serialise `payload`, never handing a model JSON that was cut mid-token.

    `json.dumps(x)[:N]` is the obvious thing and it is wrong: slicing a serialised object almost
    always lands inside a string or between a key and its value, so the model receives a blob
    labelled "(JSON)" that no parser will accept -- and has to guess at the shape of its own input.
    When the payload genuinely does not fit, say so IN valid JSON and put the clipped text in a
    string field, where being clipped is honest rather than corrupting.
    """
    text = json.dumps(payload, default=str)
    if len(text) <= limit:
        return text
    return json.dumps(
        {
            "_truncated": True,
            "_original_chars": len(text),
            "_note": "payload exceeded the prompt budget; `preview` is the leading fragment",
            "preview": text[: max(0, limit - 240)],
        },
        default=str,
    )


ADVERSARIAL_COMPLIANCE_SYSTEM_PROMPT = load_prompt("adversarial_audit_draft")


def _build_adversarial_compliance_prompt(state: GraphState) -> list[BaseMessage]:
    """Stage 7: adversarial conformance audit of the implementation against the approved Plan.

    The approved Specification and Plan are passed EXPLICITLY rather than left for the agent to find:
    they are the thing it audits against, and its whole value is comparing them to the code. The e2e
    outcome goes in too, because this stage absorbed e2e's compliance role in the consolidation --
    a suite that failed, or screenshots that were never captured, is conformance evidence the audit
    should weigh rather than rediscover.
    """
    e2e = state.get("e2e") or {}
    e2e_summary = {
        "status": e2e.get("status"),
        "passed": e2e.get("passed"),
        "total": e2e.get("total"),
        "failed_tests": (e2e.get("failed_tests") or [])[:10],
        "screenshots": e2e.get("screenshots") or [],
        "skipped_reason": e2e.get("skipped_reason"),
        # Its own key, NOT competing for the failed_tests[:10] slots: a 12-route auth failure
        # must not push the real test failures out of the auditor's view (or vice versa).
        "auth_check": (e2e.get("auth_check") or {}).get("feedback"),
    }
    messages: list[BaseMessage] = [
        SystemMessage(content=ADVERSARIAL_COMPLIANCE_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{state['stages']['specification']['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{state['stages']['plan']['approved_content']}"),
        HumanMessage(content=f"End-to-end run outcome (JSON):\n\n{_bounded_json(e2e_summary, 4000)}"),
    ]
    stage = state["stages"]["adversarial-compliance"]
    if stage.get("draft") is not None and stage.get("last_verification"):
        messages.append(
            HumanMessage(
                content="Your previous audit was rejected by the deterministic gate:\n\n"
                f"{stage['last_verification'].get('feedback')}"
            )
        )
    return messages


def _build_metrics_exit_prompt(state: GraphState) -> list[BaseMessage]:
    """Reuse exit prompt (stage 8: metrics+exit consolidation)."""
    return _build_exit_prompt(state)


_WHOLE_RESPONSE_EXCLUDED = frozenset({"readiness", "clarifying_questions"})


def _stage_content(response: BaseModel, content_field: str | None) -> Any:
    """The stage's artifact, per StageSpec.content_field.

    `content_field=None` means the response's own fields ARE the artifact; readiness and the
    clarifying questions are dropped because they are stored under their own stage keys and would
    otherwise appear twice in the persisted draft.
    """
    if content_field is not None:
        return getattr(response, content_field)
    dumped = response.model_dump(mode="json")
    return {key: value for key, value in dumped.items() if key not in _WHOLE_RESPONSE_EXCLUDED}


def build_remediation_envelope(content: dict[str, Any], audit_findings: list[str] | None) -> dict[str, Any]:
    """Surface envelope for the remediation stage."""
    return {"stage": "remediation", "content": content}


def render_remediation_markdown(content: dict[str, Any]) -> str:
    """Human-readable remediation summary.

    Was `json.dumps(content)`, which made 06-remediation.md a wall of JSON -- the stage now reports
    which findings it fixed, which dependencies it moved and what it deliberately left, and those are
    the three things a reviewer actually reads.
    """
    lines = ["# Remediation", "", content.get("remediation_summary") or "(no summary reported)", ""]
    for heading, key in (
        ("Dependencies upgraded", "dependencies_upgraded"),
        ("Findings addressed", "findings_addressed"),
        ("Known gaps (deliberately not fixed)", "known_gaps"),
    ):
        items = content.get(key) or []
        if items:
            lines += [f"## {heading}", ""] + [f"- {item}" for item in items] + [""]
    return "\n".join(lines).strip() + "\n"


@dataclass(frozen=True)
class StageSpec:
    key: str
    response_schema: (
        type[SpecificationDraftResponse]
        | type[PlanDraftResponse]
        | type[TechStackDraftResponse]
        | type[AcceptanceCriteriaTestsDraftResponse]
        | type[MinimalCodeToGreenDraftResponse]
        | type[AdversarialAuditDraftResponse]
        | type[ExitDraftResponse]
        | type[RemediationDraftResponse]
    )
    content_field: str | None
    """Which response field becomes the stage's draft, or None for "the whole response".

    Most stages nest their artifact in one field (`specification`, `plan`, ...). Remediation does
    not: its report IS the response's own fields (what was fixed, what moved, what was left), so
    naming any single one of them silently discards the rest.
    """
    surface_tool_name: str
    build_envelope: Callable[[dict[str, Any], list[str] | None], dict[str, Any]]
    build_prompt: Callable[[GraphState], list[BaseMessage]]
    max_cycles: int
    # None = persist_state writes NO per-stage .md for this stage; some other writer owns that
    # file. Only metrics-exit uses it: exit_finalize_node writes the FULL report (score tables,
    # findings, tools) to 09-metrics-exit.md, and the generic 4-section render here would revert
    # it to a stub on the NEXT run's first persist (intake keeps approved_content across runs).
    render_markdown: Callable[[dict[str, Any]], str] | None

    # The adversarial audit leg is OPT-IN: specification, plan, ac-to-tests, and
    # minimal-code-to-green configure it (a second model revising the artifact a human will
    # approve / that becomes code / that gates every downstream stage). Every other stage's draft
    # flows straight to verify/gate -- an audit nobody reads was pure latency. All three fields
    # must be set together or left None together.
    audit_response_schema: (
        type[SpecificationAuditResponse]
        | type[PlanAuditResponse]
        | type[AcToTestsAuditResponse]
        | type[MinimalCodeToGreenAuditResponse]
        | None
    ) = None
    audit_content_field: str | None = None
    build_audit_prompt: Callable[[GraphState], list[BaseMessage]] | None = None

    # Everything below is optional and defaults to today's exact behavior.
    requires_human_gate: bool = True
    """False for stages that are supporting infrastructure with no tab to review them in (e.g.
    tech-stack detection) -- make_gate_node skips the interrupt() and proceeds straight to
    approved, same body that already runs post-interrupt-resolve for every other stage."""

    post_audit_hook: Callable[[str, dict[str, Any], "GraphState", SandboxProvider], Awaitable[None]] | None = None
    """Fire-and-forget side effect called at the end of make_audit_node, right after persistence
    (thread_id, revised content dict, full GraphState, provider). Used for deterministic follow-up
    writes driven by a stage's approved content (e.g. writing Directory.Build.props once tech-stack
    detection reports dotnet_detected) or by this run's own input (e.g. raw-requirements
    persisting the seed text that produced this draft, for its own hydrate_from_repo_file to
    compare future runs against)."""

    post_approve_hook: Callable[[str, dict[str, Any], "GraphState", SandboxProvider], Awaitable[None]] | None = None
    # Prompt name for a WRITE-capable pass inserted between a failed deterministic_verify and the
    # redraft. Needed when the stage itself cannot act on its own findings: adversarial-compliance is
    # read-only by design (an auditor that edits the code it audits is not an audit), so blocking on
    # its divergences with no fix step made the stage structurally unable to pass -- it re-audited,
    # found the same real gaps, and exhausted its cycles.
    verify_fix_prompt: str | None = None
    """Same signature as post_audit_hook, but fired from every place a stage reaches "approved" --
    gate_node, auto_approve_node, AND make_draft_node's hydrate_from_repo_file short-circuit.

    That last one is the reason this exists as a separate hook rather than more post_audit_hook
    users: hydration marks a stage approved inside draft_node and routes "already_approved"
    straight to the next stage, bypassing audit_node entirely -- so a post_audit_hook never runs
    again for a repo that has been onboarded once. Deterministic follow-up writes that must stay
    applied for the life of the repo (tech-stack's convention files) therefore belong here, not
    there; writes that are genuinely about *this run's* draft (raw-requirements' seed text) stay
    on post_audit_hook.

    Called with the stage's approved content, after persistence, and must be idempotent -- it runs
    on every single run, including pure no-op re-runs."""

    deterministic_verify: (
        Callable[[str, dict[str, Any], str, str | None, SandboxProvider, str], Awaitable[VerificationResult]] | None
    ) = None
    """A routing-capable check (thread_id, revised content dict, run_id, baseline_commit,
    provider, chat_provider) -> VerificationResult, inserted between audit and gate when set.
    baseline_commit is the value StageSpec.capture_baseline_commit stored on this stage's
    StageState (None if that flag is unset) -- write_scope_gate.py's write-scope check is the
    reason this exists; ledger-/diagram-style checks that don't need it just ignore the argument.
    `chat_provider` (added by Ruling 4/Task 5 fix round 2, docs/superpowers/plans/
    part-4-org-settings-tasks.md) is this run's own pinned `state["provider"]` ("claude"/"copilot"),
    threaded through because two real implementations (write_scope_gate.verify_ac_to_tests,
    test_coverage_gate.verify_coverage) call into stack_runner.run_and_report, which now requires
    it (Ruling 4) -- named distinctly from `provider` (the pre-existing SandboxProvider connection
    object every implementation already takes) to avoid colliding with it, same disambiguation
    chat_model.read_skill_invocations uses. Every OTHER implementation (ledger sync, diagram
    render, remediation, adversarial-compliance, exit readiness) has no dispatch call of its own
    and simply accepts-and-ignores this argument, the same way they already ignore baseline_commit
    when it isn't relevant. Never LLM self-attestation -- a real script/parse. Failing routes back
    to draft (with VerificationResult.feedback as context) up to max_verify_cycles, then to a
    human-interrupt escalation node -- never auto-approved past a failed deterministic gate."""

    max_verify_cycles: int = 3
    """Safety cap for the verify->draft retry loop, independent of max_cycles (the LLM's own
    clarification-loop cap)."""

    hydrate_from_repo_file: Callable[[str, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
    """Idempotency short-circuit (thread_id, state, provider) -> pre-approved content dict, or
    None to draft normally. Invoked by make_draft_node before it ever calls ainvoke_structured --
    lets a stage skip its LLM call entirely and hydrate as already-approved when its own artifact
    already exists on disk (and, where relevant, there's no fresh human-submitted input this run
    that should override the skip)."""

    draft_prompt_context_from_repo_file: (
        Callable[[str, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None
    ) = None
    """Same (thread_id, state, provider) -> Awaitable[dict[str, Any] | None] shape as
    hydrate_from_repo_file, but for a different purpose: hydrate_from_repo_file's non-None return
    SKIPS drafting entirely (see its own docstring above), which is wrong for a stage whose
    repo-file check should only change how the draft prompt is FRAMED, never whether a real
    draft/audit/gate cycle runs (Global Constraint, docs/superpowers/plans/part-3-tickets-tasks.md:
    "hydrate checks decide how much a stage's draft has to do, never whether audit or the human
    gate run"). make_draft_node calls this (same guards as hydrate_from_repo_file) immediately
    before build_prompt and, on a non-None return, merges it into a PROMPT-ONLY copy of this
    stage's StageState -- never the state actually persisted/returned by draft_node, since the
    signal is cheap to recompute every call and has no "must stay stable across this run's retry
    cycles" requirement (unlike capture_baseline_commit) to protect. None leaves the prompt exactly
    as it would be without this hook. Specification's ticket-mode segment
    (spec_ledger.hydrate_ticket_mode_context) was the first user of this hook; plan
    (hydrate_plan_ticket_mode_context) and ac-to-tests (spec_ledger.
    hydrate_ac_to_tests_ticket_mode_context) have since added their own ticket-mode reframes on the
    same shape -- named here rather than counted, since a count goes stale the next time a stage
    adds one."""

    session_options: Callable[[GraphState, str], dict[str, Any]] | None = None
    """Extra kwargs (agent_mode/available_tools/excluded_tools/pre_tool_use_hook/mcp_servers) to
    forward to get_chat_model_for_thread, called with (state, "draft"|"audit") so a stage can
    give its audit pass different (typically stricter/read-only) options than its draft pass --
    P4's audit is always read-only even though its draft gets real write access, per the
    "adversarial second opinion never has more trust than it needs" principle every other audit
    node already follows implicitly. None preserves today's behavior exactly (unrestricted "plan"
    mode) -- set for a stage that needs the read-only available_tools allowlist (Phase A0's spike
    finding: an allowlist, not a blocklist, is what actually enforces read-only) or, later, real
    write access."""

    use_custom_agent: bool = False
    """Default False: the custom_agents mechanism is BROKEN in the GHCP SDK -- a session created
    with custom_agents silently drops the agent's own declared `tools:`, leaving the model with no
    tools and no error (isolated repro in agent-work/ghcp-bug-report-custom-agents-tools.md). It
    bit four stages before being defaulted off: ac-to-tests and minimal-code-to-green wrote no
    files, the rebuild fix node could not edit, and plan reported "I can't inspect the repository"
    on any stack where session_options did not happen to pass available_tools as well. Tools now
    come from session_options/available_tools, which IS honored; models come from models.yaml.

    True routes make_draft_node through the custom_agents/agent mechanism, using
    session_options' own available_tools/model config instead. ac-to-tests sets this: empirically
    confirmed (not just theorized) by directly comparing "custom_agents + builtin:create in its
    tools:" (still failed -- zero test files, same as before create was ever added) against
    "no custom_agents + available_tools including builtin:create" (worked, ac-to-tests approved)
    in back-to-back full pipeline runs on the same repo/stack. Whatever's silently dropping/
    ignoring part of a custom agent's own tools: list, available_tools is not affected by it."""

    prefill_from_repo_file: Callable[[str, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
    """Like hydrate_from_repo_file, but the result becomes an UNAPPROVED draft
    (status="ready_for_review") rather than auto-approved -- it still passes through the human
    gate normally. For "this stage's own artifact file already exists on disk but has never been
    reviewed through this tool" (the Tech Stack tab's "tech-stack.md exists" case): skip the LLM
    call, show the file's own content, let the human confirm or edit it via the normal gate."""

    build_interrupt_extra: Callable[["GraphState"], dict[str, Any]] | None = None
    """Extra keys merged into this stage's interrupt() payload alongside {"stage", "draft"} --
    computed synchronously from state right before pausing, no I/O (whatever a stage needs was
    already gathered at draft time). None adds nothing, today's exact payload shape."""

    resolve_from_interrupt: Callable[[str, Any, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
    """(thread_id, raw interrupt() resume value, state, provider) -> override content dict, or
    None to keep today's behavior (approve stage["draft"] verbatim -- the resume value is
    otherwise discarded entirely). Only called when a sandbox is registered for this thread, same
    guard as every other hook here that does real I/O. Lets a stage's gate actually receive
    edited content back from the frontend instead of just re-approving whatever was drafted --
    the Tech Stack tab's Submit needs this to save the human's edits, not the LLM's draft."""

    capture_baseline_commit: bool = False
    """When True, make_draft_node captures `git rev-parse HEAD` into this stage's StageState the
    first time draft_node runs this run (guarded by "not already set," see make_draft_node) --
    the write-scope gate's (agent/src/gates/write_scope_gate.py) "before this stage touched
    anything" reference point. False (the default) preserves today's behavior for every stage
    that doesn't need to diff its own writes against a pre-stage baseline."""

    sign_approval: bool = False
    """When True, make_gate_node appends a content-hash-signed row to APPROVALS.md (approvals.py)
    on approval, in the same commit as the rest of that gate's persistence. False (the default)
    preserves today's behavior for stages that don't need approval-integrity tracking (tech-stack,
    raw-requirements) -- set for specification/plan, whose approved content other stages build on
    and whose approval is worth being able to detect tampering/accidental edits against later."""


STAGES: list[StageSpec] = [
    StageSpec(
        key="tech-stack",
        response_schema=TechStackDraftResponse,
        content_field="tech_stack",
        surface_tool_name="present_tech_stack",
        build_envelope=build_tech_stack_envelope,
        build_prompt=_build_tech_stack_prompt,
        max_cycles=workflow_config.TECH_STACK_MAX_CLARIFICATION_CYCLES,
        render_markdown=render_tech_stack_markdown,
        requires_human_gate=True,
        post_approve_hook=preflight_nodes.apply_stack_conventions,
        hydrate_from_repo_file=preflight_nodes.hydrate_tech_stack_from_repo_file,
        prefill_from_repo_file=preflight_nodes.prefill_tech_stack_from_repo_file,
        build_interrupt_extra=_build_tech_stack_interrupt_extra,
        resolve_from_interrupt=preflight_nodes.resolve_tech_stack_submission,
        session_options=lambda _state, _role: {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS},
    ),
    # raw-requirements is deliberately NOT a StageSpec anymore: the human's text is accepted
    # as-is by the deterministic record_raw_requirements_node (no draft, no audit, no gate) and
    # the specification stage does all processing. Its stage KEY still exists in state (see
    # _STAGE_KEYS / _RENDER_MARKDOWN_BY_STAGE) so hydration, persistence, the frontend
    # Requirements tab, and exit's content hash keep working unchanged.
    StageSpec(
        key="specification",
        response_schema=SpecificationDraftResponse,
        content_field="specification",
        surface_tool_name="present_specification",
        build_envelope=build_specification_envelope,
        build_prompt=_build_specification_prompt,
        max_cycles=workflow_config.SPEC_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=SpecificationAuditResponse,
        audit_content_field="revised_specification",
        build_audit_prompt=_build_specification_audit_prompt,
        render_markdown=render_specification_markdown,
        deterministic_verify=_verify_specification_ledger,
        draft_prompt_context_from_repo_file=spec_ledger.hydrate_ticket_mode_context,
        # Second phase of the two-phase tracking reset (spec_ledger.PENDING_RESET_FIELD): the
        # destructive stamp clear for genuinely-reworded ACs runs only once a human (or headless
        # auto-approve) actually approved the Specification -- a rejected draft's markers stay
        # inert and are swept by the next sync.
        post_approve_hook=spec_ledger.apply_tracking_resets_hook,
        sign_approval=True,
        # The one stage that may use `brainstorming` -- exploring intent and requirements before
        # anything is built is precisely its purpose. It stays disabled for every other stage
        # (config.COPILOT_DISABLED_SKILLS), where it fires as a blanket mandate on mechanical work.
        session_options=lambda _state, _role: {
            "disabled_skills": workflow_config.COPILOT_DISABLED_SKILLS_SPECIFICATION
        },
    ),
    StageSpec(
        key="plan",
        response_schema=PlanDraftResponse,
        content_field="plan",
        surface_tool_name="present_plan",
        build_envelope=build_plan_envelope,
        build_prompt=_build_plan_prompt,
        max_cycles=workflow_config.PLAN_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=PlanAuditResponse,
        audit_content_field="revised_plan",
        build_audit_prompt=_build_plan_audit_prompt,
        render_markdown=render_plan_markdown,
        sign_approval=True,
        deterministic_verify=verify_plan_diagrams,
        # US/AC -> plan-step provenance: record which approved steps cite each live AC
        # (spec_ledger entries' plan_step_ids). Overwrite semantics -- idempotent on resume.
        post_approve_hook=spec_ledger.stamp_plan_links_hook,
        draft_prompt_context_from_repo_file=hydrate_plan_ticket_mode_context,
        # Wireframes are LLM-emitted self-contained HTML validated by verify_plan_diagrams -- no
        # MCP servers needed (Excalidraw MCP deleted: never spike-tested, fetched unpinned
        # `npx -y mcp-excalidraw` at runtime, and had no export path). The UI-repo branch keeps
        # the same read-only allowlist it had when the MCP was attached.
        # Unconditional read-only tools: the plan agent must inspect the repo and
        # .ai-dev-workflow/tech-stack.md to plan concretely, and it never writes. The previous
        # UI-framework conditional left non-UI stacks (e.g. blazor-dotnet) with no tools at all
        # once custom_agents stopped supplying them -- the agent answered "I can't inspect the
        # repository contents from this interface" and the stage died with an empty plan.
        session_options=lambda _state, _role: {
            "available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS
        },
        # Above the default 3 because this stage's one recurring failure -- not invoking
        # writing-plans -- is now answered by restarting the draft session (see make_verify_node),
        # and a restart only helps if there are laps left to spend on it. At 3, the first attempt
        # plus one reset exhausted the budget before a fresh session got a fair chance.
        max_verify_cycles=5,
    ),
    StageSpec(
        key="ac-to-tests",
        response_schema=AcceptanceCriteriaTestsDraftResponse,
        content_field="test_suite",
        surface_tool_name="present_ac_to_tests",
        build_envelope=build_ac_to_tests_envelope,
        build_prompt=_build_ac_to_tests_prompt,
        max_cycles=workflow_config.AC_TO_TESTS_MAX_CLARIFICATION_CYCLES,
        # User decision 2026-08-24: ac-to-tests writes the failing TDD-RED tests every downstream
        # stage builds against, so it now gets the same second-model adversarial leg as
        # specification/plan/minimal-code-to-green -- a bad test suite here is a bad foundation for
        # everything after it.
        audit_response_schema=AcToTestsAuditResponse,
        audit_content_field="revised_test_suite",
        build_audit_prompt=_build_ac_to_tests_audit_prompt,
        render_markdown=render_ac_to_tests_markdown,
        requires_human_gate=False,
        capture_baseline_commit=True,
        deterministic_verify=verify_ac_to_tests,
        draft_prompt_context_from_repo_file=spec_ledger.hydrate_ac_to_tests_ticket_mode_context,
        # Higher than the default 3: this stage's dominant failure is a FLAKE, not a hard block --
        # the model returns a fully-detailed coverage_plan claiming it "created failing RED-phase
        # tests" while making zero write calls (confirmed from its own session log: glob/view/skill
        # only). The gate catches the fabrication every time and the redraft usually succeeds, so
        # the cheapest reliability win is simply not running out of retries mid-flake.
        max_verify_cycles=6,
        # Root cause of the long escalation streak: `builtin:edit` only edits EXISTING files -- a
        # greenfield repo with no test files yet needs `builtin:create`. That alone wasn't the
        # full story: also needed the session's working directory to actually be /workspace/repo
        # (without it, file tools are never even attempted) -- true unconditionally now, since
        # every CLI-exec turn already execs with cwd /workspace/repo (provider.exec_in_sandbox's
        # own -w/cd handling, both providers), not something a per-session setting has to
        # establish anymore the way the old SDK session once required.
        # use_custom_agent=False is EMPIRICALLY required, not stylistic -- back-to-back full runs
        # showed custom_agents + builtin:create in its own tools: list still failing (zero test
        # files, identical symptom to before create existed), while available_tools with the same
        # tool list worked. Something in custom_agents' tool resolution silently drops/ignores part
        # of its own declared list; available_tools isn't affected by it.
        use_custom_agent=False,
        session_options=lambda state, role: (
            {
                "agent_mode": "autopilot",
                "available_tools": [
                    "builtin:view", "builtin:grep", "builtin:glob",
                    "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
                ],
                # No bash here, deliberately reconsidered for Task 8 (retired-AC test cleanup) and
                # rejected, not merely never proposed. write_scope_gate.check_write_scope reverts by
                # RESULTING path regardless of which tool produced a change, so bash could not have
                # smuggled an out-of-scope FILE edit past that gate -- but that gate is file-diff-only
                # and has no visibility at all into what else a shell can do in the same turn: exfiltrate
                # a sandbox secret (GITHUB_TOKEN, the git-credential-helper script) over the network
                # before any revert ever runs, write into `.git/hooks/` to persist past a file-level
                # revert, read outside the repo, spawn processes. That is a categorically different risk
                # than "can this tool's file writes be reverted," and the practical need (a retired AC's
                # test stops running/blocking) is fully met without it: `edit`/`apply_patch` already
                # remove one test case from a file with siblings, and empty a file down to nothing (or a
                # one-line "intentionally retired" comment) when it was the last test in it -- the file
                # lingers, inert, in the tree; Adversarial Review's dead-code scan is the intended
                # backstop for that cosmetic residue, not a reason to grant shell execution here.
                # No Playwright MCP here. It was attached for UI repos so the drafting model could
                # drive a live browser while writing specs -- but this stage runs in the TDD RED
                # phase, before any implementation exists, so on a greenfield run there is nothing
                # to browse. Selectors are now authored, not discovered: the codegen stage is
                # required to put a `data-testid` on every element a test touches, and these specs
                # locate by `getByTestId`. Against that, the MCP is pure risk -- a wrong server name
                # once "cascaded into the whole session losing every OTHER tool too", leaving this
                # stage with no edit tool and 8+ runs escalating with zero test files. The server
                # stays in the image for a future stage that audits a RUNNING app.
            }
            if role == "draft"
            else {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS}
        ),
    ),
    StageSpec(
        key="minimal-code-to-green",
        response_schema=MinimalCodeToGreenDraftResponse,
        content_field="iteration",
        surface_tool_name="present_minimal_code_to_green",
        build_envelope=build_minimal_code_to_green_envelope,
        build_prompt=_build_minimal_code_to_green_prompt,
        max_cycles=workflow_config.MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=MinimalCodeToGreenAuditResponse,
        audit_content_field="revised_iteration",
        build_audit_prompt=_build_minimal_code_to_green_audit_prompt,
        render_markdown=render_minimal_code_to_green_markdown,
        deterministic_verify=verify_coverage,
        # Coverage verification (the deterministic gate) is the real check; the human checkpoints
        # are specification and plan only.
        requires_human_gate=False,
        # Higher than the default 3: closing a real coverage gap is iterative, and each lap makes
        # measurable progress (observed live: a genuine app landed at 100% lines / 83.3% branches
        # and simply ran out of laps before reaching the 95% branch threshold). A stage that is
        # genuinely stuck still fails -- just after it has actually had a chance to converge.
        # Raised 6 -> 12 after a vue-dotnet run climbed 88.1% -> 91.9% branches (74/84 -> 79/86)
        # over six laps and was cut off mid-convergence: the remaining gap was a handful of guard
        # clauses, and each lap was closing roughly one. Six laps is enough to prove a stage is
        # moving, not enough to let it finish; a truly stuck stage still burns out, just later.
        max_verify_cycles=12,
        # Draft gets full, unscoped write access -- "minimal code to green" is definitionally a
        # code-writing task (Part A Decisions point 6, tier (iii)). Audit stays read-only, same
        # asymmetry as P4's session_options.
        # Same empirically-confirmed fix ac-to-tests needed: with custom_agents in play the
        # session silently loses part of the agent's own declared `tools:` list, so the model
        # cannot write files and answers in JSON prose instead. Observed live here too --
        # minimal-code-to-green "completed" a full Angular+.NET scaffold in 18 seconds having
        # written nothing at all. available_tools is honored where the custom agent's list is not.
        use_custom_agent=False,
        session_options=lambda _state, role: (
            {
                "agent_mode": "autopilot",
                # builtin:task (Claude's subagent-launch tool, real CLI name "Agent"): the prompt
                # has ALWAYS mandated subagent-driven-development/dispatching-parallel-agents
                # here, but until the 2026-08-24 audit nothing mapped a subagent tool into
                # --tools, so the mandate was dangling -- the model was told to delegate with a
                # tool the allowlist had stripped. Copilot's CLI has no such tool;
                # copilot_chat_model filters it (_CLAUDE_ONLY_TOOLS) before --available-tools.
                "available_tools": [
                    "builtin:view", "builtin:grep", "builtin:glob", "builtin:bash",
                    "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
                    "builtin:task",
                ],
            }
            if role == "draft"
            else {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS}
        ),
    ),
    StageSpec(
        key="remediation",
        response_schema=RemediationDraftResponse,
        content_field=None,  # the whole response is the report -- see _stage_content
        surface_tool_name="present_remediation",
        build_envelope=build_remediation_envelope,
        build_prompt=_build_remediation_prompt,
        max_cycles=2,
        render_markdown=render_remediation_markdown,
        requires_human_gate=False,
        # Nothing read this stage's output until now: it could report "fixed the pre-auth RCE" and
        # be believed. The gate re-scans and blocks on any finding still gating that the report does
        # not explain. capture_baseline_commit is what lets it diff the stage's own changes and
        # catch a scanner being silenced instead of a defect being fixed.
        capture_baseline_commit=True,
        deterministic_verify=remediation_gate.verify_remediation,
        draft_prompt_context_from_repo_file=hydrate_remediation_ticket_mode_context,
        max_verify_cycles=3,
        # Full write access + bash: this stage upgrades dependencies (npm install / dotnet add) and
        # edits source to fix scanner findings. Without them it could only ever describe the work --
        # which is exactly what it did, for every run, until now. builtin:task is what lets the
        # required code-simplifier agent launch (REQUIRED_SKILLS_BY_STAGE's "agent:code-simplifier"
        # entry) -- without the mapping the requirement would be physically unsatisfiable.
        session_options=lambda _state, _role: {
            "agent_mode": "autopilot",
            "available_tools": [
                "builtin:view", "builtin:grep", "builtin:glob", "builtin:bash",
                "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
                "builtin:task",
            ],
        },
    ),
    StageSpec(
        key="adversarial-compliance",
        response_schema=AdversarialAuditDraftResponse,
        content_field="report",
        surface_tool_name="present_adversarial_compliance",
        build_envelope=build_adversarial_audit_envelope,
        build_prompt=_build_adversarial_compliance_prompt,
        max_cycles=workflow_config.ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES,
        render_markdown=render_adversarial_audit_markdown,
        requires_human_gate=False,
        # Read-only, but it MUST have tools: the prompt says "you are read-only in this session" and
        # asks it to verify every Plan step and AC against the actual code and wireframes. It
        # previously had none at all, so it could only speculate -- which is how a stage meant to be
        # the last substantive gate approved every run it ever saw.
        session_options=lambda _state, _role: {
            "available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS
        },
        deterministic_verify=adversarial_gate.verify_adversarial_compliance,
        # 6, not 3: this stage's fix laps carry the whole back-half workload (wireframe
        # conformance, negative-path e2e specs, frontend unit tests) and each lap is ~8 minutes of
        # real multi-file work. Observed live (s04 run 6): 3 laps all made measurable progress --
        # the audit's own findings went from absent panels to "closer now, still not full match" --
        # and the run was cut off mid-convergence, same failure shape that moved
        # minimal-code-to-green from 6 to 12.
        max_verify_cycles=6,
        verify_fix_prompt="adversarial_compliance_fix",
    ),
    StageSpec(
        key="metrics-exit",
        response_schema=ExitDraftResponse,
        content_field="report",
        surface_tool_name="present_exit",
        build_envelope=build_exit_envelope,
        build_prompt=_build_metrics_exit_prompt,
        max_cycles=workflow_config.EXIT_MAX_CLARIFICATION_CYCLES,
        # None, not render_exit_markdown: exit_finalize_node (the post_approve_hook below) is the
        # ONLY writer of 09-metrics-exit.md -- it renders the full report (health table, findings
        # dispositions, scanner tools), and a generic persist here would overwrite that file with
        # the 4-section stub at the start of every SUBSEQUENT run (approved_content survives
        # intake's reset on purpose; see intake_node).
        render_markdown=None,
        requires_human_gate=False,
        sign_approval=True,
        # Read-only tools, added because this stage SIGNS THE MERGE VERDICT. Its prompt is real and
        # it receives spec/plan/metrics JSON, so it was never a stub -- but with no tools it could
        # only restate what it was handed, unable to check a single claim against the branch it is
        # declaring merge-ready.
        session_options=lambda _state, _role: {
            "available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS
        },
        deterministic_verify=exit_nodes.verify_exit_readiness,
        # Was 0, on the (then-true) reasoning that verify_exit_readiness always returns passed=True
        # so no retry could ever be needed. The skill gate now runs BEFORE deterministic_verify and
        # CAN fail, which turned any missed skill here into an instant, unrecoverable run failure --
        # observed live: one `invoked: []` at metrics-exit ended a run that had cleared every other
        # stage. Any stage with a required skill needs laps to correct it (asserted below).
        max_verify_cycles=3,
        # Every run reaches this stage's approval (requires_human_gate=False, deterministic_verify
        # always returns passed=True) -- this is the only place exit_finalize_node ever runs; it
        # was never add_node'd/wired before this hook existed.
        post_approve_hook=exit_nodes.exit_finalize_node,
    ),
]



async def _persist_if_sandboxed(
    thread_id: str, state: GraphState, stages: dict[str, Any], commit_message: str
) -> None:
    """Best-effort persistence (architecture plan Section B) -- a no-op when no sandbox is
    registered for this thread (Section A not wired up, or the thread predates sandboxing).

    Failures are logged and swallowed, not raised: this runs inside gate/audit/auto_approve
    nodes, and a transient persistence failure (e.g. the sandbox idled out between provisioning
    and this gate resolving -- flagged as an open gap in the plan's Section B, "re-provision
    sandbox on demand" is not implemented here) should not block the human's actual approval
    action, which is durable in the in-memory checkpoint regardless.
    """
    sandbox = sandbox_registry.get(thread_id)
    if sandbox is None:
        return
    try:
        provider = get_sandbox_provider()
        await workflow_persistence.persist_state(
            provider,
            thread_id,
            stages=stages,
            render_markdown=_RENDER_MARKDOWN_BY_STAGE,
        )
        await git_ops.commit_ai_dev_workflow(provider, thread_id, commit_message)
    except Exception:
        logger.warning("Failed to persist workflow state for thread_id=%s", thread_id, exc_info=True)


async def _run_post_approve_hook(
    stage_spec: "StageSpec", thread_id: str, content: dict[str, Any] | None, state: GraphState
) -> None:
    """Fires StageSpec.post_approve_hook from all three places a stage becomes "approved"
    (gate_node, auto_approve_node, and make_draft_node's hydrate short-circuit) -- and, since this
    is the one choke point all three already share, also updates the session row's current_stage
    (drives the session-list UI's progress indicator) unconditionally, whether or not the stage
    has its own post_approve_hook.

    Failures are logged and swallowed for the same reason _persist_if_sandboxed swallows its own:
    a convention file that couldn't be written must not take down a run whose approval already
    happened. The hook itself is expected to record its own partial failures where they matter.

    Known limitation (Task 10 sweep item #2, mirrors a pre-existing current_stage gap): a session
    whose sandbox is evicted while paused at a gate, then approved anyway, skips this whole
    function -- including the update_current_stage call below that clears awaiting_gate -- and can
    leave that column stuck True until the next touch_run. Only affects the Board's cosmetic ⏸
    marker; not fixed here since the real fix (decoupling awaiting_gate's clear from this sandbox
    guard) would touch this hot, already-reviewed choke point for a cosmetic-only edge case.
    """
    if sandbox_registry.get(thread_id) is None:
        return
    try:
        await session_store.update_current_stage(thread_id, stage_spec.key)
    except Exception:
        logger.warning("update_current_stage failed for stage=%s thread_id=%s", stage_spec.key, thread_id, exc_info=True)
    if stage_spec.post_approve_hook is None or not content:
        return
    try:
        await stage_spec.post_approve_hook(thread_id, content, state, get_sandbox_provider())
    except Exception:
        logger.warning(
            "post_approve_hook failed for stage=%s thread_id=%s", stage_spec.key, thread_id, exc_info=True
        )


def _split_text_and_attachments(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Split a HumanMessage's content into its text and any non-text (AG-UI InputContent)
    parts. A plain string (every submission before multimodal attachments existed, and every
    text-only submission since) passes through unchanged with no attachments.
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
            elif isinstance(part, dict):
                attachments.append(part)
        return "\n".join(text_parts), attachments
    return str(content), []


async def _resolve_thread_provider(thread_id: str, state: GraphState) -> Literal["copilot", "claude"]:
    """The 3-level provider-pinning fallback GraphState.provider's own comment documents in full:
    prefer this thread's already-pinned state, then this session's own durable `dbo.sessions` row,
    resolve the live org setting only if genuinely neither exists. Split out of intake_node (its
    only caller) so this one expression has a direct, real self-check below instead of needing to
    drive intake_node's much larger body just to reach it.

    Pinned per-THREAD, not re-resolved per intake call -- see GraphState.provider's own comment for
    the full reasoning (Ruling 2, docs/superpowers/plans/part-4-org-settings-tasks.md) and for why
    this deliberately does NOT follow run_id's unconditional-remint pattern. The `or` chain
    short-circuits at each step -- both across plain values AND across `await`s, since Python
    evaluates operands left-to-right and stops at the first truthy one -- so the DB reads below only
    ever fire the first time this process has seen this thread (a brand new thread, or the first
    intake after a restart -- InMemorySaver keeps neither): every later intake on the same thread,
    including a blank-resume reattach, keeps whatever this run already pinned and touches the DB
    not at all.

    The middle term (Phase E audit I-3 review, Important 1) prefers this session's OWN stored
    `dbo.sessions.provider` over a live re-resolve when the checkpoint is empty -- without it, a
    post-restart intake and sessions_api.provision_session's own stored-or-live resolution could
    each independently land on a DIFFERENT value if the org setting changed since this session was
    first provisioned (intake live-resolving the NEW setting while the container was already baked,
    or about to be rebaked, for the OLD stored one) -- the exact split-provider hazard this field
    exists to prevent, just relocated from "mid-run" to "across a restart." `get_session` returning
    None (a genuinely new thread) or a row with no stored provider (pre-migration-0008) both fall
    through to the live read exactly as before.

    The `get_session` call is try/excepted (Phase E audit I-3 review round 2) -- unlike
    chat_model.get_provider() right after it, which was already hardened (chat_model.py:149-204,
    Part 4 whole-branch review) to serve stale cache or the env var rather than raise on a DB blip.
    Without this, a transient DB error here raises straight through intake_node and hard-fails the
    run -- and per this field's own comment above, this branch runs on EVERY thread's first
    post-restart intake, a permanent steady-state condition, not a rare one-off. Falls through to
    get_provider() on failure -- exactly the pre-fix behavior (live-resolve), so a DB blip degrades
    to "briefly no better than before I-3" rather than "run dies."
    """
    pinned = state.get("provider")
    if pinned:
        return pinned

    stored_provider = None
    try:
        stored_row = await session_store.get_session(thread_id)
        stored_provider = (stored_row or {}).get("provider")
    except Exception:
        logger.warning(
            "session_store.get_session failed while resolving provider for thread_id=%s -- "
            "falling through to the live org setting",
            thread_id, exc_info=True,
        )

    return stored_provider or await chat_model.get_provider()


async def intake_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    stages = {key: dict(value) for key, value in state.get("stages", {}).items()}

    provider: Literal["copilot", "claude"] = await _resolve_thread_provider(thread_id, state)

    # Popped unconditionally, right here, on EVERY intake -- a resume=1 provision sets this meta
    # flag once, and it must not survive past the first run that consumes it (else a later,
    # unrelated follow-up message on the same thread would also skip the fresh-run stage reset
    # below). Whether it actually APPLIES depends on `is_new_submission`, computed next.
    resume_flag_popped = sandbox_registry.pop_meta_flag(thread_id, "resume")

    # The Raw Requirements Text (AC-1.3/AC-6.2) is submitted as an ordinary
    # chat message — the human's "submit" action is agent.addMessage(...) +
    # runAgent() on the frontend. That message's content is a plain string for a text-only
    # submission, or a multimodal InputContent list when screenshots/documents were attached in
    # the Requirements area -- either way, only the text half becomes raw_requirements_text; any
    # attachments are carried separately (see GraphState) since they only matter to this specific
    # run's specification draft, not the persisted text itself.
    #
    # "Fresh submission" is decided by MESSAGE ID, not by text presence: AppShell's Resume button
    # fires a blank runAgent() call (no new message), which merges an EMPTY list into `messages`
    # via add_messages -- the checkpoint still ends in the ORIGINAL HumanMessage from the failed
    # run, so a text-presence check would find that old message's non-empty text and wrongly
    # conclude this run has new requirements. Comparing the latest HumanMessage's id against
    # consumed_message_id (this state channel, set below whenever a message is genuinely
    # consumed) tells the two apart: add_messages assigns every message a uuid if it arrives
    # without one, and LangGraph's checkpointer preserves that id verbatim across every later
    # invocation on the same thread, so the SAME still-checkpointed message always compares equal
    # to what was already consumed, while a real new submission (a fresh chat message, including a
    # clarification answer) always gets a new id.
    latest_human_message = next(
        (m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)), None
    )
    consumed_message_id = state.get("consumed_message_id")
    is_new_submission = latest_human_message is not None and latest_human_message.id != consumed_message_id

    raw_requirements_text = state.get("raw_requirements_text", "") if not is_new_submission else ""
    requirements_attachments: list[dict[str, Any]] = []
    if is_new_submission:
        assert latest_human_message is not None  # narrows for the type checker
        raw_requirements_text, requirements_attachments = _split_text_and_attachments(
            latest_human_message.content
        )
        consumed_message_id = latest_human_message.id

    # Hydration (architecture plan Section B.2): only when this thread has never had any stage
    # state in this process's memory yet -- i.e. genuinely the first invoke for this thread since
    # the agent process started, whether because it's a returning session after a restart, or a
    # different session picking up the same repo/branch/user. A thread already mid-session (any
    # prior invoke populated `stages`) never re-hydrates; its in-memory checkpoint is authoritative.
    if not stages and sandbox_registry.get(thread_id) is not None:
        hydrated = await workflow_persistence.hydrate_state(get_sandbox_provider(), thread_id, _STAGE_KEYS)
        if hydrated is not None:
            stages = hydrated
            logger.info("intake_node: hydrated prior workflow state for thread_id=%s", thread_id)

    # _ALL_STAGE_SPECS (STAGES + every standalone StageSpec: adversarial-audit/b/d, exit, brownfield-baseline-brownfield) is
    # assigned near the bottom of this module, after all of them are defined -- referencing it
    # here is fine, since intake_node only ever runs after the module has finished loading. Fixes
    # a real bug: standalone StageSpecs were never getting a default_stage_state() here at all,
    # so the first node touching e.g. stages["adversarial-audit"] would KeyError.
    for stage_spec in _ALL_STAGE_SPECS:
        stages.setdefault(stage_spec.key, default_stage_state())

    # AC-6.3: a Plan that had already advanced is reset to Not Started; its
    # last content stays visible (AC-8.4) but is no longer current/approved.
    # tech-stack and raw-requirements are both supporting/evergreen artifacts (not "review this
    # run's draft" stages the way specification/plan still are) -- once approved, they stay
    # approved across every later fresh run on the same thread; each one's own draft node
    # idempotency check (not this reset loop) is what decides whether it needs to redraft. Every
    # other stage (specification onward plus every standalone StageSpec) resets on a fresh run.
    #
    # AIDW_RESUME=1 (headless --thread resume) OR a popped `resume` meta flag (the frontend's
    # Resume button, /select's history UI) skip the approved-stage reset so a rerun on the same
    # thread/sandbox picks up at the first UNapproved stage -- hydration above restored every
    # approved stage from the repo (or the in-memory checkpoint already has them, if the agent
    # process never restarted), and make_draft_node's approved short-circuit routes each one
    # straight to the next. Per-run mechanics (verify counters) still reset below.
    #
    # The meta flag only counts here when `not is_new_submission` -- both the restart-resume path
    # (an empty checkpoint, no HumanMessage at all) and a live-thread Resume click (checkpoint's
    # message list unchanged, same id as last consumed) satisfy that. A genuinely new requirements
    # submission (fresh id, including a clarification answer) never does, so it can't be misread
    # as a resume just because some earlier provision call set the flag.
    resume = os.environ.get("AIDW_RESUME") == "1" or (resume_flag_popped and not is_new_submission)
    for stage_spec in STAGES[1:] + _STANDALONE_STAGE_SPECS:
        stage = stages[stage_spec.key]
        # metrics-exit is NEVER skip-ahead eligible, resume or not: its whole job is to re-judge
        # THIS run. A hydrated approved metrics-exit takes make_draft_node's short-circuit, which
        # re-fires exit_finalize_node with the PREVIOUS run's approved content while bypassing
        # verify_exit_readiness entirely -- so a thread whose last run escalated (merge_ready=False,
        # "terminal pipeline failure" blocker) would stamp that stale failed verdict onto the
        # resumed run's fresh exit report and re-close its session "failed" even when everything
        # is now green.
        stage_resume = resume and stage_spec.key != "metrics-exit"
        # "drafting" (make_draft_node's start-of-turn mark) resets on a fresh submission like any
        # other per-run status, and is deliberately KEPT on a resume: it is the proof a killed
        # draft already touched the workspace (rebuild.py's TDD-red guard reads it).
        if not stage_resume and stage["status"] in ("ready_for_review", "approved", "drafting"):
            # Reset touches STATUS/mechanics only -- `approved_content` deliberately survives.
            # Load-bearing beyond hydration: on a fresh run 2, persist_state keeps rewriting
            # 04-plan.approved.json (and 03-specification.approved.json) from this surviving
            # value, which is exactly what diagram_gate.check_plan_linkage reads as the PRIOR
            # plan for its verbatim-carryover exemption until run 2's own gate approves a new
            # one. Clearing approved_content here would break that carryover detection.
            stage["status"] = "not_started"
            stage["cycle_count"] = 0
            stage["readiness"] = False
            stage["clarifying_questions"] = []
        elif stage_resume and stage["status"] == "ready_for_review":
            # A stage caught mid-review when the previous run died still redrafts -- only
            # APPROVED work is trusted for skip-ahead.
            stage["status"] = "not_started"
            stage["cycle_count"] = 0
            stage["readiness"] = False
            stage["clarifying_questions"] = []
        # Per-run mechanics reset unconditionally -- a stage whose previous run escalated at the
        # verify cap otherwise re-enters every later run already AT the cap, so its first
        # transient failure escalates instantly (observed live: spec verify logged cycle 3 on a
        # fresh run's very first attempt).
        stage["verify_cycle_count"] = 0
        stage["last_verification"] = None
        # A rejection's own feedback (Part 2 Task 10) is per-run the same way -- a fresh
        # submission must not hand a brand-new draft attempt guidance about a completely
        # different requirements round. Approval already clears it (make_gate_node); this is the
        # defense-in-depth counterpart for a thread that was left rejected and never resolved.
        stage["reviewer_feedback"] = None
        # Same reasoning for draft-level infra exhaustion (make_draft_node/make_draft_escalate_node):
        # a prior run's Copilot outage must not make this run's very first draft attempt
        # instant-escalate before it even tries. make_draft_escalate_node also clears this on its
        # own handling path; reset here too as the same defense-in-depth every other per-run
        # counter in this loop already gets.
        stage["infra_exhausted"] = False
        stage["last_infra_error"] = None
        # Stall-detector state (make_verify_node): a resume should get a fresh 2-lap window to
        # re-detect an in-progress stall rather than immediately treating this run's first verify
        # lap as "identical to whatever the previous run's last lap reported."
        stage["last_verify_feedback"] = None
        stage["last_verify_changed_paths"] = None
        stage["verify_stall_count"] = 0
        stage["best_verify_coverage_rate"] = None

    # e2e has no StageState (it's a bespoke cluster, see e2e_nodes.py) but its fix-cycle "attempt"
    # counter needs the exact same unconditional per-run reset as verify_cycle_count above -- a run
    # that escalated at the cap must not re-enter every later run already AT it.
    e2e_state = dict(state.get("e2e") or {})
    e2e_state["attempt"] = 0

    if not raw_requirements_text.strip():
        # Textless run (no new submission): hydration may have restored an already-approved
        # raw-requirements doc from the repo into `stages` -- the specification stage's draft
        # prompt reads raw_requirements_text directly (never stages["raw-requirements"]), so
        # leaving it empty here would draft the spec from nothing even though the approved doc
        # still exists on disk.
        hydrated_raw_requirements = (stages.get("raw-requirements") or {}).get("approved_content") or {}
        raw_requirements_text = hydrated_raw_requirements.get("content") or raw_requirements_text

    return {
        "stages": stages,
        # Resume keeps the run's identity: the ledger's first_seen/last_revised/coded/tested
        # stamps carry the ORIGINAL run_id, and change_status/exit rows derive "new/modified this
        # run" by comparing against state["run_id"] -- a reminted id on resume made every entry
        # the interrupted run created read as "unchanged" and split coded/tested across two ids.
        "run_id": (state.get("run_id") if resume and state.get("run_id") else uuid.uuid4().hex[:8]),
        "raw_requirements_text": raw_requirements_text,
        "requirements_attachments": requirements_attachments,
        # Unchanged (echoes state.get("consumed_message_id")) on a textless run; set to the newly
        # consumed message's id on a fresh submission -- either way, the next intake's dedupe
        # check compares against exactly what this run actually consumed.
        "consumed_message_id": consumed_message_id,
        # Echoes whatever this thread already pinned (see the `provider` local above and
        # GraphState.provider's own comment) unless this is genuinely the first intake this
        # process has seen for this thread, in which case it's this run's one live resolution.
        "provider": provider,
        "e2e": e2e_state,
    }


async def _stage_skills_evidence(
    thread_id: str, stage_key: str, state: "GraphState", self_reported: list[str] | None = None
) -> dict[str, Any]:
    """One stage's skill-evidence record, sandbox or not (2026-08-24 provider-evidence
    requirement: EVERY stage record carries the chat provider that ran it). With no sandbox
    registered, skills_record's None-provider path emits an honest
    {provider, verified: False, invoked: []} stub instead of skipping the record entirely --
    the shape every early-return path in make_draft_node reuses too, so "which provider ran
    this stage" never depends on which path the node exited through.

    The stage's previously persisted `invoked` list is unioned in (skills_record's
    prior_invoked): a redraft after a skill-gate session reset reads a FRESH transcript, and a
    plain overwrite here destroyed lap-1 evidence right before make_verify_node tried to union
    it -- the lap-oscillation bug the 2026-08-24 audit caught in this very change's first cut."""
    sandbox = get_sandbox_provider() if sandbox_registry.get(thread_id) is not None else None
    prior = (state["stages"].get(stage_key) or {}).get("skills") or {}
    return await skill_gate.skills_record(
        sandbox,
        thread_id,
        stage_key,
        self_reported,
        chat_provider=state["provider"],
        prior_invoked=prior.get("invoked"),
    )


def make_draft_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def draft_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        # Part 2 Task 10 review fix: a stage re-entering draft on a human REJECTION must not let
        # either short-circuit below silently re-serve the exact pre-existing repo file content
        # the human just rejected -- rejection never touches that file, so without this a rejected
        # tech-stack draft (the only StageSpec with either hook today) would re-hydrate/re-prefill
        # the SAME content, flip straight back to ready_for_review/approved, and re-present at the
        # gate with the feedback silently discarded forever (neither short-circuit touches
        # cycle_count, so auto-approve can't rescue it either). A rejection always means "don't
        # just trust what's already there" -- go draft for real so reviewer_feedback (folded in by
        # build_prompt below) actually reaches the model.
        just_rejected = bool(state["stages"][stage_spec.key].get("reviewer_feedback"))

        if stage_spec.hydrate_from_repo_file is not None and not just_rejected and sandbox_registry.get(thread_id) is not None:
            hydrated = await stage_spec.hydrate_from_repo_file(thread_id, state, get_sandbox_provider())
            if hydrated is not None:
                stages = {key: dict(value) for key, value in state["stages"].items()}
                stage = stages[stage_spec.key]
                used_ids: set[str] = set(stage["used_ids"])
                _extract_ids(hydrated, used_ids)
                stage["draft"] = hydrated
                stage["approved_content"] = hydrated
                stage["status"] = "approved"
                stage["readiness"] = True
                stage["ever_ready_for_review"] = True
                stage["used_ids"] = sorted(used_ids)
                # No session ran (hydrated straight from disk) -- record still says which provider
                # owned the run, verified False. Never overwrite real evidence a prior lap stored.
                if not stage.get("skills"):
                    stage["skills"] = await _stage_skills_evidence(thread_id, stage_spec.key, state)
                stages[stage_spec.key] = stage
                # The whole point of post_approve_hook (vs post_audit_hook): this branch routes
                # "already_approved" straight past audit_node and gate_node, so this is the ONLY
                # place a hook can run for a repo that has been onboarded before.
                await _run_post_approve_hook(stage_spec, thread_id, hydrated, state)
                return {"stages": stages}

        # Resume short-circuit: a stage hydrated back as approved (AIDW_RESUME runs) skips its
        # LLM call entirely -- make_route_after_draft's "already_approved" route then bypasses
        # audit and gate. Placed AFTER the hydrate_from_repo_file block so stages with a hydrate
        # hook (tech-stack) keep firing their post_approve_hook on every run. On normal runs
        # intake resets every spec-onward stage to not_started, so this never triggers there.
        stage_now = state["stages"][stage_spec.key]
        if should_skip_draft(stage_now):
            logger.info("draft skipped for already-approved stage %s (resume)", stage_spec.key)
            # Skipping the LLM is right; skipping the hook is not. exit_finalize is what writes this
            # run's exit report, refreshes the manifest and closes the session, and it is idempotent
            # by design (its PR creation already guards against re-opening one). Without this, a
            # resumed run that re-ran e2e and metrics left the PREVIOUS run's report on the branch
            # -- still reading "no e2e screenshots were captured" beside 14 fresh screenshots.
            await _run_post_approve_hook(stage_spec, thread_id, stage_now["approved_content"], state)
            if not stage_now.get("skills"):
                # A resumed stage usually hydrates its original run's evidence; only when that's
                # absent does this stub in the provider record ({} return would omit it entirely).
                stages = {key: dict(value) for key, value in state["stages"].items()}
                stages[stage_spec.key]["skills"] = await _stage_skills_evidence(thread_id, stage_spec.key, state)
                return {"stages": stages}
            return {}

        if stage_spec.prefill_from_repo_file is not None and not just_rejected and sandbox_registry.get(thread_id) is not None:
            prefilled = await stage_spec.prefill_from_repo_file(thread_id, state, get_sandbox_provider())
            if prefilled is not None:
                stages = {key: dict(value) for key, value in state["stages"].items()}
                stage = stages[stage_spec.key]
                stage["draft"] = prefilled
                stage["readiness"] = True
                stage["ever_ready_for_review"] = True
                stage["status"] = "ready_for_review"
                stage["clarifying_questions"] = []
                if not stage.get("skills"):
                    stage["skills"] = await _stage_skills_evidence(thread_id, stage_spec.key, state)
                stages[stage_spec.key] = stage
                logger.info("draft prefilled from repo file for stage %s, skipping LLM", stage_spec.key)
                return {"stages": stages}

        if (
            stage_spec.capture_baseline_commit
            and state["stages"][stage_spec.key].get("baseline_commit") is None
            and sandbox_registry.get(thread_id) is not None
        ):
            # Captured once (guarded by "not already set," not by cycle count) -- persists across
            # retry cycles within this run via LangGraph's checkpointed state; only reset to None
            # by an escalate node when a human is assumed to have intervened out-of-band. This is
            # the write-scope gate's (agent/src/gates/write_scope_gate.py) "before this stage
            # touched anything" reference point.
            provider = get_sandbox_provider()
            head_result = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
            stages = {key: dict(value) for key, value in state["stages"].items()}
            stages[stage_spec.key]["baseline_commit"] = head_result.stdout.strip() if head_result.ok else None
            state = {**state, "stages": stages}

        # draft_prompt_context_from_repo_file (see its own docstring on StageSpec): unlike the
        # hydrate_from_repo_file block above, a non-None return here must NOT skip drafting -- it
        # only reframes the prompt. Applied to a throwaway `prompt_state` used solely for the
        # build_prompt call just below, so it never rides along in this node's own `state`/
        # `stages` locals (which the final `return {"stages": stages}` further down is built from)
        # and is never persisted -- recomputed fresh from disk on every draft call instead.
        prompt_state = state
        if stage_spec.draft_prompt_context_from_repo_file is not None and sandbox_registry.get(thread_id) is not None:
            prompt_context = await stage_spec.draft_prompt_context_from_repo_file(thread_id, state, get_sandbox_provider())
            if prompt_context is not None:
                prompt_stages = {key: dict(value) for key, value in state["stages"].items()}
                prompt_stages[stage_spec.key] = {**prompt_stages[stage_spec.key], **prompt_context}
                prompt_state = {**state, "stages": prompt_stages}

        # Load custom agent if available, falling back to legacy session_options
        custom_agents = []
        agent_config = load_agent_for_stage(stage_spec.key, "draft") if stage_spec.use_custom_agent else {}
        if agent_config:
            custom_agents = [agent_config]

        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "draft",
            provider=state["provider"],
            # Task 3b (Part 2 Ruling 10): thread the real run_id through so a Copilot turn's own
            # tool-call RunEvents (copilot_chat_model.py's _agenerate_inner) carry it instead of
            # the "unknown" placeholder -- same sentinel-fallback convention this file already
            # uses at the RunEvent(...) construction just below.
            run_id=state.get("run_id", "unknown"),
            model_name=agent_config.get("model") if agent_config else model_config.get_model_name(stage_spec.key, "draft", state["provider"]),
            sandbox=sandbox_registry.get(thread_id),
            custom_agents=custom_agents if custom_agents else None,
            agent=agent_config.get("name") if agent_config else None,
            # session_options (agent_mode/hooks/mcp_servers/tool scoping) is orthogonal to
            # agent_config (model/prompt/tools list) -- both apply together, always. The old
            # `and not agent_config` guard here silently skipped session_options for every stage
            # with a custom agent file, which is every draft stage. Confirmed live: ac-to-tests
            # never got its "autopilot" agent_mode override and ran the whole time on
            # get_chat_model_for_thread's "plan" (read-only) default -- 10+ headless runs
            # escalating with zero test files written, because the session could plan but never
            # actually call edit.
            **(stage_spec.session_options(state, "draft") if stage_spec.session_options is not None else {}),
        )

        prompt_messages = stage_spec.build_prompt(prompt_state)
        # Headless mode (run_headless.py): the runner cannot answer clarifying questions, so a
        # not-ready draft would dead-end the run at the needs_clarification END edge. Checked at
        # call time on purpose -- no import-order coupling. The injected message is never
        # persisted (draft node returns only stages; audit prompts rebuild from the draft).
        if os.environ.get("AIDW_HEADLESS"):
            headless_note = (
                "Headless mode: clarifying questions are disallowed. Make reasonable "
                "engineering assumptions, record them in the document itself, and set "
                "readiness to true."
            )
            prior_questions = [
                q.get("question") for q in state["stages"][stage_spec.key].get("clarifying_questions") or [] if q.get("question")
            ]
            if prior_questions:
                # Redraft lap after a not-ready draft: the model must resolve its OWN questions.
                headless_note += (
                    " You previously asked: "
                    + " | ".join(str(q) for q in prior_questions)
                    + " -- answer each yourself with the most reasonable assumption, record the "
                    "assumption in the document, and deliver the COMPLETE draft with readiness true."
                )
            prompt_messages = [*prompt_messages, HumanMessage(content=headless_note)]

        # Durable + live NODE_STARTED (Part 2 Task 9 run-visibility) -- placed right before the
        # actual model call, not at this function's top, so it only ever fires on the path that can
        # reach this node's own NODE_FINISHED below (the hydrate/prefill/should_skip_draft
        # early-returns above never touch either event). Gives the wall-clock swimlane a real
        # start+end span for this node instead of only the single end-of-node point NODE_FINISHED
        # alone provides -- see task-9-report.md for why this was genuinely missing (no call site
        # anywhere emitted NODE_STARTED before this). An infra-exhausted draft (the TimeoutError/
        # RuntimeError branch just below) still returns early without a matching NODE_FINISHED --
        # a legitimate "started, never finished" span, not a bug; the swimlane renders that as
        # open-ended rather than silently dropping it. Same fail-soft two-call pattern (append_event
        # then emit_live, rebind before the live call) as every other RunEvent site in this file.
        if sandbox_registry.get(thread_id) is not None:
            start_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_STARTED,
                stage=stage_spec.key,
                node="draft",
                summary="draft started",
            )
            start_event = await run_event_store.append_event(start_event)
            await run_event_stream.emit_live(start_event, config)
            # Mark the stage "drafting" in state.json (and the session row's current_stage)
            # BEFORE the model call, so a draft killed mid-turn (timeout, quota) leaves durable
            # proof it began. Two consumers: rebuild.py's TDD-red guard, which must not mistake a
            # workspace with a half-written implementation for one codegen never touched (run
            # d16959d3: it did, and stubbed 200 passing tests back to red); and the board, whose
            # current_stage otherwise lagged a whole stage behind during the longest turns.
            # intake keeps "drafting" on a resume and resets it on a fresh submission.
            drafting_stages = {key: dict(value) for key, value in state["stages"].items()}
            drafting_stages[stage_spec.key]["status"] = "drafting"
            try:
                await _persist_if_sandboxed(thread_id, state, drafting_stages, f"ai-dev-workflow: {stage_spec.key} drafting")
                await session_store.update_current_stage(thread_id, stage_spec.key)
            except Exception:  # noqa: BLE001 -- bookkeeping must never block the draft itself
                logger.warning("could not mark %s as drafting", stage_spec.key, exc_info=True)

        try:
            response = await call_with_infra_retry(
                lambda: ainvoke_structured(model, prompt_messages, stage_spec.response_schema),
                label=f"{stage_spec.key}:draft",
            )
        except (TimeoutError, RuntimeError) as exc:
            # A Copilot session failure (quota, 429, stream hiccup) that survived infra_retry's own
            # backoff attempts. This must NOT consume cycle_count -- that budget bounds genuine
            # "not good enough yet" clarification attempts, and charging an infra outage against it
            # would just move the same "run dies for an infra reason" failure a few laps later while
            # shrinking the budget available for real fixes. There is also no new draft content to
            # hand an audit/verify step, so this cannot fall through to the stage's normal routing
            # the way a not-ready draft does -- it takes its own dedicated escalate edge instead
            # (see make_route_after_draft / _wire_stage's draft_escalate wiring).
            logger.warning("draft infra-exhausted for stage %s -- escalating without consuming cycle_count", stage_spec.key, exc_info=exc)
            stages = {key: dict(value) for key, value in state["stages"].items()}
            stages[stage_spec.key]["infra_exhausted"] = True
            stages[stage_spec.key]["last_infra_error"] = str(exc)[-2000:]
            if not stages[stage_spec.key].get("skills"):
                stages[stage_spec.key]["skills"] = await _stage_skills_evidence(thread_id, stage_spec.key, state)
            return {"stages": stages}

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        content = _stage_content(response, stage_spec.content_field)
        if content is None:
            content_dict = stage["draft"]
        elif isinstance(content, BaseModel):
            content_dict = content.model_dump(mode="json")
        else:
            content_dict = content

        used_ids: set[str] = set(stage["used_ids"])
        if content_dict is not None:
            _extract_ids(content_dict, used_ids)

        stage["draft"] = content_dict
        stage["clarifying_questions"] = [q.model_dump(mode="json") for q in response.clarifying_questions]
        stage["readiness"] = response.readiness
        stage["used_ids"] = sorted(used_ids)

        # Note: no A2UI envelope is built here even when readiness=true. That happens once, in
        # make_audit_node, against the *audited* (revised) content -- building it here too would
        # double-emit the surface (once pre-audit, once post-audit) for every ready draft.
        if response.readiness:
            stage["status"] = "ready_for_review"
            stage["ever_ready_for_review"] = True
        else:
            stage["status"] = "needs_clarification"
            stage["cycle_count"] = stage["cycle_count"] + 1

        # Skill evidence for EVERY stage, on every path -- not just when a required skill is missing.
        # Recorded here rather than in the verify branch because make_draft_node is the one node every
        # StageSpec has: doing it in verify skipped stages with no deterministic_verify entirely, and
        # skipped the pass path even where verify existed, which is why a green run left no trace that
        # any skill had been used. Persisted into state.json's per-stage record. Unconditional now
        # (2026-08-24 provider-evidence requirement): with no sandbox registered, skills_record's
        # None-provider path still emits a {provider, verified: False} record, so which coding-agent
        # provider ran a stage is never reconstructable-only.
        stage["skills"] = await _stage_skills_evidence(
            thread_id, stage_spec.key, state, self_reported=getattr(response, "skills_invoked", None)
        )

        stages[stage_spec.key] = stage

        if sandbox_registry.get(thread_id) is not None:
            # metrics-report's token-consumption tracking reads these ledger entries. model._last_usage is
            # None if no ASSISTANT_USAGE event fired (shouldn't happen in practice, but the field
            # is optional upstream, so tolerated here rather than assumed).
            await repo_files.append_ledger_entry(
                get_sandbox_provider(),
                thread_id,
                {"stage": stage_spec.key, "node": "draft", "readiness": response.readiness, "token_usage": model._last_usage},
            )
            # Durable counterpart (Part 2 run-visibility) -- same data, second destination. See
            # run_event_store.py's module docstring for why the ledger write above isn't enough on
            # its own (gone once this sandbox is torn down). Additive: the ledger write is untouched.
            # Fails soft by design -- append_event itself swallows+logs a DB error rather than
            # raising, so a transient blip here can never abort this node (see its own docstring).
            run_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_FINISHED,
                stage=stage_spec.key,
                node="draft",
                summary=f"draft {'ready for review' if response.readiness else 'needs clarification'}",
                payload={"readiness": response.readiness},
                token_usage=model._last_usage,
            )
            run_event = await run_event_store.append_event(run_event)
            # Live counterpart (Part 2 Task 2) -- same event, third destination: a LangGraph custom
            # event that ag_ui_langgraph's already-mounted bridge relays to the browser as an AG-UI
            # CUSTOM event while this run is still in progress. See run_event_stream.py's module
            # docstring for the real (verified against the installed packages) mechanism. Also
            # fails soft, same reasoning as append_event above.
            await run_event_stream.emit_live(run_event, config)

        return {"stages": stages}

    return draft_node


def make_audit_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Stringent second-opinion pass (SPECIFICATION.md-adjacent, see plan doc) run once per draft
    that reaches readiness=true, by a separately-configured model, before the human ever sees it.

    Only wired onto the "gate" routing branch (see build_graph) -- a draft forced through via
    auto_approve (the clarification-cycle safety cap) skips the audit entirely; it's already
    known-incomplete, and an adversarial pass over admittedly-incomplete content mostly just
    re-describes its own incompleteness.
    """

    async def audit_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]

        # Load custom agent if available, falling back to legacy session_options
        custom_agents = []
        # Same use_custom_agent gate the draft node applies. Without it the audit leg loaded
        # agents/<stage>-audit.md unconditionally, which (a) passed --agent <name> against a
        # --agents payload the Claude CLI never registers, so every audit turn died with
        # "--agent 'plan-audit' not found" and the stage redrafted forever, and (b) let that
        # file's Copilot-era `model: gemini-3.6-flash` shadow models.yaml's provider-keyed
        # choice. The audit system prompt lives in prompts/<stage>_audit.md (build_audit_prompt)
        # either way, so nothing is lost by skipping the agent file.
        agent_config = load_agent_for_stage(stage_spec.key, "audit") if stage_spec.use_custom_agent else {}
        if agent_config:
            custom_agents = [agent_config]

        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "audit",
            provider=state["provider"],
            # Task 3b (Part 2 Ruling 10) -- see the draft-role call's own comment above.
            run_id=state.get("run_id", "unknown"),
            model_name=agent_config.get("model") if agent_config else model_config.get_model_name(stage_spec.key, "audit", state["provider"]),
            sandbox=sandbox_registry.get(thread_id),
            custom_agents=custom_agents if custom_agents else None,
            agent=agent_config.get("name") if agent_config else None,
            # See the draft-role call's own comment above -- session_options must apply
            # regardless of agent_config; they're orthogonal concerns.
            **(stage_spec.session_options(state, "audit") if stage_spec.session_options is not None else {}),
        )

        prompt_messages = stage_spec.build_audit_prompt(state)
        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]
        audit_skipped_infra = False

        # Durable + live NODE_STARTED (Part 2 Task 9 run-visibility) -- same placement rationale as
        # make_draft_node's identical block above: right before the model call, paired with this
        # node's own NODE_FINISHED below on every path that reaches it (the ValidationError/
        # ValueError/TimeoutError/RuntimeError catches below still fall through to that same
        # NODE_FINISHED; only a genuinely unhandled exception would leave this unpaired, which the
        # swimlane renders open-ended rather than dropping). Same fail-soft two-call pattern as
        # every other RunEvent site in this file.
        if sandbox_registry.get(thread_id) is not None:
            start_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_STARTED,
                stage=stage_spec.key,
                node="audit",
                summary="audit started",
            )
            start_event = await run_event_store.append_event(start_event)
            await run_event_stream.emit_live(start_event, config)

        try:
            response = await call_with_infra_retry(
                lambda: ainvoke_structured(model, prompt_messages, stage_spec.audit_response_schema),
                label=f"{stage_spec.key}:audit",
            )
            content_dict = getattr(response, stage_spec.audit_content_field).model_dump(mode="json")
            audit_findings = list(response.audit_findings)
        except (ValidationError, ValueError) as exc:
            # An adversarial second opinion that never actually parses (observed live: 3
            # consecutive attempts all truncated mid-JSON, same root output-length issue every
            # retry, not a formatting fluke ainvoke_structured's own retry loop could fix) must
            # not crash the whole run over a draft that already passed its OWN readiness check --
            # "an audit nobody reads was pure latency" is this codebase's own stated bar for when
            # skipping the audit is fine (tech-stack has none at all). Keep the pre-audit draft
            # unchanged rather than lose the whole run to a nice-to-have adversarial pass.
            logger.warning("audit response failed to parse for stage %s -- keeping pre-audit draft unchanged", stage_spec.key, exc_info=exc)
            content_dict = stage["draft"]
            audit_findings = []
        except (TimeoutError, RuntimeError) as exc:
            # Same "an audit nobody reads was pure latency" bar applies to a Copilot session
            # failure that survived infra_retry's own backoff attempts -- unlike make_draft_node,
            # there is a perfectly good pre-audit draft to fall back to here, so this degrades to
            # "the audit genuinely did not run" rather than escalating the whole stage. Distinct
            # from the ValidationError/ValueError branch's silent skip: audit_skipped_infra is
            # recorded so a human/dashboard can tell "no findings because it's clean" apart from
            # "no findings because the audit never ran."
            logger.warning("audit infra-exhausted for stage %s -- keeping pre-audit draft unchanged", stage_spec.key, exc_info=exc)
            content_dict = stage["draft"]
            audit_findings = []
            audit_skipped_infra = True

        used_ids: set[str] = set(stage["used_ids"])
        _extract_ids(content_dict, used_ids)

        stage["draft"] = content_dict
        stage["used_ids"] = sorted(used_ids)
        stage["audit_findings"] = audit_findings
        stages[stage_spec.key] = stage

        # A stage with a deterministic_verify gate (e.g. specification's ledger sync) can still
        # rewrite this exact content_dict's own ids/fields between here and the gate -- building
        # the human-facing A2UI surface now would show content that's about to change out from
        # under it. Those stages instead build+send their surface from make_verify_node, once
        # verification has actually passed and the content is final. Every other stage's behavior
        # (build+send here) is byte-identical to before deterministic_verify existed.
        extra_messages: list[BaseMessage] = []
        if stage_spec.deterministic_verify is None:
            envelope = stage_spec.build_envelope(content_dict, stage["audit_findings"])
            extra_messages = present_surface_messages(stage_spec.surface_tool_name, envelope)

        thread_id = config["configurable"]["thread_id"]
        await _persist_if_sandboxed(
            thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} draft revised (audit)"
        )

        if sandbox_registry.get(thread_id) is not None:
            await repo_files.append_ledger_entry(
                get_sandbox_provider(),
                thread_id,
                {
                    "stage": stage_spec.key,
                    "node": "audit",
                    "audit_findings_count": len(stage["audit_findings"]),
                    "token_usage": model._last_usage,
                    # Distinguishes "clean, zero findings" from "the audit itself never ran" (a
                    # Copilot infra failure that survived infra_retry) -- both look identical as
                    # audit_findings_count=0 otherwise.
                    "audit_skipped_infra": audit_skipped_infra,
                },
            )
            # Durable counterpart (Part 2 run-visibility) -- same data, second destination, additive
            # to the ledger write above (see run_event_store.py's module docstring). Fails soft:
            # append_event swallows+logs a DB error internally rather than raising.
            run_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_FINISHED,
                stage=stage_spec.key,
                node="audit",
                summary=(
                    "audit skipped (infra failure)" if audit_skipped_infra
                    else f"audit found {len(stage['audit_findings'])} finding(s)"
                ),
                payload={
                    "audit_findings_count": len(stage["audit_findings"]),
                    "audit_skipped_infra": audit_skipped_infra,
                },
                token_usage=model._last_usage,
            )
            run_event = await run_event_store.append_event(run_event)
            # Live counterpart (Part 2 Task 2) -- same event, third destination; see
            # run_event_stream.py's module docstring. Also fails soft, same reasoning as above.
            await run_event_stream.emit_live(run_event, config)

        if stage_spec.post_audit_hook is not None and sandbox_registry.get(thread_id) is not None:
            await stage_spec.post_audit_hook(thread_id, content_dict, state, get_sandbox_provider())

        return {"stages": stages, "messages": extra_messages, "last_push": git_ops.get_last_push(thread_id)}

    return audit_node


@dataclass(frozen=True)
class _StallSignals:
    feedback_similar: bool
    paths_unchanged: bool
    coverage_not_improving: bool
    # The stage's own best-branch-rate-seen, updated (never lowered) -- the caller writes this
    # straight back into stage["best_verify_coverage_rate"] regardless of whether a stall triggered.
    new_best_coverage_rate: float | None

    @property
    def any_triggered(self) -> bool:
        return self.feedback_similar or self.paths_unchanged or self.coverage_not_improving


def _detect_verify_stall(
    feedback: str,
    last_feedback: str | None,
    changed_paths: list[str] | None,
    last_changed_paths: list[str] | None,
    branch_rate: float | None,
    best_branch_rate: float | None,
) -> _StallSignals:
    """Pure stall-detection logic for make_verify_node's 4th session-reset trigger. Separated from
    the stateful node so it is directly testable (see this module's _demo()) -- the same "pure
    function, stateful caller" split every gates/*.py check already uses.

    Three independent heuristics, ORed together:
    - feedback_similar: the model answering the same way twice in a row (SequenceMatcher, same
      0.92 threshold ac_coverage_gate.py's MAX_TEST_BODY_SIMILARITY already uses for near-duplicate
      test bodies -- one fewer bespoke cutoff in the codebase).
    - paths_unchanged: the exact changed_paths list write_scope_gate already computes is identical
      to the prior lap's -- nothing new was written. Only ever non-None for stages that set
      capture_baseline_commit (today: ac-to-tests); permanently inert (both sides None) elsewhere.
    - coverage_not_improving: for stages with no changed_paths signal at all but a numeric progress
      metric each lap (minimal-code-to-green's branch_rate, the documented binding constraint in
      the live incidents that motivated that stage's own max_verify_cycles=12 -- 93.6% branches
      stuck 8 laps; 88.1% -> 91.9% cut off mid-convergence at 6). Tracks the BEST rate seen, not
      just the immediately-prior lap: a naive consecutive-pair comparison misses a genuine
      OSCILLATING stall (89 -> 85 -> 87 -> 85 -- no two adjacent laps are equal, but none beat 89
      either), which is exactly the shape observed live.
    """
    feedback_similar = bool(
        last_feedback and SequenceMatcher(None, feedback, last_feedback).ratio() >= MAX_TEST_BODY_SIMILARITY
    )
    paths_unchanged = (
        last_changed_paths is not None
        and changed_paths is not None
        and sorted(changed_paths) == sorted(last_changed_paths)
    )
    coverage_not_improving = (
        isinstance(branch_rate, (int, float)) and best_branch_rate is not None and branch_rate <= best_branch_rate + 0.01
    )
    if isinstance(branch_rate, (int, float)) and (best_branch_rate is None or branch_rate > best_branch_rate):
        new_best = float(branch_rate)
    else:
        new_best = best_branch_rate
    return _StallSignals(feedback_similar, paths_unchanged, coverage_not_improving, new_best)


def make_verify_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Runs stage_spec.deterministic_verify (a real script/parse, never LLM self-attestation)
    between audit and gate. Only wired in when the StageSpec sets deterministic_verify -- see
    build_graph()."""

    async def verify_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        assert stage_spec.deterministic_verify is not None
        thread_id = config["configurable"]["thread_id"]
        provider = get_sandbox_provider()

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        if sandbox_registry.get(thread_id) is None:
            # Every deterministic_verify (ledger sync, mermaid render, write-scope/AC-coverage git
            # diff, coverage run, license scan) needs the sandbox. Without one the check cannot run,
            # so escalate to a human rather than let the stage pass unverified (route reads
            # cannot_verify).
            stage["last_verification"] = {
                "passed": False,
                "cannot_verify": True,
                "feedback": "no sandbox -- deterministic verification did not run",
                "report": {},
            }
            stages[stage_spec.key] = stage
            return {"stages": stages}

        # Methodology enforcement, checked before the stage's own content check: a stage built
        # around a skill (RED-before-GREEN, writing-plans, receiving-code-review) that silently
        # skipped it produced work that only LOOKS finished, and no content check can see that.
        # Verified from the session's own skill.invoked events, never the model's self-report;
        # fails open when the log is unreadable (see gates/skill_gate.py). prior_invoked is the
        # stage's already-persisted transcript evidence: the close_session below wipes the live
        # transcript this check reads, so without the union one missed skill would retroactively
        # "un-invoke" everything lap 1 genuinely did and the fresh session would have to re-run
        # all of it or fail again (session-reset amplification, 2026-08-24 plan audit).
        prior_invoked = [
            s for s in (stage.get("skills") or {}).get("invoked", []) if isinstance(s, str)
        ]
        skill_check = await skill_gate.check_required_skills(
            provider, thread_id, stage_spec.key, chat_provider=state["provider"], prior_invoked=prior_invoked
        )
        if skill_check.verified:
            # Refresh the persisted evidence with this check's union: the draft node records
            # BEFORE the lap's audit session runs, so audit-role invocations (and the union with
            # earlier laps) only reach state.json through this write -- without it the telemetry
            # the encouraged-skill promotion decision reads permanently undercounts audit skills
            # (2026-08-24 audit, "lesser wart" on the HIGH finding).
            stage["skills"] = {
                **(stage.get("skills") or {}),
                "invoked": skill_check.invoked,
                "missing": skill_check.missing,
                "verified": True,
            }
            stages[stage_spec.key] = stage
        if not skill_check.passed:
            stage["last_verification"] = {
                "passed": False,
                "feedback": skill_gate.feedback_for(skill_check),
                "report": {
                    "missing_skills": skill_check.missing,
                    "invoked_skills": skill_check.invoked,
                    "required_skills": skill_check.required,
                },
            }
            stage["verify_cycle_count"] = stage.get("verify_cycle_count", 0) + 1
            logger.warning(
                "%s: REDRAFT %d/%d (skill gate) -- missing %s",
                stage_spec.key, stage["verify_cycle_count"], stage_spec.max_verify_cycles,
                skill_check.missing,
            )
            stages[stage_spec.key] = stage
            # Restart the draft session, for the reason spelled out in the deterministic_verify
            # branch below: a skill shapes HOW a turn is done, so it must be invoked before the
            # work, and a session that has already answered will only revise its own text. This
            # branch returns early, so it needs its own reset -- putting it only below meant three
            # identical `invoked: []` turns with no restart between them (observed live at
            # metrics-exit, which then exhausted its budget and failed an otherwise-complete run).
            await close_session(thread_id, stage_spec.key, "draft", provider=state["provider"])
            return {"stages": stages}

        # Durable + live NODE_STARTED (Part 2 Task 9 run-visibility) -- same pattern as
        # make_draft_node/make_audit_node's identical blocks. Placed here rather than at this
        # function's top: the no-sandbox and missing-required-skill branches above already return
        # early with no RunEvent of any kind today, so sandbox_registry.get(thread_id) is
        # guaranteed not-None by the time execution reaches this point -- the same guard is kept
        # anyway, purely for literal consistency with the other two nodes' identical blocks. Fails
        # soft, same two-call pattern as every other RunEvent site in this file.
        if sandbox_registry.get(thread_id) is not None:
            start_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_STARTED,
                stage=stage_spec.key,
                node="verify",
                summary="verify started",
            )
            start_event = await run_event_store.append_event(start_event)
            await run_event_stream.emit_live(start_event, config)

        # `or {}`: on the safety-cap auto-approve path a stage can reach verify with draft=None,
        # and a gate doing content_dict.get(...) then dies with AttributeError instead of a
        # verdict -- observed live (diagram_gate crashed two whole runs before ITS local guard
        # was added; _verify_specification_ledger had the identical latent crash). Guarded once
        # here so no individual gate has to remember to.
        result = await stage_spec.deterministic_verify(
            thread_id, stage["draft"] or {}, state.get("run_id", "unknown"), stage.get("baseline_commit"), provider,
            state["provider"],
        )
        stage["last_verification"] = {"passed": result.passed, "feedback": result.feedback, "report": result.report}
        if not result.passed:
            stage["verify_cycle_count"] = stage.get("verify_cycle_count", 0) + 1
            # A redraft is otherwise invisible in the log: the run just shows <stage>_verify
            # followed by <stage>_draft again, with no reason. Diagnosing a thrashing stage then
            # means reconstructing it from whatever the failing sub-system happened to log, which
            # is exactly how an audit outage masqueraded as "the plan keeps getting rejected".
            # 1200, not 300: a single-line gate verdict fits in 300, but an adversarial-compliance
            # rejection is a LIST of findings and 300 characters stops inside the first one --
            # observed live, the log preserved only "[major] PS-1 (scaffol" and the rest of the
            # audit was unrecoverable from disk, since state.json holds the hydration snapshot
            # rather than in-flight state. A truncated reason is barely better than no reason: the
            # whole point of this line is that a thrash explains itself without a container autopsy.
            logger.warning(
                "%s: REDRAFT %d/%d -- %s",
                stage_spec.key, stage["verify_cycle_count"], stage_spec.max_verify_cycles,
                " ".join((result.feedback or "no feedback").split())[:1200],
            )
            # Reset the session ONLY when the stage fabricated -- i.e. claimed work while writing
            # nothing but pipeline artifacts. That specific failure is self-reinforcing: the false
            # claim sits in the session's own history and the model re-reads and repeats it (six
            # identical fabrications, observed live). A fresh session sees only the prompt and this
            # feedback.
            #
            # For every OTHER failure (a tautological test, a coverage gap, a bad plan) the history
            # is exactly what the retry needs -- it knows what it already wrote and why that was
            # rejected. Resetting there makes the model rewrite the same flawed artifact from
            # scratch each lap, which is how a single bad test consumed a whole cycle budget.
            verify_report = result.report or {}
            changed_paths = verify_report.get("changed_paths")
            wrote_nothing_real = changed_paths is not None and not [
                path for path in changed_paths if not path.startswith(_PIPELINE_ARTIFACT_PREFIXES)
            ]
            # Partial fabrication: real files WERE written, but a specific required artifact the
            # model claimed to have created is absent. Same self-reinforcing shape as a total
            # fabrication -- observed live, a session reported "Added required Playwright e2e
            # skeleton files (config + spec)" verbatim on four consecutive turns while making no
            # write call for either, because its own history already asserted the work was done.
            # Only a fresh session stops it re-reading that claim as fact.
            fabricated_artifact = bool(verify_report.get("missing_e2e"))
            # A missing required skill belongs in the same bucket, for the same reason. The skill
            # is meant to shape HOW the turn is done, so it has to be invoked before the work, not
            # bolted on after. Once the session has already produced an answer, a redraft just
            # revises that text -- observed live (nextjs-dotnet, plan): three consecutive turns
            # with `invoked: []` and no attempt to call the skill tool, while the very same stage
            # invoked it correctly on a fresh session in the previous run. Restarting is what gives
            # the model a turn where invoking the skill can still change the work.
            skipped_required_skill = bool(verify_report.get("missing_skills"))
            # Stall detector (4th session-reset trigger): the same self-reinforcing shape as the
            # three fabrication triggers above, just not one of the specific patterns those already
            # name. See _detect_verify_stall's own docstring for what each heuristic catches.
            signals = _detect_verify_stall(
                result.feedback, stage.get("last_verify_feedback"),
                changed_paths, stage.get("last_verify_changed_paths"),
                verify_report.get("branch_rate"), stage.get("best_verify_coverage_rate"),
            )
            stage["best_verify_coverage_rate"] = signals.new_best_coverage_rate
            if signals.any_triggered:
                stage["verify_stall_count"] = stage.get("verify_stall_count", 0) + 1
            else:
                stage["verify_stall_count"] = 0
            stalled = stage["verify_stall_count"] >= workflow_config.VERIFY_STALL_LAPS
            stage["last_verify_feedback"] = result.feedback
            stage["last_verify_changed_paths"] = changed_paths
            if wrote_nothing_real or skipped_required_skill or fabricated_artifact or stalled:
                if stalled and not (wrote_nothing_real or skipped_required_skill or fabricated_artifact):
                    logger.warning(
                        "verify stall detected for stage %s after %d laps (feedback_similar=%s, paths_unchanged=%s, coverage_not_improving=%s) -- resetting session",
                        stage_spec.key, stage["verify_stall_count"],
                        signals.feedback_similar, signals.paths_unchanged, signals.coverage_not_improving,
                    )
                # Phase E audit C-2: make resume continuity legible right where a stalled/
                # fabricating draft session is about to be reset -- without restructuring
                # _detect_verify_stall's own pure signature/logic (untouched; this only reads an
                # existing per-provider cache one layer up, via chat_model's dispatcher). If the
                # session's last known resume state was already "unknown"/"rejected" (e.g. a prior
                # turn was killed mid-turn -- see cli_agent_exec.classify_resume's own docstring),
                # this reset may be discarding a session whose continuity was already suspect
                # BEFORE this stall/fabrication trigger ever fired, not one this reset itself is
                # the first thing to disturb -- worth knowing when diagnosing why a stage looks
                # stuck.
                resume_state = chat_model.get_resume_state(
                    thread_id, stage_spec.key, "draft", provider=state["provider"]
                )
                if resume_state in ("unknown", "rejected"):
                    logger.warning(
                        "resetting draft session for stage %s while its last resume continuity "
                        "was %s -- this session's context may already have been suspect before "
                        "this stall/fabrication trigger fired",
                        stage_spec.key, resume_state,
                    )
                await close_session(thread_id, stage_spec.key, "draft", provider=state["provider"])
                stage["verify_stall_count"] = 0
                # A reset session starts fresh either way; the coverage high-water mark is about
                # detecting non-improvement, not about the session's memory, so it is NOT cleared
                # here -- resetting it would let a stalled stage "re-earn" laps against a lowered
                # bar instead of the real best it has already achieved this run.
        stages[stage_spec.key] = stage

        extra_messages: list[BaseMessage] = []
        if result.passed:
            # deterministic_verify (e.g. spec_ledger.sync_ledger) may have mutated stage["draft"]
            # in place (ids resolved/overwritten) -- build+send the human-facing surface only now,
            # against the final, ledger-correct content (see make_audit_node's matching comment).
            envelope = stage_spec.build_envelope(stage["draft"], stage["audit_findings"])
            extra_messages = present_surface_messages(stage_spec.surface_tool_name, envelope)
            await _persist_if_sandboxed(
                thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} draft revised (verify)"
            )

        if sandbox_registry.get(thread_id) is not None:
            await repo_files.append_ledger_entry(
                provider,
                thread_id,
                {
                    "stage": stage_spec.key,
                    "node": "verify",
                    "passed": result.passed,
                    "cycle": stage["verify_cycle_count"],
                },
            )
            # Durable counterpart (Part 2 run-visibility) -- same data, second destination, additive
            # to the ledger write above (see run_event_store.py's module docstring). No token_usage:
            # deterministic_verify is a real script/parse, never an LLM call. Fails soft:
            # append_event swallows+logs a DB error internally rather than raising.
            run_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_FINISHED,
                stage=stage_spec.key,
                node="verify",
                summary=f"verify {'passed' if result.passed else 'failed'} (cycle {stage['verify_cycle_count']})",
                payload={"passed": result.passed, "cycle": stage["verify_cycle_count"]},
            )
            run_event = await run_event_store.append_event(run_event)
            # Live counterpart (Part 2 Task 2) -- same event, third destination; see
            # run_event_stream.py's module docstring. Also fails soft, same reasoning as above.
            await run_event_stream.emit_live(run_event, config)

        # minimal-code-to-green's verify_coverage report carries a numeric line_rate whether the
        # gate passed or not -- promote it onto repo_scan so the metrics bar's coverage chip lights
        # up mid-run instead of waiting for metrics_report at the very end. Any other
        # deterministic_verify's report (ledger sync, mermaid render, license scan) has no
        # line_rate key and this is a no-op.
        line_rate = result.report.get("line_rate")
        if isinstance(line_rate, (int, float)):
            prior_repo_scan = dict(state.get("repo_scan") or {})
            return {
                "stages": stages,
                "messages": extra_messages,
                "repo_scan": {**prior_repo_scan, "coverage": {"line_rate": line_rate, "branch_rate": result.report.get("branch_rate")}},
            }

        return {"stages": stages, "messages": extra_messages}

    return verify_node


def make_route_after_verify(stage_spec: StageSpec) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        stage = state["stages"][stage_spec.key]
        last = stage.get("last_verification") or {}
        if last.get("cannot_verify"):
            return "escalate"  # no sandbox -- never loop or pass, a human must see it
        if last.get("passed"):
            return "gate"
        if stage.get("verify_cycle_count", 0) < stage_spec.max_verify_cycles:
            return "retry"
        return "escalate"

    return route


def make_verify_fix_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Write-capable pass that closes what the stage's deterministic_verify reported.

    Modelled on e2e_fix: the findings already carry a plan reference and cited evidence, so the fix
    agent is handed those verbatim rather than a summary of them.
    """
    system_prompt, human_template = load_prompt_pair(stage_spec.verify_fix_prompt or "")

    async def verify_fix_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        stage = state["stages"][stage_spec.key]
        report = (stage.get("last_verification") or {}).get("report") or {}
        reasons = report.get("blocking_reasons") or []
        if not reasons:
            return {}

        # Durable + live NODE_STARTED (Part 2 Task 9 run-visibility, Ruling 12) -- same pattern as
        # make_draft_node/make_audit_node's identical blocks, placed after the only early-return
        # above so it only ever fires on the path that reaches this node's own NODE_FINISHED below.
        # Unlike make_verify_node (a deterministic script, no LLM), this node genuinely calls the
        # model (see model.ainvoke below) -- same "real work" shape as draft/audit, just missing
        # its own lifecycle pair until now. Fails soft, same two-call pattern as every other
        # RunEvent site in this file.
        if sandbox_registry.get(thread_id) is not None:
            start_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_STARTED,
                stage=stage_spec.key,
                node="fix",
                summary="fix started",
            )
            start_event = await run_event_store.append_event(start_event)
            await run_event_stream.emit_live(start_event, config)

        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "fix",
            provider=state["provider"],
            # Task 3b (Part 2 Ruling 10) -- see make_draft_node's draft-role call site's own
            # comment above. This node builds no RunEvent of its own, but a Copilot turn here
            # still runs through _agenerate_inner's own tool-call RunEvent building, which would
            # otherwise keep seeing "unknown" the same as every other un-threaded call site.
            run_id=state.get("run_id", "unknown"),
            # The `fix` role is declared in model_config precisely so this write-capable pass can be
            # tiered separately from the read-only audit it serves; falls back to the stage's draft
            # model, matching how e2e_fix resolves its own.
            model_name=(
                model_config.get_model_name(stage_spec.key, "fix", state["provider"])
                or model_config.get_model_name(stage_spec.key, "draft", state["provider"])
            ),
            # WITHOUT this the session runs Copilot locally, outside the sandbox, with no access to
            # the worktree -- so a pass declared write-capable silently could not write. Observed
            # live: `Creating Copilot session '...:adversarial-compliance:fix' ... sandbox=None`,
            # returning in 10 seconds with the same four divergences still open.
            sandbox=sandbox_registry.get(thread_id),
            # Flat kwargs, not a session_options dict -- this helper takes them directly.
            agent_mode="autopilot",
            available_tools=[
                "builtin:view", "builtin:grep", "builtin:glob", "builtin:bash",
                "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
            ],
        )
        rendered = render_prompt(human_template, blocking_reasons="\n".join(f"- {r}" for r in reasons))
        await model.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=rendered)],
            config={"metadata": {"emit-messages": False}},
        )
        if sandbox_registry.get(thread_id) is not None:
            await git_ops.commit_all(
                get_sandbox_provider(), thread_id, f"ai-dev-workflow: {stage_spec.key} conformance fixes"
            )
            # Durable + live NODE_FINISHED (Part 2 Task 9 run-visibility, Ruling 12) -- pairs with
            # NODE_STARTED above. token_usage=model._last_usage included, unlike make_verify_node's
            # NODE_FINISHED, because this genuinely is an LLM call (mirrors draft/audit's own
            # NODE_FINISHED shape, not verify's no-LLM one).
            run_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.NODE_FINISHED,
                stage=stage_spec.key,
                node="fix",
                summary="fix applied",
                token_usage=model._last_usage,
            )
            run_event = await run_event_store.append_event(run_event)
            await run_event_stream.emit_live(run_event, config)
        return {}

    return verify_fix_node


def make_draft_escalate_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Draft-level infra exhaustion only (make_draft_node's own infra_retry attempts all lost to a
    Copilot quota/timeout failure) -- a distinct node from make_escalate_node's deterministic-verify
    cap exhaustion, wired unconditionally for every stage (unlike the verify-cap escalate, which
    only exists for stages with a deterministic_verify) so this path never depends on whether a
    given stage happens to have one. Never routes to auto_approve: silently force-approving a draft
    that never successfully generated any content would push stale/empty content to a human
    approval gate or downstream stage, which is worse than clearly failing the run."""

    async def draft_escalate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        stage = state["stages"][stage_spec.key]
        detail = stage.get("last_infra_error") or ""
        payload = {"stage": stage_spec.key, "type": "draft_infra_exhausted", "detail": detail[-2000:]}
        prior_failure = state.get("run_failure")
        if prior_failure:
            # Same first-cause preservation as make_escalate_node below: a run already carrying a
            # terminal failure (rebuild/e2e escalate routed into metrics-exit) keeps that cause.
            payload = {**prior_failure, "subsequent_failure": {"stage": stage_spec.key, "type": "draft_infra_exhausted"}}
        else:
            # This node is only ever reached via make_route_after_draft's infra_exhausted check --
            # never a genuine content/gate failure -- so the default (when the exception message
            # itself doesn't happen to contain a recognizable quota marker) is infra_transient, not
            # classify_failure's generic gate_exhausted fallback. See record_run_failure_and_reset's
            # own docstring for why this matters.
            payload = await run_failure.record_run_failure_and_reset(
                thread_id, state.get("run_id"),
                payload=payload, detail_for_classification=detail, default_failure_type="infra_transient",
            )

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stages[stage_spec.key]["infra_exhausted"] = False
        stages[stage_spec.key]["status"] = "needs_clarification"
        await _persist_if_sandboxed(
            thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} escalated -- draft infra exhausted",
        )
        await close_thread_session(thread_id, provider=state["provider"])
        return {"stages": stages, "run_failure": payload}

    return draft_escalate_node


def make_escalate_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Deterministic-verify cap exhaustion, e.g. a gate that keeps failing a real check (coverage,
    write-scope, ledger-sync). Never auto-approved past a failed deterministic gate -- and never
    paused for a human either (spec/plan approval are the only human touchpoints): the run ENDs
    with `run_failure` set. A human couldn't fix a failing deterministic gate from a chat banner
    anyway; the recovery path is fix-out-of-band + resubmit, which re-enters at intake with
    counters reset."""

    async def escalate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        stage = state["stages"][stage_spec.key]
        last = stage.get("last_verification") or {}
        payload = {
            "stage": stage_spec.key,
            "type": "cannot_verify" if last.get("cannot_verify") else "verification_cap_exceeded",
            "report": last.get("report"),
            "feedback": last.get("feedback"),
        }
        prior_failure = state.get("run_failure")
        if prior_failure:
            # A terminal failure was already recorded this run (e.g. a rebuild escalate routed into
            # metrics-exit, whose own gate then capped out). Keep the FIRST cause -- the DB row and
            # ledger already carry it, and re-recording would overwrite the real killer with
            # downstream fallout. The second failure survives as a nested note.
            payload = {**prior_failure, "subsequent_failure": {"stage": stage_spec.key, "type": payload["type"]}}
        else:
            payload = await run_failure.record_run_failure_and_reset(
                thread_id, state.get("run_id"),
                payload=payload,
                detail_for_classification=f"{last.get('feedback') or ''} {last.get('report') or ''}",
            )

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stages[stage_spec.key]["verify_cycle_count"] = 0
        # The approval is REVOKED, and persisted revoked. A stage that exhausted its deterministic
        # gate did not pass, so leaving status=approved on the branch is simply false -- and it has
        # a concrete consequence: `intake` clears `last_verification` on every run, so the next
        # resume would hydrate this stage as cleanly approved, skip its draft entirely, and continue
        # on the exact content the gate rejected. Observed live: ac-to-tests escalated on a real
        # depth shortfall, and the following run logged "draft skipped for already-approved stage
        # ac-to-tests (resume)" and went on to write production code against it.
        stages[stage_spec.key]["status"] = "needs_clarification"
        stages[stage_spec.key]["approved_content"] = None
        await _persist_if_sandboxed(
            thread_id, state, stages,
            f"ai-dev-workflow: {stage_spec.key} escalated -- approval revoked ({payload['type']})",
        )
        # The other genuine run terminal (exit_nodes.exit_finalize_node is the success one): an
        # escalated deterministic gate ENDs the run, and recovery is fix-out-of-band + resubmit,
        # which re-enters at intake and rebuilds whatever sessions it needs. Nothing downstream
        # reads these, so release them rather than leaving ~20 to ride until the idle reaper.
        await close_thread_session(thread_id, provider=state["provider"])
        return {"stages": stages, "run_failure": payload}

    return escalate_node


def make_route_after_draft(stage_spec: StageSpec) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        stage = state["stages"][stage_spec.key]
        # Checked before readiness/cycle_count: an infra-exhausted draft (make_draft_node, after
        # its own infra_retry backoff attempts were all lost to a Copilot quota/timeout failure)
        # has no draft content at all, so it must not be treated as "not ready yet" -- that would
        # increment cycle_count for a reason that has nothing to do with clarification quality.
        if stage.get("infra_exhausted"):
            return "escalate"
        # A hydrate_from_repo_file short-circuit (see make_draft_node) marks the stage "approved"
        # directly, in draft_node itself -- this content was already audited and approved in a
        # prior run, so it must bypass BOTH audit_node and gate_node entirely. Routing on
        # readiness alone (the pre-hydrate design) would send it through audit_node anyway,
        # re-running a live, non-deterministic LLM call on already-approved content on every
        # single idempotent re-run -- caught by real end-to-end testing (a second, unaffected
        # gate re-interrupt and slightly-reworded "approved" content on a run that should have
        # been a no-op), not by inspection.
        if stage["status"] == "approved":
            return "already_approved"
        if stage["readiness"]:
            return "gate"
        if stage["cycle_count"] >= stage_spec.max_cycles:
            return "auto_approve"
        # Headless: there is no human to answer, so a not-ready draft loops straight back into
        # its own draft node (the draft node already incremented cycle_count, so this is bounded
        # by max_cycles and then falls into auto_approve above). The redraft prompt tells the
        # model to answer its own questions -- see make_draft_node's headless injection.
        if os.environ.get("AIDW_HEADLESS"):
            return "headless_redraft"
        return "needs_clarification"

    return route


def make_gate_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def gate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        stage = state["stages"][stage_spec.key]
        # Pauses here (BR-4/Section 6 Gate) until the frontend's useInterrupt
        # resolve(payload) resumes this exact node with that payload -- unless this stage is
        # supporting infrastructure with no tab to review it in (requires_human_gate=False), in
        # which case it proceeds straight through to the same approved-marking body every other
        # stage already runs post-interrupt-resolve. The resume value is captured (previously
        # discarded outright) so resolve_from_interrupt below can actually see what the frontend
        # resolved with -- every OTHER stage's gate still just re-approves stage["draft"]
        # verbatim, since resolve_from_interrupt is None for all of them.
        resume_value: Any = None
        if stage_spec.requires_human_gate:
            extra = stage_spec.build_interrupt_extra(state) if stage_spec.build_interrupt_extra else {}
            # Durable "paused here" signal for dbo.sessions (Part 3 Task 1,
            # docs/superpowers/plans/part-3-tickets-tasks.md) -- current_stage alone can't tell
            # "still drafting stage_spec.key" apart from "paused at its gate", since
            # update_current_stage only ever fires post-approval (_run_post_approve_hook below),
            # and LangGraph's own InMemorySaver interrupt bookkeeping is in-process only, invisible
            # to the board's separate GET /sessions DB read and lost on a restart. Best-effort and
            # swallowed like every other sandbox-adjacent write in this function: no sandbox
            # registered means no row worth updating (mirrors _run_post_approve_hook's own guard).
            # This line re-runs harmlessly on resume too -- LangGraph re-executes the node from the
            # top, so it sets True a second time immediately before update_current_stage clears it
            # a few lines down.
            if sandbox_registry.get(thread_id) is not None:
                # Phase E review (Important 2, on top of finding 4): this whole block re-runs on
                # EVERY resumed replay of this node, not just the genuine first pause -- the
                # set_awaiting_gate(True) call below already documents that as harmless for its own
                # idempotent DB write, but an append-only event log is not idempotent, and a naive
                # copy of that comment's reasoning onto GATE_PAUSED double-emitted it (once for the
                # real pause, once again milliseconds later when the resumed replay reaches this
                # exact point right before interrupt() returns the resume value instead of pausing
                # again). No in-memory `state` field distinguishes the two executions -- LangGraph
                # replays this node with the SAME input state both times, and nothing between them
                # writes a different value into state["stages"][stage_spec.key] (gate_node itself
                # hasn't returned anything yet either time). The one reliable, durable signal is the
                # cross-process awaiting_gate column set_awaiting_gate already exists to answer:
                # read it BEFORE writing True. False (or unreadable) means this is a genuine new
                # pause; True means this is the resumed replay of an already-recorded one.
                already_paused = False
                try:
                    existing_session = await session_store.get_session(thread_id)
                    already_paused = bool(existing_session and existing_session.get("awaiting_gate"))
                except Exception:
                    logger.warning(
                        "reading awaiting_gate failed for stage=%s thread_id=%s -- assuming this "
                        "is a genuine new pause (may double-emit GATE_PAUSED for this one "
                        "occurrence if it is actually a replay)",
                        stage_spec.key, thread_id, exc_info=True,
                    )
                try:
                    await session_store.set_awaiting_gate(thread_id, True)
                except Exception:
                    logger.warning(
                        "set_awaiting_gate failed for stage=%s thread_id=%s", stage_spec.key, thread_id, exc_info=True
                    )
                # Phase E audit finding 4: GATE_PAUSED had no producer anywhere, so the event log
                # jumped straight from one node's finish to the next node's start with an
                # unexplained gap for however long a human took to review -- the single most
                # operationally significant wait in the whole pipeline, invisible in the view built
                # to explain where time went. Same fail-soft append_event-then-emit_live rebind
                # pattern the four existing NODE_* sites in this file already use. Guarded by
                # `not already_paused` (see above) so this fires once per genuine pause, not once
                # per replay -- additive only, no topology change.
                if not already_paused:
                    pause_event = RunEvent(
                        run_id=state.get("run_id", "unknown"),
                        session_id=thread_id,
                        type=RunEventType.GATE_PAUSED,
                        stage=stage_spec.key,
                        node="gate",
                        summary=f"gate paused for review: {stage_spec.key}",
                    )
                    pause_event = await run_event_store.append_event(pause_event)
                    await run_event_stream.emit_live(pause_event, config)
            resume_value = interrupt({"stage": stage_spec.key, "draft": stage["draft"], **extra})

        # Part 2 Task 10 (Ruling 3): the one resume shape that means a human REJECTION --
        # discriminated from every other resume_value this function has ever accepted (tech-
        # stack's own {"markdown": ...} submit shape, the generic InterruptCard's
        # {"decision": "approved"}, headless's bare `True`, and every non-gated stage's
        # resume_value of None) -- all of those fall through to the unchanged approval body below
        # exactly as before. Loops back to THIS stage's own draft node (make_route_after_gate,
        # wired in _wire_stage) instead of ending the run -- the same "loop back and try again"
        # shape this pipeline's own deterministic_verify retry already uses for a failed check
        # (make_route_after_verify), just triggered by a human's opinion instead of a script's.
        # Never reached for a non-gated stage: resume_value stays None there, so the isinstance
        # check is False and this whole branch is skipped, unchanged from before this task.
        if isinstance(resume_value, dict) and resume_value.get("decision") == "rejected":
            stages = {key: dict(value) for key, value in state["stages"].items()}
            rejected = stages[stage_spec.key]
            feedback = str(resume_value.get("feedback") or "").strip()
            rejected["status"] = "needs_clarification"
            rejected["reviewer_feedback"] = feedback or "(rejected with no feedback provided)"
            stages[stage_spec.key] = rejected
            # Mirrors the True-setting call above: this session is no longer sitting at a gate --
            # it is about to redraft. Never routes through _run_post_approve_hook/
            # update_current_stage (that would fire post_approve_hook, which is only correct for
            # genuine approvals -- tech-stack's apply_stack_conventions must never run over
            # content a human just rejected).
            if sandbox_registry.get(thread_id) is not None:
                try:
                    await session_store.set_awaiting_gate(thread_id, False)
                except Exception:
                    logger.warning(
                        "set_awaiting_gate failed for stage=%s thread_id=%s", stage_spec.key, thread_id, exc_info=True
                    )
                # Phase E audit finding 4: GATE_RESOLVED, rejection branch. Payload carries the
                # decision (approved/rejected) since gate_node is the one place that already knows
                # which branch it took -- see the approval tail below for the other outcome. Same
                # fail-soft pattern as GATE_PAUSED above.
                resolved_event = RunEvent(
                    run_id=state.get("run_id", "unknown"),
                    session_id=thread_id,
                    type=RunEventType.GATE_RESOLVED,
                    stage=stage_spec.key,
                    node="gate",
                    summary=f"gate rejected: {stage_spec.key}",
                    payload={"decision": "rejected"},
                )
                resolved_event = await run_event_store.append_event(resolved_event)
                await run_event_stream.emit_live(resolved_event, config)
            await _persist_if_sandboxed(
                thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} rejected by reviewer"
            )
            # No approvals.record_approval call here, deliberately: that ledger's whole purpose
            # (approvals.py's own docstring) is a tamper-evident record of content a human
            # APPROVED -- a rejection has no approved content to hash, and logging it there would
            # conflate "a human said no" with the one thing that file is meant to prove happened.
            return {"stages": stages}

        stages = {key: dict(value) for key, value in state["stages"].items()}
        approved = stages[stage_spec.key]
        content = approved["draft"]
        if stage_spec.resolve_from_interrupt is not None and sandbox_registry.get(thread_id) is not None:
            resolved = await stage_spec.resolve_from_interrupt(thread_id, resume_value, state, get_sandbox_provider())
            if resolved is not None:
                content = resolved
        approved["status"] = "approved"
        approved["approved_content"] = content
        approved["cycle_count"] = 0
        # Cleared on approval (belt-and-suspenders with intake_node's own per-run reset): a stage
        # approved right after being rejected once must not carry that stale feedback into some
        # unrelated future redraft (e.g. a later escalate-and-revoke on a DIFFERENT gate).
        approved["reviewer_feedback"] = None
        stages[stage_spec.key] = approved

        # Phase E audit finding 4: GATE_RESOLVED, approval branch -- the far more common outcome
        # than rejection, and the one that closes out most of the "how long was this gate open"
        # gaps GATE_PAUSED above now makes visible. Only for stages that actually paused
        # (requires_human_gate) -- the 5 non-gated stages fall through this whole function with no
        # GATE_PAUSED to resolve, so emitting one here for them would describe a pause that never
        # happened. Same fail-soft pattern as the other two sites.
        if stage_spec.requires_human_gate and sandbox_registry.get(thread_id) is not None:
            resolved_event = RunEvent(
                run_id=state.get("run_id", "unknown"),
                session_id=thread_id,
                type=RunEventType.GATE_RESOLVED,
                stage=stage_spec.key,
                node="gate",
                summary=f"gate approved: {stage_spec.key}",
                payload={"decision": "approved"},
            )
            resolved_event = await run_event_store.append_event(resolved_event)
            await run_event_stream.emit_live(resolved_event, config)

        await _persist_if_sandboxed(thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} approved")

        if stage_spec.sign_approval and sandbox_registry.get(thread_id) is not None:
            provider = get_sandbox_provider()
            await approvals.record_approval(
                provider, thread_id, stage_spec.key, state.get("run_id", "unknown"), approved["approved_content"]
            )
            await git_ops.commit_paths(
                provider, thread_id, [approvals.APPROVALS_PATH], f"ai-dev-workflow: {stage_spec.key} approval signed"
            )

        await _run_post_approve_hook(stage_spec, thread_id, approved["approved_content"], state)
        return {"stages": stages, "last_push": git_ops.get_last_push(thread_id)}

    return gate_node


def make_route_after_gate(stage_spec: StageSpec) -> Callable[[GraphState], str]:
    """Routes make_gate_node's outgoing edge (Part 2 Task 10). "approved" -- the only outcome that
    existed before this task, and still the only one for the 5 non-gated stages, which always
    reach "approved" -- proceeds to the next stage's draft, unchanged from the unconditional edge
    this replaced. "rejected" (Ruling 3) loops back to THIS stage's own draft node instead.

    Reads state AFTER gate_node has already run: LangGraph evaluates a conditional edge's routing
    function against the state its source node just returned (the same reliance
    make_route_after_draft/make_route_after_verify already have on make_draft_node/
    make_verify_node's own writes), so `stage["status"]` here already reflects whichever branch
    gate_node took."""

    def route(state: GraphState) -> str:
        stage = state["stages"][stage_spec.key]
        return "approved" if stage["status"] == "approved" else "rejected"

    return route


def assert_gates_have_self_checks() -> None:
    """Every gate module must expose a runnable `_demo`. Fails the build when one does not.

    A broken gate does not crash -- it runs, returns a number, and the number is wrong. Measured this
    session: the per-AC depth counter read a real `.cs` file holding 14 tests and reported ZERO tests
    for every criterion (the generated names strip punctuation, `TestUS00012...`, and both the id
    variants and the test-declaration regex missed them). Nothing errored; the pipeline carried on
    and wrote production code against what it believed were no tests. The only thing that catches
    that class of defect is handing the pure function a real artifact and asserting the answer.

    So the convention is enforced rather than hoped for. Note what this does NOT guarantee: a
    self-check whose fixtures were invented from the same wrong assumption as the code will pass
    while both are wrong -- which is exactly what happened. Fixtures should come from artifacts
    captured out of a real run; the checks in ac_coverage_gate, e2e_nodes and repo_scan now do.
    """
    gates_dir = Path(__file__).parent / "gates"
    missing: list[str] = []
    for path in sorted(gates_dir.glob("*.py")):
        if path.stem == "__init__":
            continue
        module = importlib.import_module(f"{__package__}.gates.{path.stem}")
        if not callable(getattr(module, "_demo", None)):
            missing.append(path.stem)
    if missing:
        raise AssertionError(
            "these gate module(s) have no runnable `_demo` self-check: "
            + ", ".join(missing)
            + ". Add one that exercises the module's pure functions against a REAL captured "
            "artifact -- a gate with no check is a gate nobody can prove works."
        )


def should_skip_draft(stage: dict[str, Any]) -> bool:
    """Whether a stage's LLM draft can be skipped because it is already approved. Pure.

    A FAILED verification cancels the skip, and that clause is the whole reason this is a named
    function rather than an inline condition. Without it the verify-retry loop is a NO-OP for every
    auto-approved stage: `auto_approve` sets status=approved BEFORE verify runs, so when verify
    fails and routes back to the draft node, the stage looks "already approved" and the redraft is
    skipped -- the run then continues on exactly the content verification had rejected.

    Observed live on a fresh nextjs-dotnet run: ac-to-tests auto-approved a test suite of
    `{coverage_plan: [], test_files: [], summary: "No test files were written in this turn."}`; its
    verify correctly closed the Copilot session so the next attempt would start fresh; the retry
    then logged "draft skipped for already-approved stage ac-to-tests (resume)" and the pipeline
    proceeded to write production code against no tests at all.
    """
    if stage.get("status") != "approved" or not stage.get("approved_content"):
        return False
    last_verification = stage.get("last_verification") or {}
    if last_verification and not last_verification.get("passed"):
        return False
    return True


def make_auto_approve_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def auto_approve_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        # US-10/AC-10.3: safety cap hit while still not-ready. Proceed to
        # Approved exactly as if the human had approved, bypassing the gate.
        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]
        stage["status"] = "approved"
        stage["approved_content"] = stage["draft"]
        stage["cycle_count"] = 0
        stages[stage_spec.key] = stage

        thread_id = config["configurable"]["thread_id"]
        # A verify-bearing stage's approval is NOT persisted here, because it hasn't passed
        # anything yet: this node runs BEFORE the deterministic verify (build_graph wires
        # auto_approve -> verify), and gate_node persists the same approval on the pass edge. The
        # in-memory status is only what routing needs. Persisting it early wrote an unverified
        # approval to the branch, and that file is what the NEXT run hydrates -- intake clears
        # last_verification, so should_skip_draft's failed-verification guard cannot see the
        # rejection and the stage is skipped as "already approved" forever. Observed live
        # (bookmarks/nextjs-dotnet, thread 70bbfc95): ac-to-tests hit its clarification cap with
        # `{coverage_plan: [], test_files: [], summary: "No test files were written in this turn."}`,
        # committed it as "auto-approved (safety cap)", failed its verify -- and every later resume
        # hydrated that empty suite and wrote production code against zero tests.
        if stage_spec.deterministic_verify is None:
            await _persist_if_sandboxed(
                thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} auto-approved (safety cap)"
            )
            await _run_post_approve_hook(stage_spec, thread_id, stage["approved_content"], state)
        return {"stages": stages, "last_push": git_ops.get_last_push(thread_id)}

    return auto_approve_node


# R placements (agent/src/rebuild.py). Keyed by the STAGES entry whose gate/auto_approve should
# route into R instead of straight to the next stage's draft -- see build_graph()'s use of this
# dict below. "After P4" uses fix_scope="scaffold_only" since brand-new tests against
# not-yet-existing production symbols won't compile at all, TDD-red or not -- that fix node may
# only add compile-enabling stubs, never real behavior (see rebuild.py's own docstring). "After
# P6" gets a full-scope fix, real bug-fixing being exactly what a failed rebuild after real
# implementation work calls for. Both currently route to END on success since quality-remediation (the next real
# stage after P6) doesn't exist yet -- update REBUILD_AFTER_P6.next_node the moment quality-remediation is wired in.
REBUILD_AFTER_AC_TO_TESTS = rebuild.RebuildSpec(
    key="r_ac_to_tests",
    max_fix_cycles=3,
    fix_prompt_addendum="",  # unused for scaffold_only -- rebuild.py substitutes its own addendum
    fix_scope="scaffold_only",
    next_node="minimal-code-to-green_draft",
)

REBUILD_AFTER_P6 = rebuild.RebuildSpec(
    key="r_minimal_code_to_green",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    # Into the pre-scan, not straight into the draft: remediation cannot act on findings nobody has
    # measured yet, and the scan of the code minimal-code-to-green just wrote is that measurement.
    next_node="remediation_scan",
)

# Stage 6 (remediation): rebuild after fixes applied
REBUILD_FOR_REMEDIATION = rebuild.RebuildSpec(
    key="r_remediation",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    # Into test-hardening, NOT adversarial-compliance: the audit was moved to the end of the back
    # half so it can judge the finished state. See REBUILD_FOR_ADVERSARIAL_COMPLIANCE below.
    next_node="test_hardening_run_tests",
)

# Stage 7 (adversarial-compliance): rebuild after compliance checks
REBUILD_FOR_ADVERSARIAL_COMPLIANCE = rebuild.RebuildSpec(
    key="r_adversarial_compliance",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    # Into the metrics pass: adversarial-compliance is now the LAST judgement before metrics-exit,
    # because it audits conformance against the approved Plan and its evidence includes this run's
    # test-hardening and e2e outcomes. Running it earlier made it block on evidence that had not
    # been produced yet -- observed live: it failed the run with "[major] PS-9: Fresh successful
    # verification evidence still not present", citing an e2e outcome of all-nulls, which was simply
    # e2e not having run. An audit that demands evidence the pipeline emits after it is a deadlock,
    # not a gate.
    #
    # The one placement with scan_delta_gate on, because it is the LAST rebuild after the LAST
    # code-writing stage: everything this gate lets through goes straight to metrics-exit, which can
    # only fail the run. See RebuildSpec.scan_delta_gate for the run this was written from.
    scan_delta_gate=True,
    # Into readme_write (W7), which chains to metrics_compute -- the README leg runs BEFORE the
    # final scan so metrics-latest and the exit report cover the finished tree, README included.
    # This is the ONLY next_node="metrics_compute" in the file (verified), so every normal
    # successful path gets the README leg; failure/escalate paths route straight to
    # metrics-exit_draft and deliberately skip it.
    next_node="readme_write",
)

# Maps a STAGES entry's key -> the R placement immediately after it, so build_graph()'s per-stage
# loop can route that stage's gate/auto_approve into R instead of straight to the next draft node.
POST_STAGE_REBUILD: dict[str, rebuild.RebuildSpec] = {
    "ac-to-tests": REBUILD_AFTER_AC_TO_TESTS,
    "minimal-code-to-green": REBUILD_AFTER_P6,
    "remediation": REBUILD_FOR_REMEDIATION,
    "adversarial-compliance": REBUILD_FOR_ADVERSARIAL_COMPLIANCE,
}


def _route_after_tech_stack(state: GraphState) -> str:
    """The brownfield branch: tech-stack detection runs before brownfield-baseline, so its
    approved content (dotnet_detected etc.) is available to brownfield-baseline's own draft.

    A greenfield repo (no manifest, the deterministic scan found nothing) skips brownfield-
    baseline's LLM stage entirely and goes straight to the deterministic ratification node: there
    is nothing to baseline in an empty repo, and that stage's brownfield draft would just
    needs_clarification->END after the human already confirmed a tech stack on the Tech Stack
    tab."""
    if state.get("manifest_exists", True):
        return "next"
    if tech_stack_signals.is_greenfield_repo(state):
        return "brownfield_write_manifest"
    return "brownfield_baseline_pre"


def _route_after_intake(state: GraphState) -> str:
    """END for a blank run with nothing to do; scaffold otherwise.

    "Nothing to do" = no requirements text this run (typed or carried in the latest
    HumanMessage) AND nothing hydrated back out of the repo (a returning thread whose clone
    holds an approved raw-requirements doc proceeds so downstream stages can hydrate/resume).
    """
    if (state.get("raw_requirements_text") or "").strip():
        return "scaffold"
    raw_req = (state.get("stages") or {}).get("raw-requirements") or {}
    if raw_req.get("approved_content") or raw_req.get("draft"):
        return "scaffold"
    return END


async def _manifest_branch_node(_state: GraphState) -> dict[str, Any]:
    """Pass-through branch point. `_wire_stage` gives every stage a plain gate -> next edge, so a
    conditional branch after tech-stack needs a node of its own to hang the edges on."""
    return {}


def _wire_tech_stack_intake(builder: StateGraph) -> None:
    """Wires the repo-write ordering: scaffold (read-mostly) -> app_discovery_pre (deterministic
    scan, no LLM, no verdict) -> scaffold_finalize -> tech-stack (draft -> human-gated tab,
    STAGES[0]) -> manifest_branch -> (brownfield-baseline brownfield | brownfield_write_manifest
    directly, for a greenfield repo | app_check_record) -> repo_scan_baseline -> raw-requirements.

    No hard-rejection anymore, and no separate greenfield interrupt: the Tech Stack tab (backed
    by the tech-stack StageSpec's human gate) is what every repository goes through now, empty or
    not -- see graph.py's/preflight_nodes.py's Tech Stack tab redesign. The deterministic scan's
    only remaining jobs are grounding the tech-stack draft's exploration and feeding
    app_check_record_node/e2e_nodes.py's fresh rescans.

    repo_scan_baseline sits at the convergence point of both branches and immediately before P1,
    so it measures the repository as it arrived: the clone exists, the tech stack is known, and
    nothing has written application code yet. It is idempotent on its own committed artifact
    because this node -- like every node on the main path -- is re-entered on every clarification
    round; see repo_scan.repo_scan_baseline_node's docstring for why that matters.

    Verification status: the deterministic scan has NOT been exercised against a real sandbox;
    app_discovery.py's own self-check covers the pure half only.
    """
    builder.add_node("app_discovery_pre", app_discovery.app_discovery_pre_node)
    builder.add_node("app_check_record", app_discovery.app_check_record_node)
    builder.add_node("repo_scan_baseline", repo_scan.repo_scan_baseline_node)
    builder.add_node("scaffold_finalize", preflight_nodes.scaffold_finalize_node)
    builder.add_node("manifest_branch", _manifest_branch_node)

    builder.add_edge("app_discovery_pre", "scaffold_finalize")
    builder.add_edge("scaffold_finalize", f"{STAGES[0].key}_draft")
    builder.add_conditional_edges(
        "manifest_branch",
        _route_after_tech_stack,
        {
            "next": "app_check_record",
            "brownfield_baseline_pre": "brownfield_baseline_pre",
            "brownfield_write_manifest": "brownfield_write_manifest",
        },
    )
    builder.add_edge("app_check_record", "repo_scan_baseline")
    builder.add_node("record_raw_requirements", record_raw_requirements_node)
    builder.add_edge("repo_scan_baseline", "record_raw_requirements")
    builder.add_edge("record_raw_requirements", f"{STAGES[1].key}_draft")


BROWNFIELD_BASELINE_SPEC = StageSpec(
    key="brownfield-baseline",
    response_schema=BrownfieldBaselineDraftResponse,
    content_field="baseline",
    surface_tool_name="present_brownfield_baseline",
    build_envelope=build_brownfield_baseline_envelope,
    build_prompt=_build_brownfield_baseline_prompt,
    max_cycles=2,
    render_markdown=render_brownfield_baseline_markdown,
    # Ratification (brownfield_write_manifest_node, this stage's next_draft_name below) now runs
    # automatically after the audit -- the human checkpoints are specification and plan only.
    requires_human_gate=False,
    session_options=lambda _state, _role: {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS},
)


def _wire_brownfield(builder: StateGraph) -> None:
    """Wires brownfield-baseline's brownfield sub-flow: only reached when scaffold's manifest_exists check finds
    no manifest.json (_wire_app_discovery's "manifest_branch" conditional edge, not this function,
    does that branch). brownfield_baseline_pre (deterministic schema/migration/route grep) -> BROWNFIELD_BASELINE_SPEC
    (draft->audit->gate) -> brownfield_write_manifest (deterministic: ratification IS what creates
    manifest.json) -> app_check_record, where both branches converge before raw-requirements.
    Verification status: NOT exercised against a real sandbox."""
    builder.add_node("brownfield_baseline_pre", preflight_nodes.brownfield_baseline_context_node)
    builder.add_node("brownfield_write_manifest", preflight_nodes.brownfield_write_manifest_node)
    _wire_stage(builder, BROWNFIELD_BASELINE_SPEC, "brownfield_write_manifest")
    builder.add_edge("brownfield_baseline_pre", "brownfield-baseline_draft")
    builder.add_edge("brownfield_write_manifest", "app_check_record")


def _wire_p14(builder: StateGraph) -> None:
    """Wires metrics-report's metrics + traceability + token-tracking node, plus the one named LLM
    exception (ponytail-gain), plus the regression gate: metrics_compute already runs the final
    full-profile scan and computes the delta, so gating on it costs nothing extra on the first
    pass. Clean -> ponytail-gain -> exit. Regressed -> one automatic re-scan (tool flake), then
    metrics_regression_record sets run_failure and the run continues INTO exit -- never END, so
    exit.md/manifest/session close still happen and exit's verify blocks the merge.
    Verification status: NOT exercised against a real sandbox, same caveat as
    quality-remediation/security-remediation/audit-cluster/test-hardening."""
    builder.add_node("readme_write", metrics_nodes.readme_write_node)
    builder.add_node("metrics_compute", metrics_nodes.metrics_compute_node)
    builder.add_node("metrics_regression_record", metrics_nodes.metrics_regression_record_node)
    builder.add_node("metrics_ponytail_gain", metrics_nodes.metrics_ponytail_gain_node)
    # The README leg (W7) fronts the metrics pass -- REBUILD_FOR_ADVERSARIAL_COMPLIANCE's
    # next_node lands here, and the final scan then covers the committed README.
    builder.add_edge("readme_write", "metrics_compute")
    builder.add_conditional_edges(
        "metrics_compute",
        metrics_nodes.make_metrics_route_after_compute(),
        {"next": "metrics_ponytail_gain", "retry": "metrics_compute", "fail": "metrics_regression_record"},
    )
    # "exit_draft" until the stage-8 consolidation renamed that stage to metrics-exit; these two
    # edges were left pointing at a node that no longer exists, so reviving this function without
    # fixing them would fail graph compilation.
    builder.add_edge("metrics_regression_record", "metrics-exit_draft")
    builder.add_edge("metrics_ponytail_gain", "metrics-exit_draft")


def _wire_p13(builder: StateGraph) -> None:
    """Wires test-hardening's node cluster (test_hardening_nodes.py). "next" from
    test_hardening_exit_check routes into e2e's own gate check (_wire_e2e, e2e_nodes.py) -- e2e
    sits between test-hardening and metrics-report's compute node. Verification status: NOT
    exercised against a real sandbox, same caveat as quality-remediation/security-remediation/audit-cluster."""
    builder.add_node("test_hardening_run_tests", test_hardening_nodes.test_hardening_run_tests_node)
    builder.add_node("test_hardening_regression_gate", test_hardening_nodes.test_hardening_regression_gate_node)
    builder.add_node("test_hardening_flake_triage", test_hardening_nodes.test_hardening_flake_triage_node)
    builder.add_node("test_hardening_mint_tickets", test_hardening_nodes.test_hardening_mint_tickets_node)
    builder.add_node("test_hardening_exit_check", test_hardening_nodes.test_hardening_exit_check_node)
    builder.add_node("test_hardening_exit_escalate", test_hardening_nodes.test_hardening_exit_escalate_node)

    builder.add_node("test_hardening_fix", test_hardening_nodes.test_hardening_fix_node)
    builder.add_conditional_edges(
        "test_hardening_run_tests",
        test_hardening_nodes.make_test_hardening_route_after_run(),
        {"regression": "test_hardening_regression_gate", "triage": "test_hardening_flake_triage", "fix": "test_hardening_fix"},
    )
    # The fix lap loops back into the SAME deterministic re-run -- green is proven by the full
    # suite (three identical attempts), never by the fix agent's word.
    builder.add_edge("test_hardening_fix", "test_hardening_run_tests")
    # Routes INTO metrics-exit rather than END, for exactly the reason _wire_p14's docstring gives
    # for metrics regressions: "never END, so exit.md/manifest/session close still happen and exit's
    # verify blocks the merge". Ending here instead meant a test regression produced no exit report,
    # no manifest completion and no session close -- the run just stopped, and the human got a
    # failure with none of the artifacts that explain it. The regression still blocks the merge;
    # test_hardening_regression_gate_node sets run_failure, which exit_finalize_node reads first.
    builder.add_edge("test_hardening_regression_gate", "metrics-exit_draft")
    builder.add_edge("test_hardening_flake_triage", "test_hardening_mint_tickets")
    builder.add_edge("test_hardening_mint_tickets", "test_hardening_exit_check")
    builder.add_conditional_edges(
        "test_hardening_exit_check", test_hardening_nodes.make_test_hardening_route_after_exit(), {"next": "e2e_gate_check", "escalate": "test_hardening_exit_escalate"}
    )
    builder.add_edge("test_hardening_exit_escalate", "metrics-exit_draft")  # see e2e_escalate


def _wire_e2e(builder: StateGraph) -> None:
    """Wires e2e's node cluster (e2e_nodes.py): e2e_gate_check (deterministic: UI-framework repo?
    playwright config/tests present? a runner resolvable?) -> route("skip" -> metrics_compute |
    "run" -> e2e_run) -> e2e_run (boots the app, runs the suite, harvests screenshots) ->
    route("pass" -> metrics_compute | "fix" [attempt < E2E_MAX_FIX_CYCLES] -> e2e_fix -> e2e_run |
    "escalate" -> e2e_escalate -> metrics-exit_draft). Verification status: NOT exercised against a
    real container -- see e2e_nodes.py's own module docstring for exactly what's unconfirmed."""
    builder.add_node("e2e_gate_check", e2e_nodes.e2e_gate_check_node)
    builder.add_node("e2e_run", e2e_nodes.e2e_run_node)
    builder.add_node("e2e_fix", e2e_nodes.e2e_fix_node)
    builder.add_node("e2e_escalate", e2e_nodes.e2e_escalate_node)

    # Both non-failure exits land on adversarial-compliance, the audit that now closes the back
    # half. "skip" included: a repo with no UI still gets audited for Plan conformance.
    builder.add_conditional_edges(
        "e2e_gate_check",
        e2e_nodes.make_e2e_route_after_gate_check(),
        {"skip": "adversarial-compliance_draft", "run": "e2e_run"},
    )
    builder.add_conditional_edges(
        "e2e_run",
        e2e_nodes.make_e2e_route_after_run(),
        {"pass": "adversarial-compliance_draft", "fix": "e2e_fix", "escalate": "e2e_escalate"},
    )
    builder.add_edge("e2e_fix", "e2e_run")
    # Into metrics-exit, not END -- same reasoning as test_hardening_regression_gate above. The node
    # has already recorded run_failure, so the merge stays blocked; routing onward just means the
    # human also gets exit.md, the manifest and a closed session explaining WHY e2e failed, instead
    # of a run that stops with no artifact naming the failure.
    builder.add_edge("e2e_escalate", "metrics-exit_draft")


def _wire_rebuild(builder: StateGraph, spec: rebuild.RebuildSpec) -> str:
    """Adds one RebuildSpec's rebuild/fix/escalate nodes and routing to `builder`. Returns the
    rebuild node's own name -- the edge callers should route *into* to enter this R placement."""
    rebuild_name = f"{spec.key}_rebuild"
    fix_name = f"{spec.key}_fix"
    escalate_name = f"{spec.key}_escalate"

    builder.add_node(rebuild_name, rebuild.make_rebuild_node(spec))
    builder.add_node(fix_name, rebuild.make_fix_node(spec))
    builder.add_node(escalate_name, rebuild.make_escalate_node(spec))

    builder.add_conditional_edges(
        rebuild_name,
        rebuild.make_route_after_rebuild(spec),
        {"next": spec.next_node, "fix": fix_name, "escalate": escalate_name},
    )
    builder.add_edge(fix_name, rebuild_name)
    # Into metrics-exit, not END -- same reasoning as e2e_escalate/test_hardening above: the node
    # has already recorded run_failure, so the merge stays blocked; routing onward means the human
    # also gets exit.md, the manifest and a closed session naming the failure. The one exception is
    # cannot_verify (sandbox gone): metrics-exit executes in the sandbox, so that branch still ENDs
    # -- git_ops.record_run_failure at least closed the DB session row.
    builder.add_conditional_edges(
        escalate_name,
        rebuild.route_after_escalate,
        {"exit": "metrics-exit_draft", "end": END},
    )
    return rebuild_name


def _wire_stage(builder: StateGraph, stage_spec: StageSpec, next_draft_name: str) -> None:
    """Registers one StageSpec's full draft->audit->[verify]->gate/auto_approve subgraph and
    routes its exit into `next_draft_name`. Extracted from build_graph()'s STAGES loop (which
    still computes next_draft_name for each STAGES entry and calls this) so adversarial-audit/b/d -- which sit
    outside the flat STAGES list because finding-cluster's bespoke cluster interrupts the otherwise-linear
    chain -- can reuse the exact same per-stage wiring with an explicit custom next target,
    without fighting STAGES' automatic "next entry in the list" assumption."""
    draft_name = f"{stage_spec.key}_draft"
    gate_name = f"{stage_spec.key}_gate"
    auto_approve_name = f"{stage_spec.key}_auto_approve"
    draft_escalate_name = f"{stage_spec.key}_draft_escalate"
    verify_name = f"{stage_spec.key}_verify" if stage_spec.deterministic_verify is not None else None

    builder.add_node(draft_name, make_draft_node(stage_spec))
    builder.add_node(gate_name, make_gate_node(stage_spec))
    builder.add_node(auto_approve_name, make_auto_approve_node(stage_spec))
    # Wired for every stage regardless of whether it has a deterministic_verify (unlike the
    # verify-cap escalate below, which only exists then) -- draft-level infra exhaustion can
    # happen on any stage, including tech-stack, the one STAGES entry with no verify_name.
    builder.add_node(draft_escalate_name, make_draft_escalate_node(stage_spec))
    builder.add_edge(draft_escalate_name, END)

    # The adversarial audit leg is opt-in (specification/plan/ac-to-tests/minimal-code-to-green
    # only): a stage without audit config routes its ready draft straight to verify (when present)
    # or the gate. Everything else -- persistence, signing, hooks -- happens in the gate body either way.
    if stage_spec.audit_response_schema is not None:
        after_draft = f"{stage_spec.key}_audit"
        builder.add_node(after_draft, make_audit_node(stage_spec))
        builder.add_edge(after_draft, verify_name or gate_name)
    else:
        after_draft = verify_name or gate_name

    builder.add_conditional_edges(
        draft_name,
        make_route_after_draft(stage_spec),
        {
            "gate": after_draft,
            "auto_approve": auto_approve_name,
            "needs_clarification": END,
            "headless_redraft": draft_name,  # headless only: self-answer and redraft, no human exit
            "already_approved": next_draft_name,
            "escalate": draft_escalate_name,
        },
    )

    if verify_name is not None:
        # Real script/parse gate before the approval gate -- fail routes back to draft (with
        # feedback context) up to max_verify_cycles; at the cap the run ENDs with run_failure
        # (no human escalation -- spec/plan approval are the only human touchpoints).
        escalate_name = f"{stage_spec.key}_escalate"
        builder.add_node(verify_name, make_verify_node(stage_spec))
        builder.add_node(escalate_name, make_escalate_node(stage_spec))
        retry_target = draft_name
        if stage_spec.verify_fix_prompt:
            fix_name = f"{stage_spec.key}_verify_fix"
            builder.add_node(fix_name, make_verify_fix_node(stage_spec))
            builder.add_edge(fix_name, draft_name)
            retry_target = fix_name
        builder.add_conditional_edges(
            verify_name,
            make_route_after_verify(stage_spec),
            {"gate": gate_name, "retry": retry_target, "escalate": escalate_name},
        )
        builder.add_edge(escalate_name, END)

    # Part 2 Task 10 (Ruling 3): was an unconditional add_edge(gate_name, next_draft_name) --
    # gate_node could only ever mark a stage approved before this task. "rejected" only ever
    # fires for the 3 requires_human_gate=True stages (make_route_after_gate); every non-gated
    # stage's gate_node always reaches "approved", so this is a no-op change for those 5 stages,
    # not a behavior change -- see make_route_after_gate's own docstring.
    builder.add_conditional_edges(
        gate_name,
        make_route_after_gate(stage_spec),
        {"approved": next_draft_name, "rejected": draft_name},
    )
    # auto_approve (clarification cap) skips the AUDIT and the HUMAN gate -- never the
    # deterministic verify. Observed live (run 14): a stage auto-approved with NO draft content
    # sailed past the coverage gate entirely and the pipeline "progressed" with zero feature
    # code. A verify-bearing stage's verify IS its gate; auto-approve must still face it.
    builder.add_edge(auto_approve_name, verify_name or next_draft_name)


# Every standalone StageSpec (not in the flat STAGES list, because a bespoke cluster sits between
# it and its neighbors -- see audit-cluster's own note above). intake_node's setdefault/reset loops need
# every stage key that will ever appear in GraphState.stages, not just STAGES' own eight.
# Note: ADVERSARIAL_AUDIT_SPEC, DEDUP_SPEC, LICENSE_AUDIT_SPEC, EXIT_SPEC are now consolidated
# into stages 7 (adversarial-compliance) and 8 (metrics-exit), so they're no longer standalone.
_STANDALONE_STAGE_SPECS: list[StageSpec] = [
    BROWNFIELD_BASELINE_SPEC,
]
_ALL_STAGE_SPECS: list[StageSpec] = STAGES + _STANDALONE_STAGE_SPECS
# raw-requirements has no StageSpec (deterministic record node) but its stage KEY must stay in
# both derived collections: hydration restores it and persistence renders its .md from it.
_STAGE_KEYS = [stage.key for stage in _ALL_STAGE_SPECS] + ["raw-requirements"]
_RENDER_MARKDOWN_BY_STAGE = {stage.key: stage.render_markdown for stage in _ALL_STAGE_SPECS}
_RENDER_MARKDOWN_BY_STAGE["raw-requirements"] = render_raw_requirements_markdown
# tech-stack.md has exactly one writer now: preflight_nodes.resolve_tech_stack_submission (the
# human's own edited text, written at gate-approval time). The generic per-stage render below
# would otherwise re-render approved_content (the machine-extracted TechStack JSON) over that
# same path on every persist call, silently discarding whatever the human actually typed --
# confirmed by tracing persist_state's "content_to_render = approved_content ... write .md" step,
# which runs on every gate approval regardless of which stage. Popped, not left mapped to None:
# persist_state's `.get(stage_key)` treats an absent key and an explicit None identically, but an
# absent key is what documents "deliberately no writer" rather than "renderer not set up yet".
del _RENDER_MARKDOWN_BY_STAGE["tech-stack"]


def _with_live_refresh(name: str, action):
    """Second choke-point wrap (see _TracedStateGraph): after any node returns, fold in a finished
    background refresh scan + running token totals (metrics_nodes.collect_live_refresh) so the
    metrics bar updates at the next node boundary after every code-writing commit. Display-only
    and strictly non-invasive: dict returns only, never a node that wrote repo_scan itself, and
    any collector error is swallowed -- a display refresh must never fail a real node.

    Must mirror telemetry.traced_node's arity dispatch: this wrapper's own (state, config) shape
    hides the inner node's signature from traced_node, so (state)-only pass-throughs like
    _manifest_branch_node have to be dispatched here."""

    wants_config = len(inspect.signature(action).parameters) >= 2

    async def wrapped(state, config):
        result = action(state, config) if wants_config else action(state)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict) and "repo_scan" not in result:
            try:
                update = await metrics_nodes.collect_live_refresh(state, config["configurable"]["thread_id"])
            except Exception:  # noqa: BLE001 -- display-only; the node's own result is what matters
                logger.warning("collect_live_refresh failed after node %s", name, exc_info=True)
                update = None
            if update:
                result = {**result, **update}
        return result

    return wrapped


class _TracedStateGraph(StateGraph):
    """Wraps every node callable in a telemetry span at the single choke point all 51
    add_node call sites flow through -- no per-node edits, no per-cluster wiring."""

    def add_node(self, node, action=None, **kwargs):  # type: ignore[override]
        if isinstance(node, str) and callable(action):
            action = telemetry.traced_node(node, _with_live_refresh(node, action))
        return super().add_node(node, action, **kwargs)


def build_graph() -> StateGraph:
    builder = _TracedStateGraph(GraphState)
    builder.add_node("intake", intake_node)
    builder.add_node("scaffold", preflight_nodes.scaffold_node)
    builder.add_edge(START, "intake")
    # The frontend fires a blank run on mount as its only reload/reattach transport (it re-emits
    # a pending interrupt or rehydrates state). A blank run on a thread with NO requirements --
    # neither typed text nor anything hydrated back out of the repo -- must be a free no-op, not
    # a trip through app-discovery/tech-stack/baseline LLM stages on empty input.
    builder.add_conditional_edges("intake", _route_after_intake, {"scaffold": "scaffold", END: END})
    # Suitability first: app discovery decides whether this workflow applies at all, before
    # tech-stack detection, before brownfield-baseline's ratification, and before anything is written to
    # the repository (see preflight_nodes.scaffold_finalize_node).
    builder.add_edge("scaffold", "app_discovery_pre")

    _wire_tech_stack_intake(builder)
    _wire_brownfield(builder)
    # CONSOLIDATED into stages 6-8 -- these clusters' work genuinely moved into a StageSpec, so
    # their wiring is dead on purpose:
    # _wire_p15(builder)  # exit → stage 8 (metrics-exit + exit_finalize post_approve_hook)
    #
    # NOT consolidated -- these three were only ever switched OFF, and nothing took over their
    # work. Their nodes are add_node'd exclusively inside these functions, so leaving them
    # commented removed test-hardening, e2e and the metrics/regression pass from the compiled graph
    # entirely: no app was ever booted, no screenshot taken, and metrics-latest.json was never
    # written for any run. Every completed run therefore ended with exit.md carrying
    # "no e2e screenshots were captured" + "metrics were not recorded for this run", which forced
    # merge_ready=False and status="failed" -- the pipeline could not report a successful run at all.
    # `assert_pipeline_nodes_registered()` below now fails loudly if any of them goes missing again.
    _wire_p13(builder)  # test-hardening: runs the suite N x, triages flakes
    _wire_e2e(builder)  # e2e: boots the app, drives playwright, harvests screenshots
    _wire_p14(builder)  # metrics: final full scan, baseline delta, regression gate

    # Deterministic pre-draft scan for remediation: REBUILD_AFTER_P6 routes here instead of
    # straight into remediation_draft, so the stage reads the findings of the code that was just
    # written rather than a scan file that did not exist yet. No LLM, no gate -- one scan, published.
    builder.add_node("remediation_scan", remediation_gate.remediation_scan_node)
    builder.add_edge("remediation_scan", "remediation_draft")

    # R placements are registered BEFORE the stages that route into them, so each stage can be
    # wired with its true successor in one shot. The previous order (wire stage -> next_draft,
    # then "replace" that edge with one to the rebuild node) could not work: builder.add_edge only
    # ADDS, so both edges stayed live and the gate fanned out into two concurrent branches. Both
    # branches write `stages`, which LangGraph rejects within one super-step -- observed live:
    # "InvalidUpdateError: At key 'stages': Can receive only one value per step", ~15 minutes into
    # a run, once the pipeline finally got far enough for the second branch to do real work.
    post_stage_rebuild_entry_name = {key: _wire_rebuild(builder, spec) for key, spec in POST_STAGE_REBUILD.items()}

    for index, stage_spec in enumerate(STAGES):
        if stage_spec.key == STAGES[0].key:
            # tech-stack exits into the brownfield branch, not straight into raw-requirements --
            # _wire_tech_stack_intake owns that edge (and everything before it).
            next_draft_name = "manifest_branch"
        elif index + 1 < len(STAGES):
            next_draft_name = f"{STAGES[index + 1].key}_draft"
        else:
            next_draft_name = END

        # A stage with an R placement hands off to the rebuild node, which owns the edge onward to
        # the next stage (RebuildSpec.next_node) once the build is green.
        rebuild_entry = post_stage_rebuild_entry_name.get(stage_spec.key)
        _wire_stage(builder, stage_spec, rebuild_entry or next_draft_name)

    return builder


# Nodes that do work no other node took over, listed so their ABSENCE is a loud failure rather than
# a silently shorter pipeline. Three of these sat unregistered for the whole consolidation (their
# _wire_* calls were commented out as "now part of stage 7/8" when nothing had in fact taken them
# over), and every run completed "successfully" without ever booting the app, taking a screenshot,
# or recording a metric -- visible only as two stale-looking blocker lines in exit.md.
REQUIRED_PIPELINE_NODES = (
    "test_hardening_run_tests",  # runs the suite N x to find flakes
    "e2e_gate_check",
    "e2e_run",  # boots the app, drives playwright, harvests screenshots
    "readme_write",  # standard-readme leg -- runs before the final scan so it covers the README
    "metrics_compute",  # final scan + baseline delta -> metrics-latest.json
    "metrics_regression_record",
)


def assert_pipeline_nodes_registered(builder: StateGraph) -> None:
    """Fails loudly when a pipeline node is not in the compiled graph.

    A missing node does not raise on its own: the graph simply routes around the work, every stage
    still reports "approved", and the loss shows up only as an empty section in a report nobody
    reads closely. That is precisely how test-hardening, e2e and metrics stayed dead across 5+
    end-to-end runs, so this is an assertion rather than a comment.
    """
    missing = [name for name in REQUIRED_PIPELINE_NODES if name not in builder.nodes]
    assert not missing, (
        f"pipeline nodes missing from the graph: {missing} -- a _wire_* call is commented out or "
        "renamed. The run would silently skip that work and still report success."
    )


# Node-defining modules that must still contribute at least one node to the compiled graph. A module
# whose EVERY *_node function is unreachable is a cluster that was switched off without being
# deleted -- which is exactly what happened to quality/security/audit/finding-cluster: three
# `_wire_*` calls commented out as "now part of stage 6/7", both replacement stages turned out to be
# stubs, and 22 node functions sat dead while every run reported success.
_NODE_MODULES = (
    "test_hardening_nodes",
    "e2e_nodes",
    "metrics_nodes",
    "app_discovery",
    "preflight_nodes",
)
# Functions named *_node that are NOT graph nodes: post_approve hooks and prompt helpers. Without
# this the detector reports them as dead (it did, on its first run).
_NON_GRAPH_NODE_FUNCTIONS = frozenset({
    "exit_finalize_node",              # StageSpec.post_approve_hook, never add_node'd
    "brownfield_baseline_context_node",  # prompt-context helper
})


def assert_no_dead_clusters(builder: StateGraph) -> None:
    """Fail when a node module has been orphaned rather than deleted.

    Retiring a cluster is fine; retiring it by commenting out one line and leaving the module behind
    is what cost this pipeline its e2e, test-hardening and metrics stages for an entire session. This
    makes that state a build failure instead of a comment nobody reads.
    """
    import importlib
    import inspect

    live = set(builder.nodes)
    orphaned: list[str] = []
    for module_name in _NODE_MODULES:
        module = importlib.import_module(f".{module_name}", __package__)
        node_fns = [
            name
            for name, fn in inspect.getmembers(module, inspect.isfunction)
            if name.endswith("_node")
            and name not in _NON_GRAPH_NODE_FUNCTIONS
            and (inspect.getmodule(fn).__name__ if inspect.getmodule(fn) else "").endswith(module_name)
        ]
        if node_fns and not any(name.removesuffix("_node") in live or name in live for name in node_fns):
            orphaned.append(f"{module_name} ({len(node_fns)} node functions, none registered)")
    assert not orphaned, (
        f"dead node cluster(s): {orphaned} -- a _wire_* call is commented out or renamed. Delete the "
        "module, or register it; do not leave it dormant."
    )


def assert_no_stub_stages() -> None:
    """Fail when a StageSpec is a stub: a literal prompt, an uncheckable schema, or no tools.

    Every marker here comes from a stage that shipped this way. remediation's prompt was the single
    sentence "Review prior quality, security, dedup, and license findings and recommend fixes", with
    no findings passed in and no write tools -- so it fixed nothing while approving every run.
    adversarial-compliance was the same shape with `report: dict | None`.

    Prompt provenance is checked BY VALUE, not by inspecting build_prompt for a `load_prompt` call:
    stages load their prompt into a module constant and merely reference it, so a source-inspection
    check passes only the stages that happen to load inline -- it would have flagged all eight
    healthy stages and missed nothing.
    """
    from pathlib import Path

    # Both whole files AND their system halves: load_prompt_pair splits a prompt file on its first
    # `---` line, so a paired prompt's system constant is only the top of the file and would never
    # equal the whole text. (The detector caught this about itself on first run.)
    prompt_texts: set[str] = set()
    # Via load_prompt, not a raw read_text: the loader prepends prompts/global_system_segment.md
    # to every non-_segment prompt, so the constants being checked carry that preamble too -- a
    # raw disk read would flag every healthy stage as literal.
    for path in (Path(__file__).parent / "prompts").glob("*.md"):
        text = load_prompt(path.stem)
        prompt_texts.add(text)
        system_half, separator, _ = text.partition("\n---\n")
        if separator:
            prompt_texts.add(system_half.strip())
    # ac_to_tests_draft.md's playwright.config.ts snippet is a <<placeholder>> resolved from a
    # template file at module load (AC_TO_TESTS_SYSTEM_PROMPT, above) -- one canonical copy shared
    # with the sandbox image's pinned Playwright version, instead of hand-duplicated prose that can
    # drift. Add the POST-substitution text too, so this by-value check still recognizes it as
    # prompt-file-backed rather than flagging every deliberate template substitution as a stub.
    prompt_texts.add(
        load_prompt("ac_to_tests_draft")
        .replace("<<playwright_config_template>>", template_loader.load_template("playwright/playwright.config.ts").strip())
        .strip()
    )
    problems: list[str] = []
    for spec in _ALL_STAGE_SPECS:
        fields = set(getattr(spec.response_schema, "model_fields", {}) or {})
        if not (fields - {"readiness", "clarifying_questions"}):
            problems.append(f"{spec.key}: response schema has no typed payload field")
        if not spec.session_options:
            problems.append(f"{spec.key}: declares no session_options, so it has no tools at all")

    # The prompt check needs a rendered message, which needs state; assert instead that every stage's
    # system prompt constant is one of the prompt files. Constants are module-level, so compare the
    # set of them rather than calling build_prompt with a fake state.
    literal_prompts = [
        name
        for name, value in globals().items()
        if name.endswith("SYSTEM_PROMPT")
        and isinstance(value, str)
        and value.strip() not in prompt_texts
    ]
    if literal_prompts:
        problems.append(
            f"system prompt(s) not backed by a prompts/*.md file: {sorted(literal_prompts)}"
        )
    assert not problems, "stub stage(s) detected: " + "; ".join(problems)


def compile_graph():
    builder = build_graph()
    assert_pipeline_nodes_registered(builder)
    assert_no_dead_clusters(builder)
    assert_no_stub_stages()
    checkpointer = InMemorySaver()
    store = InMemoryStore()
    # Async checkpoint durability (Section 3.5): "async" is the documented
    # default for invoke/stream/astream_events, so not overriding it here is
    # sufficient; noted explicitly rather than left as an unremarked default.
    return builder.compile(checkpointer=checkpointer, store=store)


graph = compile_graph()


def _demo() -> None:
    """`cd agent && uv run python -m src.graph`.

    build_graph() already asserts the structural invariants (every required node registered, no dead
    cluster, no stub stage). What this adds is the routing PREDICATE that no structural check can
    see: whether a stage that failed verification actually gets redrafted.
    """
    # Genuine resume: approved, content present, verification clean (or never run) -> skip.
    assert should_skip_draft({"status": "approved", "approved_content": {"x": 1}, "last_verification": None})
    assert should_skip_draft({"status": "approved", "approved_content": {"x": 1}})
    assert should_skip_draft(
        {"status": "approved", "approved_content": {"x": 1}, "last_verification": {"passed": True}}
    )

    # The live bug this function exists for: auto-approved, then verify FAILED. The redraft MUST run,
    # or the verify-retry loop silently does nothing and the rejected content ships.
    assert not should_skip_draft(
        {
            "status": "approved",
            "approved_content": {"coverage_plan": [], "test_files": []},
            "last_verification": {"passed": False, "feedback": "no e2e specs for any UI criterion"},
        }
    )
    # cannot_verify (no sandbox) is a failure too -- never skip on it.
    assert not should_skip_draft(
        {"status": "approved", "approved_content": {"x": 1}, "last_verification": {"passed": False, "cannot_verify": True}}
    )
    # Nothing approved, or approved with no content -> the draft has to run.
    assert not should_skip_draft({"status": "in_progress", "approved_content": {"x": 1}})
    assert not should_skip_draft({"status": "approved", "approved_content": None})

    # The cross-process half of the same bug: `intake` clears last_verification on every run, so a
    # stage that escalated would hydrate as cleanly approved and skip its redraft forever. escalate_node
    # therefore REVOKES the approval, and this is the shape it leaves behind.
    assert not should_skip_draft(
        {"status": "needs_clarification", "approved_content": None, "last_verification": None}
    ), "an escalated stage must be redrafted on the next run, not skipped as approved"

    # The other half of that bug: auto_approve (clarification cap) used to COMMIT its approval to
    # the branch before verify had run, and that committed file is what the next run hydrates. A
    # verify-bearing stage must leave nothing on disk until it passes; a stage with no verify still
    # has to persist, since nothing downstream would.
    import asyncio

    persisted: list[str] = []

    async def _record_persist(thread_id, state, stages, message):  # noqa: ANN001, ANN202
        persisted.append(message)

    async def _skip_hook(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return None

    real_persist, real_hook = _persist_if_sandboxed, _run_post_approve_hook
    globals()["_persist_if_sandboxed"] = _record_persist
    globals()["_run_post_approve_hook"] = _skip_hook
    try:
        by_key = {spec.key: spec for spec in STAGES}
        for spec, expect_persist in ((by_key["ac-to-tests"], False), (by_key["tech-stack"], True)):
            persisted.clear()
            demo_state = {"stages": {spec.key: {**default_stage_state(), "draft": {"x": 1}}}}
            asyncio.run(
                make_auto_approve_node(spec)(demo_state, {"configurable": {"thread_id": "demo-thread"}})  # type: ignore[arg-type]
            )
            assert bool(persisted) is expect_persist, (
                f"{spec.key}: auto_approve persisted={bool(persisted)}, expected {expect_persist} "
                "(a verify-bearing stage's unverified approval must never reach the branch)"
            )
    finally:
        globals()["_persist_if_sandboxed"] = real_persist
        globals()["_run_post_approve_hook"] = real_hook

    build_graph()  # runs assert_pipeline_nodes_registered + assert_no_dead_clusters
    assert_no_stub_stages()
    assert_gates_have_self_checks()

    # make_route_after_gate (Part 2 Task 10): pure predicate, same convention as
    # should_skip_draft/_detect_verify_stall in this same function -- the real interrupt()/resume
    # round-trip (gate_node's own rejection branch) cannot run outside a real LangGraph execution
    # context, so it's exercised for real against the compiled graph separately (task-10-report.md),
    # not here.
    route_after_gate = make_route_after_gate(by_key["specification"])
    assert route_after_gate({"stages": {"specification": {**default_stage_state(), "status": "approved"}}}) == "approved"
    assert (
        route_after_gate(
            {
                "stages": {
                    "specification": {
                        **default_stage_state(),
                        "status": "needs_clarification",
                        "reviewer_feedback": "add multi-tenant support to US-3",
                    }
                }
            }
        )
        == "rejected"
    )
    # Never true before this task; confirms the "always approved" no-op claim for the 5 non-gated
    # stages' own StageSpec.requires_human_gate default is irrelevant here -- make_route_after_gate
    # only ever reads status, which gate_node always sets to "approved" for those stages.
    route_after_gate_nongated = make_route_after_gate(by_key["ac-to-tests"])
    assert route_after_gate_nongated({"stages": {"ac-to-tests": {**default_stage_state(), "status": "approved"}}}) == "approved"

    # Phase E review (Important 2): GATE_PAUSED must fire once per GENUINE pause, not once per
    # replay -- LangGraph re-executes gate_node from the top on every resume, and the naive
    # version double-emitted (once on the real pause, once again on the replay, right before
    # interrupt() returns the resume value instead of pausing again). Drives the REAL gate_node
    # closure through a scripted pause -> reject -> re-pause -> approve sequence, same
    # globals()-patching technique the auto_approve/draft checks above already use for a real
    # LangGraph mechanism this file cannot cheaply compile+run for real (see make_route_after_gate's
    # own comment above for why that full round-trip is otherwise exercised separately, not here):
    # `interrupt` is faked to behave like the real one (raises to simulate a pause, returns a
    # scripted resume value to simulate a resume); session_store.get_session/set_awaiting_gate are
    # faked with a tiny in-memory awaiting_gate flag -- the actual durable signal this fix reads;
    # a fake sandbox_registry entry makes gate_node take the awaiting_gate/GATE_PAUSED branch at
    # all; RunEvent emission is captured via a spy on run_event_store.append_event.
    class _FakeGraphInterrupt(Exception):
        pass

    demo_gate_spec = StageSpec(
        key="demo-gate",
        response_schema=object,  # unused by gate_node
        content_field=None,
        surface_tool_name="demo",
        build_envelope=lambda *a: {},
        build_prompt=lambda *a: [],
        max_cycles=1,
        render_markdown=lambda *a: "",
    )
    assert demo_gate_spec.requires_human_gate and not demo_gate_spec.sign_approval and demo_gate_spec.resolve_from_interrupt is None, (
        "this fixture must take the plain pause/approve/reject path with no extra side hooks"
    )

    fake_awaiting_gate = {"value": False}
    scripted_resume: list[Any] = []

    def _fake_interrupt(payload):  # noqa: ANN001, ANN202
        if scripted_resume:
            return scripted_resume.pop(0)
        raise _FakeGraphInterrupt()

    async def _fake_get_session(session_id):  # noqa: ANN001, ANN202
        return {"awaiting_gate": fake_awaiting_gate["value"]}

    async def _fake_set_awaiting_gate(session_id, awaiting):  # noqa: ANN001, ANN202
        fake_awaiting_gate["value"] = awaiting

    captured_gate_events: list[RunEvent] = []

    async def _fake_append_event(event):  # noqa: ANN001, ANN202
        captured_gate_events.append(event)
        return event

    async def _fake_emit_live(event, config=None):  # noqa: ANN001, ANN202, ARG001
        return None

    real_interrupt = globals()["interrupt"]
    real_get_session = session_store.get_session
    real_set_awaiting_gate = session_store.set_awaiting_gate
    real_append_event = run_event_store.append_event
    real_emit_live = run_event_stream.emit_live
    real_persist_2, real_hook_2 = _persist_if_sandboxed, _run_post_approve_hook
    demo_gate_thread = "demo-gate-pause-thread"
    sandbox_registry.set(demo_gate_thread, SandboxSession(session_id=demo_gate_thread, host="fake", port=0, connection_token=""))
    globals()["interrupt"] = _fake_interrupt
    session_store.get_session = _fake_get_session
    session_store.set_awaiting_gate = _fake_set_awaiting_gate
    run_event_store.append_event = _fake_append_event
    run_event_stream.emit_live = _fake_emit_live
    globals()["_persist_if_sandboxed"] = _skip_hook
    globals()["_run_post_approve_hook"] = _skip_hook
    try:
        gate_fn = make_gate_node(demo_gate_spec)
        gate_state = {
            "stages": {demo_gate_spec.key: {**default_stage_state(), "draft": {"x": 1}, "status": "ready_for_review"}},
            "run_id": "demo-gate-run",
        }
        gate_cfg = {"configurable": {"thread_id": demo_gate_thread}}

        # 1st genuine pause: awaiting_gate starts False -> must emit GATE_PAUSED, then "pause".
        try:
            asyncio.run(gate_fn(gate_state, gate_cfg))
            raise AssertionError("expected the fake interrupt to pause (raise) on a genuine pause")
        except _FakeGraphInterrupt:
            pass

        # Simulate LangGraph's replay-on-resume: gate_node re-executes from the top (awaiting_gate
        # is now True from the pause above) -- must NOT re-emit GATE_PAUSED -- and this time
        # interrupt() returns a REJECTED resume value instead of pausing again.
        scripted_resume.append({"decision": "rejected", "feedback": "needs more detail"})
        asyncio.run(gate_fn(gate_state, gate_cfg))

        # Redraft happens for real between here and the next pause (not simulated -- out of scope
        # for this test); the rejection branch already reset awaiting_gate to False, so the stage
        # becoming ready again is a SECOND genuine pause.
        try:
            asyncio.run(gate_fn(gate_state, gate_cfg))
            raise AssertionError("expected the fake interrupt to pause again for the second genuine pause")
        except _FakeGraphInterrupt:
            pass

        # Replay again, this time approved.
        scripted_resume.append({"decision": "approved"})
        asyncio.run(gate_fn(gate_state, gate_cfg))

        paused = [e for e in captured_gate_events if e.type is RunEventType.GATE_PAUSED]
        resolved = [e for e in captured_gate_events if e.type is RunEventType.GATE_RESOLVED]
        assert len(paused) == 2, f"expected exactly 2 GATE_PAUSED for 2 genuine pauses, got {len(paused)}"
        assert len(resolved) == 2, f"expected exactly 2 GATE_RESOLVED (1 rejected + 1 approved), got {len(resolved)}"
        assert [r.payload["decision"] for r in resolved] == ["rejected", "approved"], resolved
    finally:
        globals()["interrupt"] = real_interrupt
        session_store.get_session = real_get_session
        session_store.set_awaiting_gate = real_set_awaiting_gate
        run_event_store.append_event = real_append_event
        run_event_stream.emit_live = real_emit_live
        globals()["_persist_if_sandboxed"] = real_persist_2
        globals()["_run_post_approve_hook"] = real_hook_2
        sandbox_registry.pop(demo_gate_thread)

    # _resolve_thread_provider (Phase E audit I-3 review, Important 1): the restart scenario --
    # an empty checkpoint (state.get("provider") falsy) must prefer this session's OWN stored
    # dbo.sessions.provider over a live re-resolve, so a post-restart intake and
    # sessions_api.provision_session's own stored-or-live resolution can't independently land on
    # different values. session_store.get_session and chat_model.get_provider are faked to
    # DISAGREE on purpose ("claude" stored vs. "copilot" live) so a regression that fell back to
    # live can't accidentally pass by coincidence.
    async def _fake_get_session_stored_claude(session_id):  # noqa: ANN001, ANN202
        return {"provider": "claude"}

    async def _fake_get_provider_live_copilot():  # noqa: ANN202
        return "copilot"

    real_get_session_provider = session_store.get_session
    real_get_provider_thread = chat_model.get_provider
    session_store.get_session = _fake_get_session_stored_claude
    chat_model.get_provider = _fake_get_provider_live_copilot
    try:
        # Empty checkpoint -- must resolve the STORED "claude", never the disagreeing live "copilot".
        resolved = asyncio.run(_resolve_thread_provider("restart-thread", {}))
        assert resolved == "claude", (
            f"an empty checkpoint must prefer the session's stored provider over a live "
            f"re-resolve, got {resolved!r}"
        )

        # Already-pinned state wins over BOTH the stored row and the live setting -- neither fake
        # above is even consulted (both would fail this assertion if reached, since neither returns
        # "claude-pinned").
        resolved = asyncio.run(_resolve_thread_provider("pinned-thread", {"provider": "claude-pinned"}))
        assert resolved == "claude-pinned", resolved
    finally:
        session_store.get_session = real_get_session_provider
        chat_model.get_provider = real_get_provider_thread

    # Genuinely new thread (no prior dbo.sessions row at all) -- falls through to the live setting,
    # exactly the "provisioning is the moment a live change takes effect" behavior for a real first
    # provision.
    async def _fake_get_session_none(session_id):  # noqa: ANN001, ANN202
        return None

    async def _fake_get_provider_live_claude():  # noqa: ANN202
        return "claude"

    real_get_session_new = session_store.get_session
    real_get_provider_new = chat_model.get_provider
    session_store.get_session = _fake_get_session_none
    chat_model.get_provider = _fake_get_provider_live_claude
    try:
        resolved = asyncio.run(_resolve_thread_provider("brand-new-thread", {}))
        assert resolved == "claude", resolved
    finally:
        session_store.get_session = real_get_session_new
        chat_model.get_provider = real_get_provider_new

    # DB-blip case (Phase E audit I-3 review round 3): session_store.get_session raising must NOT
    # propagate out of _resolve_thread_provider -- it must fall through to the live get_provider()
    # path (already hardened to survive its OWN DB failure), same degraded-but-alive behavior as
    # before this field's stored-provider preference existed.
    async def _fake_get_session_raises(session_id):  # noqa: ANN001, ANN202
        raise RuntimeError("simulated transient DB blip")

    real_get_session_blip = session_store.get_session
    real_get_provider_blip = chat_model.get_provider
    session_store.get_session = _fake_get_session_raises
    chat_model.get_provider = _fake_get_provider_live_claude
    try:
        resolved = asyncio.run(_resolve_thread_provider("db-blip-thread", {}))
        assert resolved == "claude", (
            f"a raising get_session must degrade to the live path, not propagate, got {resolved!r}"
        )
    finally:
        session_store.get_session = real_get_session_blip
        chat_model.get_provider = real_get_provider_blip

    # Regression check (Part 2 Task 10 review fix -- a real Critical bug this file's own review
    # caught): a stage re-entering make_draft_node on a REJECTION must not let
    # prefill_from_repo_file/hydrate_from_repo_file silently re-serve the exact pre-existing repo
    # file the human just rejected, discarding reviewer_feedback forever (neither short-circuit
    # touches cycle_count, so auto-approve cannot rescue it either). Exercises the real
    # make_draft_node closure for tech-stack (the only StageSpec with either hook) against a fake
    # pre-existing tech-stack.md, called twice: once fresh, once with the exact input shape
    # gate_node's own rejection branch produces. Same globals()-patching technique as the
    # auto_approve persistence check above, plus a real-shaped fake SandboxSession
    # (get_chat_model_for_thread validates it) and faked repo_files.read_repo_file/
    # append_ledger_entry -- the only two real-I/O calls this path reaches with no real container
    # behind the fake session (append_event/emit_live already fail soft on their own).
    real_ainvoke_structured = ainvoke_structured
    real_read_repo_file = repo_files.read_repo_file
    real_append_ledger_entry = repo_files.append_ledger_entry
    demo_thread_id = "demo-reject-prefill-thread"
    draft_call_count = {"n": 0}

    async def _fake_read_repo_file(_provider, _thread_id, path):  # noqa: ANN001
        return "# Tech Stack\n\nFake pre-existing stack.\n" if path.endswith("tech-stack.md") else None

    async def _fake_append_ledger_entry(_provider, _thread_id, _entry):  # noqa: ANN001
        return None

    async def _fake_ainvoke_structured(_model, _messages, _schema):  # noqa: ANN001
        draft_call_count["n"] += 1
        return TechStackDraftResponse(
            readiness=True, clarifying_questions=[],
            tech_stack=TechStack(summary=f"real draft call #{draft_call_count['n']}"),
        )

    globals()["ainvoke_structured"] = _fake_ainvoke_structured
    repo_files.read_repo_file = _fake_read_repo_file
    repo_files.append_ledger_entry = _fake_append_ledger_entry
    sandbox_registry.set(
        demo_thread_id, SandboxSession(session_id=demo_thread_id, host="localhost", port=0, connection_token="")
    )
    try:
        draft_node_fn = make_draft_node(by_key["tech-stack"])
        demo_cfg = {"configurable": {"thread_id": demo_thread_id}}

        first = asyncio.run(
            draft_node_fn(
                {"stages": {"tech-stack": default_stage_state()}, "provider": "copilot", "run_id": "demo"}, demo_cfg
            )
        )
        first_stage = first["stages"]["tech-stack"]
        assert first_stage["status"] == "ready_for_review" and draft_call_count["n"] == 0, (
            "prefill_from_repo_file should skip the LLM entirely on a fresh (never-rejected) draft"
        )

        rejected_input = {**first_stage, "status": "needs_clarification", "reviewer_feedback": "add a Python backend"}
        second = asyncio.run(
            draft_node_fn(
                {"stages": {"tech-stack": rejected_input}, "provider": "copilot", "run_id": "demo"}, demo_cfg
            )
        )
        second_stage = second["stages"]["tech-stack"]
        assert draft_call_count["n"] == 1, (
            "a rejection must force a REAL draft call -- prefill_from_repo_file silently "
            "re-served the same pre-existing file and discarded reviewer_feedback"
        )
        assert second_stage["draft"] != first_stage["draft"], (
            "post-rejection draft must not be byte-identical to the pre-rejection one"
        )
    finally:
        globals()["ainvoke_structured"] = real_ainvoke_structured
        repo_files.read_repo_file = real_read_repo_file
        repo_files.append_ledger_entry = real_append_ledger_entry
        sandbox_registry.pop(demo_thread_id)

    # _detect_verify_stall: the three heuristics, independently.
    s = _detect_verify_stall("same feedback", "same feedback", None, None, None, None)
    assert s.feedback_similar and s.any_triggered
    s = _detect_verify_stall("totally different this time", "wildly unrelated text entirely", None, None, None, None)
    assert not s.feedback_similar and not s.any_triggered
    s = _detect_verify_stall("f", "f2", ["a.py"], ["a.py"], None, None)
    assert s.paths_unchanged and s.any_triggered
    s = _detect_verify_stall("f", "f2", ["a.py"], ["b.py"], None, None)
    assert not s.paths_unchanged
    # No baseline_commit captured (minimal-code-to-green) -- both changed_paths sides are None, so
    # paths_unchanged must stay permanently inert (never a false trigger from "None == None").
    s = _detect_verify_stall("f", "f2", None, None, None, None)
    assert not s.paths_unchanged and not s.any_triggered

    # The exact documented live oscillation: 89 -> 85 -> 87 -> 85 covered branches. Consecutive-pair
    # comparison would miss every one of these (no two adjacent values are equal); tracking the
    # best-seen-so-far must not.
    best = None
    laps = []
    for rate in (89.0, 85.0, 87.0, 85.0):
        sig = _detect_verify_stall("f", "f2", None, None, rate, best)
        best = sig.new_best_coverage_rate
        laps.append(sig.coverage_not_improving)
    assert laps == [False, True, True, True], f"expected only the first lap (a new best) to NOT flag: {laps}"
    assert best == 89.0, "the high-water mark must survive laps that don't beat it, not reset to the latest value"
    # A genuinely improving run (each lap beats the last) must never be flagged as stalled.
    best = None
    for rate in (70.0, 80.0, 95.0):
        sig = _detect_verify_stall("f", "f2", None, None, rate, best)
        best = sig.new_best_coverage_rate
        assert not sig.coverage_not_improving, f"a strictly-improving rate ({rate}) must never count as a stall"

    # Task 7a: the plan/ac-to-tests ticket-mode reframe hooks actually change behavior on a cache
    # hit vs. a cache miss -- not just that they compile. A minimal duck-typed fake stands in for
    # SandboxProvider, same pattern as chat_model.py's own self-check: read_repo_file only ever
    # calls .exec_in_sandbox on it.
    class _FakeReadResult:
        def __init__(self, ok: bool, stdout: str = "") -> None:
            self.ok = ok
            self.stdout = stdout

    class _FakeRepoFileProvider:
        def __init__(self, files: dict[str, str]) -> None:
            self._files = files

        async def exec_in_sandbox(self, _thread_id: str, command: str):  # noqa: ANN201
            for path, content in self._files.items():
                if path in command:
                    return _FakeReadResult(True, content)
            return _FakeReadResult(False)

    # Cache MISS (no 04-plan.approved.json on disk -- project's first-ever ticket): no reframe.
    assert asyncio.run(
        hydrate_plan_ticket_mode_context("t", {}, _FakeRepoFileProvider({}))  # type: ignore[arg-type]
    ) is None
    # Cache HIT (an earlier ticket's approved plan already exists): reframe as ticket-mode.
    assert asyncio.run(
        hydrate_plan_ticket_mode_context(  # type: ignore[arg-type]
            "t", {}, _FakeRepoFileProvider({workflow_persistence.PLAN_APPROVED_PATH: "{}"})
        )
    ) == {"ticket_mode_baseline": True}

    # The hook is wired, not just correct in isolation: _build_plan_prompt actually appends
    # PLAN_TICKET_MODE_SEGMENT when (and only when) the prompt-only stage copy carries the signal.
    plan_demo_state = {
        "stages": {
            "specification": {**default_stage_state(), "approved_content": {}},
            "plan": {**default_stage_state()},
        },
        "app_scan": {"candidates": [{"path": "."}]},
    }
    assert not any(
        PLAN_TICKET_MODE_SEGMENT in str(m.content) for m in _build_plan_prompt(plan_demo_state)  # type: ignore[arg-type]
    )
    plan_demo_state["stages"]["plan"]["ticket_mode_baseline"] = True
    assert any(
        PLAN_TICKET_MODE_SEGMENT in str(m.content) for m in _build_plan_prompt(plan_demo_state)  # type: ignore[arg-type]
    )

    # Task 14: BR-2 stays exactly as it is (Plan gets zero raw attachment access) -- but
    # Specification's own attachment_notes field, once inside the approved Specification JSON,
    # must reach _build_plan_prompt unmodified through the very same "Approved Specification
    # (JSON)" message it already reads for user_stories/assumptions/etc -- no separate plumbing
    # exists for it, or should. Wired, not just correct in isolation, same proof as
    # PLAN_TICKET_MODE_SEGMENT above.
    plan_demo_state["stages"]["specification"]["approved_content"] = {
        "attachment_notes": [
            "task-14-demo-marker: screenshot showed a red error banner reading 'Session expired'"
        ]
    }
    assert any(
        "task-14-demo-marker: screenshot showed a red error banner reading 'Session expired'"
        in str(m.content)
        for m in _build_plan_prompt(plan_demo_state)  # type: ignore[arg-type]
    ), "attachment_notes content must reach _build_plan_prompt unmodified via approved_content"

    # All three prompts actually teach the behavior, not just the schema silently accepting the
    # field. The audit leg has no deterministic code to prove wired (unlike _build_plan_prompt
    # above, revised_specification's content is whatever the LLM decides to return) -- same
    # ceiling as the pre-existing retired_ac_ids/retired_us_ids carry-forward instruction, which
    # is likewise only ever verified as prompt text, never as enforced behavior.
    assert "attachment_notes" in SPEC_SYSTEM_PROMPT, "specification_draft.md must name the field it's asked to populate"
    assert "attachment" in SPEC_SYSTEM_PROMPT.lower(), "specification_draft.md must instruct the model to use attachments"
    assert "attachment_notes" in PLAN_SYSTEM_PROMPT, "plan_draft.md must explain the attachment_notes field it receives"
    assert "attachment_notes" in SPEC_AUDIT_SYSTEM_PROMPT, (
        "specification_audit.md must instruct the model to carry attachment_notes forward, "
        "mirroring its existing retired_ac_ids/retired_us_ids carry-forward instruction"
    )

    # spec_ledger.hydrate_ac_to_tests_ticket_mode_context: a coarser "the ledger has entries" check
    # would ALWAYS fire here, even on a project's first-ever ticket (its own spec just populated
    # the ledger moments before) -- must fire only when the ledger holds an AC beyond this ticket's
    # own approved Specification.
    own_spec = {"user_stories": [{"acceptance_criteria": [{"id": "US-0002.1"}]}]}
    solo_ticket_state = {"stages": {"specification": {"approved_content": own_spec}}}
    ledger_only_own = json.dumps(
        {"entries": [{"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"}]}
    )
    assert asyncio.run(
        spec_ledger.hydrate_ac_to_tests_ticket_mode_context(  # type: ignore[arg-type]
            "t", solo_ticket_state, _FakeRepoFileProvider({spec_ledger.LEDGER_PATH: ledger_only_own})
        )
    ) is None, "a first ticket must not be reframed around other tickets' ACs that don't exist"

    ledger_with_other_ticket = json.dumps(
        {
            "entries": [
                {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
                {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"},
            ]
        }
    )
    assert asyncio.run(
        spec_ledger.hydrate_ac_to_tests_ticket_mode_context(  # type: ignore[arg-type]
            "t", solo_ticket_state, _FakeRepoFileProvider({spec_ledger.LEDGER_PATH: ledger_with_other_ticket})
        )
    ) == {"ticket_mode_baseline": True}, "a genuine multi-ticket project must trigger the reframe"

    # Same wiring proof as plan, for _build_ac_to_tests_prompt.
    ac_demo_state = {
        "stages": {
            "specification": {"approved_content": own_spec},
            "plan": {"approved_content": {}},
            "ac-to-tests": {**default_stage_state()},
        },
        "app_scan": {"candidates": [{"path": "."}]},
    }
    assert not any(
        AC_TO_TESTS_TICKET_MODE_SEGMENT in str(m.content)
        for m in _build_ac_to_tests_prompt(ac_demo_state)  # type: ignore[arg-type]
    )
    ac_demo_state["stages"]["ac-to-tests"]["ticket_mode_baseline"] = True
    assert any(
        AC_TO_TESTS_TICKET_MODE_SEGMENT in str(m.content)
        for m in _build_ac_to_tests_prompt(ac_demo_state)  # type: ignore[arg-type]
    )

    # Task 7b: remediation's own ticket-mode reframe hook -- an earlier ticket's own accepted
    # known_gaps reasoning is otherwise completely invisible to a later ticket's draft (remediation
    # has no "immediately-prior draft" block the way plan/spec/ac-to-tests do; every attempt
    # re-reads the live scan from scratch by design). Cache miss vs. cache hit must actually change
    # the hook's answer, not just compile.
    assert asyncio.run(
        hydrate_remediation_ticket_mode_context("t", {}, _FakeRepoFileProvider({}))  # type: ignore[arg-type]
    ) is None, "a project's first-ever ticket has no prior remediation report to carry forward"
    assert asyncio.run(
        hydrate_remediation_ticket_mode_context(  # type: ignore[arg-type]
            "t", {}, _FakeRepoFileProvider({workflow_persistence.REMEDIATION_APPROVED_PATH: "{}"})
        )
    ) == {"ticket_mode_baseline": True}, "an earlier ticket's approved remediation report must trigger the reframe"

    # Wired, not just correct in isolation: _build_remediation_prompt actually appends
    # REMEDIATION_TICKET_MODE_SEGMENT when (and only when) the prompt-only stage copy carries the
    # signal -- same wiring proof as plan/ac-to-tests above.
    remediation_demo_state = {"stages": {"remediation": {**default_stage_state()}}}
    assert not any(
        REMEDIATION_TICKET_MODE_SEGMENT in str(m.content)
        for m in _build_remediation_prompt(remediation_demo_state)  # type: ignore[arg-type]
    )
    remediation_demo_state["stages"]["remediation"]["ticket_mode_baseline"] = True
    assert any(
        REMEDIATION_TICKET_MODE_SEGMENT in str(m.content)
        for m in _build_remediation_prompt(remediation_demo_state)  # type: ignore[arg-type]
    )

    # Task 7b: adversarial-compliance's wireframe-conformance instruction must scope to the
    # wireframes THIS ticket's own approved Plan lists, not every wireframe ever produced for this
    # project. The prior wording ("for EACH wireframe" under the whole, ticket-accumulating
    # .ai-dev-workflow/plan/wireframes/ directory) audited every earlier ticket's already-shipped,
    # already-approved screens too -- and verify_adversarial_compliance blocks on ANY critical/major
    # divergence_finding with no per-ticket filter (agent/src/gates/adversarial_gate.py), so a false
    # positive against an unrelated, already-approved screen could fail a run over work this ticket
    # never touched. Checked in both the live prompt and its dormant agents/ duplicate, which must
    # teach the same thing (consistency sweep). Whitespace-normalized before comparing -- this
    # prompt is hand-wrapped at a fixed column, so a multi-word phrase can legitimately straddle a
    # line break in the raw file.
    def _flat(text: str) -> str:
        return " ".join(text.split())

    assert "for EACH wireframe, find the implemented screen" not in _flat(ADVERSARIAL_COMPLIANCE_SYSTEM_PROMPT)
    assert "Only audit the screens the Plan above actually lists" in _flat(ADVERSARIAL_COMPLIANCE_SYSTEM_PROMPT)
    _dormant_adversarial_prompt = _flat(load_agent_for_stage("adversarial-compliance", "draft")["prompt"])
    assert "for EACH wireframe, find the implemented screen" not in _dormant_adversarial_prompt
    assert "Only audit the screens the Plan above actually lists" in _dormant_adversarial_prompt

    # Every audit step must carry the mandated audit sentence verbatim -- in the live prompt AND
    # its dormant agents/ duplicate where one exists (same consistency sweep as above).
    _audit_mandate = "perform a stringent audit, adversarial probe; find gaps, suggest improvements"
    for _audit_prompt in (
        SPEC_AUDIT_SYSTEM_PROMPT,
        PLAN_AUDIT_SYSTEM_PROMPT,
        AC_TO_TESTS_AUDIT_SYSTEM_PROMPT,
        MINIMAL_CODE_TO_GREEN_AUDIT_SYSTEM_PROMPT,
    ):
        assert _audit_mandate in _flat(_audit_prompt)
    for _audit_stage in ("specification", "plan", "minimal-code-to-green"):
        assert _audit_mandate in _flat(load_agent_for_stage(_audit_stage, "audit")["prompt"])

    # The global writing-rules preamble (prompts/global_system_segment.md) is injected by
    # prompt_loader.load_prompt into every non-_segment prompt -- exactly once (a second copy
    # would mean a segment got double-injected or the suffix rule broke).
    _global_rules = load_prompt("global_system_segment")
    assert SPEC_SYSTEM_PROMPT.count(_global_rules) == 1
    assert AC_TO_TESTS_AUDIT_SYSTEM_PROMPT.count(_global_rules) == 1
    assert MEMORY_SEGMENT.count(_global_rules) == 0

    # minimal-code-to-green's brownfield segment deliberately reuses tech_stack_signals.
    # is_greenfield_repo directly rather than a new StageSpec hook (see
    # MINIMAL_CODE_TO_GREEN_BROWNFIELD_SEGMENT's own comment) -- so the "cache" here is app_scan,
    # already computed once per run for every other stage that needs it. Same hit/miss proof.
    mctg_demo_state = {
        "stages": {
            "specification": {"approved_content": {}},
            "plan": {"approved_content": {}},
            "ac-to-tests": {"approved_content": {}},
            "minimal-code-to-green": {**default_stage_state()},
        },
        "app_scan": {},  # no candidates found == greenfield
    }
    assert not any(
        MINIMAL_CODE_TO_GREEN_BROWNFIELD_SEGMENT in str(m.content)
        for m in _build_minimal_code_to_green_prompt(mctg_demo_state)  # type: ignore[arg-type]
    )
    mctg_demo_state["app_scan"] = {"candidates": [{"path": "."}]}
    assert any(
        MINIMAL_CODE_TO_GREEN_BROWNFIELD_SEGMENT in str(m.content)
        for m in _build_minimal_code_to_green_prompt(mctg_demo_state)  # type: ignore[arg-type]
    )

    # Task 8: ac-to-tests handles a just-retired AC's now-orphaned test with the edit/create/
    # apply_patch tools it already had -- bash was considered and deliberately rejected (see the
    # comment on this stage's own available_tools list: write_scope_gate only reverts file-content
    # drift, and has no visibility into what else a shell can do in the same turn -- network
    # exfiltration of a sandbox secret, `.git/hooks/` persistence, reading outside the repo). This
    # guards against a future re-introduction as much as it proves today's state: neither role gets
    # it, and the audit role must stay exactly as read-only as every other stage's audit regardless.
    ac_to_tests_spec = by_key["ac-to-tests"]
    draft_tools = ac_to_tests_spec.session_options({}, "draft")["available_tools"]  # type: ignore[misc]
    assert "builtin:bash" not in draft_tools, "ac-to-tests draft must not get shell execution -- see Task 8 report"
    audit_tools = ac_to_tests_spec.session_options({}, "audit")["available_tools"]  # type: ignore[misc]
    assert "builtin:bash" not in audit_tools, "ac-to-tests audit must stay read-only like every other stage's audit"
    assert audit_tools == workflow_config.READ_ONLY_AVAILABLE_TOOLS

    # User decision 2026-08-24 added ac-to-tests as a 4th audited stage (it writes the failing
    # TDD-RED tests every downstream stage builds against) -- pin the whole set so a future
    # regression (a StageSpec losing its audit_response_schema, or a fifth one added without
    # updating models.yaml/README alongside it) fails loudly here instead of silently drifting
    # from what those two files document.
    audited_stage_keys = {spec.key for spec in STAGES if spec.audit_response_schema is not None}
    assert audited_stage_keys == {"specification", "plan", "ac-to-tests", "minimal-code-to-green"}, audited_stage_keys

    # The prompt actually teaches the retirement-cleanup behavior, not just leaving the tool wiring
    # unchanged -- same "wired, not just correct in isolation" proof already used above for the
    # wireframe-scoping and ticket-mode segments.
    assert "retired_ac_ids" in AC_TO_TESTS_SYSTEM_PROMPT and "retired_us_ids" in AC_TO_TESTS_SYSTEM_PROMPT

    print("graph self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
