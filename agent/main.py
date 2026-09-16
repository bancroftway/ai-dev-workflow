"""FastAPI app exposing the LangGraph workflow over AG-UI (SPECIFICATION.md Section 3.2)."""

from __future__ import annotations

from src.env_bootstrap import bootstrap_env

bootstrap_env()  # .env, then AZURE_CONFIG_VAULT_URI -- before any import that reads os.environ

import asyncio
import logging
import os

from contextlib import asynccontextmanager

from ag_ui.core import EventType, RunStartedEvent, StateSnapshotEvent
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# INFO level so per-role model selection (copilot_chat_model.py) and Plan Mode exit events are
# actually visible -- Python's root logger defaults to WARNING, which silently drops them.
logging.basicConfig(level=logging.INFO)

from src import checkpoint, run_activity
from src.graph import graph
from src.sessions_api import catalog_router as tech_stack_catalog_router
from src.sessions_api import config_router as vault_config_router
from src.sessions_api import github_link_router
from src.sessions_api import org_settings_router
from src.sessions_api import projects_router
from src.sessions_api import repo_auth_settings_router
from src.sessions_api import repo_test_config_router
from src.sessions_api import repo_test_users_router
from src.sessions_api import router as sessions_router
from src.telemetry import setup as telemetry_setup


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Durable checkpointing (src/checkpoint.py): swap the compiled graph's boot-time
    # InMemorySaver for the AsyncSqliteSaver BEFORE the first request -- open gates and
    # in-flight thread state then survive agent restarts. Fail-soft inside; a failed attach
    # boots on the in-memory saver with a loud warning.
    await checkpoint.attach_sqlite_checkpointer(graph)
    try:
        yield
    finally:
        # Background graph tasks (_ReattachStateAgent._drive_graph) are detached from any HTTP
        # request, so uvicorn's own graceful-shutdown drain (in-flight requests only) never waits
        # for them -- cancel them explicitly before the checkpointer connection underneath them
        # closes, or a task can still be mid-write to an already-closed SQLite connection.
        await run_activity.cancel_all_tasks()
        await checkpoint.close_checkpointer()


app = FastAPI(lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router)
app.include_router(vault_config_router)
app.include_router(org_settings_router)
app.include_router(tech_stack_catalog_router)
app.include_router(projects_router)
app.include_router(repo_auth_settings_router)
app.include_router(github_link_router)
app.include_router(repo_test_config_router)
app.include_router(repo_test_users_router)

