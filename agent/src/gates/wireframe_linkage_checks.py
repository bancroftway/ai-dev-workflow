"""Pure, dependency-free (stdlib `json`/`os`/`re`/`sys` only) wireframe<->AC/PlanStep linkage
checks -- extracted from `diagram_gate.py` (2026-09-24) specifically so the sandbox's own same-turn
Stop hooks (`check-plan-citations-stop.mjs`, `check-plan-schema-stop.mjs`, and, since Task 12,
`check-diagram-staleness-stop.mjs`) can run the REAL checks by shelling out to `python3` on this ONE
file, instead of a hand-ported JavaScript reimplementation drifting from it.

Why a subprocess, not a straight import: `diagram_gate.py`/`spec_ledger.py` (and everything
upstream of them -- `chat_model.py`, `sandbox/provider.py`, `repo_files.py`, this pipeline's DB
drivers) live entirely outside the sandbox and are never copied into the image; even if they were,
importing those modules pulls in a dependency graph with no business running inside a per-turn Stop
hook. This module is the answer to "duplicate the logic, or ship the real thing": everything below
is provably pure (a list of dicts in, a list of strings out, confirmed by this file's own
self-check) and needs nothing beyond what `python3`'s standard library already provides, so it is
the ONE file this pipeline ships into `/opt/aidw-hooks/` and calls directly -- the real
implementation, not a port of it.

`diagram_gate.py`/`spec_ledger.py` import every name below UNCHANGED (this is a pure code-move, not
a fork); their own self-checks are the proof this extraction changed no behavior.

Extended 2026-09-29 (Task 9) with the two hooks' REMAINING hand-ported halves, following the exact
pattern the 2026-09-24 extraction above and the same-day narrative_format_checks.py/
full_read_checks.py extraction both already used:
- `check_wireframe`/`check_wireframes_content` (wireframe byte-size/forbidden-pattern content
  check) and `check_manifest_orphans` (disk-vs-manifest.json orphan sweep, both directions, both
  artifact kinds) -- `check-plan-schema-stop.mjs`'s own two remaining hand-ported halves, both
  originally from `diagram_gate.py` (`check_wireframe` and `_load_and_check_manifest`'s pure
  set-difference half respectively).
- `check_ac_id_citation` (the hand-ported JS `checkAcIds`'s exact behavior: existence+kind, plus an
  optional liveness check) and `check_retired_step_ids` -- `check-plan-citations-stop.mjs`'s own
  remaining hand-ported half, originally hand-derived from `diagram_gate.py`'s `check_plan_linkage`
  and `spec_ledger.py`'s `sync_plan_ledger` respectively. `check_plan_linkage` itself keeps its own,
  richer liveness/carryover/coverage-side/rework-forbidden logic untouched (a later, separate task's
  scope -- see `check_ac_id_citation`'s own docstring for exactly where the two diverge); only
  `spec_ledger.py`'s `sync_plan_ledger`'s `retired_step_ids` validity loop, which has no such
  entanglement, was updated to import `check_retired_step_ids` directly. A task review (2026-09-29)
  then caught that the ONE sub-check `check_plan_linkage` and `check_ac_id_citation` both genuinely
  share -- id existence + kind='acceptance_criterion', with no liveness involved -- had been
  independently retyped in both places rather than called from one; `ac_id_existence_kind_problems`
  is that shared sub-check, called by both.

Extended 2026-09-30 (Task 12) with `check_plan_linkage`'s remaining un-shared halves and three
previously-un-hooked-but-portable checks -- see the "Task 12" section comment below for the full
list and each function's own docstring for exactly what moved from where. In short:
`check_dangling_visual_retirement` (diagram_gate.py), `check_removes_ids_validity`/
`check_plan_removal_demand`/`check_plan_step_coverage_and_rework` (all split out of
`diagram_gate.check_plan_linkage`, which now calls them instead of keeping its own copy),
`check_plan_step_id_guard`/`check_plan_step_ids` (spec_ledger.sync_plan_ledger's id guard), and
`reopened_or_changed_ac_ids` (diagram_gate.py's private `_reopened_or_changed_ac_ids`, now also
callable by check-diagram-staleness-stop.mjs now that AIDW_RUN_ID/Task 5 makes `run_id` available
to a Stop hook). `own_ac_ids_from_specification`/`eligible_ac_ids` also moved here from
spec_ledger.py (re-exported from there unchanged) since `check_plan_step_coverage_and_rework` needs
them and this module cannot import spec_ledger.py (see this file's own "why a subprocess" reasoning
above).

CLI mode (`python3 wireframe_linkage_checks.py --check-hook`, stdin JSON: `{"wireframes": [...],
"ledger_entries": [...], "ui_related_ac_ids": [...], "plan_steps": [...], "diagrams": [...],
"retired_step_ids": [...], "wireframe_html": [...], "disk_wireframe_screens": [...],
"manifest_wireframe_screens": [...], "retired_wireframe_screens": [...], "disk_diagram_names": [...],
"manifest_diagram_names": [...], "retired_diagram_names": [...], "specification": {...} | null,
"prior_plan_steps": [...], "run_id": "..." | null, "bug_affected_ac_ids": [...]}`, EVERY key
optional -- a caller passes only the keys its own checks need, stdout JSON: `{"wireframe_ac_ids":
[...], "wireframe_has_ac_ids": [...], "ui_wireframe_coverage": [...],
"plan_step_wireframe_coverage": [...], "step_ac_id_problems": [...], "diagram_ac_id_problems": [...],
"retired_step_id_problems": [...], "wireframe_content_problems": [...],
"manifest_orphan_problems": [...], "dangling_visual_retirement_problems": [...],
"removes_ids_problems": [...], "plan_step_coverage_rework_problems": [...],
"plan_step_id_problems": [...], "reopened_or_changed_ac_ids": [...] (only when `run_id` was sent)}`,
each value a list of problem strings (except `reopened_or_changed_ac_ids`, a sorted list of AC ids))
is what the Stop hooks actually invoke -- ONE flag, ONE combined contract; each hook only reads the
output keys its own checks produced.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def check_wireframe_ac_ids(
    wireframes: list[dict[str, Any]], ledger_entries: list[dict[str, Any]]
) -> list[str]:
    """Citation-validity only (user requirement 2026-08-31: 'the wireframes must indicate which
    US/AC they are fulfilling') -- each id a wireframe names must actually exist and be an
    acceptance criterion, same discipline as PlanStep.ac_ids. Deliberately NOT a coverage
    direction (no demand that every UI-touching AC have a wireframe, or that ui_related steps
    have one) -- that would be new scope beyond what was asked; this only catches an invented or
    mistyped id. Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    problems: list[str] = []
    for wf in wireframes:
        screen = wf.get("screen") or "?"
        bad = [i for i in wf.get("ac_ids") or [] if by_id.get(i) is None or by_id[i].get("kind") != "acceptance_criterion"]
        if bad:
            problems.append(
                f"wireframe {screen!r}: cites {', '.join(bad)} which is not an acceptance criterion "
                "in the ledger -- copy ids exactly from the approved Specification"
            )
    return problems


def check_wireframe_has_ac_ids(wireframes: list[dict[str, Any]]) -> list[str]:
    """Every wireframe must cite >=1 ac_id. `Wireframe.ac_ids` (schemas.py) defaults to an empty
    list, and until now nothing rejected that: check_wireframe_ac_ids only validates ids a
    wireframe DOES cite are real, and check_ui_wireframe_coverage only checks the other direction
    (every ui_related AC has SOME wireframe). Neither stops a wireframe from citing nothing at
    all -- which would dodge the e2e stage's wireframe-coverage gate (e2e_nodes.py), which has
    nothing to match an AC-less screen against. Pure."""
    return [
        f"wireframe {wf.get('screen')!r}: cites no ac_ids -- every wireframe must name at least "
        "one acceptance criterion it is evidence for, or the e2e stage cannot verify this screen "
        "was actually built and tested"
        for wf in wireframes
        if not (wf.get("ac_ids") or [])
    ]


def check_ui_wireframe_coverage(ui_related_ac_ids: set[str], wireframes: list[dict[str, Any]]) -> list[str]:
    """Coverage direction (user requirement 2026-09-01): every criterion the approved
    Specification marks ui_related must be cited by at least one wireframe's ac_ids -- a
    UI-facing requirement with zero wireframe evidence is exactly what this exists to catch. The
    caller only ever passes LIVE, non-deferred ids (see verify_plan_diagrams's own build of
    ui_related_ac_ids) -- nothing is demanded for scope not being built this ticket. Pure."""
    covered: set[str] = set()
    for wf in wireframes:
        covered.update(wf.get("ac_ids") or [])
    return [
        f"{ac_id}: marked ui_related in the Specification, but no wireframe's ac_ids cites it -- "
        "add a wireframe for the screen that satisfies it (or fix the Specification if ui_related "
        "is wrong for this criterion)"
        for ac_id in sorted(ui_related_ac_ids - covered)
    ]


def check_plan_step_wireframe_coverage(
    plan_steps: list[dict[str, Any]], wireframes: list[dict[str, Any]]
) -> list[str]:
    """Every plan step marked ui_related must have >=1 of its ac_ids cited by some wireframe --
    the PlanStep-side half of AC/wireframe coverage (PlanStep.ui_related's own docstring used to
    say 'not gate-enforced against wireframe coverage' -- now it is, 2026-09-24). plan_steps here
    is always this draft's LIVE steps only (a retired step is named in retired_step_ids and absent
    from this list entirely, same convention as retired ACs being absent from the approved
    Specification's user_stories). A step with ui_related=True but empty ac_ids (only valid for
    kind='infrastructure') can't be matched against anything and isn't this check's job -- an
    infrastructure step claiming a visible surface with no citable requirement is a modeling
    choice this check doesn't second-guess. Pure."""
    covered = {a for wf in wireframes for a in (wf.get("ac_ids") or [])}
    return [
        f"{step.get('id')}: marked ui_related but none of its ac_ids "
        f"({', '.join(step.get('ac_ids') or [])}) is cited by any wireframe -- add a wireframe "
        "for the screen(s) this step changes."
        for step in plan_steps
        if step.get("ui_related") and (step.get("ac_ids") or [])
        and not any(a in covered for a in step["ac_ids"])
    ]


# --- check-plan-schema-stop.mjs's remaining hand-ported halves (Task 9, 2026-09-29) ---

DRAFT_WIREFRAMES_DIR = ".ai-dev-workflow/plan/_draft/wireframes"
DRAFT_DIAGRAMS_DIR = ".ai-dev-workflow/plan/_draft/diagrams"

# Same env var name as config.py's own DIAGRAM_MAX_WIREFRAME_BYTES read (AGENTS.md's own rule: two
# reads of the same env var, not a second knob) -- kept in sync by that identity, not by import,
# since config.py itself pulls in dependencies this sandbox-side module must not (see this file's
# own header). diagram_gate.py imports THIS name rather than reading config.DIAGRAM_MAX_WIREFRAME_
# BYTES directly, so the two can never independently drift even by accident.
MAX_WIREFRAME_BYTES = int(os.environ.get("AIDW_DIAGRAM_MAX_WIREFRAME_BYTES", str(30 * 1024)))

SAFE_DIAGRAM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Trust-boundary checks on model-emitted wireframe HTML. This denylist is hygiene for the
# committed artifact, NOT the security boundary -- the frontend confines every wireframe (both
# thumbnail and full-size) to an empty-`sandbox` iframe, whose null origin and script ban hold
# even against markup these regexes miss. The on\w+= check is anchored inside a tag (after
# `<tag ` and before its `>`) so prose like "conversion=..." never false-positives. Moved
# byte-for-byte from diagram_gate.py's `_WIREFRAME_FORBIDDEN` (2026-09-29) -- same order, same
# messages.
WIREFRAME_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<\s*script\b", re.IGNORECASE), "contains a <script> tag"),
    (re.compile(r"<[a-zA-Z][^>]*\son\w+\s*=", re.IGNORECASE), "contains an inline on*= event handler"),
    (re.compile(r"""(?:src|href|action|data|xlink:href)\s*=\s*["']?\s*(?:https?:)?//""", re.IGNORECASE), "references an external URL"),
    (re.compile(r"""(?:src|href|action|data|xlink:href)\s*=\s*["']?\s*(?:javascript|vbscript|data|file)\s*:""", re.IGNORECASE), "uses a dangerous URL scheme (javascript:/vbscript:/data:/file:)"),
    (re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.IGNORECASE), "references an external URL (css url())"),
    (re.compile(r"@import\b", re.IGNORECASE), "uses @import (external stylesheet)"),
    (re.compile(r"<\s*(?:iframe|object|embed|base|form)\b", re.IGNORECASE), "contains an embedding/navigation element (iframe/object/embed/base/form)"),
    (re.compile(r"""<\s*meta\b[^>]*http-equiv""", re.IGNORECASE), "contains <meta http-equiv> (refresh/CSP override)"),
)


