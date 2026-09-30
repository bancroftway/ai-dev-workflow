"""Pure, dependency-free (stdlib `json`/`os`/`re`/`sys` only) wireframe<->AC/PlanStep linkage
checks -- extracted from `diagram_gate.py` (2026-09-24) specifically so the sandbox's own same-turn
Stop hooks (`check-plan-citations-stop.mjs`, `check-plan-schema-stop.mjs`) can run the REAL checks
by shelling out to `python3` on this ONE file, instead of a hand-ported JavaScript reimplementation
drifting from it.

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
  and `spec_ledger.py`'s `sync_plan_ledger` respectively. Deliberately NOT the same code as
  `check_plan_linkage` itself: that function's own ac_ids validity logic is inseparably fused with
  its coverage-side/rework-forbidden rules (a later, separate task's scope -- see this module's own
  functions' docstrings below for exactly where the two diverge), so `diagram_gate.py` keeps its
  existing, richer `check_plan_linkage` untouched; only `spec_ledger.py`'s `sync_plan_ledger`, whose
  `retired_step_ids` validity loop has no such entanglement, was updated to import from here.

CLI mode (`python3 wireframe_linkage_checks.py --check-hook`, stdin JSON: `{"wireframes": [...],
"ledger_entries": [...], "ui_related_ac_ids": [...], "plan_steps": [...], "diagrams": [...],
"retired_step_ids": [...], "wireframe_html": [...], "disk_wireframe_screens": [...],
"manifest_wireframe_screens": [...], "retired_wireframe_screens": [...], "disk_diagram_names": [...],
"manifest_diagram_names": [...], "retired_diagram_names": [...]}`, EVERY key optional -- a caller
passes only the keys its own checks need, stdout JSON: `{"wireframe_ac_ids": [...],
"wireframe_has_ac_ids": [...], "ui_wireframe_coverage": [...], "plan_step_wireframe_coverage": [...],
"step_ac_id_problems": [...], "diagram_ac_id_problems": [...], "retired_step_id_problems": [...],
"wireframe_content_problems": [...], "manifest_orphan_problems": [...]}`, each value a list of
problem strings) is what the Stop hooks actually invoke -- ONE flag, ONE combined contract; each
hook only reads the output keys its own checks produced.
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


def check_ac_id_citation(
    label: str, ac_ids: list[str], ledger_entries: list[dict[str, Any]], require_live: bool
) -> list[str]:
    """Citation-validity for an id list that names acceptance criteria (a plan step's or a
    diagram's own ac_ids) -- moved from check-plan-citations-stop.mjs's own hand-ported `checkAcIds`
    (itself hand-derived from diagram_gate.py's `check_plan_linkage`): every id must exist in the
    ledger and be kind='acceptance_criterion'; when `require_live` is set (plan steps, which may
    only cite currently-live work), every id must also be status active/revised -- a diagram may
    cite a deferred id too (existence+kind only matters there, same as check_wireframe_ac_ids's own
    citation-validity-only scope above).

    Deliberately the SIMPLER of the two liveness checks this pipeline has for plan-step ac_ids:
    `check_plan_linkage` itself additionally distinguishes "every cited criterion is non-live" (drop
    the whole step -- a different message) from "only some are" (the mixed-citation message) as
    part of its own coverage-side/rework-forbidden logic -- explicitly out of scope for this
    extraction (a separate, later task's job; see this file's own header). This function reports
    ANY non-live id the same way, exactly matching what the hand-ported JS always did -- so
    `diagram_gate.check_plan_linkage` is NOT rewired to call this; it keeps its own richer,
    untouched implementation. Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    bad = [i for i in ac_ids if by_id.get(i) is None or by_id[i].get("kind") != "acceptance_criterion"]
    if bad:
        return [
            f"{label}: cites {', '.join(bad)} which is not an acceptance criterion in the ledger "
            "-- copy ids exactly from the approved Specification"
        ]
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

    # --- check_ac_id_citation (Task 9: the checkAcIds JS hand-port's exact behavior) ---
    plan_ledger = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0001.3", "kind": "acceptance_criterion", "status": "deferred"},
        {"id": "US-0002", "kind": "user_story", "status": "active"},
    ]
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
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    if "--check-hook" in sys.argv:
        _run_check_hook_cli()
    else:
        _demo()
