"""The stage tab strip's and gate screens' view models, built server-side so the frontend only
renders them.

Every rule for what the tab strip and a verification gate show -- whether a tab is open, its
status tone, the gate icon's look and each check row's status/text/tone -- lives here (it used to
live in AppShell.tsx, src/lib/gate-rows.ts and GateButton.tsx). The frontend iterates
tabs/columns/sections/groups/rows/cells and maps a `tone` to a CSS class; it knows nothing about
stages, checks or statuses.

Pure: callers (sessions_api's /sessions/{id}/tabs and /gates/{tab_id} endpoints) gather the graph
state, the session row's current_stage, pending interrupt payloads, running phases, attempt
history and repo insights and pass them in.

`cd agent && uv run python -m src.gate_view`.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from . import config
from .gates.checks import AUDIT_MODES, Check
from .pipeline_layout import PIPELINE, Pipeline, TabSpec

# All of the gate screens' display copy. `{name}`-style placeholders, filled here.
GATE_TEXT: dict[str, Any] = {
    # Status column of one check row, by row state (_derive_rows).
    "row_status": {
        "passed": "Passed",
        "failed": "Failed",
        "infra": "Couldn't run (platform issue)",
        "skipped": "Skipped",
        "advisory": "Heads-up (doesn't block)",
        "advisory_failed": "Heads-up (doesn't block)",  # a failed advisory-mode check
        "no_sandbox": "Not run: no sandbox",
        "no_detail_passed": "Passed, no per-check detail recorded",
        "no_detail_failed": "Failed, no per-check detail recorded",
        "audit_off": "Skipped: no audit",
        "not_reached": "Not reached (an earlier check stopped it)",
        "not_recorded": "Not recorded",
        "policy_off": "Skipped: not enforced in {mode}",
        "policy_off_mode_fallback": "this mode",
        "approved_earlier": "Approved earlier, not re-verified this run",
        "will_run": "Will run",
        "lap_note": "lap {lap} (redraft in progress)",
    },
    # Effect column: Check.mode in plain words.
    "check_effect": {
        "blocking": "Stops the stage",
        "collected": "Stops the stage (reported together)",
        "advisory": "Informational only",
    },
    # The tab-strip gate icon's status, its badge glyph and its aria-label/title.
    "icon_status": {
        "off": "not enforced",
        "unknown": "mode not known yet",
        "not_run": "not run yet",
        "verifying": "verifying",
        "passed": "passed",
        "failed": "failed",
        "warn": "needs attention",
        "no_sandbox": "cannot verify (no sandbox)",
        "advisory_failure": "advisory failure",
        "approved_earlier": "approved earlier, not re-verified",
        "awaiting_approval": "checks passed, waiting for your approval",
    },
    "icon_badge": {"off": "", "unknown": "?", "not_run": "", "verifying": "…", "passed": "", "failed": "✕", "warn": "!"},
    "icon_aria": "{name} verification: {policy}, {status}",
    "policy_unknown": "mode unknown",
    # The gate screen.
    "title": "{name} verification",
    "subtitle": "Every deterministic check this gate runs, and what the latest (or a past) attempt recorded.",
    "legend": [
        {"text": "A check that ", "bold": False},
        {"text": "stops the stage", "bold": True},
        {"text": " sends the work back for another attempt when it fails. ", "bold": False},
        {"text": "Informational", "bold": True},
        {"text": " checks never block: a heads-up just flags something worth knowing, and needs nothing from you.", "bold": False},
    ],
    "mode": "Mode: {mode}",
    "mode_unknown": "not known yet",
    "policy": "Policy: {policy}",
    "policy_none": "—",
    "lap": "lap {lap} of {max}",
    "verdict": "Verdict: {verdict}",
    "verdict_values": {"none": "no verdict yet", "cannot_verify": "cannot verify", "passed": "passed", "failed": "failed"},
    "attempt": "Attempt",
    "attempt_latest": "Latest",
    "attempt_option": "{n} · {result}",  # the frontend appends the attempt's localized time
    "attempt_result": {"passed": "passed", "failed": "failed"},
    "fail_rate": "fails in {pct}% of runs in this repo",
    # `style` is presentation only (the frontend maps it to classes, like a tone).
    "columns": [
        {"key": "n", "label": "#", "style": "dim"},
        {"key": "check", "label": "Check", "style": "strong"},
        {"key": "what", "label": "What it verifies", "style": "muted"},
        {"key": "effect", "label": "Effect", "style": "muted"},
        {"key": "when", "label": "When it runs", "style": "muted"},
        {"key": "status", "label": "Status", "style": "nowrap"},
        {"key": "detail", "label": "Detail", "style": "wide"},
    ],
    "group_heading": {
        "wrapper": "Platform checks around every verification",
        "uncatalogued": "Reported but not in this gate's catalog",
    },
    "uncatalogued_badge": "uncatalogued",
}

# Worst-first across a tab's gated stages (Quality has two).
_STATUS_RANK = ("off", "unknown", "not_run", "passed", "warn", "failed", "verifying")
_REPORTED_TONE = {"passed": "pass", "failed": "fail", "infra": "warn", "skipped": "muted", "advisory": "warn"}


def running_phases(events: list[Any], run_active: bool | None) -> dict[str, str]:
    """stage -> the node currently executing in it, from run events (RunEvent-like: run_id/stage/
    node/type), scoped to the latest run_id. Same rule as the frontend's old computeRunningPhases:
    an explicit run_active=False (nothing attached to this session) means nothing is running."""
    if run_active is False or not events:
        return {}
    latest = events[-1].run_id
    open_by_stage: dict[str, str] = {}
    for e in events:
        if not e.stage or not e.node or e.run_id != latest:
            continue
        kind = getattr(e.type, "value", e.type)
        if kind == "node_started":
            open_by_stage[e.stage] = e.node
        elif kind == "node_finished" and open_by_stage.get(e.stage) == e.node:
            del open_by_stage[e.stage]
    return open_by_stage


def _interrupt_verification(interrupts: list[Any], stage_key: str) -> dict[str, Any] | None:
    """An after-submit gate's (tech-stack's) verdict on the last submission, from the pending
    interrupt payload (graph._build_tech_stack_interrupt_extra) -- the same object the frontend's
    InterruptCard reads."""
    for payload in interrupts:
        if isinstance(payload, dict) and payload.get("stage") == stage_key and payload.get("verification"):
            return payload["verification"]
    return None


def _mode_facts(mode: str | None, pipeline: Pipeline) -> tuple[str | None, bool | None]:
    """(mode label, audit on) -- both None while the mode isn't known."""
    found = next((m for m in pipeline.modes if m["id"] == mode), None) if mode else None
    label = (found["label"] if found else mode) if mode else None
    return label, (mode in AUDIT_MODES) if found else None


