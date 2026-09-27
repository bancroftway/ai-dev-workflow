"""Per-repo DESIGN.md override (dbo.repo_design_settings, migration 0016), falling back to the
deployment-wide default (dbo.org_settings.design_md, migration 0017).

Keyed on (owner, repo), same reasoning as repo_test_config: the design system is a property of the
codebase/product, not the user -- a single deployment can plausibly build more than one product or
serve more than one client, each with its own visual identity, so this is repo-scoped rather than
folded entirely into org_settings the way provider/credential are.

get_effective_design_md() is the one place the repo-override-vs-org-default fallback is decided;
both preflight_nodes.scaffold_finalize_node (the seeder) and gates/design_tokens_gate.py (the
deterministic check) call it rather than each re-deriving the fallback themselves.

Offline self-check: `cd agent && uv run python -m src.repo_design_settings`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import yaml

from . import org_settings, session_store

# The DESIGN.md format spec's frontmatter is a leading `---\n...\n---` YAML block (impeccable's
# reference/document.md:9-40). DOTALL so the block's own newlines match; anchored to the very start
# of the file since a frontmatter block appearing later is just prose that happens to contain `---`.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def extract_color_tokens(design_md: str | None) -> dict[str, str] | None:
    """The frontmatter's `colors:` map (token name -> CSS color string), or None when design_md is
    empty, has no parseable `---` frontmatter block, or that frontmatter has no `colors:` entries.

    None is the deliberate "nothing to check against" signal -- a prose-only brand doc, or a
    seed-mode DESIGN.md placeholder (impeccable's document.md:350-383) that hasn't captured real
    tokens yet -- distinct from an empty dict, which would mean "frontmatter present, explicitly no
    colors". Both design_tokens_gate.py (skip the check) and the settings API's tokens_detected hint
    (warn at save time) treat None the same way: nothing to enforce yet.
    """
    if not design_md:
        return None
    match = _FRONTMATTER_RE.match(design_md)
    if not match:
        return None
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    colors = frontmatter.get("colors")
    if not isinstance(colors, dict) or not colors:
        return None
    return {str(k): str(v) for k, v in colors.items() if isinstance(v, (str, int, float))}


def has_parseable_tokens(design_md: str | None) -> bool:
    return extract_color_tokens(design_md) is not None


@dataclass(frozen=True)
class EffectiveDesignMd:
    content: str | None
    source: Literal["repo", "org_default", "none"]


async def get_design_md(owner: str, repo: str) -> str | None:
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT design_md FROM dbo.repo_design_settings WHERE owner = ? AND repo = ?", owner, repo,
        )
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def set_design_md(owner: str, repo: str, design_md: str | None, updated_by: str | None) -> None:
    """None/blank clears the repo override, falling back to the org default again -- same
    "None or blank clears the setting" convention as org_settings.set_support_repo."""
    value = (design_md or "").strip() or None
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.repo_design_settings AS target
            USING (SELECT ? AS owner, ? AS repo) AS src
              ON target.owner = src.owner AND target.repo = src.repo
            WHEN MATCHED THEN UPDATE SET design_md = ?, updated_by = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (owner, repo, design_md, updated_by) VALUES (?, ?, ?, ?);
            """,
            owner, repo,
            value, updated_by,
            owner, repo, value, updated_by,
        )


async def get_effective_design_md(owner: str, repo: str) -> EffectiveDesignMd:
    repo_value = await get_design_md(owner, repo)
    if repo_value:
        return EffectiveDesignMd(content=repo_value, source="repo")
    settings = await org_settings.get_org_settings()
    org_value = settings.design_md if settings is not None else None
    if org_value:
        return EffectiveDesignMd(content=org_value, source="org_default")
    return EffectiveDesignMd(content=None, source="none")


def _demo() -> None:
    """Offline self-check: no live DB in this environment (same limitation as org_settings.py's own
    self-check), so this exercises the pure frontmatter parser only. The MERGE/SELECT SQL and the
    repo-vs-org fallback are verified against a real DB per this change's own verification plan."""
    with_tokens = """---
name: Test
colors:
  primary: "#b8422e"
  neutral-bg: "#faf7f2"
---

# Design System: Test
"""
    tokens = extract_color_tokens(with_tokens)
    assert tokens == {"primary": "#b8422e", "neutral-bg": "#faf7f2"}, tokens
    assert has_parseable_tokens(with_tokens) is True

    prose_only = "# Brand Guidelines\n\nUse our blue everywhere.\n"
    assert extract_color_tokens(prose_only) is None
    assert has_parseable_tokens(prose_only) is False

    seed_mode = """---
name: Test
description: seed
---

<!-- SEED: established with the user before implementation -->
"""
    assert extract_color_tokens(seed_mode) is None, "frontmatter with no colors: key is None, not {}"

    assert extract_color_tokens(None) is None
    assert extract_color_tokens("") is None

    not_a_map = "---\ncolors: [not, a, map]\n---\n"
    assert extract_color_tokens(not_a_map) is None, "colors: present but not a mapping is None, not a crash"

    invalid_yaml = "---\ncolors: {unclosed\n---\n"
    assert extract_color_tokens(invalid_yaml) is None, "malformed YAML must not raise"

    print("repo_design_settings self-check: ok (frontmatter parser only, no live DB in this environment)")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.repo_design_settings
    from src.repo_design_settings import _demo as _packaged_demo

    _packaged_demo()