def check_wireframe(screen: str, html_source: str) -> str | None:
    """Returns a rejection reason, or None if the wireframe is acceptable. Moved byte-for-byte from
    diagram_gate.py (2026-09-29) -- pure, self-checkable without a sandbox. There is deliberately no
    wireframe COUNT cap (removed 2026-09-24) -- a plan may cite as many wireframes as the work
    actually needs; do not reintroduce one here."""
    if not SAFE_DIAGRAM_NAME_RE.match(screen or ""):
        return f"screen name {screen!r} must match {SAFE_DIAGRAM_NAME_RE.pattern} (letters, digits, _, - only)"
    if len(html_source.encode("utf-8")) > MAX_WIREFRAME_BYTES:
        return f"wireframe {screen!r} exceeds {MAX_WIREFRAME_BYTES // 1024} KB -- simplify it"
    lowered = html_source.lower()
    if "<html" not in lowered and "<body" not in lowered and "<div" not in lowered:
        return f"wireframe {screen!r} does not look like an HTML page"
    for pattern, reason in WIREFRAME_FORBIDDEN:
        if pattern.search(html_source):
            return f"wireframe {screen!r} {reason} -- wireframes must be fully self-contained (inline CSS only)"
    return None


def check_wireframes_content(wireframes: list[dict[str, Any]]) -> list[str]:
    """Bulk wrapper over check_wireframe -- each dict shaped {"screen": ..., "html_source": ...}
    (check-plan-schema-stop.mjs's own payload shape: it already has each wireframe's real HTML body
    on disk). Returns one problem string per rejected wireframe (empty = all pass). Pure."""
    problems: list[str] = []
    for wf in wireframes:
        reason = check_wireframe(wf.get("screen") or "?", wf.get("html_source") or "")
        if reason:
            problems.append(reason)
    return problems