def _stage_view(spec: Any, state: dict[str, Any], mode: str | None, running: dict[str, str], interrupts: list[Any]) -> dict[str, Any]:
    gate = spec.gate
    policy = gate.policy.get(mode) if mode else None
    st = (state.get("stages") or {}).get(spec.key) or {}
    iv = _interrupt_verification(interrupts, spec.key)
    verdict = iv if iv is not None else st.get("last_verification")
    lap = iv["attempts"] if iv is not None else st.get("verify_cycle_count") or 0
    max_laps = iv["max_attempts"] if iv is not None else st.get("max_verify_cycles") or 0
    T = GATE_TEXT["icon_status"]
    text = None
    if running.get(spec.key) == "verify":
        status = "verifying"
    elif verdict is not None and verdict.get("cannot_verify"):
        status, text = "warn", T["no_sandbox"]
    elif verdict is not None and verdict.get("passed"):
        # The icon shows work going THROUGH the gate: a human-gated stage whose checks passed but
        # that the reviewer hasn't approved yet hasn't gone through -- amber, the reviewer's turn.
        if spec.requires_human_gate and st.get("status") != "approved":
            status, text = "warn", T["awaiting_approval"]
        else:
            status = "passed"
    elif verdict is not None:
        status, text = ("warn", T["advisory_failure"]) if policy == "advisory" else ("failed", None)
    elif policy == "off":
        status = "off"
    elif policy is None:
        status = "unknown"
    elif st.get("status") == "approved":
        status, text = "passed", T["approved_earlier"]
    else:
        status = "not_run"
    return {
        "spec": spec, "policy": policy, "stage_status": st.get("status"), "verdict": verdict,
        "lap": lap, "max_laps": max_laps, "status": status, "status_text": text or T[status],
    }


def _gate_views(tab: TabSpec, state: dict[str, Any], mode: str | None, running: dict[str, str], interrupts: list[Any], pipeline: Pipeline) -> tuple[list[dict[str, Any]], str] | None:
    """(per-stage views, display name) for a tab's gated stages; None when the tab gates nothing."""
    specs = [s for k in tab.stage_keys if (s := pipeline.stage(k)) is not None and s.gate is not None]
    if not specs:
        return None
    stages = state.get("stages") or {}

    # A tab can gate an optional stage (brownfield-spec on Specification) that most sessions never
    # run; intake still creates its StageState as not_started. Once a sibling has progressed, an
    # untouched stage is not part of this session -- listing it would show "Will run" forever and
    # drag the tab's icon to "not run yet".
    def untouched(s: Any) -> bool:
        st = stages.get(s.key)
        return not st or (st.get("status") == "not_started" and st.get("last_verification") is None)

    shown = [s for s in specs if not untouched(s)] or specs
    views = [_stage_view(s, state, mode, running, interrupts) for s in shown]
    return views, (shown[0].label if len(shown) == 1 else tab.label)


def _gate_icon(tab: TabSpec, state: dict[str, Any], mode: str | None, running: dict[str, str], interrupts: list[Any], pipeline: Pipeline) -> dict[str, Any] | None:
    """The tab-strip icon of the gate after `tab`; None when the tab gates nothing."""
    built = _gate_views(tab, state, mode, running, interrupts, pipeline)
    if built is None:
        return None
    views, name = built
    worst = max(views, key=lambda v: _STATUS_RANK.index(v["status"]))  # first max wins
    policies = list(dict.fromkeys(v["policy"] or GATE_TEXT["policy_unknown"] for v in views))
    label = GATE_TEXT["icon_aria"].format(name=name, policy="/".join(policies), status=worst["status_text"])
    return {"icon": {"tone": worst["status"], "badge": GATE_TEXT["icon_badge"][worst["status"]], "label": label}}


def _order_index(key: str | None, pipeline: Pipeline) -> int:
    """Position in run order; legacy keys sort after every real stage (an old session's "exit"
    still reads as past metrics-exit); -1 for anything unknown. Purely ordinal."""
    if not key:
        return -1
    if key in pipeline.order:
        return pipeline.order.index(key)
    legacy = list(pipeline.legacy_labels)
    return len(pipeline.order) + legacy.index(key) if key in legacy else -1


def _tab_tone(tab: TabSpec, state: dict[str, Any], mode: str | None, running: dict[str, str], pipeline: Pipeline) -> str:
    """The tab label's status colour. Green clears on resubmission: intake resets later stages to
    not_started on each fresh run.

    Running (the event stream) is checked first, before any stage state need exist: a mid-run
    reattach can leave `stages` empty while the run is genuinely active, and a non-gated stage
    cycles through ready_for_review between verify attempts, so `status` alone would read
    "awaiting" while it retries. A failed verdict under an advisory policy doesn't block the run,
    so it doesn't paint the tab red."""
    if any(k in running for k in tab.stage_keys):
        return "running"
    stages = state.get("stages") or {}
    present = [(k, stages[k]) for k in tab.stage_keys if stages.get(k) is not None]
    if not present:
        return "none"
    statuses = [s.get("status") for _, s in present]
    if "drafting" in statuses:
        return "running"
    if any(st in ("ready_for_review", "needs_clarification") for st in statuses):
        return "awaiting"

    def blocking_failure(key: str, s: dict[str, Any]) -> bool:
        verdict = s.get("last_verification")
        if verdict is None or verdict.get("passed") or s.get("status") == "approved":
            return False
        spec = pipeline.stage(key)
        return not (mode and spec is not None and spec.gate is not None and spec.gate.policy.get(mode) == "advisory")

    if any(blocking_failure(k, s) for k, s in present):
        return "error"
    return "done" if all(st == "approved" for st in statuses) else "none"


