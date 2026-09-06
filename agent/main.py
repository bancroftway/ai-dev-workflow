"""FastAPI app exposing the LangGraph workflow over AG-UI (SPECIFICATION.md Section 3.2)."""

from __future__ import annotations

from src.env_bootstrap import bootstrap_env

bootstrap_env()  # .env, then AZURE_CONFIG_VAULT_URI -- before any import that reads os.environ

import logging
import os

from contextlib import asynccontextmanager, nullcontext

from ag_ui.core import EventType, StateSnapshotEvent
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

    async def run(self, input):  # noqa: A002 - library signature
        """Marks this session's run_active signal (run_activity.py) for the lifetime of the
        stream -- endpoint.py clones the registered agent per request (`agent.clone()`), so this
        override runs once per HTTP call, not once per process; the `finally` is what makes it
        cover normal completion, a mid-stream exception (see _RECURSION_LIMIT below), AND a client
        disconnect (StreamingResponse cancels the streaming task on disconnect, which propagates
        into this generator's current await point same as any other exception).

        The actual graph execution (super().run(), which internally calls prepare_stream() too) is
        serialized per thread_id via run_activity.get_lock(): multiple browser tabs on the same
        session each independently trigger a reattach/resume call, and without this a second tab's
        call used to drive graph.astream concurrently with the first's -- a real risk of doubled
        side effects (duplicate git ops, duplicate PR opens) and checkpoint-write races. incr/decr
        stay OUTSIDE the lock so the run_active display flag still flips true immediately for
        every attaching tab, not only whichever currently holds the lock. Safe to have a tab wait
        here a long time: this exact shape (a turn running silent for 5-10+ minutes) is why
        route.ts already disabled the proxy's idle timeouts, and a waiting tab still sees live
        progress via the separate run-events poll in the meantime."""
        thread_id = input.thread_id
        if thread_id:
            run_activity.incr(thread_id)
        try:
            lock = run_activity.get_lock(thread_id) if thread_id else nullcontext()
            async with lock:
                is_first_event = True
                async for event in super().run(input):
                    yield event
                    # Inserted right after the first event (RUN_STARTED, same position
                    # prepare_stream's own injection uses above) rather than before the loop --
                    # AG-UI clients assume RUN_STARTED always leads.
                    if is_first_event:
                        is_first_event = False
                        if thread_id:
                            snapshot = await self._reattach_snapshot_if_stale(thread_id)
                            if snapshot is not None:
                                yield snapshot
        finally:
            if thread_id:
                run_activity.decr(thread_id)


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
