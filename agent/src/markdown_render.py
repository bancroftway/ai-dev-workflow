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
            deferred_suffix = " _(deferred — later phase)_" if story.get("deferred") else ""
            lines.append(f"### {story.get('id', '')}: {story.get('title', '')}{deferred_suffix}")
            lines.append("")
            lines.append(story.get("narrative", ""))
            lines.append("")
            criteria = story.get("acceptance_criteria") or []
            if criteria:
                lines.append("**Acceptance Criteria**")
                lines.append("")
                for ac in criteria:
                    ac_suffix = " _(deferred)_" if (ac.get("deferred") or story.get("deferred")) else ""
                    lines.append(f"- **{ac.get('id', '')}**: {ac.get('description', '')}{ac_suffix}")
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
            # Show the US/AC linkage the human gate is approving -- provenance must be visible in
            # the reviewable document, not only in the JSON.
            ac_ids = step.get("ac_ids") or []
            suffix = f" _(ACs: {', '.join(ac_ids)})_" if ac_ids else (
                " _(infrastructure)_" if step.get("kind") == "infrastructure" else ""
            )
            removes = step.get("removes_ids") or []
            if removes:
                suffix += f" **_(removes: {', '.join(removes)})_**"
            lines.append(f"- **{step.get('id', '')}**: {step.get('description', '')}{suffix}")
        lines.append("")

    risk_notes = content.get("risk_notes") or []
    if risk_notes:
        lines.append("## Risk Notes")
        lines.append("")
        lines.extend(f"- {note}" for note in risk_notes)
        lines.append("")

    # Link, don't inline: the wireframes are self-contained HTML files written by plan verify to
    # plan/wireframes/<screen>.html (relative to this file's own .ai-dev-workflow/ home). Without
    # this section they were invisible from the plan document -- present on the branch, referenced
    # by the adversarial audit, and unfindable by a human reading 04-plan.md.
    wireframes = content.get("wireframes") or []
    if wireframes:
        lines.append("## Wireframes")
        lines.append("")
        for wf in wireframes:
            screen = str(wf.get("screen", "")).strip()
            if screen:
                # GitHub renders the relative link as raw HTML source; the preview link (stamped by
                # plan verify once the repo/branch are known, diagram_gate.wireframe_preview_url)
                # opens the rendered page via html-preview.github.io.
                preview = str(wf.get("preview_url") or "").strip()
                suffix = f" -- [preview]({preview})" if preview else ""
                # Same "provenance must be visible in the reviewable document" reasoning as plan
                # steps' own _(ACs: ...)_ suffix above -- without it, a stage that reads this
                # rendered .md (ac-to-tests, minimal-code-to-green: both told to "read the approved
                # Implementation Plan", never pointed at the raw JSON specifically) has no way to
                # tell which AC a wireframe is evidence for, only that the screen exists.
                ac_ids = wf.get("ac_ids") or []
                if ac_ids:
                    suffix += f" _(ACs: {', '.join(ac_ids)})_"
                lines.append(f"- [{screen}](plan/wireframes/{screen}.html){suffix}")
        lines.append("")

    diagrams = content.get("diagrams") or []
    if diagrams:
        lines.append("## Diagrams")
        lines.append("")
        for diagram in diagrams:
            lines.append(f"### {diagram.get('name', '')} ({diagram.get('kind', '')})")
            lines.append("")
            lines.append("```mermaid")
            lines.append((diagram.get("mermaid_source") or "").strip())
            lines.append("```")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def _render_presence_section(lines: list[str], content: dict[str, Any], field: str, heading: str) -> None:
    """One PresenceList-shaped field (languages/frameworks/package_managers/testing_frameworks/
    conventions/config_inventory): its values when status="present", else its `reason` -- a human
    reviewing this document should SEE why a category came back empty, not just an absent section."""
    entry = content.get(field) or {}
    lines.append(f"## {heading}")
    lines.append("")
    if entry.get("status") == "present":
        lines.extend(f"- {v}" for v in (entry.get("values") or []))
    else:
        lines.append(entry.get("reason") or "(not checked)")
    lines.append("")