def check_manifest_orphans(
    disk_wireframe_screens: set[str],
    manifest_wireframe_screens: set[str],
    retired_wireframe_screens: set[str],
    disk_diagram_names: set[str],
    manifest_diagram_names: set[str],
    retired_diagram_names: set[str],
) -> list[str]:
    """Disk<->manifest.json orphan sweep, both directions, both artifact kinds -- the ONLY pure
    half of diagram_gate.py's `_load_and_check_manifest` (listing the sandbox's actual
    _draft/wireframes//_draft/diagrams/ directory stays there -- this module has no sandbox
    access). A wireframe/diagram file on disk that manifest.json doesn't list (and doesn't
    explicitly retire) is orphaned -- same "explicit retirement only, silence is not retirement"
    discipline the ledger itself enforces (diagram_gate.check_dangling_visual_retirement's own
    docstring). The reverse direction (manifest names a file that was never created) is the
    mirror-image mistake. Messages moved byte-for-byte from `_load_and_check_manifest`. Pure."""
    problems: list[str] = []
    for screen in sorted(disk_wireframe_screens - manifest_wireframe_screens - retired_wireframe_screens):
        problems.append(
            f"{DRAFT_WIREFRAMES_DIR}/{screen}.html exists on disk but is not listed in "
            "manifest.json's wireframes (or retired_wireframe_screens)"
        )
    for screen in sorted(manifest_wireframe_screens - disk_wireframe_screens):
        problems.append(
            f"manifest.json lists wireframe {screen!r} but {DRAFT_WIREFRAMES_DIR}/{screen}.html "
            "does not exist -- create it"
        )
    for name in sorted(disk_diagram_names - manifest_diagram_names - retired_diagram_names):
        problems.append(
            f"{DRAFT_DIAGRAMS_DIR}/{name}.mmd exists on disk but is not listed in manifest.json's "
            "diagrams (or retired_diagram_names)"
        )
    for name in sorted(manifest_diagram_names - disk_diagram_names):
        problems.append(
            f"manifest.json lists diagram {name!r} but {DRAFT_DIAGRAMS_DIR}/{name}.mmd does not "
            "exist -- create it"
        )
    return problems


# --- check-plan-citations-stop.mjs's remaining hand-ported half (Task 9, 2026-09-29) ---

_LIVE_STATUSES = frozenset({"active", "revised"})


def ac_id_existence_kind_problems(
    label: str, ac_ids: list[str], by_id: dict[str, dict[str, Any] | None]
) -> list[str]:
    """Existence+kind validity ONLY -- an id must exist in the ledger and be
    kind='acceptance_criterion'. Shared by `check_ac_id_citation` below AND
    `diagram_gate.check_plan_linkage`'s own step-side citation check: a task review (2026-09-29)
    caught the two independently retyping this exact one-liner with the exact same rejection
    message -- a real, not hypothetical, drift risk (this rule's own liveness-branching sibling
    below was already revised once in production, 2026-08-31, for the mixed-citation case; nothing
    ties two copies of this simpler check together the same way). Deliberately narrower than
    `check_ac_id_citation` -- no liveness branching lives here, since `check_plan_linkage`'s OWN
    liveness/carryover logic (the part that genuinely IS entangled with its coded_run_id/prior-step
    state, and stays untouched by this extraction) needs to run only when THIS check passes. Pure.
    """
    bad = [i for i in ac_ids if by_id.get(i) is None or by_id[i].get("kind") != "acceptance_criterion"]
    if not bad:
        return []
    return [
        f"{label}: cites {', '.join(bad)} which is not an acceptance criterion in the ledger "
        "-- copy ids exactly from the approved Specification"
    ]


def check_ac_id_citation(
    label: str, ac_ids: list[str], ledger_entries: list[dict[str, Any]], require_live: bool
) -> list[str]:
    """Citation-validity for an id list that names acceptance criteria (a plan step's or a
    diagram's own ac_ids) -- moved from check-plan-citations-stop.mjs's own hand-ported `checkAcIds`
    (itself hand-derived from diagram_gate.py's `check_plan_linkage`): every id must exist in the
    ledger and be kind='acceptance_criterion' (delegated to `ac_id_existence_kind_problems` above,
    shared with `check_plan_linkage`); when `require_live` is set (plan steps, which may only cite
    currently-live work), every id must also be status active/revised -- a diagram may cite a
    deferred id too (existence+kind only matters there, same as check_wireframe_ac_ids's own
    citation-validity-only scope above).

    Deliberately the SIMPLER of the two liveness checks this pipeline has for plan-step ac_ids:
    `check_plan_linkage` itself additionally distinguishes "every cited criterion is non-live" (drop
    the whole step -- a different message) from "only some are" (the mixed-citation message) as
    part of its own coverage-side/rework-forbidden logic -- explicitly out of scope for this
    extraction (a separate, later task's job; see this file's own header). This function reports
    ANY non-live id the same way, exactly matching what the hand-ported JS always did -- so
    `diagram_gate.check_plan_linkage`'s own liveness/carryover branching is NOT rewired to call
    this; only its existence/kind sub-check is shared (via `ac_id_existence_kind_problems`). Pure.
    """
    by_id = {e.get("id"): e for e in ledger_entries}
    existence_problems = ac_id_existence_kind_problems(label, ac_ids, by_id)
    if existence_problems:
        return existence_problems
    if not require_live:
        return []
    non_live = [i for i in ac_ids if by_id[i].get("status") not in _LIVE_STATUSES]
    if not non_live:
        return []
    return [
        f"{label}: cites {', '.join(non_live)}, which {'is' if len(non_live) == 1 else 'are'} "
        "retired or deferred -- ac_ids may only name LIVE criteria; a retired criterion's "
        "delivered artifacts belong in removes_ids instead, and deferred scope must not be "
        "planned at all"
    ]


def check_retired_step_ids(
    retired_step_ids: list[str], ledger_entries: list[dict[str, Any]], touched_step_ids: set[str]
) -> list[str]:
    """retired_step_ids validity -- moved from spec_ledger.py's `sync_plan_ledger` (root-caused live
    2026-09-19, income-investor session 5c555dac, plan lap 1: a plan step id retired that was never
    a real ledger entry). Every id must already be a ledger entry of kind='plan_step', and must not
    also be revised in this same draft (`touched_step_ids` -- ids this draft's own plan_steps cite
    by id; revise-or-retire, never both). This is the VALIDITY half only -- the actual
    status-flip-to-'retired' mutation stays in sync_plan_ledger itself, the authoritative, stateful
    ledger writer this module has no business duplicating. Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    problems: list[str] = []
    for step_id in retired_step_ids:
        entry = by_id.get(step_id)
        if entry is None:
            problems.append(f"retired_step_ids cites {step_id!r}, which does not exist in the ledger")
        elif entry.get("kind") != "plan_step":
            problems.append(f"retired_step_ids cites {step_id!r}, which is not a plan step id")
        elif step_id in touched_step_ids:
            problems.append(
                f"retired_step_ids cites {step_id!r}, but this draft also revises it -- a step "
                "cannot be both revised and retired in the same draft"
            )
    return problems


# --- Task 12 (2026-09-30): check_plan_linkage's remaining un-shared halves (coverage-side/
# rework-forbidden, ported per a task review's judgment call -- see check-plan-citations-stop.mjs's
# own header for the exact comment that call was based on -- and the run_id-gated removal-side
# demand, now portable via AIDW_RUN_ID/Task 5), plus three previously-un-hooked-but-portable checks
# that were plain misses rather than deliberate exclusions: check_dangling_visual_retirement,
# removes_ids existence/retired-status validity, and the plan-step id-missing/id-collision guard.

def own_ac_ids_from_specification(specification: dict[str, Any] | None) -> set[str]:
    """Moved from spec_ledger.py (2026-09-30, Task 12) so this stdlib-only module has no
    dependency on spec_ledger.py for check_plan_step_coverage_and_rework's own_ac_ids need;
    spec_ledger.py re-exports this exact name (`from .gates.wireframe_linkage_checks import
    own_ac_ids_from_specification`) so every existing `spec_ledger.own_ac_ids_from_specification(...)`
    caller is unaffected -- a straight code MOVE, not a fork. This ticket's own approved
    Specification's AC ids (schemas.Specification shape: {id, kind, ...} nested under
    user_stories[].acceptance_criteria[]) -- the ledger-resolved ids sync_ledger already wrote back
    onto the draft in place, so these are real US-####.# ids, not placeholders. Pure."""
    specification = specification or {}
    return {
        ac.get("id")
        for story in (specification.get("user_stories") or [])
        for ac in (story.get("acceptance_criteria") or [])
    }


