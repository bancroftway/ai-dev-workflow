"""FastAPI app exposing the LangGraph workflow over AG-UI (SPECIFICATION.md Section 3.2)."""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import logging
import os

from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# INFO level so per-role model selection (copilot_chat_model.py) and Plan Mode exit events are
# actually visible -- Python's root logger defaults to WARNING, which silently drops them.
logging.basicConfig(level=logging.INFO)

from src.graph import graph

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

add_langgraph_fastapi_endpoint(
    app=app,
    agent=LangGraphAGUIAgent(name="workflow", graph=graph),
    path="/",
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8123")))
