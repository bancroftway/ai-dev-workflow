"""Discover build/test/coverage commands via read-only audit model inspection."""

from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel

from .custom_agent_loader import load_agent
from .copilot_chat_model import get_chat_model_for_thread
from .prompt_loader import render_prompt
from langchain_core.messages import HumanMessage


class StackCommandRecommendation(BaseModel):
    """Model's discovered command and root directory for a build/test/coverage task."""

    root: str
    """Repo-relative path to cd into. Use "." for repo root."""

    build_command: str | None = None
    test_command: str | None = None
    coverage_command: str | None = None
    coverage_artifact: str | None = None
    """Repo-relative path to the coverage report file."""

    coverage_artifact_format: Literal["cobertura", "istanbul-json-summary"] | None = None
    notes: str = ""


async def discover_stack_commands(
    thread_id: str,
    *,
    owning_stage: str,
    task: str,
    model_name: str | None = None,
) -> StackCommandRecommendation:
    """Discover the correct build/test/coverage command for the repo.

    Args:
        thread_id: Workflow thread ID.
        owning_stage: The stage name this discovery is for (for logging).
        task: What to discover ("the build command", "the test command", "the coverage command").
        model_name: Optional model override. If set, passed to create_session.

    Returns:
        StackCommandRecommendation with discovered root, command, and artifact path.
    """
    # Load the custom agent
    agent_path = os.path.join(os.path.dirname(__file__), "agents", "stack-discovery-audit.md")
    agent_config = load_agent(agent_path)

    # Create a read-only session with the agent
    model = get_chat_model_for_thread(
        thread_id,
        f"{owning_stage}-discover",
        "audit",
        model_name=model_name or agent_config.get("model"),
        custom_agents=[agent_config],
        agent=agent_config["name"],
    )

    # Render the data-only template
    data_template = render_prompt("stack_discovery_data", task=task)

    # Send the message; the agent's system prompt is already in the CustomAgentConfig
    response = await model.ainvoke([HumanMessage(content=data_template)])

    # Extract JSON from response
    response_text = response.content if isinstance(response.content, str) else str(response.content)
    try:
        # Try to extract JSON if it's wrapped
        if "```json" in response_text:
            json_start = response_text.index("```json") + 7
            json_end = response_text.index("```", json_start)
            json_text = response_text[json_start:json_end].strip()
        else:
            json_text = response_text.strip()
        result_dict = json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Failed to parse model response as JSON: {response_text}") from e

    return StackCommandRecommendation(**result_dict)