def eligible_ac_ids(entries: list[dict[str, Any]], own_ac_ids: set[str]) -> list[str]:
    """Moved from spec_ledger.py (2026-09-30, Task 12) -- same reasoning as
    own_ac_ids_from_specification above; spec_ledger.py re-exports this exact name so every
    existing `spec_ledger.eligible_ac_ids(...)` caller (diagram_gate.py, write_scope_gate.py) is
    unaffected. The work queue: this ticket's own ACs that are live and have never been delivered
    by a healthy run (no coded_run_id -- stamps are written only by metrics_compute on a
    regression-clean run, and cleared on spec approval when the requirement's wording really
    changed). Completed ACs are deliberately absent: gates must never send delivered work back for
    rework. Pure."""
    return [
        e["id"]
        for e in entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") in _LIVE_STATUSES
        and e.get("id") in own_ac_ids
        and not e.get("coded_run_id")
    ]


def check_dangling_visual_retirement(
    wireframe_refs: list[dict[str, Any]],
    diagram_refs: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    retired_wireframe_screens: set[str],
    retired_diagram_names: set[str],
) -> list[str]:
    """Moved from diagram_gate.py (2026-09-30, Task 12) byte-for-byte -- a plain miss rather than a
    deliberate exclusion (not mentioned in any hook's own "deliberately not ported" comments).
    File-based-editing plan, Part 2 sect. 6 (gap found and closed, user-raised): a wireframe or
    `user_flow` diagram whose every cited AC is now retired is a deleted feature's leftover and
    must be named in retired_wireframe_screens/retired_diagram_names -- mirrors
    check_plan_linkage's own removal side for plan steps ("a step whose every criterion this
    Specification retires is a deleted feature's leftover and must be dropped"), applied to visual
    artifacts instead. A wireframe/diagram with a live citation, or with NO citations at all
    (caught separately by check_wireframe_has_ac_ids), is never flagged here. `er`/`architecture`
    diagrams are exempt -- whole-system views, not retired this way (schemas.ImplementationPlan's
    own retired_diagram_names docstring). Pure.
    """
    retired_ac_ids = {
        e["id"] for e in ledger_entries if e.get("kind") == "acceptance_criterion" and e.get("status") == "retired"
    }
    problems: list[str] = []
    for wf in wireframe_refs:
        ac_ids = wf.get("ac_ids") or []
        if ac_ids and all(i in retired_ac_ids for i in ac_ids) and wf.get("screen") not in retired_wireframe_screens:
            problems.append(
                f"wireframe {wf.get('screen')!r} cites only retired criteria ({', '.join(ac_ids)}) -- "
                "name it in retired_wireframe_screens or fix its citations"
            )
    for d in diagram_refs:
        if d.get("kind") != "user_flow":
            continue
        ac_ids = d.get("ac_ids") or []
        if ac_ids and all(i in retired_ac_ids for i in ac_ids) and d.get("name") not in retired_diagram_names:
            problems.append(
                f"diagram {d.get('name')!r} cites only retired criteria ({', '.join(ac_ids)}) -- "
                "name it in retired_diagram_names or fix its citations"
            )
    return problems


def reopened_or_changed_ac_ids(
    ledger_entries: list[dict[str, Any]], run_id: str, bug_affected_ac_ids: set[str]
) -> set[str]:
    """Moved from diagram_gate.py's private `_reopened_or_changed_ac_ids` (2026-09-30, Task 12) so
    check-diagram-staleness-stop.mjs can compute the exact same trigger set now that AIDW_RUN_ID
    (Task 5) makes `run_id` available to a Stop hook -- previously graph-side only. AC ids this run
    either genuinely changed (first-seen/last-revised this run_id) or reopened via
    bug_affected_ac_ids (wording unchanged, the "reopened" case) -- the citation-scoped trigger set
    for wireframe/user_flow-diagram stale-review enforcement. Pure."""
    changed = {
        e["id"]
        for e in ledger_entries
        if e.get("kind") == "acceptance_criterion"
        and (e.get("first_seen_run_id") == run_id or e.get("last_revised_run_id") == run_id)
    }
    return changed | bug_affected_ac_ids


