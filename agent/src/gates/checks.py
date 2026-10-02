"""Per-check vocabulary for a stage's deterministic verify gate.

A `Gate` (attached to a graph.StageSpec) owns the verify function, the ordered `Check`s it can
report, and a per-code_gen_mode `policy`. A `CheckLog` is created by the caller and handed to the
verify function, which records one `CheckResult` row per check it actually evaluated -- so a crash
mid-gate still keeps the rows recorded before it. Pure data, no graph import (graph.py imports
this module, never the reverse).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

CheckMode = Literal["blocking", "collected", "advisory"]
CheckStatus = Literal["passed", "failed", "infra", "skipped", "advisory"]
Policy = Literal["off", "advisory", "blocking"]
Timing = Literal["before_review", "after_submit"]

CODE_GEN_MODES: tuple[str, ...] = ("yolo", "draft_verify", "mission_critical")
# Missing or unrecognised code_gen_mode resolves to the strictest mode -- same rule as
# graph._resolve_thread_code_gen_mode's final fallback ("more verification is the safe direction").
DEFAULT_CODE_GEN_MODE = "mission_critical"


# Modes that run the adversarial audit leg. Read by graph._audit_enabled and by gate_view (the
# gate screen's "Skipped: no audit" rows) so the frontend never re-derives it.
AUDIT_MODES = frozenset({"mission_critical"})


# Gate checks a sandbox Stop hook (agent/sandbox-image/hooks/*.mjs) ALSO enforces in-turn: the hook
# runs the same helper while the model drafts and blocks the turn from ending until it passes. Hooks
# are installed for every session and never look at code_gen_mode, so these still run when a mode
# turns the stage's gate "off" -- the gate screen says so instead of a bare "Skipped" (gate_view).
# "partial": the hook enforces only part of the check's rule. Audit-only hooks (full-read) are left
# out -- no audit turn exists in a mode that turns a gate off. Mapped from each hook's own source
# (2026-10-01); a hook change that alters what it enforces must update this table.
# ponytail: Claude only -- a Stop hook's exit-2 block is unverified on Copilot (Dockerfile notes).
IN_TURN_CHECKS: dict[str, tuple[Literal["full", "partial"], str]] = {
    "spec.draft_file_parses": ("partial", "check-ledger-sync-stop"),
    "spec.draft_not_empty": ("full", "check-ledger-sync-stop"),
    "spec.no_open_questions": ("full", "check-ledger-sync-stop"),
    "spec.story_narrative": ("full", "check-narrative-format-stop"),
    "spec.ledger_citations": ("full", "check-ledger-sync-stop"),
    "spec.ledger_duplicates": ("full", "check-ledger-sync-stop"),
    "spec.ledger_retirements": ("full", "check-ledger-sync-stop"),
    "spec.ledger_bug_affected": ("partial", "check-ledger-sync-stop"),
    "spec.story_decisions": ("full", "check-ledger-sync-stop"),
    "spec.prd_changes_addressed": ("full", "check-ledger-sync-stop"),
    "plan.steps_json": ("partial", "check-plan-schema-stop"),
    "plan.ledger_sync": ("partial", "check-plan-citations-stop"),
    "plan.manifest_json": ("partial", "check-plan-schema-stop"),
    "plan.visual_retirement": ("full", "check-plan-citations-stop"),
    "plan.visual_review_current": ("partial", "check-diagram-staleness-stop"),
    "plan.step_linkage": ("full", "check-plan-citations-stop"),
    "plan.wireframe_ac_ids": ("full", "check-plan-citations-stop"),
    "plan.wireframe_has_ac_ids": ("full", "check-plan-citations-stop"),
    "plan.ui_wireframe_coverage": ("full", "check-plan-citations-stop"),
    "plan.step_wireframe_coverage": ("full", "check-plan-citations-stop"),
    "plan.wireframe_html": ("full", "check-plan-schema-stop"),
    "plan.mermaid_render": ("full", "check-diagram-render-stop"),
    "ac_tests.write_scope": ("partial", "check-ac-residue-stop"),
    "ac_tests.ledger_integrity": ("full", "check-ac-residue-stop"),
    "ac_tests.retired_residue": ("full", "check-ac-residue-stop"),
    "ac_tests.deferred_residue": ("full", "check-ac-residue-stop"),
    "ac_tests.completed_protection": ("full", "check-ac-residue-stop"),
    "ac_tests.wrote_tests": ("full", "check-ac-residue-stop"),
    "ac_tests.not_e2e_only": ("full", "check-ac-residue-stop"),
    "ac_tests.e2e_spec_present": ("partial", "check-ac-residue-stop"),
    "ac_tests.screenshot_on": ("full", "check-ac-residue-stop"),
    "ac_tests.depth": ("partial", "check-test-quality-stop"),
    "ac_tests.testid_locators": ("full", "check-testid-locators-stop"),
    "ac_tests.nav_waits": ("full", "check-testid-locators-stop"),
    "code.coverage_threshold": ("full", "check-coverage-stop"),
    "code.ac_depth": ("partial", "check-coverage-stop"),
    "code.testid_locators": ("full", "check-testid-locators-stop"),
    "code.nav_waits": ("full", "check-testid-locators-stop"),
    "remediation.fabricated_ids": ("full", "check-remediation-stop"),
    "remediation.ignore_files": ("partial", "check-remediation-stop"),
    "remediation.suppression_comments": ("partial", "check-remediation-stop"),
    "adversarial.report": ("full", "check-adversarial-stop"),
    "adversarial.verdict": ("full", "check-adversarial-stop"),
    "adversarial.blocking_findings": ("full", "check-adversarial-stop"),
    "exit.manifest": ("partial", "check-exit-readiness-stop"),
    "exit.screenshots": ("full", "check-exit-readiness-stop"),
    "exit.metrics": ("full", "check-exit-readiness-stop"),
    "exit.targeted_fix": ("full", "check-exit-readiness-stop"),
    "exit.auth": ("full", "check-exit-readiness-stop"),
    "wrapper.skills": ("partial", "require-skills-stop"),
}


def resolve_code_gen_mode(mode: str | None) -> str:
    return mode if mode in CODE_GEN_MODES else DEFAULT_CODE_GEN_MODE


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    description: str
    mode: CheckMode
    condition: str = "always"
    needs_audit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "description": self.description, "mode": self.mode,
            "condition": self.condition, "needs_audit": self.needs_audit,
        }


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: CheckStatus
    detail: str | None
    source: str
    uncatalogued: bool = False

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"id": self.id, "status": self.status, "detail": self.detail, "source": self.source}
        if self.uncatalogued:
            row["uncatalogued"] = True
        return row


# eq=False: identity hash, so a frozen StageSpec holding a Gate (whose policy is a dict) stays hashable.
@dataclass(eq=False)
class Gate:
    verify: Callable[..., Any]
    policy: Mapping[str, Policy]
    persists: bool
    """True = this verify is also where the stage's required persistence happens (ledger save,
    diagram commit, merge-verdict correction), so no mode may turn it "off"."""
    checks: tuple[Check, ...] = ()
    timing: Timing = "before_review"
    stage_key: str = field(default="", repr=False)  # set by StageSpec.__post_init__

    @property
    def id(self) -> str:
        return f"{self.stage_key}_verify"

    def policy_for(self, mode: str | None) -> Policy:
        return self.policy[resolve_code_gen_mode(mode)]


class CheckLog:
    def __init__(self, gate_id: str, allowed: tuple[Check, ...], strict: bool = False) -> None:
        self.gate_id = gate_id
        self._allowed = {c.id: c for c in allowed}
        self.strict = strict
        self._rows: list[CheckResult] = []

    def _source(self) -> str:
        # Caller of passed()/failed()/... is two frames up (this helper, then _record). Fail soft.
        try:
            frame = sys._getframe(3)
            code = frame.f_code
            name = getattr(code, "co_qualname", code.co_name)
            module = frame.f_globals.get("__name__", "").rsplit(".", 1)[-1]
            return f"{self.gate_id} › {module}.{name}"
        except Exception:  # noqa: BLE001 -- provenance is cosmetic, never worth failing a gate over
            return self.gate_id

    def _record(self, check: Check, status: CheckStatus, detail: str | None) -> None:
        uncatalogued = self._allowed.get(check.id) != check
        if uncatalogued:
            if self.strict:
                raise ValueError(f"{self.gate_id}: check {check.id!r} is not declared on this gate")
            logger.error("%s: recorded undeclared check %r", self.gate_id, check.id)
        self._rows.append(CheckResult(check.id, status, detail, self._source(), uncatalogued))

    def passed(self, check: Check, detail: str | None = None) -> None:
        self._record(check, "passed", detail)

    def failed(self, check: Check, detail: str | None = None) -> None:
        self._record(check, "failed", detail)

    def infra(self, check: Check, detail: str | None = None) -> None:
        self._record(check, "infra", detail)

    def skipped(self, check: Check, detail: str | None = None) -> None:
        self._record(check, "skipped", detail)

    def advisory(self, check: Check, detail: str | None = None) -> None:
        self._record(check, "advisory", detail)

    def record_tagged(
        self, check_map: dict[str, Check], tagged: list[tuple[str, str]], status: CheckStatus = "failed"
    ) -> None:
        """For the mirrored stdlib *_checks.py helpers, which can't import Check and return
        (check_id, reason) tuples instead. An id missing from check_map is recorded uncatalogued."""
        for check_id, reason in tagged:
            check = check_map.get(check_id) or Check(check_id, check_id, "", "blocking")
            self._record(check, status, reason)

    def results(self) -> list[CheckResult]:
        return list(self._rows)


# Recorded by graph.make_verify_node itself around every stage's own checks (and listed once, as
# Pipeline.wrapper_checks, rather than on every Gate).
WRAPPER_SANDBOX = Check(
    "wrapper.sandbox", "Sandbox available",
    "A sandbox was registered for this session, so the gate could run at all.", "blocking",
)
WRAPPER_SKILLS = Check(
    "wrapper.skills", "Required skills invoked",
    "The stage's sessions invoked every skill it requires, per their own transcripts.", "blocking",
    "only when the stage requires skills",
)
WRAPPER_VERIFY_CRASHED = Check(
    "wrapper.verify_crashed", "Verify completed",
    "The stage's verify function returned a verdict instead of raising.", "blocking",
)
WRAPPER_AUDIT_FINDINGS = Check(
    "wrapper.audit_findings", "Audit findings resolved",
    "Every finding the second-opinion audit raised this lap was addressed.", "blocking",
    "only when the audit runs or the draft admitted gaps",
    needs_audit=True,
)
WRAPPER_CHECKS: tuple[Check, ...] = (WRAPPER_SANDBOX, WRAPPER_SKILLS, WRAPPER_VERIFY_CRASHED, WRAPPER_AUDIT_FINDINGS)


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.checks`."""
    a = Check("a", "A", "first", "blocking")
    b = Check("b", "B", "second", "advisory")
    stray = Check("z", "Z", "not declared", "blocking")

    gate = Gate(verify=lambda: None, policy={"yolo": "off", "draft_verify": "blocking", "mission_critical": "advisory"},
                persists=False, checks=(a, b))
    gate.stage_key = "demo"
    assert gate.id == "demo_verify"
    assert gate.policy_for("yolo") == "off" and gate.policy_for("draft_verify") == "blocking"
    for unknown in (None, "bogus"):
        assert gate.policy_for(unknown) == "advisory"  # falls back to mission_critical
    assert hash(gate)  # identity-hashable despite the dict field

    log = CheckLog(gate.id, gate.checks)
    log.passed(a)
    log.advisory(b, "soft")
    log.failed(stray, "oops")  # non-strict: logged, kept, flagged
    log.record_tagged({"a": a}, [("a", "bad"), ("nope", "unknown id")])
    rows = [r.to_dict() for r in log.results()]
    assert [(r["id"], r["status"]) for r in rows] == [
        ("a", "passed"), ("b", "advisory"), ("z", "failed"), ("a", "failed"), ("nope", "failed")
    ], rows
    assert "uncatalogued" not in rows[0] and rows[2]["uncatalogued"] and rows[4]["uncatalogued"]
    # module is "__main__" when run via -m, "checks" when imported
    assert rows[0]["source"].startswith("demo_verify › ") and rows[0]["source"].endswith("._demo"), rows[0]["source"]
    assert rows[3]["source"] == rows[0]["source"], rows[3]["source"]  # via record_tagged, same caller

    strict = CheckLog(gate.id, gate.checks, strict=True)
    try:
        strict.failed(stray)
        raise AssertionError("strict CheckLog must reject an undeclared check")
    except ValueError:
        pass
    assert strict.results() == []
    print("checks self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
