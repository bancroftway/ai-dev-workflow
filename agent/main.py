"""FastAPI app exposing the LangGraph workflow over AG-UI (SPECIFICATION.md Section 3.2)."""

from __future__ import annotations

from src.env_bootstrap import bootstrap_env

bootstrap_env()  # .env, then AZURE_CONFIG_VAULT_URI -- before any import that reads os.environ

import logging
import os

from contextlib import asynccontextmanager

from ag_ui.core import EventType, StateSnapshotEvent
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# INFO level so per-role model selection (copilot_chat_model.py) and Plan Mode exit events are
# actually visible -- Python's root logger defaults to WARNING, which silently drops them.
logging.basicConfig(level=logging.INFO)

from src import checkpoint
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


add_langgraph_fastapi_endpoint(
    app=app,
    agent=_ReattachStateAgent(name="workflow", graph=graph),
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
