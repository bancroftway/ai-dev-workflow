"""Load a custom agent from YAML frontmatter + markdown body."""

from __future__ import annotations

from typing import TypedDict

import yaml


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
