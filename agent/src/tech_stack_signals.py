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


def frameworks_have_ui(frameworks: list[Any]) -> bool:
    """The pure check behind tech_stack_has_ui_framework, for callers that hold a frameworks list
    but no graph state (exit's deterministic verify reads tech-stack.approved.json off disk)."""
    lowered = [str(f).lower() for f in frameworks or []]
    return any(marker in fw for fw in lowered for marker in UI_FRAMEWORK_MARKERS)


def tech_stack_has_ui_framework(state: dict[str, Any]) -> bool:
    """Shared signal for P3's wireframe requirement, P4's Playwright MCP, and e2e's gate check --
    all three key off whether this repo has a UI framework at all, using tech-stack's own
    TechStack.frameworks report (a real, deliberate simplification from "only for UI-relevant
    content specifically" to "only for UI-framework repos at all")."""
    tech_stack = (state.get("stages") or {}).get("tech-stack", {}).get("approved_content") or {}
    return frameworks_have_ui(tech_stack.get("frameworks") or [])


def is_greenfield_repo(state: dict[str, Any]) -> bool:
    """No candidate the deterministic scan found -- a genuinely blank (or non-startable-only)
    repository. Replaces the removed interrupt-driven state["greenfield"] dict; pure function of
    the scan already taken at app_discovery_pre_node, no separate state channel needed."""
    return not (state.get("app_scan") or {}).get("candidates")


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
    return _cd_prefix(tech_stack.get("dotnet_solution_root"), "dotnet_solution_root")


def ecosystem_root_prefix(tech_stack: dict[str, Any], ecosystem: str) -> str:
    """dotnet_root_prefix's twin for the non-.NET ecosystems: `cd`s into
    TechStack.convention_roots[ecosystem] (e.g. node=apps/web) before a bare npm/tsc/ruff command.
    Same rationale and same validation -- a greenfield monorepo has no root package.json, so a
    bare `npx tsc --noEmit` at repo root prints tsc's help text and fails the rebuild gate by
    construction (observed live, headless sc1). Empty/missing/unsafe root returns ""."""
    roots = tech_stack.get("convention_roots") or {}
    return _cd_prefix(roots.get(ecosystem), f"convention_roots[{ecosystem}]")


def _cd_prefix(root: Any, field: str) -> str:
    root = root or ""
    if not root:
        return ""
    try:
        repo_files.validate_repo_relative_path(root)
    except ValueError:
        logger.warning("ignoring unsafe %s %r", field, root)
        return ""
    return f"cd {shlex.quote(str(root).strip('/'))} && "


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.tech_stack_signals`."""
    assert is_greenfield_repo({}) is True
    assert is_greenfield_repo({"app_scan": {}}) is True
    assert is_greenfield_repo({"app_scan": {"candidates": []}}) is True
    assert is_greenfield_repo({"app_scan": {"candidates": [{"path": "."}]}}) is False

    assert dotnet_root_prefix({"dotnet_solution_root": "apps"}) == "cd apps && "
    assert dotnet_root_prefix({"dotnet_solution_root": "apps/backend"}) == "cd apps/backend && "
    assert dotnet_root_prefix({"dotnet_solution_root": ""}) == ""
    assert dotnet_root_prefix({}) == ""
    assert dotnet_root_prefix({"dotnet_solution_root": None}) == ""
    assert dotnet_root_prefix({"dotnet_solution_root": "../evil"}) == ""
    assert dotnet_root_prefix({"dotnet_solution_root": "/abs"}) == ""
    assert ecosystem_root_prefix({"convention_roots": {"node": "apps/web"}}, "node") == "cd apps/web && "
    assert ecosystem_root_prefix({"convention_roots": {"node": "apps/web"}}, "python") == ""
    assert ecosystem_root_prefix({"convention_roots": {"python": "../evil"}}, "python") == ""
    assert ecosystem_root_prefix({}, "node") == ""
    print("tech_stack_signals self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.tech_stack_signals`
    _demo()
