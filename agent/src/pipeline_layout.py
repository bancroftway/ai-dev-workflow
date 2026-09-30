"""The pipeline as the frontend should render it: tabs, the stages under each, every stage's gate
(checks + per-mode policy), run order, the code-gen modes, rebuild placements and legacy labels.

Served mode-independent by `GET /pipeline` (sessions_api.py) so the UI renders it instead of
hardcoding stage keys. Stage labels/descriptions/gates are read off graph.py's StageSpecs -- this
module only adds the layout (which tab shows which stage) and the few UI facts that have no
StageSpec of their own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import graph
from .gates.checks import AUDIT_MODES, CODE_GEN_MODES, WRAPPER_CHECKS, Check


@dataclass(frozen=True)
class TabSpec:
    id: str
    view: str
    label: str
    stage_keys: tuple[str, ...]
    enable_after: tuple[str, ...] = ()  # stage keys that must be approved before the tab opens
    enable_state_keys: tuple[str, ...] = ()  # top-level state keys whose presence opens the tab
    # "<stage_key>:<status>" transitions that auto-focus this tab (AppShell's old RULES table).
    focus_on: tuple[str, ...] = ()

    def describe(self, stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": self.id, "view": self.view, "label": self.label,
            "enable_after": list(self.enable_after), "enable_state_keys": list(self.enable_state_keys),
            "focus_on": list(self.focus_on), "stages": [stages[k] for k in self.stage_keys],
        }


# Stage keys with no StageSpec (deterministic record nodes) that still show up in a tab.
_PLAIN_STAGES: dict[str, tuple[str, str]] = {
    "raw-requirements": ("Requirements", "The requirements text exactly as submitted."),
}


@dataclass(frozen=True)
class Pipeline:
    tabs: tuple[TabSpec, ...]
    order: tuple[str, ...]
    modes: tuple[dict[str, Any], ...]
    rebuild_placements: tuple[dict[str, str], ...]
    failure_stage_map: dict[str, str]
    legacy_labels: dict[str, str]
    wrapper_checks: tuple[Check, ...]
    stages: tuple[graph.StageSpec, ...] = field(default_factory=lambda: tuple(graph._ALL_STAGE_SPECS))

    def stage(self, key: str) -> graph.StageSpec | None:
        return next((s for s in self.stages if s.key == key), None)

    @property
    def verifiers(self) -> dict[str, Callable[..., Any]]:
        """`{"<stage>_verify": fn}` -- derived from each StageSpec's Gate, never hand-written."""
        return {s.gate.id: s.gate.verify for s in self.stages if s.gate is not None}

    def _describe_stage(self, key: str) -> dict[str, Any]:
        spec = self.stage(key)
        if spec is None:
            label, description = _PLAIN_STAGES[key]
            return {"key": key, "label": label, "description": description, "gate": None}
        gate = spec.gate
        return {
            "key": key, "label": spec.label, "description": spec.description,
            "gate": None if gate is None else {
                "id": gate.id, "timing": gate.timing, "persists": gate.persists,
                "policy": {m: gate.policy_for(m) for m in CODE_GEN_MODES},
                "checks": [c.to_dict() for c in gate.checks],
            },
        }

    def describe(self) -> dict[str, Any]:
        stages = {k: self._describe_stage(k) for t in self.tabs for k in t.stage_keys}
        return {
            "tabs": [t.describe(stages) for t in self.tabs],
            "order": list(self.order),
            "modes": [{**m, "audit": m["id"] in AUDIT_MODES} for m in self.modes],
            "rebuild_placements": [dict(p) for p in self.rebuild_placements],
            "failure_stage_map": dict(self.failure_stage_map),
            "legacy_labels": dict(self.legacy_labels),
            "wrapper_checks": [c.to_dict() for c in self.wrapper_checks],
        }