def _tab_enabled(tab: TabSpec, index: int, state: dict[str, Any], running: dict[str, str], current_stage: str | None, pipeline: Pipeline) -> bool:
    """Whether the tab can be opened. Once a stage has ever executed its tab never closes again,
    whatever the run is doing now: the durable current_stage (monotonic, survives failed/completed)
    and the running phases (the event stream) each open a tab on their own, since a stage's own
    state is empty for the whole mid-run reattach gap.
     - the first (landing) tab and stage-less tabs (Overview) are always open;
     - enable_on_review tabs wait for a draft actually ready for review (or clarifying questions),
       or a current_stage PAST the tab's last stage (current_stage == X can mean "X drafting");
     - every other tab opens on that same readiness, on any of its stages past not_started (intake
       pre-creates every stage at not_started), running, or reached by current_stage; once all of
       enable_after are approved (Requirements: tech stack first); or once one of its
       enable_state_keys is present (Quality: test_hardening/metrics_report)."""
    if index == 0 or not tab.stage_keys:
        return True
    keys = tab.stage_keys
    stages = state.get("stages") or {}
    reached = _order_index(current_stage, pipeline)

    def stage(k: str) -> dict[str, Any]:
        return stages.get(k) or {}

    def durable_at_least(target: str | None) -> bool:
        return target is not None and reached >= _order_index(target, pipeline)

    ready = any(stage(k).get("ever_ready_for_review") or stage(k).get("clarifying_questions") for k in keys)
    if tab.enable_on_review:
        last = pipeline.order.index(keys[-1]) if keys[-1] in pipeline.order else -1
        after_last = pipeline.order[last + 1] if 0 <= last < len(pipeline.order) - 1 else None
        return bool(ready) or durable_at_least(after_last)

    def started(k: str) -> bool:
        status = stage(k).get("status")
        return (status if status is not None else "not_started") != "not_started"

    return bool(
        ready
        or any(started(k) for k in keys)
        or any(k in running for k in keys)
        or durable_at_least(keys[0])
        or (tab.enable_after and all(stage(k).get("status") == "approved" for k in tab.enable_after))
        or any(state.get(k) is not None for k in tab.enable_state_keys)
    )


def build_tab_strip(
    state: dict[str, Any], *, code_gen_mode: str | None, running: dict[str, str], interrupts: list[Any],
    current_stage: str | None, pipeline: Pipeline = PIPELINE,
) -> dict[str, Any]:
    """The stage tab strip in descriptor order: each tab's label, whether it can be opened, its
    status tone (done/error/awaiting/running/none) and the icon of the gate after it (None when it
    gates nothing). `current_stage` is the durable session row's."""
    return {"tabs": [
        {
            "tab_id": tab.id,
            "label": tab.label,
            "enabled": _tab_enabled(tab, i, state, running, current_stage, pipeline),
            "tone": _tab_tone(tab, state, code_gen_mode, running, pipeline),
            "gate": _gate_icon(tab, state, code_gen_mode, running, interrupts, pipeline),
        }
        for i, tab in enumerate(pipeline.tabs)
    ]}


def _derive_rows(
    *, checks: tuple[Check, ...] | list[Check], wrapper_checks: tuple[Check, ...] | list[Check],
    verdict: dict[str, Any] | None, policy: str | None, mode_label: str | None, audit_on: bool | None,
    stage_status: str | None, lap: int,
) -> list[dict[str, Any]]:
    """One row per catalog check (stage then wrapper), then one per reported id the catalog
    doesn't know. Each row: id/label/description/mode/condition/group/state/text/tone/detail/
    uncatalogued/lap_note. `stage_status` None = drawing a history attempt."""
    T = GATE_TEXT["row_status"]
    reported_checks = (verdict or {}).get("checks") or []
    reported = {r["id"]: r for r in reported_checks}
    catalog = {c.id: c for c in [*wrapper_checks, *checks]}
    redraft = verdict is not None and not verdict.get("passed") and stage_status == "drafting"
    lap_note = T["lap_note"].format(lap=lap) if redraft else None

    # A reported blocking failure stops the chain: a wrapper failure stops every stage row; a stage
    # failure stops the stage rows after it (catalog order).
    blocking = [r for r in reported_checks if r["status"] in ("failed", "infra") and r["id"] in catalog and catalog[r["id"]].mode == "blocking"]
    wrapper_ids = {c.id for c in wrapper_checks}
    wrapper_stopped = any(r["id"] in wrapper_ids for r in blocking)
    stage_index = {c.id: i for i, c in enumerate(checks)}
    first_stage_stop = min((stage_index[r["id"]] for r in blocking if r["id"] in stage_index), default=math.inf)

    def unreported(c: Check, index: int, group: str) -> tuple[str, str, str]:
        if verdict is not None:
            if verdict.get("cannot_verify"):
                return "no_sandbox", T["no_sandbox"], "warn"
            if verdict.get("checks") is None:  # a verdict recorded before per-check rows existed
                return "no_detail", T["no_detail_passed" if verdict.get("passed") else "no_detail_failed"], "muted"
            if c.needs_audit and audit_on is False:
                return "audit_off", T["audit_off"], "muted"
            reached = not wrapper_stopped and not index > first_stage_stop if group == "stage" else True
            if not reached:
                return "not_reached", T["not_reached"], "muted"
            return "not_recorded", T["not_recorded"], "muted"
        if policy == "off":
            return "policy_off", T["policy_off"].format(mode=mode_label or T["policy_off_mode_fallback"]), "muted"
        if stage_status == "approved":
            return "approved_earlier", T["approved_earlier"], "muted"
        if c.needs_audit and audit_on is False:
            return "audit_off", T["audit_off"], "muted"
        return "will_run", T["will_run"], "muted"

    def row(c: Check, index: int, group: str) -> dict[str, Any]:
        base = {"id": c.id, "label": c.label, "description": c.description, "mode": c.mode,
                "condition": c.condition, "group": group, "lap_note": lap_note}
        r = reported.get(c.id)
        if r is None:
            state, text, tone = unreported(c, index, group)
            return {**base, "state": state, "text": text, "tone": tone, "detail": None, "uncatalogued": False}
        # An advisory-mode check's failure doesn't block: amber, not red.
        if r["status"] == "failed" and c.mode == "advisory":
            text, tone = T["advisory_failed"], "warn"
        else:
            text, tone = T[r["status"]], _REPORTED_TONE[r["status"]]
        return {**base, "state": r["status"], "text": text, "tone": tone, "detail": r.get("detail"),
                "uncatalogued": bool(r.get("uncatalogued"))}

    rows = [row(c, i, "stage") for i, c in enumerate(checks)] + [row(c, i, "wrapper") for i, c in enumerate(wrapper_checks)]
    for r in reported_checks:  # reported ids the catalog doesn't know
        if r["id"] in catalog:
            continue
        rows.append({
            "id": r["id"], "label": r["id"], "description": "", "mode": "", "condition": "", "group": "uncatalogued",
            "lap_note": lap_note, "state": r["status"], "text": T[r["status"]], "tone": _REPORTED_TONE[r["status"]],
            "detail": r.get("detail"), "uncatalogued": True,
        })
    return rows


