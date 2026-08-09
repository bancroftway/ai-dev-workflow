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
from .a2ui_tools import build_plan_envelope, build_specification_envelope, present_surface_messages
from .copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from .schemas import PlanDraftResponse, SpecificationDraftResponse

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


class GraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    raw_requirements_text: str
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


SPEC_SYSTEM_PROMPT = """You are the Specification Agent in a spec-and-plan drafting workflow.
Read the Human Operator's Raw Requirements Text and produce a Specification: a title, a short
summary, a list of User Stories (each with a stable id, a title, a narrative in the form
"As a <role>, I want <capability>, so that <benefit>", and a list of Acceptance Criteria, each
with a stable id scoped to its parent User Story and a description of one specific, testable
condition), a list of stated Assumptions, and a list of items explicitly marked Out of Scope.

If the Raw Requirements Text is insufficient to draft confidently, set readiness to false and
include specific Clarifying Questions instead of (or alongside) a draft. Only set readiness to
true when the draft is complete enough to be worth a human review.

Identity preservation: if you are given your own immediately-prior draft, reuse the exact same
id for any User Story or Acceptance Criterion whose meaning is unchanged (even if wording is
polished), mint a new id (never one already listed as used) for anything genuinely new, and
simply omit anything that no longer applies. Never reuse a previously-used id for something
unrelated."""

PLAN_SYSTEM_PROMPT = """You are the Planning Agent in a spec-and-plan drafting workflow.
Read the given approved Specification's full structured content and produce an Implementation
Plan: an overview, an ordered list of Plan Steps (each with a stable id and a description of one
concrete action, referencing the id(s) of any Acceptance Criteria it fulfills wherever that
traceability is meaningful), and a list of Risk Notes.

If the Specification is insufficient to plan from, set readiness to false and include specific
Clarifying Questions instead of (or alongside) a draft. Only set readiness to true when the draft
is complete enough to be worth a human review.

Identity preservation: if you are given your own immediately-prior draft, reuse the exact same id
for any Plan Step whose meaning is unchanged, mint a new id (never one already listed as used) for
anything genuinely new, and simply omit anything that no longer applies."""


def _build_specification_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    messages: list[BaseMessage] = [
        SystemMessage(content=SPEC_SYSTEM_PROMPT),
        HumanMessage(content=f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"),
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


@dataclass(frozen=True)
class StageSpec:
    key: str
    response_schema: type[SpecificationDraftResponse] | type[PlanDraftResponse]
    content_field: str
    surface_tool_name: str
    build_envelope: Callable[[dict[str, Any]], dict[str, Any]]
    build_prompt: Callable[[GraphState], list[BaseMessage]]
    max_cycles: int


STAGES: list[StageSpec] = [
    StageSpec(
        key="specification",
        response_schema=SpecificationDraftResponse,
        content_field="specification",
        surface_tool_name="present_specification",
        build_envelope=build_specification_envelope,
        build_prompt=_build_specification_prompt,
        max_cycles=workflow_config.SPEC_MAX_CLARIFICATION_CYCLES,
    ),
    StageSpec(
        key="plan",
        response_schema=PlanDraftResponse,
        content_field="plan",
        surface_tool_name="present_plan",
        build_envelope=build_plan_envelope,
        build_prompt=_build_plan_prompt,
        max_cycles=workflow_config.PLAN_MAX_CLARIFICATION_CYCLES,
    ),
]

_STAGE_BY_KEY = {stage.key: stage for stage in STAGES}


def intake_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    stages = {key: dict(value) for key, value in state.get("stages", {}).items()}
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
    # simply the latest HumanMessage, never a delta.
    raw_requirements_text = state.get("raw_requirements_text", "")
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            raw_requirements_text = str(message.content)
            break

    return {"stages": stages, "raw_requirements_text": raw_requirements_text}


def make_draft_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def draft_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        model = get_chat_model_for_thread(
            thread_id,
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=workflow_config.COPILOT_MODEL_NAME,
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

        extra_messages: list[BaseMessage] = []
        if response.readiness:
            stage["status"] = "ready_for_review"
            stage["ever_ready_for_review"] = True
            if content_dict is not None:
                envelope = stage_spec.build_envelope(content_dict)
                extra_messages = present_surface_messages(stage_spec.surface_tool_name, envelope)
        else:
            stage["status"] = "needs_clarification"
            stage["cycle_count"] = stage["cycle_count"] + 1

        stages[stage_spec.key] = stage
        result: dict[str, Any] = {"stages": stages}
        if extra_messages:
            result["messages"] = extra_messages
        return result

    return draft_node


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
        return {"stages": stages}

    return auto_approve_node


def build_graph() -> StateGraph:
    builder = StateGraph(GraphState)
    builder.add_node("intake", intake_node)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", f"{STAGES[0].key}_draft")

    for index, stage_spec in enumerate(STAGES):
        draft_name = f"{stage_spec.key}_draft"
        gate_name = f"{stage_spec.key}_gate"
        auto_approve_name = f"{stage_spec.key}_auto_approve"
        next_draft_name = f"{STAGES[index + 1].key}_draft" if index + 1 < len(STAGES) else END

        builder.add_node(draft_name, make_draft_node(stage_spec))
        builder.add_node(gate_name, make_gate_node(stage_spec))
        builder.add_node(auto_approve_name, make_auto_approve_node(stage_spec))

        builder.add_conditional_edges(
            draft_name,
            make_route_after_draft(stage_spec),
            {"gate": gate_name, "auto_approve": auto_approve_name, "needs_clarification": END},
        )
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
