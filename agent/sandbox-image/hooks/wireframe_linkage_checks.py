"""Pure, dependency-free (stdlib `json`/`sys` only) wireframe<->AC/PlanStep linkage checks --
extracted from `diagram_gate.py` (2026-09-24) specifically so the sandbox's own same-turn Stop hook
(`sandbox-image/hooks/check-plan-citations-stop.mjs`) can run the REAL check by shelling out to
`python3` on this ONE file, instead of a hand-ported JavaScript reimplementation drifting from it
(the exact drift risk `check-plan-schema-stop.mjs`'s own header still calls out for the wireframe
byte-size/forbidden-pattern half, which stays hand-ported: those need the wireframe's full HTML
body, not just its metadata, and are out of scope for this extraction).

Why a subprocess, not a straight import: `diagram_gate.py` (and everything upstream of it --
`chat_model.py`, `sandbox/provider.py`, `repo_files.py`, this pipeline's DB drivers) lives entirely
outside the sandbox and is never copied into the image; even if it were, importing that module
pulls in a dependency graph with no business running inside a per-turn Stop hook. This module is
the answer to "duplicate the logic, or ship the real thing": everything below is provably pure (a
list of dicts in, a list of strings out, confirmed by this file's own self-check) and needs nothing
beyond what `python3`'s standard library already provides, so it is the ONE file this pipeline
ships into `/opt/aidw-hooks/` and calls directly -- the real implementation, not a port of it.

`diagram_gate.py` imports every name below UNCHANGED (this is a pure code-move, not a fork); its
own self-check is the proof this extraction changed no behavior.

CLI mode (`python3 wireframe_linkage_checks.py --check-hook`, stdin JSON: `{"wireframes": [...],
"ledger_entries": [...], "ui_related_ac_ids": [...], "plan_steps": [...]}`, stdout JSON:
`{"wireframe_ac_ids": [...], "wireframe_has_ac_ids": [...], "ui_wireframe_coverage": [...],
"plan_step_wireframe_coverage": [...]}`, each value a list of problem strings) is what the Stop
hook actually invokes.
"""

from __future__ import annotations

import json
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

    print("wireframe_linkage_checks self-check: all assertions passed")


def _run_check_hook_cli() -> None:
    payload = json.loads(sys.stdin.read())
    result = run_all_checks(
        wireframes=payload.get("wireframes") or [],
        ledger_entries=payload.get("ledger_entries") or [],
        ui_related_ac_ids=payload.get("ui_related_ac_ids") or [],
        plan_steps=payload.get("plan_steps") or [],
    )
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    if "--check-hook" in sys.argv:
        _run_check_hook_cli()
    else:
        _demo()
