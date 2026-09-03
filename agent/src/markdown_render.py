"""Per-stage Markdown rendering (architecture plan Section B.1).

Purpose-built per stage, not a generic dict-to-Markdown dumper: the whole reason to persist as
Markdown instead of raw JSON is that `git diff` on this folder should read as an actual reviewable
document, not a formatted JSON dump. Content shapes are fixed per stage (schemas.py's
Specification/ImplementationPlan), so a dedicated renderer per stage produces meaningfully better
prose than anything generic could.
"""

from __future__ import annotations

from typing import Any

from .schemas import presence_values


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

    # assumptions/out_of_scope are PresenceList-shaped (schemas.py, Task 10) -- always render, so
    # an absent section shows WHY (its reason) rather than silently vanishing like the old
    # if-non-empty bare-list rendering did.
    _render_presence_section(lines, content, "assumptions", "Assumptions")
    _render_presence_section(lines, content, "out_of_scope", "Out of Scope")

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

    # risk_notes is PresenceList-shaped (schemas.py, Task 10) -- always render, reason when absent.
    _render_presence_section(lines, content, "risk_notes", "Risk Notes")

    # wireframes/diagrams are WireframePresence/DiagramPresence-shaped (schemas.py, Task 10), not a
    # bare PresenceList -- their `values` are structured objects (Wireframe/PlanDiagram), not
    # strings, so they get custom rendering, same "always show why, present or absent" convention
    # as render_adversarial_audit_markdown's divergence_findings below.
    #
    # Link, don't inline: the wireframes are self-contained HTML files written by plan verify to
    # plan/wireframes/<screen>.html (relative to this file's own .ai-dev-workflow/ home). Without
    # this section they were invisible from the plan document -- present on the branch, referenced
    # by the adversarial audit, and unfindable by a human reading 04-plan.md.
    # presence_values (schemas.py, final review fix wave) tolerates wireframes_entry/diagrams_entry
    # still being a genuinely legacy bare list -- a plain `.get("status")` on a list raises
    # AttributeError, one of this fix wave's two real reachable crash paths.
    wireframes_entry = content.get("wireframes") or {}
    wireframe_values = presence_values(wireframes_entry)
    lines.append("## Wireframes")
    lines.append("")
    if wireframe_values:
        for wf in wireframe_values:
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
    else:
        reason = wireframes_entry.get("reason") if isinstance(wireframes_entry, dict) else None
        lines.append(reason or "(not checked)")
    lines.append("")

    diagrams_entry = content.get("diagrams") or {}
    diagram_values = presence_values(diagrams_entry)
    lines.append("## Diagrams")
    lines.append("")
    if diagram_values:
        for diagram in diagram_values:
            lines.append(f"### {diagram.get('name', '')} ({diagram.get('kind', '')})")
            lines.append("")
            lines.append("```mermaid")
            lines.append((diagram.get("mermaid_source") or "").strip())
            lines.append("```")
            lines.append("")
    else:
        reason = diagrams_entry.get("reason") if isinstance(diagrams_entry, dict) else None
        lines.append(reason or "(not checked)")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _render_presence_section(lines: list[str], content: dict[str, Any], field: str, heading: str) -> None:
    """One PresenceList-shaped field (TechStack's languages/frameworks/package_managers/
    testing_frameworks/conventions/config_inventory, AdversarialAuditReport's
    unresolved_risk_notes, MergeReadinessReport's blocking_reasons/risk_notes, Specification's
    assumptions/out_of_scope, ImplementationPlan's risk_notes): its values when status="present",
    else its `reason` -- a human reviewing this document should SEE why a category came back
    empty, not just an absent section.

    Routed through schemas.presence_values (final review fix wave) rather than a bare
    `entry.get("status")`/`entry.get("values")` read: a legacy on-disk sidecar/checkpoint (or
    `stage["draft"]` resumed from before PresenceList existed) can still have `content[field]` as a
    genuinely bare list, and `entry.get(...)` on a list raises AttributeError -- presence_values
    tolerates that shape the same way every other reader of a plain off-the-wire dict in this
    codebase already does.
    """
    entry = content.get(field) or {}
    values = presence_values(entry)
    lines.append(f"## {heading}")
    lines.append("")
    if values:
        lines.extend(f"- {v}" for v in values)
    else:
        reason = entry.get("reason") if isinstance(entry, dict) else None
        lines.append(reason or "(not checked)")
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
    # presence_values (schemas.py) tolerates config_inventory still being a genuinely legacy bare
    # list -- `.get("status")` on a list raises AttributeError, the same crash this fix wave found
    # in _render_presence_section and the wireframes/diagrams block above.
    config_inventory = content.get("config_inventory") or {}
    config_values = presence_values(config_inventory)
    if config_values:
        lines.append("Config the app reads -- supply test values on the repo settings page:")
        lines.append("")
        lines.extend(f"- `{k}`" for k in config_values)
    else:
        reason = config_inventory.get("reason") if isinstance(config_inventory, dict) else None
        lines.append(reason or "(not checked)")
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

    # known_gaps is PresenceList-shaped (schemas_codegen.py, Task 12) as of this task -- always
    # render, reason when absent, same convention as every other PresenceList field this module
    # renders. Was a bare `content.get("known_gaps") or []` read before the schema changed.
    _render_presence_section(lines, content, "known_gaps", "Known Gaps")

    ponytail_rejected = content.get("ponytail_rejected") or []
    if ponytail_rejected:
        lines.append("## Ponytail Suggestions Rejected")
        lines.append("")
        lines.extend(f"- {item}" for item in ponytail_rejected)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_adversarial_audit_markdown(content: dict[str, Any]) -> str:
    lines: list[str] = ["# Adversarial Audit", "", content.get("plan_conformance_summary", ""), "", f"**Overall verdict:** {content.get('overall_verdict', '')}", ""]
    # divergence_findings is DivergenceFindingPresence-shaped (schemas_audit.py), not a bare
    # PresenceList -- its `values` are structured findings, not strings, so it gets its own
    # rendering rather than _render_presence_section, but the same "always show why, present or
    # absent" convention.
    findings_entry = content.get("divergence_findings") or {}
    lines.append("## Divergence Findings")
    lines.append("")
    if findings_entry.get("status") == "present":
        for f in findings_entry.get("values") or []:
            lines.append(f"- **[{f.get('severity', '')}] {f.get('id', '')}** ({f.get('plan_reference', '')}): {f.get('description', '')} -- {f.get('proposed_resolution', '')}")
    else:
        lines.append(findings_entry.get("reason") or "(not checked)")
    lines.append("")
    _render_presence_section(lines, content, "unresolved_risk_notes", "Unresolved Risk Notes")
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
    _render_presence_section(lines, content, "blocking_reasons", "Blocking Reasons")
    lines.append(f"## {content.get('pr_title', '')}")
    lines.append("")
    lines.append(content.get("pr_description_markdown", ""))
    lines.append("")
    _render_presence_section(lines, content, "risk_notes", "Risk Notes")
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
            # wireframes is WireframePresence-shaped (schemas.py, Task 10), not a bare list.
            "wireframes": {
                "status": "present",
                "values": [{"screen": "task-list", "ac_ids": ["US-0001.1", "US-0001.2"]}],
            },
        }
    )
    assert "_(ACs: US-0001.1)_" in md, md  # plan step's own suffix, unchanged
    assert "_(ACs: US-0001.1, US-0001.2)_" in md, md  # wireframe's new suffix
    assert "[task-list](plan/wireframes/task-list.html)" in md, md

    # No ac_ids on a wireframe (schema default is an empty list) -> no suffix, no crash.
    md_no_ac_ids = render_plan_markdown(
        {"overview": "x", "wireframes": {"status": "present", "values": [{"screen": "task-list"}]}}
    )
    assert "_(ACs:" not in md_no_ac_ids, md_no_ac_ids

    # risk_notes/diagrams/wireframes absent -> each section renders its reason, not nothing.
    md_absent = render_plan_markdown(
        {
            "overview": "x",
            "risk_notes": {"status": "absent", "values": [], "reason": "no risks identified"},
            "diagrams": {"status": "absent", "values": [], "reason": "trivial change, no diagram needed"},
            "wireframes": {"status": "absent", "values": [], "reason": "no UI work in this plan"},
        }
    )
    assert "no risks identified" in md_absent, "absent risk_notes must render its reason"
    assert "trivial change, no diagram needed" in md_absent, "absent diagrams must render its reason"
    assert "no UI work in this plan" in md_absent, "absent wireframes must render its reason"

    # A minimal/legacy-shaped dict (fields missing entirely) must not crash.
    assert render_plan_markdown({"overview": "x"})
    assert render_plan_markdown({})

    # Final review fix wave (Important 1): risk_notes/wireframes/diagrams as a genuinely LEGACY
    # bare list (pre-migration on-disk sidecar/checkpoint, or stage["draft"] resumed from before
    # this plan's schema tightening) used to crash with AttributeError: 'list' object has no
    # attribute 'get' -- `.get("status")` on a bare list. Must render the values it has instead.
    md_legacy_bare_list = render_plan_markdown(
        {
            "overview": "x",
            "risk_notes": ["a bare legacy risk note"],
            "wireframes": [{"screen": "task-list"}],
            "diagrams": [{"name": "flow", "kind": "flowchart", "mermaid_source": "graph TD; A-->B;"}],
        }
    )
    assert "- a bare legacy risk note" in md_legacy_bare_list, md_legacy_bare_list
    assert "[task-list](plan/wireframes/task-list.html)" in md_legacy_bare_list, md_legacy_bare_list
    assert "### flow (flowchart)" in md_legacy_bare_list, md_legacy_bare_list
    # An EMPTY legacy bare list (nothing detected, but still not the typed absent+reason shape)
    # must fall back to "(not checked)" rather than crash on a missing `reason`.
    assert render_plan_markdown({"overview": "x", "risk_notes": [], "wireframes": [], "diagrams": []})

    # render_specification_markdown: assumptions/out_of_scope are PresenceList-shaped -- present
    # renders values, absent renders the reason.
    spec_md = render_specification_markdown(
        {
            "title": "T",
            "summary": "S",
            "assumptions": {"status": "present", "values": ["users have a verified email"]},
            "out_of_scope": {"status": "absent", "values": [], "reason": "nothing was excluded"},
        }
    )
    assert "- users have a verified email" in spec_md, spec_md
    assert "nothing was excluded" in spec_md, "absent out_of_scope must render its reason"
    assert render_specification_markdown({"title": "T", "summary": "S"})

    # Final review fix wave (Important 1): assumptions/out_of_scope as a genuinely legacy bare list
    # must not crash render_specification_markdown either (same _render_presence_section bug).
    spec_md_legacy = render_specification_markdown(
        {"title": "T", "summary": "S", "assumptions": ["a bare legacy assumption"], "out_of_scope": []}
    )
    assert "- a bare legacy assumption" in spec_md_legacy, spec_md_legacy
    assert "(not checked)" in spec_md_legacy, "empty legacy bare list must fall back, not crash"

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

    # Final review fix wave (Important 1): config_inventory as a genuinely legacy bare list (the
    # exact shape preflight_nodes.resolve_tech_stack_submission and graph.persist_state can hand
    # this renderer for an on-disk sidecar/checkpoint predating PresenceList) must not crash either.
    ts_md_legacy = render_tech_stack_markdown({"summary": "S", "config_inventory": ["DATABASE_URL"]})
    assert "- `DATABASE_URL`" in ts_md_legacy, ts_md_legacy
    ts_md_legacy_empty = render_tech_stack_markdown({"summary": "S", "config_inventory": []})
    assert "(not checked)" in ts_md_legacy_empty, "empty legacy bare list must fall back, not crash"

    # A minimal/legacy-shaped dict (fields missing entirely) must not crash -- every reader falls
    # back to "not checked" rather than KeyError/AttributeError.
    assert render_tech_stack_markdown({"summary": "S"})
    assert render_tech_stack_markdown({})

    # render_minimal_code_to_green_markdown: known_gaps is PresenceList-shaped (schemas_codegen.py,
    # Task 12) -- present renders values, absent renders the reason (was a bare
    # `content.get("known_gaps") or []` read before the schema changed).
    green_present = render_minimal_code_to_green_markdown({
        "approach_summary": "x",
        "known_gaps": {"status": "present", "values": ["needs a follow-up migration"]},
    })
    assert "- needs a follow-up migration" in green_present, green_present
    green_absent = render_minimal_code_to_green_markdown({
        "approach_summary": "x",
        "known_gaps": {"status": "absent", "values": [], "reason": "nothing left open"},
    })
    assert "nothing left open" in green_absent, "absent known_gaps must render its reason"
    assert render_minimal_code_to_green_markdown({"approach_summary": "x"})

    # render_adversarial_audit_markdown: divergence_findings is DivergenceFindingPresence-shaped
    # (structured findings, not a bare PresenceList), unresolved_risk_notes is PresenceList-shaped --
    # both must render their `reason` when absent, not silently omit the section.
    audit_present = render_adversarial_audit_markdown({
        "plan_conformance_summary": "Mostly conforms.",
        "overall_verdict": "minor_gaps",
        "divergence_findings": {
            "status": "present",
            "values": [{"id": "DIV-1", "severity": "minor", "plan_reference": "Plan Step 4",
                        "description": "copy drift", "proposed_resolution": "align the copy"}],
        },
        "unresolved_risk_notes": {"status": "absent", "values": [], "reason": "no open risks"},
    })
    assert "**[minor] DIV-1** (Plan Step 4): copy drift -- align the copy" in audit_present, audit_present
    assert "no open risks" in audit_present, "absent unresolved_risk_notes must render its reason"

    audit_absent = render_adversarial_audit_markdown({
        "plan_conformance_summary": "Fully conforms.",
        "overall_verdict": "conforms",
        "divergence_findings": {"status": "absent", "values": [], "reason": "no divergences found"},
        "unresolved_risk_notes": {"status": "present", "values": ["flaky third-party API"]},
    })
    assert "no divergences found" in audit_absent, "absent divergence_findings must render its reason"
    assert "- flaky third-party API" in audit_absent, audit_absent

    # render_exit_markdown: blocking_reasons/risk_notes are PresenceList-shaped -- reuse of
    # _render_presence_section covers both directions the same way tech-stack fields do.
    exit_blocked = render_exit_markdown({
        "merge_ready": False,
        "blocking_reasons": {"status": "present", "values": ["coverage regressed"]},
        "pr_title": "Add the thing",
        "pr_description_markdown": "Adds the thing.",
        "risk_notes": {"status": "absent", "values": [], "reason": "no open risks"},
    })
    assert "- coverage regressed" in exit_blocked, exit_blocked
    assert "no open risks" in exit_blocked, "absent risk_notes must render its reason"

    exit_ready = render_exit_markdown({
        "merge_ready": True,
        "blocking_reasons": {"status": "absent", "values": [], "reason": "no blockers"},
        "pr_title": "Add the thing",
        "pr_description_markdown": "Adds the thing.",
        "risk_notes": {"status": "present", "values": ["depends on an unreleased upstream fix"]},
    })
    assert "no blockers" in exit_ready, "absent blocking_reasons must render its reason"
    assert "- depends on an unreleased upstream fix" in exit_ready, exit_ready

    print("markdown_render self-check: ok")


if __name__ == "__main__":
    _demo()
