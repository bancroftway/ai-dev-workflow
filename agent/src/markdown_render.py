"""Per-stage Markdown rendering (architecture plan Section B.1).

Purpose-built per stage, not a generic dict-to-Markdown dumper: the whole reason to persist as
Markdown instead of raw JSON is that `git diff` on this folder should read as an actual reviewable
document, not a formatted JSON dump. Content shapes are fixed per stage (schemas.py's
Specification/ImplementationPlan), so a dedicated renderer per stage produces meaningfully better
prose than anything generic could.
"""

from __future__ import annotations

from typing import Any


def render_specification_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = [f"# {content.get('title', 'Specification')}", "", content.get("summary", ""), ""]

    user_stories = content.get("user_stories") or []
    if user_stories:
        lines.append("## User Stories")
        lines.append("")
        for story in user_stories:
            lines.append(f"### {story.get('id', '')}: {story.get('title', '')}")
            lines.append("")
            lines.append(story.get("narrative", ""))
            lines.append("")
            criteria = story.get("acceptance_criteria") or []
            if criteria:
                lines.append("**Acceptance Criteria**")
                lines.append("")
                for ac in criteria:
                    lines.append(f"- **{ac.get('id', '')}**: {ac.get('description', '')}")
                lines.append("")

    assumptions = content.get("assumptions") or []
    if assumptions:
        lines.append("## Assumptions")
        lines.append("")
        lines.extend(f"- {a}" for a in assumptions)
        lines.append("")

    out_of_scope = content.get("out_of_scope") or []
    if out_of_scope:
        lines.append("## Out of Scope")
        lines.append("")
        lines.extend(f"- {item}" for item in out_of_scope)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_plan_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Implementation Plan", "", content.get("overview", ""), ""]

    plan_steps = content.get("plan_steps") or []
    if plan_steps:
        lines.append("## Steps")
        lines.append("")
        for step in plan_steps:
            lines.append(f"- **{step.get('id', '')}**: {step.get('description', '')}")
        lines.append("")

    risk_notes = content.get("risk_notes") or []
    if risk_notes:
        lines.append("## Risk Notes")
        lines.append("")
        lines.extend(f"- {note}" for note in risk_notes)
        lines.append("")

    return "\n".join(lines).strip() + "\n"