class _ReattachStateAgent(LangGraphAGUIAgent):
    """ag_ui_langgraph's pending-interrupt short-circuit (agent.py prepare_stream) re-emits the
    stored gate on a reattach run but sends NO state snapshot -- a reloaded client then renders
    the gate card over blank, disabled tabs ("Detecting your tech stack…" under a Specification
    review card, observed live 2026-08-31). Inject the checkpoint's state right after
    RUN_STARTED so a reattach hydrates the whole page, exactly as a normal run would."""

    async def prepare_stream(self, input, agent_state, config):  # noqa: A002 - library signature
        prepared = await super().prepare_stream(input, agent_state, config)
        try:
            events = prepared.get("events_to_dispatch") if isinstance(prepared, dict) else None
            if events and prepared.get("stream") is None and agent_state is not None:
                events.insert(
                    1,
                    StateSnapshotEvent(
                        type=EventType.STATE_SNAPSHOT,
                        snapshot=self.get_state_snapshot(agent_state.values),
                    ),
                )
        except Exception:  # noqa: BLE001 - never let hydration sugar break the gate re-emit
            logging.getLogger(__name__).warning("reattach state-snapshot injection failed", exc_info=True)
        return prepared

    async def _reattach_snapshot_if_stale(self, thread_id: str) -> "StateSnapshotEvent | None":
        """The mid-run reattach gap (AppShell.tsx's own `isReattaching` comment): reattaching to a
        thread that is mid-pipeline but NOT paused at a gate -- e.g. Resume clicked while
        specification_draft has already committed its draft and specification_audit hasn't
        finished yet -- gets no snapshot from prepare_stream above (that override only fires for
        the pending-interrupt short-circuit, `stream is None`). This client then renders every tab
        as if nothing has ever run, until whatever node is next happens to touch state. Read the
        checkpoint directly (same call the base class makes internally) and build one iff there is
        real accumulated progress (`stages` non-empty) and no pending interrupt (`tasks` empty --
        that case is already covered above, a second snapshot here would just be redundant)."""
        try:
            config = {"configurable": {"thread_id": thread_id}}
            agent_state = await self.graph.aget_state(config)
            if agent_state.tasks or not (agent_state.values or {}).get("stages"):
                return None
            return StateSnapshotEvent(
                type=EventType.STATE_SNAPSHOT,
                snapshot=self.get_state_snapshot(agent_state.values),
            )
        except Exception:  # noqa: BLE001 - hydration sugar, never worth failing the run over
            logging.getLogger(__name__).warning("mid-run reattach snapshot failed for thread_id=%r", thread_id, exc_info=True)
            return None

    async def _drive_graph(self, thread_id: str, input) -> None:  # noqa: A002 - library signature
        """The actual graph execution -- runs as a detached asyncio.Task (see run() below), not
        inside any HTTP request's coroutine tree, so a browser disconnect can no longer cancel it
        (the SSE disconnect fix; run_activity.py's module docstring has the full picture).

        incr()/decr() now bracket THIS task's lifetime instead of a request's: run_active reflects
        whether the graph is actually running, regardless of whether any tab is attached to watch
        it -- the same true-state guarantee run_activity.heartbeat() already gives run_headless.py,
        extended to the interactive path.

        Exactly one call per thread_id ever reaches this method (run() below only creates a task
        when none is already registered), so there's no concurrent-astream_events risk to guard
        against here -- that's now structural (the task registry), not lock-based."""
        run_activity.incr(thread_id)
        try:
            is_first_event = True
            async for event in super().run(input):
                run_activity.publish(thread_id, event)
                # Inserted right after the first event (RUN_STARTED, same position
                # prepare_stream's own injection uses above) rather than before the loop -- AG-UI
                # clients assume RUN_STARTED always leads.
                if is_first_event:
                    is_first_event = False
                    snapshot = await self._reattach_snapshot_if_stale(thread_id)
                    if snapshot is not None:
                        run_activity.publish(thread_id, snapshot)
        except asyncio.CancelledError:
            raise  # real cancellation: run_activity.cancel_run (Stop container) or app shutdown
        except Exception:
            # Mirrors today's behavior for an exception escaping this far: node-level
            # telemetry.traced_node already owns failure recording for graph nodes, so this is
            # already-unexpected territory, not a new silent-failure mode -- just log and let the
            # stream end, same as an uncaught exception mid-request would have done before.
            logging.getLogger(__name__).exception(
                "background graph run crashed thread_id=%r", thread_id
            )
        finally:
            run_activity.decr(thread_id)
            run_activity.pop_task(thread_id)
            run_activity.publish(thread_id, run_activity.DONE)

    async def run(self, input):  # noqa: A002 - library signature
        """Per-request generator is now just a SUBSCRIBER to whichever task is driving this
        thread's graph (_drive_graph above) -- endpoint.py clones the registered agent per request
        (`agent.clone()`), so this runs once per HTTP call, same as before, but no longer drives
        the graph itself. A client disconnect now only cancels this generator's `queue.get()`
        await (the `finally` unsubscribes), never the background task.

        Subscribing BEFORE creating a new task (not after) matters: a task starts publishing the
        instant it's created, and a subscriber added afterwards would miss its first event.

        thread_id-less input (the library's type allows it, though this pipeline always sets one)
        has nothing to key a task/subscriber registry on -- fall back to driving it inline, same
        as every call used to work before this change."""
        thread_id = input.thread_id
        if not thread_id:
            async for event in super().run(input):
                yield event
            return

        queue = run_activity.subscribe(thread_id)
        existing = run_activity.get_task(thread_id)
        if existing is None or existing.done():
            task = asyncio.create_task(self._drive_graph(thread_id, input))
            run_activity.register_task(thread_id, task)
        else:
            # Reattach: a task is already driving this thread -- never call super().run() again
            # here (would double-invoke astream_events concurrently on the same graph thread).
            # Synthesize the RUN_STARTED + snapshot _drive_graph's own first iteration already
            # produced for whoever attached first. run_id reuses thread_id (no consumer correlates
            # by run_id across reconnects today); thread the real active_run id through if that
            # ever changes.
            queue.put_nowait(RunStartedEvent(thread_id=thread_id, run_id=thread_id))
            snapshot = await self._reattach_snapshot_if_stale(thread_id)
            if snapshot is not None:
                queue.put_nowait(snapshot)
        try:
            while True:
                item = await queue.get()
                if item is run_activity.DONE:
                    return
                yield item
        finally:
            run_activity.unsubscribe(thread_id, queue)


# LangGraph's own default recursion_limit (25 super-steps) is far below what this pipeline's own
# documented retry design needs -- ac-to-tests alone allows up to 6 verify cycles (draft+audit+
# verify per cycle), minimal-code-to-green up to 12, and EVERY resume replays ~8-9 super-steps
# from intake through whichever stage is already approved (should_skip_draft short-circuits, but
# each still counts as a step) before reaching the stage actually retrying. Observed live
# 2026-09-01: langgraph.errors.GraphRecursionError killed an otherwise-healthy run mid-stream, no
# run_failure recorded (the error propagated out of the AG-UI stream unhandled). The real
# runaway-loop protection is each stage's own max_verify_cycles/max_cycles (workflow_config.py);
# this is just a generous ceiling against a genuine infinite loop, not a cost control.
_RECURSION_LIMIT = 1000

add_langgraph_fastapi_endpoint(
    app=app,
    agent=_ReattachStateAgent(name="workflow", graph=graph, config={"recursion_limit": _RECURSION_LIMIT}),
    path="/",
)

# After route registration is fine -- Starlette builds its middleware stack lazily at the first
# request, and instrument_app only needs to run before serving starts.
telemetry_setup(app)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    # Exceptions raised mid-SSE-stream on "/" never reach this (response already started) --
    # those are logged and span-recorded by telemetry.traced_node. This covers everything else.
    logging.getLogger("app").exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal error"})

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8123")))
