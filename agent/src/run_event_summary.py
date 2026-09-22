"""Per-stage / per-rebuild-placement summary computed server-side from a session's ordered
run_events rows (Overview-tab fix, 2026-09-22) -- replaces SessionOverview.tsx re-deriving
duration/cost from the raw event stream on every render. Pure computation, no DB access of its
own: run_event_store.list_events_by_session already fetches the rows this consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .run_events import RunEvent, RunEventType

# Rebuild-placement inner-turn stage tags carry one of these two prefixes (rebuild.py's
# discovery/fix turns use "rebuild-<placement key>", its red-gate turn uses
# "red-gate-<placement key>") ahead of the placement's own span-event tag, which is the bare
# placement key itself (e.g. "r_ac_to_tests"). Stripping the prefix reconciles all of a
# placement's several event families onto one grouping key. No real STAGES key starts with
# either prefix, so this is a no-op strip for every real stage.
_REBUILD_STAGE_PREFIXES = ("rebuild-", "red-gate-")


def _normalize_stage(stage: str) -> str:
    for prefix in _REBUILD_STAGE_PREFIXES:
        if stage.startswith(prefix):
            return stage[len(prefix):]
    return stage


@dataclass(frozen=True)
class StageSummary:
    key: str
    first_ts: datetime
    last_ts: datetime
    cost: float
    cost_known: bool


def session_stage_summary(events: list[RunEvent]) -> list[StageSummary]:
    """One entry per normalized stage/placement key, scoped to that key's own latest run_id.

    `events` must be seq-ascending (run_event_store.list_events_by_session's own contract).
    "Latest run_id" is POSITIONAL, not a comparison of run_id values -- run_id is an opaque
    8-char token (VARCHAR(8)) with no inherent ordering. This mirrors the exact rule
    SessionOverview.tsx's own latestRunIdByStage already uses: walk events in order and
    remember the last-seen run_id per key, so whichever run_id appears in that key's LAST event
    wins -- a resumed placement's earlier, disconnected pass is excluded from the aggregate
    instead of corrupting its duration/cost.

    Deliberately omits a redraft/lap count: RedraftHistoryCell needs its own client-side
    per-stage event list for per-lap detail regardless, and can keep deriving its count from
    that same list -- duplicating it here would be dead weight.
    """
    latest_run_id: dict[str, str] = {}
    for e in events:
        if not e.stage:
            continue
        latest_run_id[_normalize_stage(e.stage)] = e.run_id

    by_key: dict[str, dict] = {}
    for e in events:
        if not e.stage or e.ts is None:
            continue
        key = _normalize_stage(e.stage)
        if e.run_id != latest_run_id.get(key):
            continue
        entry = by_key.setdefault(key, {"first": e.ts, "last": e.ts, "cost": 0.0, "cost_known": False})
        if e.ts < entry["first"]:
            entry["first"] = e.ts
        if e.ts > entry["last"]:
            entry["last"] = e.ts
        cost = (e.token_usage or {}).get("cost")
        if cost is not None:
            entry["cost"] += float(cost)
            entry["cost_known"] = True

    return [
        StageSummary(key=key, first_ts=v["first"], last_ts=v["last"], cost=v["cost"], cost_known=v["cost_known"])
        for key, v in by_key.items()
    ]


def _demo() -> None:
    """Self-check: reproduces the exact bug this module fixes -- a resumed rebuild placement
    whose span tag spans two disjoint time windows (would go negative under the old
    after.last -> next.first windowing) -- plus the cost-attribution gap (a stage_run event
    correctly summed into its own placement, never a neighbor's, at Phase 1's new placement-
    specific tags)."""
    from datetime import timedelta

    t0 = datetime(2026, 9, 19, 22, 0, 0)

    # Original pass: r_ac_to_tests runs and finishes under run_id "aaaaaaaa".
    original_pass = [
        RunEvent(run_id="aaaaaaaa", session_id="s", type=RunEventType.NODE_STARTED, stage="r_ac_to_tests", node="rebuild", ts=t0, seq=1),
        RunEvent(run_id="aaaaaaaa", session_id="s", type=RunEventType.NODE_FINISHED, stage="r_ac_to_tests", node="rebuild", ts=t0 + timedelta(minutes=5), seq=2),
    ]
    # A later, unrelated real stage's own events land after, same run.
    later_real_stage = [
        RunEvent(run_id="aaaaaaaa", session_id="s", type=RunEventType.NODE_STARTED, stage="minimal-code-to-green", node="draft", ts=t0 + timedelta(hours=1), seq=3),
    ]
    # Resume (reset-e2e/targeted-fix): a NEW run_id re-enters r_ac_to_tests a day later, with its
    # own discovery-turn cost event (Phase 1's new stage_run tag).
    t1 = t0 + timedelta(days=1)
    resumed_pass = [
        RunEvent(run_id="bbbbbbbb", session_id="s", type=RunEventType.NODE_STARTED, stage="r_ac_to_tests", node="rebuild", ts=t1, seq=4),
        RunEvent(run_id="bbbbbbbb", session_id="s", type=RunEventType.NODE_FINISHED, stage="rebuild-r_ac_to_tests", node="stage_run",
                 ts=t1 + timedelta(minutes=1), seq=5, token_usage={"cost": 1.23}),
        RunEvent(run_id="bbbbbbbb", session_id="s", type=RunEventType.NODE_FINISHED, stage="r_ac_to_tests", node="rebuild", ts=t1 + timedelta(minutes=2), seq=6),
    ]
    # A different placement's own cost event in the same window -- must NOT bleed into
    # r_ac_to_tests's total.
    other_placement = [
        RunEvent(run_id="bbbbbbbb", session_id="s", type=RunEventType.NODE_FINISHED, stage="rebuild-r_remediation", node="stage_run",
                 ts=t1 + timedelta(minutes=1, seconds=30), seq=7, token_usage={"cost": 9.99}),
    ]

    events = original_pass + later_real_stage + resumed_pass + other_placement
    summary = {s.key: s for s in session_stage_summary(events)}

    ac = summary["r_ac_to_tests"]
    assert ac.last_ts > ac.first_ts, f"duration went non-positive: {ac.first_ts} .. {ac.last_ts}"
    assert ac.first_ts == t1, f"expected the resumed pass's own first ts, got {ac.first_ts}"
    assert ac.last_ts == t1 + timedelta(minutes=2), f"expected the resumed pass's own last ts, got {ac.last_ts}"
    assert ac.cost_known and abs(ac.cost - 1.23) < 1e-9, f"expected only r_ac_to_tests's own cost, got {ac.cost}"

    remediation = summary["r_remediation"]
    assert remediation.cost_known and abs(remediation.cost - 9.99) < 1e-9, f"expected r_remediation's own cost, got {remediation.cost}"

    print("OK", {k: (v.first_ts, v.last_ts, v.cost, v.cost_known) for k, v in summary.items()})


if __name__ == "__main__":
    _demo()
