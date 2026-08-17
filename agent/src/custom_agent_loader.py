"""Load a custom agent from YAML frontmatter + markdown body."""

from __future__ import annotations

import logging
import os
from typing import Any, TypedDict

import yaml

logger = logging.getLogger(__name__)


class CustomAgentConfig(TypedDict):
    """Custom agent configuration from an .md file's YAML frontmatter + body."""
    name: str
    description: str
    tools: list[str]
    prompt: str
    model: str
    # ... additional optional fields handled by SDK


def load_agent(path: str) -> CustomAgentConfig:
    """Parse a .md file with YAML frontmatter into a CustomAgentConfig dict.

    Expected format:
        ---
        name: "example-agent"
        description: "..."
        tools:
          - builtin:view
          - builtin:grep
        model: "claude-haiku-4-5-20251001"
        ---
        # Prompt body in markdown
    """
    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Split on first --- (start) and second --- (end of frontmatter)
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"No valid frontmatter in {path}")

    _, frontmatter_text, body = parts
    frontmatter = yaml.safe_load(frontmatter_text)

    if not isinstance(frontmatter, dict):
        raise ValueError(f"Frontmatter in {path} is not a dict")

    # Extract body (strip leading/trailing whitespace)
    prompt = body.strip()

    # Ensure required fields
    required = {"name", "description", "tools", "model"}
    missing = required - set(frontmatter.keys())
    if missing:
        raise ValueError(f"Missing required fields in {path}: {missing}")

    # Build config dict
    config = {**frontmatter, "prompt": prompt}
    return config


def load_agent_for_stage(stage: str, role: str) -> dict[str, Any]:
    """Load a custom agent from agent/src/agents/<stage>-<role>.md.

    Returns a CustomAgentConfig dict with keys: name, description, tools, model, prompt.
    Falls back gracefully if the agent file doesn't exist (logs and returns empty dict) -- used
    by both graph.py's stage nodes and rebuild.py's fix nodes, which used to each carry their own
    verbatim copy of this wrapper.
    """
    agent_path = os.path.join(
        os.path.dirname(__file__),
        "agents",
        f"{stage.replace('_', '-')}-{role}.md",
    )
    try:
        return load_agent(agent_path)
    except (FileNotFoundError, ValueError) as e:
        logger.warning(f"Could not load agent {stage}-{role}: {e}")
        return {}