def check_removes_ids_validity(
    plan_steps: list[dict[str, Any]], ledger_entries: list[dict[str, Any]]
) -> tuple[list[str], set[str]]:
    """Moved from diagram_gate.check_plan_linkage (2026-09-30, Task 12) -- existence+retired-status
    validity for every step's removes_ids entries (a live id there is almost certainly ac_ids/
    removes_ids swapped), plus the resolved removed_ids set (story ids expanded to their retired
    criteria) check_plan_removal_demand below needs. No run_id/coded_run_id needed, unlike the
    demand direction -- a plain miss from hook coverage, not a deliberate exclusion. Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    problems: list[str] = []
    removed_ids: set[str] = set()
    for step in plan_steps:
        step_id = step.get("id") or "?"
        for rid in step.get("removes_ids") or []:
            entry = by_id.get(rid)
            if entry is None:
                problems.append(f"{step_id}: removes_ids cites {rid!r}, which does not exist in the ledger")
                continue
            if entry.get("status") != "retired":
                problems.append(
                    f"{step_id}: removes_ids cites {rid!r}, which is NOT retired -- removal steps "
                    "only ever name retired scope (live work belongs in ac_ids)"
                )
                continue
            removed_ids.add(rid)
            # A story id in removes_ids covers all of its (retired) criteria.
            if entry.get("kind") == "user_story":
                removed_ids.update(
                    e["id"] for e in ledger_entries
                    if e.get("kind") == "acceptance_criterion" and e.get("parent_us_id") == rid
                )
    return problems, removed_ids


def check_plan_removal_demand(
    ledger_entries: list[dict[str, Any]], removed_ids: set[str], run_id: str
) -> list[str]:
    """Moved from diagram_gate.check_plan_linkage (2026-09-30, Task 12) -- the run_id-gated removal
    DEMAND direction (user requirement 2026-08-31, brownfield/greenfield asymmetry): a criterion
    that was DELIVERED by an earlier healthy run (coded_run_id set) and RETIRED this round
    (last_revised_run_id == run_id) has real artifacts in the repo -- tests, implementation, UI,
    navigation -- so some step must name it (or its parent story) in removes_ids (see
    check_removes_ids_validity above for `removed_ids`'s own computation). Needs run_id (Task 5's
    AIDW_RUN_ID) to scope "retired THIS run" -- the reason this direction stayed graph-side only
    until now. Pure."""
    problems: list[str] = []
    delivered_retired = [
        e["id"]
        for e in ledger_entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") == "retired"
        and e.get("last_revised_run_id") == run_id
        and e.get("coded_run_id")
    ]
    for ac_id in delivered_retired:
        if ac_id not in removed_ids:
            problems.append(
                f"{ac_id}: this criterion was DELIVERED by an earlier run and retired this "
                "round -- its code/UI/navigation still exist, so a plan step must name it "
                "(or its parent story) in removes_ids and describe the removal work"
            )
    return problems


def check_plan_step_coverage_and_rework(
    plan_steps: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    eligible_ac_id_list: list[str],
    prior_steps_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Moved from diagram_gate.check_plan_linkage (2026-09-30, Task 12; see this module's own
    header and check-plan-citations-stop.mjs's own comment for why these two were deliberately
    left un-ported until now) -- coverage-side (every ELIGIBLE ac id -- this ticket's own, live,
    never delivered by a healthy run, see eligible_ac_ids above -- is cited by >=1 step; completed
    criteria need no step) and rework-forbidden (a NEW or CHANGED step -- vs the prior approved
    plan -- citing only already-delivered criteria is rework the pipeline forbids; verbatim
    carryovers are exempt because ticket mode requires restating them).

    Silently skips (does not itself report) any step that fails existence/kind or is entirely
    non-live -- diagram_gate.check_plan_linkage's own inline loop already reports those
    (ac_id_existence_kind_problems / its own liveness check), and check_ac_id_citation reports the
    plan-step ac_ids half of the same thing for the Stop-hook path; this function only tracks
    which live criteria are validly cited, to feed the coverage/rework rules. Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    cited_live: set[str] = set()
    problems: list[str] = []
    for step in plan_steps:
        step_id = step.get("id") or "?"
        ac_ids = step.get("ac_ids") or []
        if not ac_ids or ac_id_existence_kind_problems(step_id, ac_ids, by_id):
            continue
        live = [i for i in ac_ids if by_id[i].get("status") in _LIVE_STATUSES]
        if not live:
            continue
        prior = prior_steps_by_id.get(step.get("id") or "")
        carryover = prior is not None and prior.get("description") == step.get("description")
        if not carryover:
            undelivered = [i for i in live if not by_id[i].get("coded_run_id")]
            if not undelivered:
                problems.append(
                    f"{step_id}: is new/changed but cites only already-delivered criteria "
                    f"({', '.join(live)}) -- completed criteria are never re-planned; carry the "
                    "prior step over verbatim or drop it"
                )
                continue
        cited_live.update(live)
    for ac_id in eligible_ac_id_list:
        if ac_id not in cited_live:
            problems.append(
                f"{ac_id}: this ticket's undelivered criterion is cited by no plan step -- every "
                "criterion awaiting delivery needs at least one step (ac_ids) that fulfils it"
            )
    return problems


def check_plan_step_id_guard(step_id: str | None, entry: dict[str, Any] | None) -> str | None:
    """Moved from spec_ledger.sync_plan_ledger (2026-09-30, Task 12) -- the plan-step id-missing/
    id-collision guard: every draft plan step must carry its own id, that id must not collide with
    a non-plan-step ledger entry (kind mismatch), and must not reuse an already-retired step's id
    (ids are never reused). `entry` is whatever the CALLER already found for `step_id` in its own
    ledger snapshot (sync_plan_ledger's own incremental `_find(updated, step_id)`, mid-loop, so a
    within-draft duplicate id is checked the exact same way it always was; check_plan_step_ids below
    is the batch wrapper a Stop hook uses against a static ledger instead). Returns a single problem
    string, or None if the id is fine -- the ledger MUTATION (revise-or-create) stays in
    sync_plan_ledger itself, the authoritative, stateful ledger writer this module has no business
    duplicating (same division of labor as check_retired_step_ids). Pure."""
    if not step_id:
        return "a plan step is missing its own id"
    if entry is not None and entry.get("kind") != "plan_step":
        return f"plan step id {step_id!r} collides with a non-plan-step ledger entry"
    if entry is not None and entry.get("status") == "retired":
        return f"plan step id {step_id!r} refers to a retired step -- ids are never reused"
    return None


def check_plan_step_ids(
    plan_steps: list[dict[str, Any]], ledger_entries: list[dict[str, Any]]
) -> list[str]:
    """Batch wrapper over check_plan_step_id_guard above, against a STATIC ledger snapshot -- what
    check-plan-citations-stop.mjs actually calls (a Stop hook has no incrementally-mutating ledger
    to check against, only whatever sync_plan_ledger last wrote). Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    problems: list[str] = []
    for step in plan_steps:
        step_id = step.get("id")
        problem = check_plan_step_id_guard(step_id, by_id.get(step_id) if step_id else None)
        if problem:
            problems.append(problem)
    return problems


def run_plan_linkage_checks(
    plan_steps: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    wireframe_refs: list[dict[str, Any]],
    diagram_refs: list[dict[str, Any]],
    retired_wireframe_screens: list[str],
    retired_diagram_names: list[str],
    specification: dict[str, Any] | None,
    prior_plan_steps: list[dict[str, Any]],
    run_id: str | None,
) -> dict[str, list[str]]:
    """check-plan-citations-stop.mjs's own Task 12 additions -- see this module's own header.
    Computed once over the same inputs the CLI wrapper receives, same "no drift" contract as
    run_all_checks/run_citation_validity_checks above."""
    own_ac_ids = own_ac_ids_from_specification(specification)
    prior_steps_by_id = {s.get("id"): s for s in prior_plan_steps if s.get("id")}
    removal_problems, removed_ids = check_removes_ids_validity(plan_steps, ledger_entries)
    demand_problems = check_plan_removal_demand(ledger_entries, removed_ids, run_id) if run_id else []
    return {
        "dangling_visual_retirement_problems": check_dangling_visual_retirement(
            wireframe_refs, diagram_refs, ledger_entries,
            set(retired_wireframe_screens), set(retired_diagram_names),
        ),
        "removes_ids_problems": removal_problems + demand_problems,
        "plan_step_coverage_rework_problems": check_plan_step_coverage_and_rework(
            plan_steps, ledger_entries, eligible_ac_ids(ledger_entries, own_ac_ids), prior_steps_by_id,
        ),
        "plan_step_id_problems": check_plan_step_ids(plan_steps, ledger_entries),
    }


def run_staleness_trigger_check(
    ledger_entries: list[dict[str, Any]], run_id: str, bug_affected_ac_ids: list[str]
) -> dict[str, list[str]]:
    """check-diagram-staleness-stop.mjs's own Task 12 addition -- the per-item (user_flow diagram/
    wireframe) trigger set, output as a plain sorted list (JSON has no set type) under
    `reopened_or_changed_ac_ids`, for the hook's own git-ancestry-based staleness sweep to
    intersect against each item's ac_ids. Computed once, same "no drift" contract as this module's
    other run_*_checks."""
    return {
        "reopened_or_changed_ac_ids": sorted(
            reopened_or_changed_ac_ids(ledger_entries, run_id, set(bug_affected_ac_ids))
        ),
    }


