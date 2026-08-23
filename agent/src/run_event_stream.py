"""Live counterpart to run_event_store.append_event -- the same RunEvent, additionally dispatched
as a LangGraph custom event so the already-mounted ag_ui_langgraph bridge relays it to the browser
while the run is still in progress, instead of only being visible after the fact via list_events.
Separate module from run_event_store.py on purpose: this has nothing to do with the DB, so it gets
its own DB-independent self-check (no DB, no LLM, no sandbox needed -- see _demo()).

Real mechanism, verified against the actually-installed langgraph==1.2.10 / ag-ui-langgraph==0.0.42
/ copilotkit==0.1.94 (not assumed, not training-data recall -- see task-2-report.md for the
empirical probes this module's design is based on):

- `langgraph.config.get_stream_writer()` -- the plan's Ruling 6 named this as one candidate -- does
  NOT surface through ag_ui_langgraph's actual calling convention. `ag_ui_langgraph.agent.
  LangGraphAgent.get_stream_kwargs` calls `graph.astream_events(input, subgraphs=..., version="v2")`
  with no `stream_mode` kwarg at all. LangGraph's own Pregel only wires get_stream_writer()'s
  payload through to a real destination when "custom" is explicitly among the requested
  stream_modes (`pregel/main.py`'s `stream_writer` closure); otherwise it's a silent no-op. Empirically
  confirmed: 0 `on_custom_event` entries came out of `astream_events` for a get_stream_writer() call
  under this exact calling convention.
- `langchain_core.callbacks.manager.adispatch_custom_event` -- Ruling 6's other named candidate --
  DOES surface, unconditionally, because LangGraph always runs each node inside a chain run whose
  callback manager already has a `parent_run_id`, independent of `stream_mode`. Empirically
  confirmed both at the raw `astream_events` layer and through the real `copilotkit.
  LangGraphAGUIAgent` (the exact class `agent/main.py` mounts) -- see task-2-report.md. That's the
  one this module uses.

Fails soft for the same reason run_event_store.append_event does (see its own docstring): this is
non-critical, best-effort instrumentation riding alongside a real node body; an unhandled exception
here would abort the whole graph invocation (telemetry.traced_node) over what's meant to be purely
observational. Concretely confirmed exception case: adispatch_custom_event raises RuntimeError when
no parent run id is available -- must never happen in practice (LangGraph always provides one to a
node's own execution), but the swallow costs nothing and matches this codebase's own established
convention for exactly this risk class.
"""

from __future__ import annotations

import dataclasses
import logging

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.runnables import RunnableConfig

from .run_events import RunEvent

logger = logging.getLogger(__name__)

# Deliberately outside ag_ui_langgraph/copilotkit's own reserved custom-event names
# (manually_emit_message, manually_emit_tool_call, manually_emit_intermediate_state,
# copilotkit_exit -- see ag_ui_langgraph.types.CustomEventNames / copilotkit.langgraph_agui_agent.
# CustomEventNames) so it always falls through to a plain passthrough CUSTOM event, never one of
# their special-cased behaviors.
CUSTOM_EVENT_NAME = "run_event"


def _json_safe_payload(event: RunEvent) -> dict:
    """RunEvent -> plain dict, guaranteed JSON-safe. `dataclasses.asdict` leaves non-dataclass
    field values as-is (Enum's own __deepcopy__ returns the same member, not its string value), so
    `type` and `ts` are coerced explicitly -- mirroring run_event_store.append_event's own explicit
    `event.type.value` rather than relying on RunEventType's StrEnum-is-a-str behavior to survive
    whatever JSON encoder eventually serializes the AG-UI event (Pydantic's model_dump_json, per
    ag_ui.encoder.EventEncoder)."""
    data = dataclasses.asdict(event)
    data["type"] = event.type.value
    data["ts"] = event.ts.isoformat() if event.ts is not None else None
    return data