# The real run sequence (build_graph): tech-stack -> manifest_branch -> [brownfield-spec ->
# brownfield-plan] -> app_check_record -> repo_scan_baseline -> raw-requirements -> STAGES[1:].
_ORDER = ("tech-stack", "brownfield-spec", "brownfield-plan", "raw-requirements") + tuple(
    s.key for s in graph.STAGES[1:]
)


def _rebuild_placements() -> tuple[dict[str, str], ...]:
    out = []
    for after, spec in graph.POST_STAGE_REBUILD.items():
        out.append({
            "after_stage_key": after,
            "rebuild_key": spec.key,
            "next_stage_key": _ORDER[_ORDER.index(after) + 1],
            # ac-to-tests' placement is the TDD-red check, not a plain rebuild.
            "label": "Red Gate" if after == "ac-to-tests" else "Rebuild",
        })
    return tuple(out)


_REBUILD_PLACEMENTS = _rebuild_placements()

PIPELINE = Pipeline(
    tabs=(
        TabSpec("tech-stack", "tech-stack", "Tech Stack", ("tech-stack",)),
        # focus_on "tech-stack:approved" only when raw-requirements is still not_started -- that
        # guard stays in the frontend (resumed/delta threads already carry requirements).
        TabSpec("requirements", "requirements", "Requirements", ("raw-requirements",),
                enable_after=("tech-stack",), focus_on=("tech-stack:approved",)),
        TabSpec("specification", "specification", "Specification", ("brownfield-spec", "specification"),
                focus_on=("specification:ready_for_review",)),
        TabSpec("plan", "plan", "Plan", ("brownfield-plan", "plan"), focus_on=("plan:ready_for_review",)),
        TabSpec("tests", "build", "Tests", ("ac-to-tests",), focus_on=("ac-to-tests:drafting",)),
        TabSpec("code", "build", "Code", ("minimal-code-to-green",)),
        TabSpec("quality", "quality", "Quality", ("remediation", "adversarial-compliance"),
                enable_state_keys=("test_hardening", "metrics_report")),
        TabSpec("report", "report", "Report", ("metrics-exit",), enable_state_keys=("metrics_report",),
                focus_on=("metrics-exit:approved", "exit:approved")),
        TabSpec("overview", "overview", "Overview", ()),
    ),
    order=_ORDER,
    modes=(
        {
            "id": "yolo", "label": "⚡ YOLO", "default": False,
            "blurb": "Draft only — no second-opinion audit, no deterministic check before advancing.",
            "speed_cost": "Instant · 1 LLM call per stage (draft only, pipeline-wide) — the cheapest and fastest option.",
            "best_for": "Best for quick prototypes, throwaway spikes, scratch scripts — anything you'll read and test yourself end-to-end before it matters.",
            "badge": {"text": "Some checks still run in the background (not enforced)", "variant": "secondary"},
        },
        {
            "id": "draft_verify", "label": "🪵 Draft & Verify", "default": True,
            "blurb": "Draft, plus each stage's own free, deterministic check (ledger sync, diagram validity, test coverage, remediation confirmation, compliance audit, exit readiness) — no second-opinion audit.",
            "speed_cost": "Fast · 1 LLM call per stage, plus each stage's own free check. Only redrafts — costing another LLM call — if a check actually fails.",
            "best_for": "Best for day-to-day work end to end — every stage's own deterministic check still runs, without paying for a second model to review every draft regardless of whether it's needed.",
            "badge": {"text": "Recommended", "variant": "default"},
        },
        {
            "id": "mission_critical", "label": "🛡️ Mission Critical", "default": False,
            "blurb": "Draft, a second-opinion audit, and the same deterministic check/redraft loop as Draft & Verify — at every stage.",
            "speed_cost": "Thorough · 2+ LLM calls per audited stage (draft + audit) — meaningfully more tokens and wall-clock time across the whole run.",
            "best_for": "Best for production-critical work — auth, payments, security-sensitive code, complex refactors of core systems, anything you won't hand-review line by line yourself, end to end.",
            "badge": None,
        },
    ),
    rebuild_placements=_REBUILD_PLACEMENTS,
    # dbo.sessions.failure_stage values that aren't stage keys -> the stage a restart should target.
    # "exit" and "provisioning" deliberately have no entry (a verdict, or no stage ever reached).
    failure_stage_map={
        **{p["rebuild_key"]: p["after_stage_key"] for p in _REBUILD_PLACEMENTS},
        "e2e": "remediation",
        "test_hardening": "remediation",
        "metrics_report": "adversarial-compliance",
    },
    # Pre-rename stage keys an old completed session's stored data may still carry.
    legacy_labels={
        "adversarial-audit": "Adversarial Audit",
        "dedup-simplify": "De-dup / Simplify",
        "license-audit": "License Audit",
        "exit": "Exit",
    },
    # Recorded by verify_node itself around every stage's own checks.
    wrapper_checks=WRAPPER_CHECKS,
)