def render_tech_stack_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Tech Stack", "", content.get("summary", ""), ""]

    for field, heading in (
        ("languages", "Languages"),
        ("frameworks", "Frameworks"),
        ("package_managers", "Package Managers"),
        ("testing_frameworks", "Testing Frameworks"),
        ("conventions", "Conventions"),
    ):
        _render_presence_section(lines, content, field, heading)

    lines.append("## .NET")
    lines.append("")
    dotnet = content.get("dotnet") or {}
    if dotnet.get("status") == "detected":
        root = dotnet.get("solution_root")
        lines.append(f"Detected. Solution root: `{root}`" if root else "Detected, but no confident solution root.")
    else:
        lines.append(dotnet.get("reason") or "Not detected.")
    lines.append("")

    convention_roots = content.get("convention_roots") or []
    if convention_roots:
        lines.append("## Shared Config Roots")
        lines.append("")
        for entry in sorted(convention_roots, key=lambda e: e.get("ecosystem", "")):
            eco = entry.get("ecosystem", "")
            if entry.get("status") == "present":
                lines.append(f"- `{eco}`: `{entry.get('root') or '(repository root)'}`")
            else:
                lines.append(f"- `{eco}`: {entry.get('reason') or '(not present)'}")
        lines.append("")

    conventions_applied = content.get("conventions_applied") or []
    if conventions_applied:
        lines.append("## Conventions Applied This Run")
        lines.append("")
        lines.extend(f"- {c}" for c in conventions_applied)
        lines.append("")

    # Always rendered (human reviews it at the gate): a wrong auth_kind is worth correcting.
    lines.append("## Authentication")
    lines.append("")
    lines.append(f"Detected auth: **{content.get('auth_kind', 'none')}**")
    lines.append("")

    lines.append("## Configuration Keys")
    lines.append("")
    config_inventory = content.get("config_inventory") or {}
    if config_inventory.get("status") == "present":
        lines.append("Config the app reads -- supply test values on the repo settings page:")
        lines.append("")
        lines.extend(f"- `{k}`" for k in (config_inventory.get("values") or []))
    else:
        lines.append(config_inventory.get("reason") or "(not checked)")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_ac_to_tests_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Acceptance-Criteria Test Suite", "", content.get("summary", ""), ""]

    test_files = content.get("test_files") or []
    if test_files:
        lines.append("## Test Files")
        lines.append("")
        for tf in test_files:
            lines.append(f"- `{tf.get('path', '')}` ({tf.get('test_framework', '')}) -- covers: {', '.join(tf.get('ac_ids') or [])}")
        lines.append("")

    skipped = content.get("skipped_ac_ids") or []
    if skipped:
        lines.append("## Skipped Acceptance Criteria")
        lines.append("")
        for s in skipped:
            lines.append(f"- **{s.get('ac_id', '')}**: {s.get('reason', '')}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_minimal_code_to_green_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Minimal Code to Green", "", content.get("approach_summary", ""), ""]

    changed_files = content.get("changed_files") or []
    if changed_files:
        lines.append("## Changed Files")
        lines.append("")
        for cf in changed_files:
            lines.append(f"- **{cf.get('change_kind', '')}** `{cf.get('path', '')}` -- {cf.get('summary', '')}")
        lines.append("")

    known_gaps = content.get("known_gaps") or []
    if known_gaps:
        lines.append("## Known Gaps")
        lines.append("")
        lines.extend(f"- {gap}" for gap in known_gaps)
        lines.append("")

    ponytail_rejected = content.get("ponytail_rejected") or []
    if ponytail_rejected:
        lines.append("## Ponytail Suggestions Rejected")
        lines.append("")
        lines.extend(f"- {item}" for item in ponytail_rejected)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_adversarial_audit_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Adversarial Audit", "", content.get("plan_conformance_summary", ""), "", f"**Overall verdict:** {content.get('overall_verdict', '')}", ""]
    findings = content.get("divergence_findings") or []
    if findings:
        lines.append("## Divergence Findings")
        lines.append("")
        for f in findings:
            lines.append(f"- **[{f.get('severity', '')}] {f.get('id', '')}** ({f.get('plan_reference', '')}): {f.get('description', '')} -- {f.get('proposed_resolution', '')}")
        lines.append("")
    risk_notes = content.get("unresolved_risk_notes") or []
    if risk_notes:
        lines.append("## Unresolved Risk Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in risk_notes)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_license_audit_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# License Audit", "", content.get("summary", ""), ""]
    for c in content.get("classifications") or []:
        flag = " ⚠️ dual/exception" if c.get("dual_or_exception_flag") else ""
        lines.append(f"- **{c.get('package_name', '')}** ({c.get('ecosystem', '')}): {c.get('detected_license', '')} -- {c.get('bucket', '')} [{c.get('confidence', '')}]{flag}")
    return "\n".join(lines).strip() + "\n"