def run_all_checks(
    wireframes: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    ui_related_ac_ids: list[str],
    plan_steps: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Everything the CLI entry point reports, computed once over the same inputs -- the same
    function a unit test can call directly, so the CLI wrapper below has no logic of its own to
    drift from what a caller importing this module gets."""
    ui_related_set = set(ui_related_ac_ids)
    return {
        "wireframe_ac_ids": check_wireframe_ac_ids(wireframes, ledger_entries),
        "wireframe_has_ac_ids": check_wireframe_has_ac_ids(wireframes),
        "ui_wireframe_coverage": check_ui_wireframe_coverage(ui_related_set, wireframes),
        "plan_step_wireframe_coverage": check_plan_step_wireframe_coverage(plan_steps, wireframes),
    }


def run_citation_validity_checks(
    plan_steps: list[dict[str, Any]],
    diagram_refs: list[dict[str, Any]],
    retired_step_ids: list[str],
    ledger_entries: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """check-plan-citations-stop.mjs's own remaining hand-ported half (checkAcIds/retired_step_ids
    validity subset) -- see check_ac_id_citation/check_retired_step_ids for the two checks
    themselves. Computed once over the same inputs the CLI wrapper receives, same "no drift"
    contract as run_all_checks above. Steps/diagrams with no ac_ids at all are skipped here -- "no
    ac_ids and not kind=infrastructure" is a separate, already-hand-ported structural check this
    task does not touch (see this file's own header)."""
    step_problems: list[str] = []
    for step in plan_steps:
        ac_ids = step.get("ac_ids") or []
        if ac_ids:
            step_problems += check_ac_id_citation(step.get("id") or "?", ac_ids, ledger_entries, True)
    diagram_problems: list[str] = []
    for d in diagram_refs:
        ac_ids = d.get("ac_ids") or []
        if ac_ids:
            diagram_problems += check_ac_id_citation(f"diagram {d.get('name')!r}", ac_ids, ledger_entries, False)
    touched_step_ids = {s.get("id") for s in plan_steps if s.get("id")}
    return {
        "step_ac_id_problems": step_problems,
        "diagram_ac_id_problems": diagram_problems,
        "retired_step_id_problems": check_retired_step_ids(retired_step_ids, ledger_entries, touched_step_ids),
    }


def run_schema_checks(
    wireframe_html: list[dict[str, Any]],
    disk_wireframe_screens: list[str],
    manifest_wireframe_screens: list[str],
    retired_wireframe_screens: list[str],
    disk_diagram_names: list[str],
    manifest_diagram_names: list[str],
    retired_diagram_names: list[str],
) -> dict[str, list[str]]:
    """check-plan-schema-stop.mjs's own remaining hand-ported halves -- see check_wireframes_content/
    check_manifest_orphans for the two checks themselves. Computed once, same "no drift" contract as
    run_all_checks above."""
    return {
        "wireframe_content_problems": check_wireframes_content(wireframe_html),
        "manifest_orphan_problems": check_manifest_orphans(
            set(disk_wireframe_screens), set(manifest_wireframe_screens), set(retired_wireframe_screens),
            set(disk_diagram_names), set(manifest_diagram_names), set(retired_diagram_names),
        ),
    }


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.gates.wireframe_linkage_checks` (no sandbox,
    no DB -- every function here is pure).

    Also re-proves the sandbox-image STAGING COPY (sandbox-image/hooks/wireframe_linkage_checks.py,
    what the Dockerfile actually COPYs into /opt/aidw-hooks/ -- see check-plan-citations-stop.mjs's
    own header for why a physical copy exists at all: Docker's build context there is
    agent/sandbox-image, which cannot COPY a path outside itself) is byte-identical to THIS file.
    Skipped, not failed, when the staged copy is absent -- this file is also imported standalone by
    diagram_gate.py in contexts (packaging, a checkout without sandbox-image/) where that path was
    never expected to exist."""
    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "wireframe_linkage_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/wireframe_linkage_checks.py hooks/wireframe_linkage_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    ledger = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0002.1", "kind": "user_story", "status": "active"},
    ]
    assert check_wireframe_ac_ids(
        [{"screen": "task-list", "ac_ids": ["US-0001.1"]}], ledger
    ) == []
    assert any("bogus" in p for p in check_wireframe_ac_ids(
        [{"screen": "task-list", "ac_ids": ["bogus"]}], ledger
    ))
    assert any("US-0002.1" in p for p in check_wireframe_ac_ids(
        [{"screen": "task-list", "ac_ids": ["US-0002.1"]}], ledger  # a user_story, not an AC
    ))

    assert check_wireframe_has_ac_ids([{"screen": "task-list", "ac_ids": ["US-0001.1"]}]) == []
    assert any("task-list" in p for p in check_wireframe_has_ac_ids(
        [{"screen": "task-list", "ac_ids": []}]
    ))
    assert any("task-list" in p for p in check_wireframe_has_ac_ids(
        [{"screen": "task-list"}]  # field absent entirely
    ))

    assert check_ui_wireframe_coverage({"US-0001.1"}, []) and "US-0001.1" in check_ui_wireframe_coverage({"US-0001.1"}, [])[0]
    assert check_ui_wireframe_coverage({"US-0001.1"}, [{"screen": "task-list", "ac_ids": ["US-0001.1"]}]) == []
    assert check_ui_wireframe_coverage(set(), [{"screen": "task-list", "ac_ids": []}]) == []

    # check_plan_step_wireframe_coverage: the PlanStep-side half.
    assert check_plan_step_wireframe_coverage(
        [{"id": "PS-1", "ui_related": True, "ac_ids": ["US-0001.1"]}],
        [{"screen": "task-list", "ac_ids": ["US-0001.1"]}],
    ) == [], "a ui_related step whose ac_id IS cited by a wireframe is fine"
    assert any(
        "PS-1" in p
        for p in check_plan_step_wireframe_coverage(
            [{"id": "PS-1", "ui_related": True, "ac_ids": ["US-0001.1"]}], []
        )
    ), "a ui_related step with no covering wireframe is flagged"
    assert check_plan_step_wireframe_coverage(
        [{"id": "PS-2", "ui_related": False, "ac_ids": ["US-0001.1"]}], []
    ) == [], "a non-ui_related step is never demanded to have a wireframe"
    assert check_plan_step_wireframe_coverage(
        [{"id": "PS-3", "ui_related": True, "kind": "infrastructure", "ac_ids": []}], []
    ) == [], "a ui_related infrastructure step with no ac_ids can't be matched, and isn't flagged"

    # run_all_checks: same shape the --check-hook CLI mode emits.
    result = run_all_checks(
        wireframes=[{"screen": "task-list", "ac_ids": ["US-0001.1"]}],
        ledger_entries=ledger,
        ui_related_ac_ids=["US-0001.1"],
        plan_steps=[{"id": "PS-1", "ui_related": True, "ac_ids": ["US-0001.1"]}],
    )
    assert result == {
        "wireframe_ac_ids": [], "wireframe_has_ac_ids": [], "ui_wireframe_coverage": [],
        "plan_step_wireframe_coverage": [],
    }, result

    # --- check_wireframe (Task 9: moved from diagram_gate.py) ---
    ok_html = "<html><body><style>body{font-family:sans-serif}</style><div>Login</div></body></html>"
    assert check_wireframe("login", ok_html) is None
    assert check_wireframe("bad name!", ok_html) is not None
    assert check_wireframe("s", "<div><script>alert(1)</script></div>") is not None
    assert check_wireframe("s", '<div onclick=go()>x</div>') is not None
    assert check_wireframe("s", '<img src="https://cdn.example.com/x.png">') is not None
    assert check_wireframe("s", "<div>" + "x" * MAX_WIREFRAME_BYTES + "</div>") is not None
    assert check_wireframe("s", "just words, no markup") is not None
    # prose containing "conversion=" must NOT false-positive the on*= handler check
    assert check_wireframe("s", "<div>conversion=42%</div>") is None

    # check_wireframes_content: the bulk wrapper check-plan-schema-stop.mjs actually calls.
    assert check_wireframes_content([{"screen": "login", "html_source": ok_html}]) == []
    assert any("bad" in p or "must match" in p for p in check_wireframes_content(
        [{"screen": "bad name!", "html_source": ok_html}]
    ))

    # --- check_manifest_orphans (Task 9: the pure half of _load_and_check_manifest) ---
    assert check_manifest_orphans({"login"}, {"login"}, set(), set(), set(), set()) == [], (
        "a wireframe on disk AND in the manifest is never orphaned"
    )
    assert any("login" in p and "not listed" in p for p in check_manifest_orphans(
        {"login"}, set(), set(), set(), set(), set()
    )), "a wireframe on disk but absent from manifest.json AND retired_wireframe_screens is orphaned"
    assert check_manifest_orphans({"login"}, set(), {"login"}, set(), set(), set()) == [], (
        "explicitly retired -- not orphaned"
    )
    assert any("does not exist" in p for p in check_manifest_orphans(
        set(), {"login"}, set(), set(), set(), set()
    )), "manifest.json names a wireframe never created on disk"
    assert any("data-model" in p and "not listed" in p for p in check_manifest_orphans(
        set(), set(), set(), {"data-model"}, set(), set()
    )), "same sweep, diagram side: a .mmd on disk not in manifest.json is orphaned"
    assert check_manifest_orphans(set(), set(), set(), {"data-model"}, {"data-model"}, set()) == []

    # --- ac_id_existence_kind_problems (Task 9 fix-review: the shared existence/kind sub-check
    # both check_ac_id_citation below AND diagram_gate.check_plan_linkage now call, closing a
    # real duplication a task review caught -- two copies of the identical one-liner/message). ---
    plan_ledger = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0001.3", "kind": "acceptance_criterion", "status": "deferred"},
        {"id": "US-0002", "kind": "user_story", "status": "active"},
    ]
    plan_by_id = {e["id"]: e for e in plan_ledger}
    assert ac_id_existence_kind_problems("PS-1", ["US-0001.1"], plan_by_id) == []
    assert any("US-0009.9" in p and "not an acceptance criterion" in p for p in ac_id_existence_kind_problems(
        "PS-1", ["US-0009.9"], plan_by_id
    )), "an id absent from the ledger entirely"
    assert any("US-0002" in p for p in ac_id_existence_kind_problems("PS-1", ["US-0002"], plan_by_id)), (
        "a user_story id, not an acceptance criterion, fails the kind check"
    )
    assert ac_id_existence_kind_problems("PS-1", ["US-0001.2", "US-0001.3"], plan_by_id) == [], (
        "retired/deferred ids are still a real, existing acceptance criterion -- liveness is a "
        "separate, caller-specific concern this shared sub-check deliberately doesn't touch"
    )

    # --- check_ac_id_citation (Task 9: the checkAcIds JS hand-port's exact behavior) ---
    assert check_ac_id_citation("PS-1", ["US-0001.1"], plan_ledger, True) == []
    assert any("US-0009.9" in p and "not an acceptance criterion" in p for p in check_ac_id_citation(
        "PS-1", ["US-0009.9"], plan_ledger, True
    )), "an id absent from the ledger entirely"
    assert any("US-0002" in p for p in check_ac_id_citation("PS-1", ["US-0002"], plan_ledger, True)), (
        "a user_story id, not an acceptance criterion"
    )
    assert any("US-0001.2" in p and "retired or deferred" in p for p in check_ac_id_citation(
        "PS-1", ["US-0001.1", "US-0001.2"], plan_ledger, True
    )), "require_live=True: a retired id riding alongside a live one is still flagged"
    # require_live=False (diagrams may cite deferred/retired scope): only existence+kind matters.
    assert check_ac_id_citation("diagram 'flow'", ["US-0001.2", "US-0001.3"], plan_ledger, False) == []

    # --- check_retired_step_ids (Task 9: moved from spec_ledger.sync_plan_ledger) ---
    step_ledger = [
        {"id": "PS-1", "kind": "plan_step", "status": "active"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
    ]
    assert check_retired_step_ids(["PS-1"], step_ledger, set()) == []
    assert any("PS-9" in p and "does not exist" in p for p in check_retired_step_ids(
        ["PS-9"], step_ledger, set()
    ))
    assert any("US-0001.1" in p and "not a plan step id" in p for p in check_retired_step_ids(
        ["US-0001.1"], step_ledger, set()
    ))
    assert any("PS-1" in p and "also revises it" in p for p in check_retired_step_ids(
        ["PS-1"], step_ledger, {"PS-1"}
    )), "a step cannot be both revised (touched) and retired in the same draft"

    # --- Task 12 additions ---

    # own_ac_ids_from_specification / eligible_ac_ids (moved from spec_ledger.py)
    spec_doc = {
        "user_stories": [
            {"acceptance_criteria": [{"id": "US-0001.1"}, {"id": "US-0001.2"}]},
            {"acceptance_criteria": [{"id": "US-0002.1"}]},
        ]
    }
    assert own_ac_ids_from_specification(spec_doc) == {"US-0001.1", "US-0001.2", "US-0002.1"}
    assert own_ac_ids_from_specification(None) == set()
    task12_ledger = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "active", "coded_run_id": "r0"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "retired"},
    ]
    assert eligible_ac_ids(task12_ledger, {"US-0001.1", "US-0001.2", "US-0002.1"}) == ["US-0001.1"], (
        "delivered (coded_run_id set) and retired ids are both excluded from the work queue"
    )

    # check_dangling_visual_retirement (moved from diagram_gate.py)
    dangling_ledger = [{"id": "US-0001.1", "kind": "acceptance_criterion", "status": "retired"}]
    assert any("login" in p and "retired_wireframe_screens" in p for p in check_dangling_visual_retirement(
        [{"screen": "login", "ac_ids": ["US-0001.1"]}], [], dangling_ledger, set(), set()
    )), "a wireframe citing only retired criteria, not itself retired, is flagged"
    assert check_dangling_visual_retirement(
        [{"screen": "login", "ac_ids": ["US-0001.1"]}], [], dangling_ledger, {"login"}, set()
    ) == [], "already named in retired_wireframe_screens -- not dangling"
    assert check_dangling_visual_retirement(
        [], [{"name": "flow", "kind": "er", "ac_ids": ["US-0001.1"]}], dangling_ledger, set(), set()
    ) == [], "er/architecture diagrams are exempt from this check"

    # reopened_or_changed_ac_ids (moved from diagram_gate.py's private _reopened_or_changed_ac_ids)
    reopen_ledger = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "first_seen_run_id": "r1", "last_revised_run_id": "r1"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "first_seen_run_id": "r0", "last_revised_run_id": "r0"},
    ]
    assert reopened_or_changed_ac_ids(reopen_ledger, "r1", set()) == {"US-0001.1"}
    assert reopened_or_changed_ac_ids(reopen_ledger, "r1", {"US-0009.9"}) == {"US-0001.1", "US-0009.9"}, (
        "bug_affected_ac_ids is unioned in even though nothing in the ledger names it"
    )

    # check_removes_ids_validity / check_plan_removal_demand (split from check_plan_linkage)
    removes_ledger = [
        {"id": "US-0001", "kind": "user_story", "status": "retired"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "retired", "parent_us_id": "US-0001"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"},
    ]
    removal_problems, removed = check_removes_ids_validity(
        [{"id": "PS-1", "removes_ids": ["US-0001"]}], removes_ledger
    )
    assert removal_problems == [] and removed == {"US-0001", "US-0001.1"}, (
        "a retired story id in removes_ids covers all of its (retired) criteria"
    )
    bad_removal_problems, _ = check_removes_ids_validity(
        [{"id": "PS-2", "removes_ids": ["US-0002.1"]}], removes_ledger
    )
    assert any("NOT retired" in p for p in bad_removal_problems), "a LIVE id in removes_ids is rejected"
    missing_removal_problems, _ = check_removes_ids_validity(
        [{"id": "PS-3", "removes_ids": ["US-9999.9"]}], removes_ledger
    )
    assert any("does not exist" in p for p in missing_removal_problems)
    demand_ledger = [
        {"id": "US-0003.1", "kind": "acceptance_criterion", "status": "retired",
         "last_revised_run_id": "r2", "coded_run_id": "r1"},
    ]
    assert any("DELIVERED" in p for p in check_plan_removal_demand(demand_ledger, set(), "r2")), (
        "delivered-then-retired-this-run with no removes_ids citation is demanded"
    )
    assert check_plan_removal_demand(demand_ledger, {"US-0003.1"}, "r2") == [], "citation satisfies the demand"
    assert check_plan_removal_demand(demand_ledger, set(), "r9") == [], "retired a DIFFERENT run -- not this run's demand"

    # check_plan_step_coverage_and_rework (split from check_plan_linkage)
    coverage_ledger = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "active", "coded_run_id": "r0"},
    ]
    assert check_plan_step_coverage_and_rework(
        [{"id": "PS-1", "description": "build it", "ac_ids": ["US-0001.1"]}],
        coverage_ledger, ["US-0001.1"], {},
    ) == [], "a live, undelivered criterion cited by a step is covered"
    assert any("US-0001.1" in p and "cited by no plan step" in p for p in check_plan_step_coverage_and_rework(
        [], coverage_ledger, ["US-0001.1"], {}
    )), "an eligible criterion with no citing step is flagged"
    assert any("already-delivered" in p for p in check_plan_step_coverage_and_rework(
        [{"id": "PS-9", "description": "NEW description", "ac_ids": ["US-0001.2"]}],
        coverage_ledger, [], {"PS-9": {"id": "PS-9", "description": "OLD description"}},
    )), "a new/changed step citing only already-delivered criteria is rework the pipeline forbids"
    assert check_plan_step_coverage_and_rework(
        [{"id": "PS-9", "description": "same text", "ac_ids": ["US-0001.2"]}],
        coverage_ledger, [], {"PS-9": {"id": "PS-9", "description": "same text"}},
    ) == [], "a verbatim carryover of an already-delivered criterion is exempt"

    # check_plan_step_id_guard / check_plan_step_ids (moved from spec_ledger.sync_plan_ledger)
    assert check_plan_step_id_guard("PS-1", None) is None, "a brand-new id is fine"
    assert check_plan_step_id_guard(None, None) == "a plan step is missing its own id"
    assert check_plan_step_id_guard("US-0001.1", {"id": "US-0001.1", "kind": "acceptance_criterion"}) is not None, (
        "colliding with a non-plan-step ledger entry"
    )
    assert check_plan_step_id_guard("PS-1", {"id": "PS-1", "kind": "plan_step", "status": "retired"}) is not None, (
        "reusing a retired step id"
    )
    id_ledger = [
        {"id": "PS-1", "kind": "plan_step", "status": "retired"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
    ]
    assert any("PS-1" in p and "retired" in p for p in check_plan_step_ids([{"id": "PS-1"}], id_ledger))
    assert any("missing its own id" in p for p in check_plan_step_ids([{"description": "no id"}], id_ledger))
    assert check_plan_step_ids([{"id": "PS-2"}], id_ledger) == [], "a fresh id is fine"

    # run_plan_linkage_checks / run_staleness_trigger_check: same shape the --check-hook CLI emits.
    linkage_result = run_plan_linkage_checks(
        plan_steps=[{"id": "PS-1", "description": "d", "ac_ids": ["US-0001.1"]}],
        ledger_entries=coverage_ledger,
        wireframe_refs=[], diagram_refs=[],
        retired_wireframe_screens=[], retired_diagram_names=[],
        specification={"user_stories": [{"acceptance_criteria": [{"id": "US-0001.1"}]}]},
        prior_plan_steps=[], run_id=None,
    )
    assert linkage_result["plan_step_coverage_rework_problems"] == []
    assert linkage_result["plan_step_id_problems"] == []
    staleness_result = run_staleness_trigger_check(reopen_ledger, "r1", ["US-0009.9"])
    assert staleness_result["reopened_or_changed_ac_ids"] == ["US-0001.1", "US-0009.9"]

    # --- run_citation_validity_checks / run_schema_checks: same shape the --check-hook CLI emits.
    citation_result = run_citation_validity_checks(
        plan_steps=[{"id": "PS-1", "ac_ids": ["US-0009.9"]}],
        diagram_refs=[{"name": "flow", "ac_ids": ["US-0001.2"]}],
        retired_step_ids=["PS-9"],
        ledger_entries=plan_ledger + step_ledger,
    )
    assert any("PS-1" in p for p in citation_result["step_ac_id_problems"])
    assert citation_result["diagram_ac_id_problems"] == []  # US-0001.2 exists+is an AC, retired is fine here
    assert any("PS-9" in p for p in citation_result["retired_step_id_problems"])

    schema_result = run_schema_checks(
        wireframe_html=[{"screen": "login", "html_source": ok_html}],
        disk_wireframe_screens=["login", "orphan"],
        manifest_wireframe_screens=["login"],
        retired_wireframe_screens=[],
        disk_diagram_names=[],
        manifest_diagram_names=[],
        retired_diagram_names=[],
    )
    assert schema_result["wireframe_content_problems"] == []
    assert any("orphan" in p for p in schema_result["manifest_orphan_problems"])

    print("wireframe_linkage_checks self-check: all assertions passed")


def _run_check_hook_cli() -> None:
    # ONE flag, ONE combined contract (Task 9, 2026-09-29) -- every key below is optional; a caller
    # (check-plan-citations-stop.mjs or check-plan-schema-stop.mjs) passes only what its own checks
    # need and reads only the matching output keys back. See this module's own header for the full
    # stdin/stdout shape.
    payload = json.loads(sys.stdin.read())
    ledger_entries = payload.get("ledger_entries") or []
    plan_steps = payload.get("plan_steps") or []
    result = run_all_checks(
        wireframes=payload.get("wireframes") or [],
        ledger_entries=ledger_entries,
        ui_related_ac_ids=payload.get("ui_related_ac_ids") or [],
        plan_steps=plan_steps,
    )
    result.update(run_citation_validity_checks(
        plan_steps=plan_steps,
        diagram_refs=payload.get("diagrams") or [],
        retired_step_ids=payload.get("retired_step_ids") or [],
        ledger_entries=ledger_entries,
    ))
    result.update(run_schema_checks(
        wireframe_html=payload.get("wireframe_html") or [],
        disk_wireframe_screens=payload.get("disk_wireframe_screens") or [],
        manifest_wireframe_screens=payload.get("manifest_wireframe_screens") or [],
        retired_wireframe_screens=payload.get("retired_wireframe_screens") or [],
        disk_diagram_names=payload.get("disk_diagram_names") or [],
        manifest_diagram_names=payload.get("manifest_diagram_names") or [],
        retired_diagram_names=payload.get("retired_diagram_names") or [],
    ))
    # Task 12: check-plan-citations-stop.mjs's own additions -- always computed (cheap over absent/
    # empty inputs); `run_id` gates only the ONE sub-check (the removal-side demand) that genuinely
    # needs it, same as run_plan_linkage_checks/check_plan_removal_demand's own docstring.
    result.update(run_plan_linkage_checks(
        plan_steps=plan_steps,
        ledger_entries=ledger_entries,
        wireframe_refs=payload.get("wireframes") or [],
        diagram_refs=payload.get("diagrams") or [],
        retired_wireframe_screens=payload.get("retired_wireframe_screens") or [],
        retired_diagram_names=payload.get("retired_diagram_names") or [],
        specification=payload.get("specification"),
        prior_plan_steps=payload.get("prior_plan_steps") or [],
        run_id=payload.get("run_id"),
    ))
    # check-diagram-staleness-stop.mjs's own addition -- only computed when it sent a run_id (no
    # AIDW_RUN_ID means the caller never asked for this key; an empty/absent-vs-computed-empty
    # distinction the JS side can tell apart).
    run_id = payload.get("run_id")
    if run_id:
        result.update(run_staleness_trigger_check(
            ledger_entries=ledger_entries,
            run_id=run_id,
            bug_affected_ac_ids=payload.get("bug_affected_ac_ids") or [],
        ))
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    if "--check-hook" in sys.argv:
        _run_check_hook_cli()
    else:
        _demo()
