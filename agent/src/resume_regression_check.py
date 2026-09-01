"""Regression check for intake_node's message-id-based resume dedupe (graph.py).

Not a module the pipeline imports -- a standalone, assert-based script proving the one thing a
plain unit test on intake_node in isolation cannot: that LangGraph's own checkpointer round-trips
a HumanMessage's id unchanged across two invocations of the SAME thread, which is the invariant
the resume-vs-fresh-submission dedupe in intake_node depends on. Runs a REAL compiled graph (a
minimal intake_node -> END, not the full production pipeline -- everything else in that graph
needs an LLM/sandbox, and none of that is what this is testing) against a REAL InMemorySaver.

Run: `cd agent && uv run python -m src.resume_regression_check`.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from . import chat_model
from .graph import GraphState, default_stage_state, intake_node
from .sandbox import registry as sandbox_registry


async def _demo() -> None:
    # intake_node now resolves GraphState.provider via chat_model.get_provider() (Part 4 Task 4),
    # which reads org_settings.get_org_settings() -- a real SQL query against a table that only
    # exists in a real deployment. Stub it the same way chat_model.py's own _demo() stubs
    # org_settings/org_credential_vault for its offline self-check, restoring it afterward, so this
    # script keeps proving what it's actually for (the checkpoint round-trip) without needing a
    # live DB. The fixed value doesn't matter to any assertion below -- only that it resolves at all.
    original_get_provider = chat_model.get_provider

    async def _fake_get_provider() -> str:
        return "copilot"

    chat_model.get_provider = _fake_get_provider
    try:
        builder = StateGraph(GraphState)
        builder.add_node("intake", intake_node)
        builder.set_entry_point("intake")
        builder.add_edge("intake", END)
        test_graph = builder.compile(checkpointer=InMemorySaver())

        thread_id = "resume-regression-thread"
        config = {"configurable": {"thread_id": thread_id}}
        approved_spec = {**default_stage_state(), "status": "approved", "approved_content": {"x": 1}}

        # Run 1: a genuine new submission. `stages` seeds an already-approved specification, as if an
        # earlier node (not modeled in this minimal graph) had approved it before this run later
        # failed downstream -- a normal (non-resume) run must still reset it.
        out1 = await test_graph.ainvoke(
            {"messages": [HumanMessage(content="Build a login page")], "stages": {"specification": approved_spec}},
            config,
        )
        run1_message_id = out1["messages"][-1].id
        assert run1_message_id is not None, "add_messages must assign the HumanMessage an id"
        assert out1["consumed_message_id"] == run1_message_id, out1["consumed_message_id"]
        assert out1["stages"]["specification"]["status"] == "not_started", out1["stages"]["specification"]

        # Simulate a later node re-approving the stage before the run eventually failed and ended
        # (this minimal graph has no such node, so write it directly into the checkpoint).
        test_graph.update_state(config, {"stages": {"specification": approved_spec}})

        # Run 2: AppShell's Resume button -- a blank runAgent() call (messages: [], no new
        # HumanMessage) plus the registry's one-shot resume meta flag, exactly as sessions_api.py /
        # graph.py wire it.
        sandbox_registry.set_meta(thread_id, resume=True)
        out2 = await test_graph.ainvoke({"messages": []}, config)

        # (a) Checkpoint round-trip: run 2 still sees run 1's message, with the SAME id -- proving the
        # id-dedupe has a real, stable value to compare against (not a fresh one per invocation).
        assert out2["messages"][-1].id == run1_message_id, "checkpoint did not preserve the message id"
        assert out2["consumed_message_id"] == run1_message_id, out2["consumed_message_id"]

        # (b) Resume applied: the approved stage was NOT reset -- the bug this whole fix is about.
        assert out2["stages"]["specification"]["status"] == "approved", out2["stages"]["specification"]

        # Control: a genuinely NEW submission (fresh id, e.g. a clarification answer) on the SAME
        # thread must still reset, even with the meta flag set again -- a real resubmission is never
        # misread as a resume.
        sandbox_registry.set_meta(thread_id, resume=True)
        test_graph.update_state(config, {"stages": {"specification": approved_spec}})
        out3 = await test_graph.ainvoke({"messages": [HumanMessage(content="Actually, add 2FA too")]}, config)
        assert out3["consumed_message_id"] != run1_message_id
        assert out3["stages"]["specification"]["status"] == "not_started", out3["stages"]["specification"]

        print("resume_regression_check: ok")
    finally:
        chat_model.get_provider = original_get_provider


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.resume_regression_check`
    asyncio.run(_demo())
