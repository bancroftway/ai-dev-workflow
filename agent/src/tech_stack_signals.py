"""Shared UI-framework detection signal.

Used by graph.py (P3's wireframe requirement, P4's Playwright MCP, session_options gates) AND
e2e_nodes.py (e2e_gate_check_node). Pulled out into its own leaf module -- rather than duplicated,
or imported one way -- because graph.py imports e2e_nodes to wire it in, so e2e_nodes importing
graph.py back would be circular; a shared leaf with no dependency on either avoids the cycle.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from . import repo_files

logger = logging.getLogger(__name__)

UI_FRAMEWORK_MARKERS = ("react", "vue", "angular", "blazor", "svelte", "next", "nuxt", "flutter", "swiftui", "jetpack compose")


def tech_stack_has_ui_framework(state: dict[str, Any]) -> bool:
    """Shared signal for P3's wireframe requirement, P4's Playwright MCP, and e2e's gate check --
    all three key off whether this repo has a UI framework at all, using tech-stack's own
    TechStack.frameworks report (a real, deliberate simplification from "only for UI-relevant
    content specifically" to "only for UI-framework repos at all")."""
    tech_stack = (state.get("stages") or {}).get("tech-stack", {}).get("approved_content") or {}
    frameworks = [str(f).lower() for f in (tech_stack.get("frameworks") or [])]
    return any(marker in fw for fw in frameworks for marker in UI_FRAMEWORK_MARKERS)


def dotnet_root_prefix(tech_stack: dict[str, Any]) -> str:
    """Shell prefix that `cd`s into TechStack.dotnet_solution_root before a bare `dotnet` command,
    for every quality/test/finding-cluster/gate node that shells out to dotnet build/format/test.
    Brownfield repos happened to have their .sln/.csproj at the repo root; a greenfield monorepo
    (solution under e.g. "apps/") doesn't, so a bare `dotnet build` at repo root dies with MSB1003
    in ~2s (observed live) before any real work happens. Empty/missing root -- no confidently
    determined subdirectory, including the common repo-root case -- returns "" so every existing
    call site is byte-identical to before this existed.

    Validation reuses repo_files.validate_repo_relative_path's own distrust: dotnet_solution_root
    is model-reported (TechStack's own field) and must not be trusted to shell out unchecked, same
    as any other repo-relative path from that source.
    """
    root = tech_stack.get("dotnet_solution_root") or ""
    if not root:
        return ""
    try:
        repo_files.validate_repo_relative_path(root)
    except ValueError:
        logger.warning("ignoring unsafe dotnet_solution_root %r", root)
        return ""
    return f"cd {shlex.quote(root.strip('/'))} && "


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.tech_stack_signals`."""
    assert dotnet_root_prefix({"dotnet_solution_root": "apps"}) == "cd apps && "
    assert dotnet_root_prefix({"dotnet_solution_root": "apps/backend"}) == "cd apps/backend && "
    assert dotnet_root_prefix({"dotnet_solution_root": ""}) == ""
    assert dotnet_root_prefix({}) == ""
    assert dotnet_root_prefix({"dotnet_solution_root": None}) == ""
    assert dotnet_root_prefix({"dotnet_solution_root": "../evil"}) == ""
    assert dotnet_root_prefix({"dotnet_solution_root": "/abs"}) == ""
    print("tech_stack_signals self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.tech_stack_signals`
    _demo()