def render_brownfield_baseline_markdown(content: dict[str, Any]) -> str:
    spec = content.get("as_built_spec") or {}
    plan = content.get("as_built_plan") or {}
    lines: list[str] = ["# As-Built Baseline (inferred)", "", "Every story below is `origin: inferred` -- derived from existing code, not a requirements spec.", ""]
    for s in spec.get("user_stories") or []:
        lines.append(f"### {s.get('us_id', '')}: {s.get('title', '')} [{s.get('confidence', '')}]")
        lines.append(s.get("narrative", ""))
        lines.append("")
    for ac in spec.get("acceptance_criteria") or []:
        lines.append(f"- **{ac.get('ac_id', '')}** [{ac.get('confidence', '')}]: {ac.get('description', '')} (test: {ac.get('backing_test') or 'none'})")
    if plan.get("file_inventory"):
        lines.append("")
        lines.append("## File Inventory")
        lines.extend(f"- {f}" for f in plan["file_inventory"])
    return "\n".join(lines).strip() + "\n"


def render_exit_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Merge Readiness", "", f"**Ready to merge:** {content.get('merge_ready', False)}", ""]
    blocking = content.get("blocking_reasons") or []
    if blocking:
        lines.append("## Blocking Reasons")
        lines.append("")
        lines.extend(f"- {r}" for r in blocking)
        lines.append("")
    lines.append(f"## {content.get('pr_title', '')}")
    lines.append("")
    lines.append(content.get("pr_description_markdown", ""))
    lines.append("")
    risk_notes = content.get("risk_notes") or []
    if risk_notes:
        lines.append("## Risk Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in risk_notes)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_raw_requirements_markdown(content: dict[str, Any]) -> str:
    """The document's own `content` field is already Markdown prose (unlike every other stage's
    structured fields) -- this renderer is a passthrough, kept only so raw-requirements fits the
    same render_markdown-per-stage convention every other stage uses."""
    return (content.get("content") or "").strip() + "\n"


def _demo() -> None:
    # A wireframe's ac_ids must render (2026-09-01 fix): ac-to-tests/minimal-code-to-green are
    # only ever told to "read the approved Implementation Plan", never pointed at the raw JSON
    # specifically, so this rendered .md is the one place that link is guaranteed visible to them.
    md = render_plan_markdown(
        {
            "overview": "x",
            "plan_steps": [{"id": "PS-1", "description": "d", "ac_ids": ["US-0001.1"]}],
            "wireframes": [{"screen": "task-list", "ac_ids": ["US-0001.1", "US-0001.2"]}],
        }
    )
    assert "_(ACs: US-0001.1)_" in md, md  # plan step's own suffix, unchanged
    assert "_(ACs: US-0001.1, US-0001.2)_" in md, md  # wireframe's new suffix
    assert "[task-list](plan/wireframes/task-list.html)" in md, md

    # No ac_ids on a wireframe (schema default is an empty list) -> no suffix, no crash.
    md_no_ac_ids = render_plan_markdown({"overview": "x", "wireframes": [{"screen": "task-list"}]})
    assert "_(ACs:" not in md_no_ac_ids, md_no_ac_ids

    # render_tech_stack_markdown: TechStack's PresenceList/DotnetStatus/EcosystemRoot fields are
    # nested objects now, not bare lists/dicts -- present renders values, absent renders the
    # `reason` instead of silently rendering nothing for that section.
    ts_md = render_tech_stack_markdown(
        {
            "summary": "S",
            "languages": {"status": "present", "values": ["Python"]},
            "frameworks": {"status": "absent", "values": [], "reason": "no framework markers found"},
            "package_managers": {"status": "present", "values": ["pip"]},
            "testing_frameworks": {"status": "absent", "values": [], "reason": "no test files found"},
            "conventions": {"status": "present", "values": ["Repository pattern"]},
            "dotnet": {"status": "not_detected", "solution_root": None, "reason": "no .csproj/.sln found"},
            "convention_roots": [
                {"ecosystem": "node", "status": "absent", "root": "", "reason": "no package.json found"},
                {"ecosystem": "python", "status": "present", "root": "", "reason": ""},
            ],
            "auth_kind": "none",
            "config_inventory": {"status": "absent", "values": [], "reason": "no config keys found"},
        }
    )
    assert "- Python" in ts_md, ts_md
    assert "no framework markers found" in ts_md, "absent field must render its reason, not nothing"
    assert "no .csproj/.sln found" in ts_md, "not_detected dotnet must render its reason"
    assert "`node`: no package.json found" in ts_md, ts_md
    assert "`python`: `(repository root)`" in ts_md, ts_md
    assert "no config keys found" in ts_md, "absent config_inventory must render its reason"

    # A minimal/legacy-shaped dict (fields missing entirely) must not crash -- every reader falls
    # back to "not checked" rather than KeyError/AttributeError.
    assert render_tech_stack_markdown({"summary": "S"})
    assert render_tech_stack_markdown({})

    print("markdown_render self-check: ok")


if __name__ == "__main__":
    _demo()