def _cell(text: str, *, sub: str | None = None, badge: str | None = None, collapsible: bool = False, tone: str | None = None) -> dict[str, Any]:
    return {"text": text, "sub": sub, "badge": badge, "collapsible": collapsible, "tone": tone}


def _render_row(r: dict[str, Any], n: int, stats: dict[str, Any] | None) -> dict[str, Any]:
    detail = r["detail"]
    fail_rate = None
    if stats and stats.get("fails", 0) > 0:
        fail_rate = GATE_TEXT["fail_rate"].format(pct=math.floor(stats["fail_rate"] * 100 + 0.5))
    return {
        "key": r["id"],
        "cells": {
            "n": _cell(str(n)),
            "check": _cell(r["label"], sub=fail_rate, badge=GATE_TEXT["uncatalogued_badge"] if r["uncatalogued"] else None),
            "what": _cell(r["description"]),
            "effect": _cell(GATE_TEXT["check_effect"].get(r["mode"], r["mode"])),
            "when": _cell(r["condition"]),
            "status": _cell(r["text"], sub=r["lap_note"], tone=r["tone"]),
            "detail": _cell(
                detail or "",
                collapsible=bool(detail) and (len(detail) > config.AIDW_GATE_DETAIL_INLINE_CHARS or "\n" in detail),
            ),
        },
    }


def _iso(when: Any) -> str | None:
    """created_at is naive-UTC DATETIME2; say so, or the browser reads it as local time."""
    if isinstance(when, datetime):
        return (when if when.tzinfo else when.replace(tzinfo=UTC)).isoformat()
    return str(when) if when is not None else None


def attempt_id(a: dict[str, Any]) -> str:
    return f"{a['stage']}:{a['run_id']}:{a['attempt']}"


def _section(view: dict[str, Any], *, mode: str | None, attempts: list[dict[str, Any]], insights: dict[str, Any] | None, selected: str | None, pipeline: Pipeline) -> dict[str, Any]:
    gt = GATE_TEXT
    spec = view["spec"]
    mine = [a for a in attempts if a["stage"] == spec.key]
    has_live = view["verdict"] is not None
    ids = [attempt_id(a) for a in mine]
    # No live verdict (stage approved earlier, or reset by a rewind/new run): default to the newest
    # recorded attempt rather than a table of "not re-verified" rows the user must click away from.
    idx = ids.index(selected) if selected in ids else (len(mine) - 1 if not has_live and mine else None)
    attempt = mine[idx] if idx is not None else None

    shown_mode = attempt["code_gen_mode"] if attempt else mode
    mode_label, audit_on = _mode_facts(shown_mode, pipeline)
    policy = attempt["policy"] if attempt else view["policy"]
    verdict = {"passed": attempt["stage_passed"], "checks": attempt["checks"]} if attempt else view["verdict"]
    rows = _derive_rows(
        checks=spec.gate.checks,
        # The wrapper rows come from the verify node; an after-submit gate (tech-stack) runs its
        # checks inside the review gate instead, so they would only ever read "Not recorded" there.
        wrapper_checks=() if spec.gate.timing == "after_submit" else pipeline.wrapper_checks,
        verdict=verdict, policy=policy, mode_label=mode_label, audit_on=audit_on,
        stage_status=None if attempt else view["stage_status"], lap=view["lap"],
    )
    verdict_key = "none" if verdict is None else "cannot_verify" if verdict.get("cannot_verify") else "passed" if verdict.get("passed") else "failed"
    facts = [gt["mode"].format(mode=mode_label or gt["mode_unknown"]), gt["policy"].format(policy=policy or gt["policy_none"])]
    if not attempt and view["max_laps"] > 0:
        facts.append(gt["lap"].format(lap=view["lap"], max=view["max_laps"]))
    facts.append(gt["verdict"].format(verdict=gt["verdict_values"][verdict_key]))

    options = []
    if has_live or not mine:
        options.append({"id": "", "label": gt["attempt_latest"], "time": None, "selected": idx is None})
    for i, a in enumerate(mine):
        options.append({
            "id": ids[i], "selected": idx == i, "time": _iso(a.get("created_at")),
            "label": gt["attempt_option"].format(n=i + 1, result=gt["attempt_result"]["passed" if a["stage_passed"] else "failed"]),
        })

    stats = {c["check_id"]: c for c in (insights or {}).get("checks", []) if c["stage"] == spec.key}
    groups = []
    for group in ("stage", "wrapper", "uncatalogued"):
        group_rows = [r for r in rows if r["group"] == group]
        if group_rows:
            groups.append({
                "heading": None if group == "stage" else gt["group_heading"][group],
                "rows": [_render_row(r, i + 1, stats.get(r["id"])) for i, r in enumerate(group_rows)],
            })
    live = view["verdict"]
    return {
        "key": spec.key,
        "heading": spec.label,
        "facts": facts,
        "feedback": (live.get("feedback") or None) if not attempt and live is not None and not live.get("passed") else None,
        "attempts": {"label": gt["attempt"], "disabled": not mine, "options": options},
        "groups": groups,
    }


