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

import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt

from . import config as workflow_config
from . import git_ops
from . import model_config
from . import workflow_persistence
from .a2ui_tools import build_plan_envelope, build_specification_envelope, present_surface_messages
from .copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from .markdown_render import render_plan_markdown, render_specification_markdown
from .prompt_loader import load_prompt
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .schemas import (
    PlanAuditResponse,
    PlanDraftResponse,
    SpecificationAuditResponse,
    SpecificationDraftResponse,
)

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


class GraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    raw_requirements_text: str
    # Non-text InputContent parts (screenshots/documents) from the latest submission's
    # HumanMessage, if any -- only ever consumed by the specification stage's draft prompt
    # (BR-2: the plan stage's input is the approved Specification, never raw attachments).
    requirements_attachments: list[dict[str, Any]]
    stages: dict[str, StageState]


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
    }


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

PLAN_SYSTEM_PROMPT = load_prompt("plan_draft")


def _build_specification_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    requirements_text = f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"
    attachments = state.get("requirements_attachments") or []
    # Attachments (screenshots/documents) ride alongside the text as a multimodal content list
    # so copilot_chat_model.py's translator can forward them to the model as real attachments,
    # not just note their existence -- a plain string content here would lose them entirely.
    requirements_content: str | list[dict[str, Any]] = (
        [{"type": "text", "text": requirements_text}, *attachments] if attachments else requirements_text
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=SPEC_SYSTEM_PROMPT),
        HumanMessage(content=requirements_content),
    ]
    if stage["draft"] is not None:
        messages.append(
            HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}")
        )
    if stage["used_ids"]:
        messages.append(
            HumanMessage(content=f"Identifiers already used at some point, never reuse: {stage['used_ids']}")
        )
    return messages


def _build_plan_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    messages: list[BaseMessage] = [
        SystemMessage(content=PLAN_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
    ]
    if plan_stage["draft"] is not None:
        messages.append(
            HumanMessage(content=f"Your immediately-prior draft (JSON):\n{plan_stage['draft']}")
        )
    if plan_stage["used_ids"]:
        messages.append(
            HumanMessage(content=f"Identifiers already used at some point, never reuse: {plan_stage['used_ids']}")
        )
    return messages


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


@dataclass(frozen=True)
class StageSpec:
    key: str
    response_schema: type[SpecificationDraftResponse] | type[PlanDraftResponse]
    content_field: str
    surface_tool_name: str
    build_envelope: Callable[[dict[str, Any], list[str] | None], dict[str, Any]]
    build_prompt: Callable[[GraphState], list[BaseMessage]]
    max_cycles: int
    audit_response_schema: type[SpecificationAuditResponse] | type[PlanAuditResponse]
    audit_content_field: str
    build_audit_prompt: Callable[[GraphState], list[BaseMessage]]
    render_markdown: Callable[[dict[str, Any]], str]


STAGES: list[StageSpec] = [
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
    ),
]

_STAGE_BY_KEY = {stage.key: stage for stage in STAGES}
_STAGE_KEYS = [stage.key for stage in STAGES]
_RENDER_MARKDOWN_BY_STAGE = {stage.key: stage.render_markdown for stage in STAGES}


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
            raw_requirements_text=state.get("raw_requirements_text", ""),
            stages=stages,
            render_markdown=_RENDER_MARKDOWN_BY_STAGE,
        )
        await git_ops.commit_ai_dev_workflow(provider, thread_id, commit_message)
    except Exception:
        logger.warning("Failed to persist workflow state for thread_id=%s", thread_id, exc_info=True)


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


async def intake_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    stages = {key: dict(value) for key, value in state.get("stages", {}).items()}

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

    for stage_spec in STAGES:
        stages.setdefault(stage_spec.key, default_stage_state())

    # AC-6.3: a Plan that had already advanced is reset to Not Started; its
    # last content stays visible (AC-8.4) but is no longer current/approved.
    for stage_spec in STAGES[1:]:
        stage = stages[stage_spec.key]
        if stage["status"] in ("ready_for_review", "approved"):
            stage["status"] = "not_started"
            stage["cycle_count"] = 0
            stage["readiness"] = False
            stage["clarifying_questions"] = []

    # The Raw Requirements Text (AC-1.3/AC-6.2) is submitted as an ordinary
    # chat message — the human's "submit" action is agent.addMessage(...) +
    # runAgent() on the frontend — so every run's current, complete text is
    # simply the latest HumanMessage, never a delta. That message's content is a plain string
    # for a text-only submission, or a multimodal InputContent list when screenshots/documents
    # were attached in the Requirements area -- either way, only the text half becomes
    # raw_requirements_text; any attachments are carried separately (see GraphState) since they
    # only matter to this specific run's specification draft, not the persisted text itself.
    raw_requirements_text = state.get("raw_requirements_text", "")
    requirements_attachments: list[dict[str, Any]] = []
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            raw_requirements_text, requirements_attachments = _split_text_and_attachments(
                message.content
            )
            break

    return {
        "stages": stages,
        "raw_requirements_text": raw_requirements_text,
        "requirements_attachments": requirements_attachments,
    }


