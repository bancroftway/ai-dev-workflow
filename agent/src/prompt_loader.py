"""Loads agent system prompts from editable markdown files (SPECIFICATION.md agents are
described in agent/src/prompts/*.md, not inline Python strings, so they can be edited without
touching code).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