async def emit_live(event: RunEvent, config: RunnableConfig | None = None) -> None:
    """Dispatch `event` as a LangGraph custom event on the graph's own execution -- relayed by
    ag_ui_langgraph as an AG-UI CUSTOM event (name="run_event", value=the RunEvent's fields).  Call
    this alongside (never instead of) run_event_store.append_event: additive second destination,
    same relationship that function has to repo_files.append_ledger_entry. `config` mirrors
    adispatch_custom_event's own optional param -- passed explicitly here since every real call
    site already has it in scope, which sidesteps that function's own documented Python-3.10
    contextvar-propagation caveat (moot on this project's 3.12 venv, but free to pass regardless).
    """
    try:
        await adispatch_custom_event(CUSTOM_EVENT_NAME, _json_safe_payload(event), config=config)
    except Exception:  # noqa: BLE001 -- best-effort instrumentation; never abort the node/run over this
        logger.warning(
            "emit_live failed for run_id=%s stage=%s node=%s -- continuing without it",
            event.run_id, event.stage, event.node, exc_info=True,
        )


async def _demo() -> None:
    """Self-check, no DB/LLM/sandbox: a real 1-node LangGraph graph driven through the real
    copilotkit.LangGraphAGUIAgent (the exact class agent/main.py mounts via
    add_langgraph_fastapi_endpoint), asserting emit_live's dispatch survives the full real AG-UI
    bridge as an EventType.CUSTOM event with the expected shape -- then a fail-soft check mirroring
    run_event_store._demo()'s own broken-dependency check. `python -m src.run_event_stream`.
    """
    import uuid
    from datetime import datetime, timezone

    from ag_ui.core import EventType, RunAgentInput
    from copilotkit import LangGraphAGUIAgent
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    from .run_events import RunEventType

    class _State(TypedDict):
        foo: int

    probe_event = RunEvent(
        run_id="demo-run",
        session_id=str(uuid.uuid4()),
        type=RunEventType.NODE_FINISHED,
        stage="specification",
        node="draft",
        summary="draft ready for review",
        payload={"readiness": True},
    )
    assert probe_event.seq is None and probe_event.ts is None  # pre-append shape

    # Simulates the real graph.py call-site contract: run_event_store.append_event returns a copy
    # with DB-assigned seq/ts filled in, and every call site must rebind to that copy
    # (`run_event = await run_event_store.append_event(run_event)`) before calling emit_live --
    # fix round 1's own finding was exactly a call site that emitted the pre-rebind event instead,
    # silently shipping seq=None/ts=None live while the DB copy had real values. No DB here (this
    # module stays DB-independent) -- dataclasses.replace stands in for append_event's own `return
    # replace(event, seq=seq, ts=ts)` (run_event_store.py) without a real connection.
    appended_event = dataclasses.replace(probe_event, seq=42, ts=datetime.now(timezone.utc))

    async def _node(state: _State, config: RunnableConfig) -> dict:
        await emit_live(appended_event, config)
        return {"foo": state["foo"] + 1}

    graph = (
        StateGraph(_State)
        .add_node("n", _node)
        .add_edge(START, "n")
        .add_edge("n", END)
        .compile(checkpointer=InMemorySaver())
    )
    agent = LangGraphAGUIAgent(name="demo", graph=graph)
    run_input = RunAgentInput(
        thread_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
        state={"foo": 1}, messages=[], tools=[], context=[], forwarded_props={},
    )

    custom_events = [ev async for ev in agent.run(run_input) if ev.type == EventType.CUSTOM]
    assert len(custom_events) == 1, custom_events
    ev = custom_events[0]
    assert ev.name == CUSTOM_EVENT_NAME, ev.name
    # seq/ts MUST be the real, non-None values from the (simulated) post-append copy -- this is
    # the exact assertion that would have caught fix round 1's bug (a call site emitting the
    # pre-rebind event, which would land here as seq=None/ts=None instead).
    assert ev.value == {
        "run_id": "demo-run", "session_id": probe_event.session_id, "type": "node_finished",
        "stage": "specification", "node": "draft", "summary": "draft ready for review",
        "payload": {"readiness": True}, "token_usage": None,
        "seq": 42, "ts": appended_event.ts.isoformat(),
    }, ev.value

    # Fail-soft contract, mirroring run_event_store._demo()'s own broken-dependency check: called
    # with no active run/config at all (bare module-level call, no graph/node context) there is no
    # parent run id, which is the real, confirmed RuntimeError case (see this module's docstring)
    # -- must not raise.
    await emit_live(appended_event, config=None)

    print("run_event_stream self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.run_event_stream
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo())
