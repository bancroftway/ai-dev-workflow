"""Shared UI-framework detection signal.

Used by graph.py (P3's wireframe requirement, P4's Playwright MCP, session_options gates) AND
e2e_nodes.py (e2e_gate_check_node). Pulled out into its own leaf module -- rather than duplicated,
or imported one way -- because graph.py imports e2e_nodes to wire it in, so e2e_nodes importing
graph.py back would be circular; a shared leaf with no dependency on either avoids the cycle.
"""

from __future__ import annotations

from typing import Any

UI_FRAMEWORK_MARKERS = ("react", "vue", "angular", "blazor", "svelte", "next", "nuxt", "flutter", "swiftui", "jetpack compose")


def tech_stack_has_ui_framework(state: dict[str, Any]) -> bool:
    """Shared signal for P3's wireframe requirement, P4's Playwright MCP, and e2e's gate check --
    all three key off whether this repo has a UI framework at all, using tech-stack's own
    TechStack.frameworks report (a real, deliberate simplification from "only for UI-relevant
    content specifically" to "only for UI-framework repos at all")."""
    tech_stack = (state.get("stages") or {}).get("tech-stack", {}).get("approved_content") or {}
    frameworks = [str(f).lower() for f in (tech_stack.get("frameworks") or [])]
    return any(marker in fw for fw in frameworks for marker in UI_FRAMEWORK_MARKERS)