def _assert_mirrors_in_sync() -> int:
    """Every sandbox-image/hooks/*.py with a same-named copy in src/ or src/gates/ must be
    byte-identical to it: the Stop hooks and the in-process gates must grade with the same code.
    Returns the number of pairs checked."""
    src = Path(__file__).resolve().parent
    hooks = src.parent / "sandbox-image" / "hooks"
    pairs = 0
    for hook in sorted(hooks.glob("*.py")):
        for twin in (src / "gates" / hook.name, src / hook.name):
            if twin.is_file():
                assert twin.read_bytes() == hook.read_bytes(), f"{twin} drifted from its mirror {hook}"
                pairs += 1
    return pairs


def _unreferenced_checks(checks: list[Check]) -> list[str]:
    """Ids of declared checks nothing records. A check counts as recorded when a module-level name
    bound to it appears in a `log.<method>(NAME` / `fail(NAME` call, as the head of a `(NAME, ...)`
    pair a loop feeds into a log call, or as `NAME.id` (a tagged reason), or when its id literal appears somewhere other than a `Check("<id>"` declaration (the
    mirrored stdlib helpers tag reasons with bare id strings)."""
    import re
    import sys

    src = Path(__file__).resolve().parent
    text = "\n".join(f.read_text(encoding="utf-8") for f in src.rglob("*.py"))
    modules = [m for n, m in list(sys.modules.items()) if n.startswith(__package__ or "src") and m is not None]
    missing = []
    for check in checks:
        names = {n for m in modules for n, v in vars(m).items() if v is check}
        by_name = any(
            re.search(
                rf"(?:log\.\w+|\bfail)\(\s*(?:\w+\.)?{re.escape(n)}\b"  # log.failed(NAME / fail(NAME
                rf"|[(\[,]\s*\(\s*{re.escape(n)}\s*,"  # (NAME, ...) pair iterated into a log call
                rf"|\b{re.escape(n)}\.id\b",  # NAME.id tagged reason
                text,
            )
            for n in names
        )
        literal = f'"{check.id}"'
        declared = len(re.findall(rf"Check\(\s*{re.escape(literal)}", text))
        if not by_name and text.count(literal) <= declared:
            missing.append(check.id)
    return missing