def make_draft_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def draft_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name(stage_spec.key, "draft"),
            sandbox=sandbox_registry.get(thread_id),
        )

        prompt_messages = stage_spec.build_prompt(state)
        response = await ainvoke_structured(model, prompt_messages, stage_spec.response_schema)

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        content = getattr(response, stage_spec.content_field)
        content_dict = content.model_dump(mode="json") if content is not None else stage["draft"]

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

        stages[stage_spec.key] = stage
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
        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "audit",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name(stage_spec.key, "audit"),
            sandbox=sandbox_registry.get(thread_id),
        )

        prompt_messages = stage_spec.build_audit_prompt(state)
        response = await ainvoke_structured(model, prompt_messages, stage_spec.audit_response_schema)

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        revised_content = getattr(response, stage_spec.audit_content_field)
        content_dict = revised_content.model_dump(mode="json")

        used_ids: set[str] = set(stage["used_ids"])
        _extract_ids(content_dict, used_ids)

        stage["draft"] = content_dict
        stage["used_ids"] = sorted(used_ids)
        stage["audit_findings"] = list(response.audit_findings)
        stages[stage_spec.key] = stage

        envelope = stage_spec.build_envelope(content_dict, stage["audit_findings"])
        extra_messages = present_surface_messages(stage_spec.surface_tool_name, envelope)

        thread_id = config["configurable"]["thread_id"]
        await _persist_if_sandboxed(
            thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} draft revised (audit)"
        )

        return {"stages": stages, "messages": extra_messages}

    return audit_node


def make_route_after_draft(stage_spec: StageSpec) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        stage = state["stages"][stage_spec.key]
        if stage["readiness"]:
            return "gate"
        if stage["cycle_count"] >= stage_spec.max_cycles:
            return "auto_approve"
        return "needs_clarification"

    return route


def make_gate_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def gate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        stage = state["stages"][stage_spec.key]
        # Pauses here (BR-4/Section 6 Gate) until the frontend's useInterrupt
        # resolve(payload) resumes this exact node with that payload.
        interrupt({"stage": stage_spec.key, "draft": stage["draft"]})

        stages = {key: dict(value) for key, value in state["stages"].items()}
        approved = stages[stage_spec.key]
        approved["status"] = "approved"
        approved["approved_content"] = approved["draft"]
        approved["cycle_count"] = 0
        stages[stage_spec.key] = approved

        thread_id = config["configurable"]["thread_id"]
        await _persist_if_sandboxed(thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} approved")

        return {"stages": stages}

    return gate_node


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
        await _persist_if_sandboxed(
            thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} auto-approved (safety cap)"
        )

        return {"stages": stages}

    return auto_approve_node


def build_graph() -> StateGraph:
    builder = StateGraph(GraphState)
    builder.add_node("intake", intake_node)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", f"{STAGES[0].key}_draft")

    for index, stage_spec in enumerate(STAGES):
        draft_name = f"{stage_spec.key}_draft"
        audit_name = f"{stage_spec.key}_audit"
        gate_name = f"{stage_spec.key}_gate"
        auto_approve_name = f"{stage_spec.key}_auto_approve"
        next_draft_name = f"{STAGES[index + 1].key}_draft" if index + 1 < len(STAGES) else END

        builder.add_node(draft_name, make_draft_node(stage_spec))
        builder.add_node(audit_name, make_audit_node(stage_spec))
        builder.add_node(gate_name, make_gate_node(stage_spec))
        builder.add_node(auto_approve_name, make_auto_approve_node(stage_spec))

        builder.add_conditional_edges(
            draft_name,
            make_route_after_draft(stage_spec),
            {"gate": audit_name, "auto_approve": auto_approve_name, "needs_clarification": END},
        )
        builder.add_edge(audit_name, gate_name)
        builder.add_edge(gate_name, next_draft_name)
        builder.add_edge(auto_approve_name, next_draft_name)

    return builder


def compile_graph():
    builder = build_graph()
    checkpointer = InMemorySaver()
    store = InMemoryStore()
    # Async checkpoint durability (Section 3.5): "async" is the documented
    # default for invoke/stream/astream_events, so not overriding it here is
    # sufficient; noted explicitly rather than left as an unremarked default.
    return builder.compile(checkpointer=checkpointer, store=store)


graph = compile_graph()
