"""FastAPI app exposing the LangGraph workflow over AG-UI (SPECIFICATION.md Section 3.2)."""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import logging
import os

from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# INFO level so per-role model selection (copilot_chat_model.py) and Plan Mode exit events are
# actually visible -- Python's root logger defaults to WARNING, which silently drops them.
logging.basicConfig(level=logging.INFO)

from src.graph import graph
from src.sessions_api import router as sessions_router
from src.telemetry import setup as telemetry_setup

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router)

add_langgraph_fastapi_endpoint(
    app=app,
    agent=LangGraphAGUIAgent(name="workflow", graph=graph),
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
