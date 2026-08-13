"""Loads static template assets (agent/src/templates/**) that get written verbatim into a target
repo -- e.g. AGENTS.md, copilot-instructions.md, Directory.Build.props. Mirrors prompt_loader.py's
convention (editable files, not inline Python strings; cached since they never change at runtime).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=None)
def load_template(relative_path: str) -> str:
    return (_TEMPLATES_DIR / relative_path).read_text(encoding="utf-8")