def build_gate_screen(
    state: dict[str, Any], tab_id: str, *, code_gen_mode: str | None, running: dict[str, str], interrupts: list[Any],
    attempts: list[dict[str, Any]], insights: dict[str, Any] | None, attempt: str | None = None,
    pipeline: Pipeline = PIPELINE,
) -> dict[str, Any] | None:
    """A tab's gate screen; None for an unknown tab or one that gates nothing. `attempt` is an
    option id from a previous response (`<stage>:<run_id>:<attempt>`); it only applies to the
    section of that stage."""
    tab = next((t for t in pipeline.tabs if t.id == tab_id), None)
    built = _gate_views(tab, state, code_gen_mode, running, interrupts, pipeline) if tab else None
    if built is None:
        return None
    views, name = built
    return {
        "title": GATE_TEXT["title"].format(name=name),
        "subtitle": GATE_TEXT["subtitle"],
        "legend": GATE_TEXT["legend"],
        "columns": [{"key": c["key"], "label": c["label"], "style": c["style"]} for c in GATE_TEXT["columns"]],
        "sections": [
            _section(v, mode=code_gen_mode, attempts=attempts, insights=insights, selected=attempt, pipeline=pipeline)
            for v in views
        ],
    }


def _demo() -> None:
    """`cd agent && uv run python -m src.gate_view`. No DB, no network."""
    import json
    from types import SimpleNamespace

    T = GATE_TEXT["row_status"]

    # -- row states (ported from the retired scripts/check-gate-rows.mjs) --
    def chk(id_: str, mode: str = "blocking", needs_audit: bool = False) -> Check:
        return Check(id_, id_, "", mode, "always", needs_audit)  # type: ignore[arg-type]

    checks = [chk("a"), chk("b"), chk("c", "collected"), chk("aud", "collected", True)]
    wrapper = [chk("wrapper.sandbox")]
    base: dict[str, Any] = dict(checks=checks, wrapper_checks=wrapper, verdict=None, policy="blocking",
                                mode_label="Yolo", audit_on=True, stage_status="not_started", lap=1)
    seen_rows: list[dict[str, Any]] = []

    def derive(**over: Any) -> list[dict[str, Any]]:
        rows = _derive_rows(**{**base, **over})
        seen_rows.extend(rows)
        return rows

    def states(**over: Any) -> dict[str, str]:
        return {r["id"]: r["state"] for r in derive(**over)}

    def rep(id_: str, status: str) -> dict[str, Any]:
        return {"id": id_, "status": status, "detail": None, "source": "x"}

    assert states() == {"a": "will_run", "b": "will_run", "c": "will_run", "aud": "will_run", "wrapper.sandbox": "will_run"}
    assert states(policy="off")["a"] == "policy_off"
    assert derive(policy="off")[0]["text"] == T["policy_off"].format(mode="Yolo")
    assert derive(policy="off", mode_label=None)[0]["text"] == T["policy_off"].format(mode=T["policy_off_mode_fallback"])
    assert states(audit_on=False)["aud"] == "audit_off"
    # approved with no verdict -> "approved earlier"
    assert states(stage_status="approved")["a"] == "approved_earlier"
    # Reported rows win (even over policy off); a blocking failure -> later rows not reached.
    failed_b = {"passed": False, "checks": [rep("wrapper.sandbox", "passed"), rep("a", "passed"), rep("b", "failed")]}
    assert states(verdict=failed_b, policy="off") == {"a": "passed", "b": "failed", "c": "not_reached", "aud": "not_reached", "wrapper.sandbox": "passed"}
    assert states(verdict={"passed": True, "checks": [rep("b", "passed")]}) == {
        "a": "not_recorded", "b": "passed", "c": "not_recorded", "aud": "not_recorded", "wrapper.sandbox": "not_recorded"}
    # A blocking wrapper failure stops every stage row; a collected failure stops nothing.
    assert states(verdict={"passed": False, "checks": [rep("wrapper.sandbox", "failed")]})["a"] == "not_reached"
    assert states(verdict={"passed": False, "checks": [rep("c", "failed")]})["a"] == "not_recorded"
    # Pre-feature verdict (no checks key), cannot_verify.
    assert states(verdict={"passed": False})["a"] == "no_detail"
    assert derive(verdict={"passed": True})[0]["text"] == T["no_detail_passed"]
    assert derive(verdict={"passed": False})[0]["text"] == T["no_detail_failed"]
    assert states(verdict={"passed": False, "cannot_verify": True, "checks": []})["a"] == "no_sandbox"
    assert states(verdict={"passed": True, "checks": []}, audit_on=False)["aud"] == "audit_off"
    # Redraft in progress labels every row with the lap.
    assert all(r["lap_note"] == T["lap_note"].format(lap=2) for r in derive(verdict=failed_b, stage_status="drafting", lap=2))
    assert all(r["lap_note"] is None for r in derive(verdict=failed_b, stage_status="ready_for_review", lap=2))
    # Unknown reported id -> its own uncatalogued row; advisory-mode failure is amber.
    extra = derive(checks=[chk("adv", "advisory"), chk("i"), chk("s"), chk("h")], verdict={"passed": True, "checks": [
        rep("adv", "failed"), rep("new.one", "failed"), rep("i", "infra"), rep("s", "skipped"), rep("h", "advisory")]})
    by_id = {r["id"]: r for r in extra}
    assert by_id["new.one"]["group"] == "uncatalogued" and by_id["new.one"]["uncatalogued"] is True
    assert (by_id["adv"]["tone"], by_id["adv"]["text"]) == ("warn", T["advisory_failed"])
    for id_, s in (("i", "infra"), ("s", "skipped"), ("h", "advisory")):
        assert by_id[id_]["text"] == T[s]
    for r in seen_rows:
        assert r["text"] and "{" not in r["text"], r
    exercised = {r["state"] for r in seen_rows}
    for s in ("will_run", "policy_off", "audit_off", "approved_earlier", "passed", "failed", "infra", "skipped",
              "advisory", "not_reached", "not_recorded", "no_detail", "no_sandbox"):
        assert s in exercised, f"state {s} never exercised"

    # -- icon states (the old stageView) --
    p = PIPELINE
    code = p.stage("minimal-code-to-green")
    plan = p.stage("plan")
    assert code is not None and plan is not None and code.gate and plan.gate

    def icon(spec: Any, st: dict[str, Any] | None, mode: str | None = "draft_verify", running: dict[str, str] | None = None, interrupts: list[Any] | None = None) -> tuple[str, str]:
        v = _stage_view(spec, {"stages": {spec.key: st} if st else {}}, mode, running or {}, interrupts or [])
        return v["status"], v["status_text"]

    I = GATE_TEXT["icon_status"]
    assert icon(plan, {"status": "drafting"}, running={"plan": "verify"})[0] == "verifying"
    assert icon(plan, {"status": "drafting"}, running={"plan": "draft"})[0] == "not_run"
    assert icon(plan, {"last_verification": {"passed": False, "cannot_verify": True}}) == ("warn", I["no_sandbox"])
    assert icon(plan, {"status": "approved", "last_verification": {"passed": True}}) == ("passed", I["passed"])
    assert icon(plan, {"status": "ready_for_review", "last_verification": {"passed": True}}) == ("warn", I["awaiting_approval"])
    assert not code.gate is None and not code.requires_human_gate
    assert icon(code, {"status": "ready_for_review", "last_verification": {"passed": True}})[0] == "passed"
    assert icon(plan, {"last_verification": {"passed": False}}) == ("failed", I["failed"])
    adv_spec, adv_mode = next((s, m) for s in p.stages if s.gate for m, pol in s.gate.policy.items() if pol == "advisory")
    assert icon(adv_spec, {"last_verification": {"passed": False}}, mode=adv_mode) == ("warn", I["advisory_failure"])
    assert code.gate.policy["yolo"] == "off" and icon(code, {"status": "not_started"}, mode="yolo")[0] == "off"
    assert icon(plan, {"status": "not_started"}, mode=None)[0] == "unknown"
    assert icon(plan, {"status": "approved"}) == ("passed", I["approved_earlier"])
    assert icon(plan, {"status": "not_started"})[0] == "not_run"
    # Tech-stack's reopened gate: the interrupt payload's verdict + its own attempt counter win.
    ts = p.stage("tech-stack")
    iv = {"passed": False, "feedback": "langs", "checks": [{"id": "x", "status": "failed"}], "attempts": 2, "max_attempts": 3}
    v = _stage_view(ts, {"stages": {"tech-stack": {"status": "ready_for_review", "verify_cycle_count": 0}}}, "draft_verify", {}, [{"stage": "tech-stack", "verification": iv}])
    assert (v["status"], v["lap"], v["max_laps"], v["verdict"]) == ("failed", 2, 3, iv), v
    assert _stage_view(ts, {"stages": {}}, "draft_verify", {}, [{"stage": "plan", "verification": iv}])["verdict"] is None

    # -- running phases --
    ev = lambda run, stage, node, kind: SimpleNamespace(run_id=run, stage=stage, node=node, type=kind)  # noqa: E731
    events = [ev("r1", "plan", "verify", "node_started"), ev("r2", "spec", "draft", "node_started"),
              ev("r2", "plan", "verify", "node_started"), ev("r2", "plan", "draft", "node_finished")]
    assert running_phases(events, True) == {"spec": "draft", "plan": "verify"}
    assert running_phases(events + [ev("r2", "plan", "verify", "node_finished")], None) == {"spec": "draft"}
    assert running_phases(events, False) == {}

    # -- tab strip: gate icons --
    stages = {k: {"status": "not_started", "last_verification": None} for k in p.order}
    stages["plan"] = {"status": "approved", "last_verification": {"passed": True, "checks": []}}
    stages["specification"] = {"status": "drafting", "last_verification": {"passed": False, "checks": [], "feedback": "fix it"}, "verify_cycle_count": 2, "max_verify_cycles": 3}
    state = {"stages": stages}

    def strip(st: dict[str, Any], *, mode: str | None = "draft_verify", running: dict[str, str] | None = None,
              current_stage: str | None = None) -> dict[str, dict[str, Any]]:
        built = build_tab_strip(st, code_gen_mode=mode, running=running or {}, interrupts=[], current_stage=current_stage)
        json.dumps(built)
        assert [t["tab_id"] for t in built["tabs"]] == [t.id for t in p.tabs], "descriptor order"
        assert all(set(t) == {"tab_id", "label", "enabled", "tone", "gate"} for t in built["tabs"])
        return {t["tab_id"]: t for t in built["tabs"]}

    summ = strip(state)
    by_tab = {k: t["gate"]["icon"] for k, t in summ.items() if t["gate"] is not None}
    gated_tabs = {t.id for t in p.tabs if any(p.stage(k) and p.stage(k).gate for k in t.stage_keys)}  # type: ignore[union-attr]
    assert set(by_tab) == gated_tabs and {"tech-stack", "specification", "plan", "tests", "code", "quality", "report"} <= gated_tabs, by_tab
    assert summ["requirements"]["gate"] is None and summ["overview"]["gate"] is None
    assert summ["plan"]["label"] == "Plan"
    assert by_tab["plan"]["tone"] == "passed" and by_tab["plan"]["badge"] == ""
    assert by_tab["specification"]["tone"] == "failed" and by_tab["specification"]["badge"] == "✕"
    # Untouched optional brownfield-spec is dropped: the label names the one shown stage.
    assert by_tab["specification"]["label"].startswith(p.stage("specification").label + " verification"), by_tab  # type: ignore[union-attr]
    assert by_tab["tests"]["tone"] == "not_run"
    assert strip(state, running={"plan": "verify"})["plan"]["gate"]["icon"]["tone"] == "verifying"
    # All stages untouched -> every gated stage listed (Quality's two stages -> the tab's label).
    quality = strip({"stages": {}}, mode=None)["quality"]["gate"]["icon"]
    assert quality["tone"] == "unknown" and quality["label"].startswith("Quality verification: mode unknown,"), quality

    # -- tab strip: enabled + tone (the frontend's old tabEnabled/stageGroupDot) --
    def enabled(**kw: Any) -> set[str]:
        return {k for k, t in strip(**kw).items() if t["enabled"]}

    def tones(**kw: Any) -> dict[str, str]:
        return {k: t["tone"] for k, t in strip(**kw).items()}

    first, *_ = p.tabs
    stageless = {t.id for t in p.tabs if not t.stage_keys}
    # Fresh session: only the landing tab and stage-less tabs (Overview) open; no colours.
    assert enabled(st={}) == {first.id} | stageless == {"tech-stack", "overview"}
    assert set(tones(st={}).values()) == {"none"}
    pristine = {"stages": {k: {"status": "not_started"} for k in p.order}}
    assert enabled(st=pristine) == {first.id} | stageless and set(tones(st=pristine).values()) == {"none"}
    # The first tab is open even when nothing about it says so.
    assert "tech-stack" in enabled(st={"stages": {"tech-stack": {"status": "not_started"}}})
    # Tech stack first: Requirements opens once tech-stack is approved (enable_after), not before.
    ts_review = {"stages": {**pristine["stages"], "tech-stack": {"status": "ready_for_review"}}}
    assert "requirements" not in enabled(st=ts_review) and tones(st=ts_review)["tech-stack"] == "awaiting"
    ts_done = {"stages": {**pristine["stages"], "tech-stack": {"status": "approved", "last_verification": {"passed": True}}}}
    assert "requirements" in enabled(st=ts_done)
    assert tones(st=ts_done)["tech-stack"] == "done" and tones(st=ts_done)["requirements"] == "none"
    # Spec/Plan (enable_on_review) wait for a draft ready for review: drafting alone keeps them
    # shut (though running-toned), unlike an ordinary tab (Tests) which opens while drafting.
    spec_drafting = {"stages": {**ts_done["stages"], "specification": {"status": "drafting"}}}
    assert "specification" not in enabled(st=spec_drafting) and tones(st=spec_drafting)["specification"] == "running"
    assert "specification" not in enabled(st=spec_drafting, running={"specification": "draft"})
    assert "tests" in enabled(st={"stages": {"ac-to-tests": {"status": "drafting"}}})
    spec_ready = {"stages": {**ts_done["stages"], "specification": {"status": "ready_for_review", "ever_ready_for_review": True}}}
    assert "specification" in enabled(st=spec_ready) and tones(st=spec_ready)["specification"] == "awaiting"
    # ...ever_ready_for_review survives a redraft; clarifying questions open it too (amber).
    assert "specification" in enabled(st={"stages": {"specification": {"status": "drafting", "ever_ready_for_review": True}}})
    clarify = {"stages": {"plan": {"status": "needs_clarification", "clarifying_questions": ["q?"]}}}
    assert "plan" in enabled(st=clarify) and tones(st=clarify)["plan"] == "awaiting"
    assert "plan" not in enabled(st={"stages": {"plan": {"status": "needs_clarification", "clarifying_questions": []}}})
    # ...or current_stage PAST the tab's last stage (current_stage == plan can mean "plan drafting").
    assert "specification" not in enabled(st={}, current_stage="specification")
    assert {"specification"} <= enabled(st={}, current_stage="plan") and "plan" not in enabled(st={}, current_stage="plan")
    # Quality/Report open on their enable_state_keys alone.
    assert "quality" in enabled(st={"test_hardening": {}}) and "report" not in enabled(st={"test_hardening": {}})
    assert {"quality", "report"} <= enabled(st={"metrics_report": {"x": 1}})
    assert "quality" not in enabled(st={"test_hardening": None})
    # Report: done only once approved; a failed verdict is red under a blocking policy, not under
    # an advisory one, and never once approved.
    report_key = next(t for t in p.tabs if t.id == "report").stage_keys[0]
    adv_mode = next(m for m, pol in p.stage(report_key).gate.policy.items() if pol == "advisory")  # type: ignore[union-attr]
    blk_mode = next(m for m, pol in p.stage(report_key).gate.policy.items() if pol == "blocking")  # type: ignore[union-attr]
    failed_exit = {"stages": {report_key: {"status": "not_started", "last_verification": {"passed": False}}}}
    assert tones(st=failed_exit, mode=blk_mode)["report"] == "error"
    assert tones(st=failed_exit, mode=adv_mode)["report"] == "none"
    assert tones(st=failed_exit, mode=None)["report"] == "error", "mode unknown -> not advisory"
    assert tones(st={"stages": {report_key: {"status": "approved", "last_verification": {"passed": False}}}}, mode=blk_mode)["report"] == "done"
    assert tones(st={"stages": {report_key: {"status": "approved"}}})["report"] == "done"
    # Quality is done only when every present stage is approved.
    q_half = {"stages": {"remediation": {"status": "approved"}, "adversarial-compliance": {"status": "not_started"}}}
    assert tones(st=q_half)["quality"] == "none"
    assert tones(st={"stages": {"remediation": {"status": "approved"}}})["quality"] == "done", "absent stages don't count"
    # Running wins: over an approved stage, and with no stage state at all (mid-run reattach),
    # where it also opens an ordinary tab.
    assert tones(st=ts_done, running={"tech-stack": "verify"})["tech-stack"] == "running"
    assert tones(st={}, running={"minimal-code-to-green": "draft"})["code"] == "running"
    assert "code" in enabled(st={}, running={"minimal-code-to-green": "draft"})
    assert tones(st={"stages": {"remediation": {"status": "approved"}, "adversarial-compliance": {"status": "drafting"}}})["quality"] == "running"
    # Durable current_stage (reattach gap: no stage state yet) opens every tab it has reached,
    # and a legacy "exit" sorts past every real stage.
    reattached = enabled(st={}, current_stage="minimal-code-to-green")
    assert {"requirements", "specification", "plan", "tests", "code"} <= reattached and not {"quality", "report"} & reattached, reattached
    assert enabled(st={}, current_stage="exit") == {t.id for t in p.tabs}
    assert enabled(st={}, current_stage="unknown-stage") == {first.id} | stageless

    # -- screens --
    assert build_gate_screen(state, "nope", code_gen_mode="draft_verify", running={}, interrupts=[], attempts=[], insights=None) is None
    assert build_gate_screen(state, "overview", code_gen_mode="draft_verify", running={}, interrupts=[], attempts=[], insights=None) is None
    spec_checks = p.stage("specification").gate.checks  # type: ignore[union-attr]
    t0 = datetime(2026, 9, 30, 12, 0)
    attempts = [
        {"run_id": "r1", "stage": "specification", "attempt": 1, "code_gen_mode": "yolo", "policy": "off", "stage_passed": False,
         "created_at": t0, "checks": [{"id": spec_checks[0].id, "status": "failed", "detail": "x" * 500, "source": "s"}]},
        {"run_id": "r1", "stage": "specification", "attempt": 2, "code_gen_mode": "draft_verify", "policy": "blocking", "stage_passed": True,
         "created_at": t0, "checks": [{"id": spec_checks[0].id, "status": "passed", "detail": "ok", "source": "s"}]},
        {"run_id": "r1", "stage": "plan", "attempt": 1, "code_gen_mode": "draft_verify", "policy": "blocking", "stage_passed": True, "created_at": t0, "checks": []},
    ]
    insights = {"checks": [{"check_id": spec_checks[0].id, "stage": "specification", "runs": 4, "fails": 1, "fail_rate": 0.25},
                           {"check_id": spec_checks[-1].id, "stage": "specification", "runs": 4, "fails": 0, "fail_rate": 0.0}]}
    kw: dict[str, Any] = dict(code_gen_mode="draft_verify", running={}, interrupts=[], attempts=attempts, insights=insights)
    screen = build_gate_screen(state, "specification", **kw)
    assert screen is not None
    json.dumps(screen)
    assert [c["key"] for c in screen["columns"]] == ["n", "check", "what", "effect", "when", "status", "detail"]
    assert [s["key"] for s in screen["sections"]] == ["specification"], "untouched brownfield-spec must be filtered"
    sec = screen["sections"][0]
    # Live verdict -> "Latest" offered and selected by default; lap fact + feedback shown; redraft lap note.
    opts = sec["attempts"]["options"]
    assert opts[0] == {"id": "", "label": GATE_TEXT["attempt_latest"], "time": None, "selected": True}, opts
    assert [o["id"] for o in opts[1:]] == ["specification:r1:1", "specification:r1:2"] and opts[1]["time"] == "2026-09-30T12:00:00+00:00"
    assert GATE_TEXT["lap"].format(lap=2, max=3) in sec["facts"] and sec["feedback"] == "fix it"
    stage_rows = sec["groups"][0]["rows"]
    assert sec["groups"][0]["heading"] is None and sec["groups"][1]["heading"] == GATE_TEXT["group_heading"]["wrapper"]
    assert stage_rows[0]["cells"]["status"]["sub"] == T["lap_note"].format(lap=2)
    assert stage_rows[0]["cells"]["check"]["sub"] == GATE_TEXT["fail_rate"].format(pct=25)
    assert stage_rows[-1]["cells"]["check"]["sub"] is None, "fail-rate hint only when fails > 0"
    # Selecting attempt 1: its own mode/policy, no lap fact/feedback, collapsible long detail.
    sec1 = build_gate_screen(state, "specification", attempt="specification:r1:1", **kw)["sections"][0]  # type: ignore[index]
    assert [o["selected"] for o in sec1["attempts"]["options"]] == [False, True, False]
    assert sec1["facts"][:2] == [GATE_TEXT["mode"].format(mode=_mode_facts("yolo", p)[0]), GATE_TEXT["policy"].format(policy="off")]
    assert sec1["feedback"] is None and not any(f.startswith("lap ") for f in sec1["facts"])
    first = sec1["groups"][0]["rows"][0]["cells"]
    assert first["status"]["tone"] == "fail" and first["detail"]["collapsible"] is True and first["status"]["sub"] is None
    # No live verdict -> default to the newest attempt, no "Latest" option.
    plan_state = {"stages": {"plan": {"status": "approved", "last_verification": None}}}
    plan_sec = build_gate_screen(plan_state, "plan", **kw)["sections"][0]  # type: ignore[index]
    assert [(o["id"], o["selected"]) for o in plan_sec["attempts"]["options"]] == [("plan:r1:1", True)], plan_sec["attempts"]
    # ...and with no history either: "Latest" (disabled select), approved-earlier rows.
    plan_sec = build_gate_screen(plan_state, "plan", **{**kw, "attempts": []})["sections"][0]  # type: ignore[index]
    assert plan_sec["attempts"]["disabled"] and [o["id"] for o in plan_sec["attempts"]["options"]] == [""]
    assert plan_sec["groups"][0]["rows"][0]["cells"]["status"]["text"] == T["approved_earlier"]
    # after_submit gate (tech-stack): no wrapper group; reopened-gate verdict from the interrupt.
    ts_screen = build_gate_screen({"stages": {"tech-stack": {"status": "ready_for_review"}}}, "tech-stack",
                                  **{**kw, "interrupts": [{"stage": "tech-stack", "verification": iv}]})
    assert ts_screen is not None and len(ts_screen["sections"][0]["groups"]) == 2, ts_screen["sections"][0]["groups"]
    assert ts_screen["sections"][0]["groups"][1]["heading"] == GATE_TEXT["group_heading"]["uncatalogued"]
    assert GATE_TEXT["lap"].format(lap=2, max=3) in ts_screen["sections"][0]["facts"]
    # Quality: once one of its two stages has progressed, only the touched one is listed.
    q_state = {"stages": {"remediation": {"status": "approved", "last_verification": {"passed": True, "checks": []}}}}
    q = build_gate_screen(q_state, "quality", **kw)
    assert q is not None and [s["key"] for s in q["sections"]] == ["remediation"]

    # Every check mode / reported status has copy.
    all_checks = [*p.wrapper_checks, *(c for s in p.stages if s.gate for c in s.gate.checks)]
    assert {c.mode for c in all_checks} <= set(GATE_TEXT["check_effect"]), "a check mode has no Effect text"
    assert set(_STATUS_RANK) == set(GATE_TEXT["icon_badge"]) <= set(GATE_TEXT["icon_status"])
    print("gate_view self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
