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
    """Load a prompt file, with the global writing rules (global_system_segment.md) prepended.

    `*_segment` files are exempt: they get concatenated into an already-injected prompt, so
    injecting them too would duplicate the preamble -- and the global file's own `_segment`
    suffix is what stops it from injecting into itself."""
    text = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    if name.endswith("_segment"):
        return text
    return f"{load_prompt('global_system_segment')}\n\n{text}"


@lru_cache(maxsize=None)
def load_prompt_pair(name: str) -> tuple[str, str]:
    """(system, human_template) split on the file's first `---` line. The human half uses
    `<<placeholder>>` tokens rendered with render_prompt() -- not str.format, so literal braces
    in prompt text (JSON examples, suppression markers) never need escaping."""
    text = load_prompt(name)
    system, sep, human = text.partition("\n---\n")
    if not sep:
        raise ValueError(f"prompt {name!r} has no `---` system/human separator")
    return system.strip(), human.strip()


def render_prompt(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace(f"<<{key}>>", value)
    return template