def _diff_paths(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for k in sorted(set(a) | set(b)) for p in _diff_paths(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in _diff_paths(x, y, f"{path}[{i}]")]
    return [] if a == b else [path]


def _demo() -> None:
    """`cd agent && uv run python -m src.pipeline_layout`."""
    import dataclasses
    import json

    p = PIPELINE
    spec_keys = {s.key for s in p.stages}
    all_keys = spec_keys | set(_PLAIN_STAGES)

    tab_keys = [k for t in p.tabs for k in t.stage_keys]
    assert sorted(tab_keys) == sorted(all_keys), f"each stage must sit in exactly one tab: {tab_keys}"
    assert len({t.id for t in p.tabs}) == len(p.tabs)
    assert sorted(p.order) == sorted(all_keys), f"order must cover every stage once: {p.order}"
    for t in p.tabs:
        assert set(t.enable_after) <= all_keys, t.id

    for s in p.stages:
        assert s.label, f"{s.key}: StageSpec needs a label"
        if s.gate is None:
            continue
        assert set(s.gate.policy) == set(CODE_GEN_MODES), s.key
        assert not (s.gate.persists and "off" in s.gate.policy.values()), f"{s.key}: persists gate is off in a mode"

    # verifier registry keys == the verify node names _wire_stage builds.
    before_review = {s.key for s in p.stages if s.deterministic_verify is not None}
    assert set(p.verifiers) == {f"{k}_verify" for k in spec_keys if p.stage(k).gate is not None}  # type: ignore[union-attr]
    verify_nodes = {n for n in graph.build_graph().nodes if n.endswith("_verify")}
    assert verify_nodes == {f"{k}_verify" for k in before_review}, verify_nodes
    for key in before_review:
        assert p.verifiers[f"{key}_verify"] is p.stage(key).deterministic_verify  # type: ignore[union-attr]
    # after_submit gates (tech-stack) are real verifiers but run inside the gate node, never as a node.
    after_submit = {s.key for s in p.stages if s.gate is not None and s.gate.timing == "after_submit"}
    assert after_submit == {"tech-stack"}, after_submit
    assert {f"{k}_verify" for k in after_submit} <= set(p.verifiers) and not verify_nodes & {f"{k}_verify" for k in after_submit}
    assert p.verifiers["specification_verify"] is graph._verify_specification_ledger

    described = p.describe()
    json.dumps(described)

    # Acceptance flip: Code blocking in yolo is a one-field edit that changes exactly one value.
    def _flip(s: graph.StageSpec) -> graph.StageSpec:
        if s.key != "minimal-code-to-green" or s.gate is None:
            return s
        return dataclasses.replace(s, gate=dataclasses.replace(s.gate, policy={**s.gate.policy, "yolo": "blocking"}))

    flipped_specs = tuple(_flip(s) for s in p.stages)
    flipped = dataclasses.replace(p, stages=flipped_specs).describe()
    code_tab = next(i for i, t in enumerate(p.tabs) if t.id == "code")
    assert _diff_paths(described, flipped) == [f".tabs[{code_tab}].stages[0].gate.policy.yolo"], _diff_paths(described, flipped)
    assert PIPELINE.stage("minimal-code-to-green").gate.policy["yolo"] == "off"  # type: ignore[union-attr]
    graph._demo_route_policy_matrix(list(flipped_specs))  # routing follows the flipped policy

    # Every gate declares checks; ids are unique within a gate and never collide with a wrapper id;
    # every declared check is actually recorded somewhere.
    wrapper_ids = {c.id for c in p.wrapper_checks}
    assert len(wrapper_ids) == len(p.wrapper_checks)
    all_checks: list[Check] = list(p.wrapper_checks)
    for s in p.stages:
        if s.gate is None:
            continue
        ids = [c.id for c in s.gate.checks]
        assert ids, f"{s.key}: gate declares no checks"
        assert len(ids) == len(set(ids)), f"{s.key}: duplicate check ids {ids}"
        assert not wrapper_ids & set(ids), f"{s.key}: check id collides with a wrapper check"
        all_checks += [c for c in s.gate.checks if c not in all_checks]
    unreferenced = _unreferenced_checks(all_checks)
    assert not unreferenced, f"declared but never recorded: {unreferenced}"
    probe = Check("demo." + "never_recorded", "x", "x", "blocking")  # split: no literal for the scan to find
    assert _unreferenced_checks([probe]) == [probe.id]

    pairs = _assert_mirrors_in_sync()
    assert pairs >= 13, f"only {pairs} hook mirrors found -- path wrong?"
    print(f"pipeline_layout self-check: all assertions passed ({pairs} mirrored helpers in sync)")


if __name__ == "__main__":
    _demo()
