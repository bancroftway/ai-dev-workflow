"""adversarial-compliance's deterministic verify: act on the audit's own verdict.

The stage this belongs to spent the whole consolidation as a stub -- a one-sentence inline prompt,
no findings passed in, a free-form `report: dict | None`, and no tools -- so it approved every run
without reading anything. Restoring the prompt and schema is only half the fix: an audit whose
findings nothing consumes is advisory, and advisory is how it came to be ignored.

Honest about its own strength: unlike the coverage or write-scope gates, this one reads a verdict the
MODEL assigned. The divergence findings carry evidence (file/line, test name, the Plan reference they
contradict), but the SEVERITY is judgement, not measurement. It is a real gate -- it blocks the run
and feeds specifics back -- but it is not machine-verified the way a parsed cobertura number is, and
should not be read as such.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..schemas import presence_values as _findings_from
from . import adversarial_audit_checks as _ac
from .adversarial_audit_checks import BLOCKING_SEVERITIES, BLOCKING_VERDICTS, evaluate_audit
from .checks import Check, CheckLog

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

# One bounded minor-sweep lap per run: the FIRST otherwise-passing verify that still carries minor
# findings fails once, so the stage's write-capable fix pass gets one shot at closing what is
# mechanically closeable, and the re-audit referees. Exactly once -- minors are where subjectivity
# lives, and a fix-until-zero-minors loop never converges (an adversarial auditor can always find
# one more). Keyed process-local per (thread_id, run_id): a process restart at worst repeats one
# bounded lap, same tolerance as every other in-memory per-run cache in this codebase.
_MINOR_SWEEP_DONE: set[tuple[str, str]] = set()
MINOR_SWEEP_MARKER = "[minor sweep]"


# Task 13b: one line per DISTINCT rejection reason inside evaluate_audit/verify_adversarial_
# compliance below. Five: the empty-report guard, the missing-verdict and blocking-verdict
# branches of evaluate_audit's own if/elif (two independently-triggered reasons on the same
# field), the per-finding critical/major check, and the one-time minor-sweep bounce that
# verify_adversarial_compliance itself adds on top of evaluate_audit's verdict.
ADVERSARIAL_COMPLIANCE_HARD_RULES: tuple[str, ...] = (
    "You must produce a real report naming a plan_conformance_summary and an overall_verdict "
    "-- an empty or missing report cannot be told apart from an audit that never ran and is "
    "rejected outright.",
    "You must state an overall_verdict -- leaving it blank is itself a failure; this stage "
    "exists to render a judgement, not describe one.",
    "An overall_verdict of major_gaps or fails_to_conform blocks the run -- fix the code, or "
    "argue with evidence that the audit is wrong, rather than downgrading the verdict to pass.",
    "Any single divergence finding whose severity is critical or major blocks the run "
    "regardless of the overall_verdict you assign -- a per-finding critical/major cannot be "
    "waved off by an optimistic summary verdict.",
    "Even once the audit is otherwise clean (no critical/major finding, overall_verdict not "
    "blocking), any remaining minor divergence finding gets exactly one required fix pass -- "
    "close what is mechanically fixable then, not indefinitely later.",
)


ADV_REPORT = Check(
    _ac.CHECK_REPORT, "Audit report present",
    "The audit must hand back a report comparing the code to the approved Plan. An empty report can't "
    "be told apart from an audit that never happened.", "blocking",
)
ADV_VERDICT = Check(
    _ac.CHECK_VERDICT, "Verdict given and not blocking",
    "The audit must commit to an overall verdict, and major_gaps or fails_to_conform stops the run. "
    "A stage whose job is to judge has to actually judge.", "blocking",
)
ADV_BLOCKING_FINDINGS = Check(
    _ac.CHECK_BLOCKING_FINDINGS, "No critical or major divergences",
    "Any single finding rated critical or major blocks, whatever the overall verdict says. An upbeat "
    "summary can't cancel out a serious gap found underneath it.", "blocking",
)
ADV_MINOR_SWEEP = Check(
    "adversarial.minor_sweep", "Minor divergences get one fix pass",
    "When the audit is otherwise clean but minor divergences remain, the stage gets exactly one extra "
    "lap to fix the easy ones. One lap only, because a hunt for zero minors never converges.",
    "blocking", condition="once per run, when the audit is otherwise clean and minor findings remain",
)
VERIFY_CHECKS: tuple[Check, ...] = (ADV_REPORT, ADV_VERDICT, ADV_BLOCKING_FINDINGS, ADV_MINOR_SWEEP)
_CHECK_MAP = {c.id: c for c in VERIFY_CHECKS}
_SWEEP_SCOPE_NOTE = "sweep flag is per-process: a restart can repeat the one sweep lap"


async def verify_adversarial_compliance(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: Any,
    _chat_provider: str, _lap: int = 0, _audit_ran_this_lap: bool = True, *, log: CheckLog | None = None,
) -> "VerificationResult":
    # _chat_provider (StageSpec.deterministic_verify's Ruling-4 addition) is unused: this check has
    # no chat-model dispatch call of its own.
    from ..graph import VerificationResult

    log = log or CheckLog("adversarial-compliance_verify", VERIFY_CHECKS)
    await _snapshot_findings(provider, thread_id, run_id, content_dict)

    passed, tagged, ran = _ac.evaluate_audit_checks(content_dict)
    reasons = [reason for _check_id, reason in tagged]
    for check in VERIFY_CHECKS:
        check_reasons = [reason for check_id, reason in tagged if check_id == check.id]
        if check_reasons:
            log.record_tagged(_CHECK_MAP, [(check.id, "\n".join(check_reasons))])
        elif check.id in ran:
            log.passed(check)
    if passed:
        findings = _findings_from(content_dict.get("divergence_findings"))
        minors = [f for f in findings if str(f.get("severity") or "").lower() == "minor"]
        if not minors:
            log.passed(ADV_MINOR_SWEEP, "no minor findings")
        elif (thread_id, run_id) in _MINOR_SWEEP_DONE:
            log.passed(ADV_MINOR_SWEEP, f"{len(minors)} minor finding(s) left after this run's sweep lap ({_SWEEP_SCOPE_NOTE})")
        if minors and (thread_id, run_id) not in _MINOR_SWEEP_DONE:
            _MINOR_SWEEP_DONE.add((thread_id, run_id))
            log.failed(ADV_MINOR_SWEEP, f"{len(minors)} minor finding(s): one fix lap ({_SWEEP_SCOPE_NOTE})")
            logger.info("adversarial gate: minor sweep -- one fix lap for %d minor finding(s)", len(minors))
            lines = [
                f"- {MINOR_SWEEP_MARKER} [{f.get('severity')}] "
                f"{f.get('plan_reference') or 'unknown plan reference'}: {f.get('description')} -- "
                f"proposed: {f.get('proposed_resolution') or '(none)'}"
                for f in minors
            ]
            return VerificationResult(
                passed=False,
                feedback=(
                    "MINOR SWEEP (one lap, will not repeat): the audit passed -- nothing critical or "
                    "major -- but the minor divergences below are still open. Fix every one that is "
                    "mechanically closeable without risk; SKIP any that requires a judgement call or "
                    "endangers a passing test, and state per finding why you skipped it. The suite "
                    "you leave behind must be at least as green as the one you found:\n"
                    + "\n".join(lines)
                ),
                # blocking_reasons is what graph.make_verify_fix_node hands the write-capable fix
                # pass; without it the sweep lap ran a no-op fix (observed live, run d16959d3 lap 2:
                # verify_fix finished in 0.2 s and the three minors stayed open).
                report={
                    "overall_verdict": content_dict.get("overall_verdict"),
                    "minor_sweep": len(minors),
                    "blocking_reasons": [line[2:] for line in lines],
                },
                checks=[r.to_dict() for r in log.results()],
            )
        return VerificationResult(
            passed=True,
            feedback=(
                f"adversarial audit verdict {content_dict.get('overall_verdict')!r} with "
                f"{len(findings)} divergence finding(s), none critical/major"
            ),
            report={"overall_verdict": content_dict.get("overall_verdict"), "divergence_count": len(findings)},
            checks=[r.to_dict() for r in log.results()],
        )

    logger.info("adversarial gate: blocking (%d reason(s))", len(reasons))
    return VerificationResult(
        passed=False,
        feedback=(
            "The adversarial audit found the implementation does NOT conform to the approved Plan "
            "and Specification. Fix the code (or, where the audit is demonstrably wrong about the "
            "Plan, say so with evidence rather than lowering the finding's severity):\n"
            + "\n".join(f"- {reason}" for reason in reasons)
        ),
        report={
            "overall_verdict": content_dict.get("overall_verdict") if content_dict else None,
            "blocking_reasons": reasons,
        },
        checks=[r.to_dict() for r in log.results()],
    )


async def _snapshot_findings(provider: Any, thread_id: str, run_id: str, content_dict: dict[str, Any] | None) -> None:
    """One ledger row per verify lap with this lap's full findings list -- the exit report's
    divergence ledger diffs the first snapshot against the last to say deterministically which
    findings the fix laps closed and which stayed open (matched by plan_reference; no model
    self-report involved). Best-effort: a failed ledger write must never fail the gate."""
    from .. import repo_files

    try:
        await repo_files.append_ledger_entry(provider, thread_id, {
            "stage": "adversarial-compliance",
            "node": "divergence_snapshot",
            "run_id": run_id,
            "overall_verdict": (content_dict or {}).get("overall_verdict"),
            "findings": [
                {
                    "severity": f.get("severity"),
                    "plan_reference": f.get("plan_reference"),
                    "description": f.get("description"),
                    "proposed_resolution": f.get("proposed_resolution"),
                }
                for f in _findings_from((content_dict or {}).get("divergence_findings"))
            ],
        })
    except Exception:  # noqa: BLE001 -- advisory trail only
        logger.warning("divergence snapshot ledger write failed for thread_id=%s", thread_id, exc_info=True)


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.adversarial_gate`."""
    # An absent or empty report is the failure mode that let this stage rubber-stamp every run.
    for empty in (None, {}):
        passed, reasons = evaluate_audit(empty)
        assert not passed and "no report at all" in reasons[0], empty

    # divergence_findings is DivergenceFindingPresence-wrapped (schemas_audit.py) -- a bare list is
    # only the legacy shape _findings_from tolerates, not what a current content_dict carries.
    absent_findings = {"status": "absent", "values": [], "reason": "no divergences found"}
    conforms = {"overall_verdict": "conforms", "divergence_findings": absent_findings}
    assert evaluate_audit(conforms) == (True, [])

    # minor_gaps passes on purpose -- see BLOCKING_VERDICTS.
    minor = {
        "overall_verdict": "minor_gaps",
        "divergence_findings": {"status": "present", "values": [{"severity": "minor"}]},
    }
    assert evaluate_audit(minor)[0]

    # A blocking verdict blocks.
    for verdict in ("major_gaps", "fails_to_conform"):
        passed, reasons = evaluate_audit({"overall_verdict": verdict, "divergence_findings": absent_findings})
        assert not passed and verdict in reasons[0]

    # A critical/major finding blocks even when the model calls the whole thing "conforms" -- the
    # per-finding severity is not allowed to be contradicted by an optimistic summary verdict.
    contradictory = {
        "overall_verdict": "conforms",
        "divergence_findings": {
            "status": "present",
            "values": [{
                "severity": "critical", "plan_reference": "AC US-0003.2",
                "description": "reset endpoint missing", "evidence": ["apps/api/Program.cs has no /reset route"],
            }],
        },
    }
    passed, reasons = evaluate_audit(contradictory)
    assert not passed
    assert "US-0003.2" in reasons[0] and "Program.cs" in reasons[0], reasons

    # A missing verdict is itself a failure: the stage must commit to a judgement.
    assert not evaluate_audit({"divergence_findings": absent_findings})[0]

    # _findings_from also tolerates a legacy bare list (pre-wrapper on-disk content), never crashes.
    assert _findings_from([{"severity": "minor"}]) == [{"severity": "minor"}]
    assert _findings_from(None) == []
    assert _findings_from({"status": "absent", "values": [], "reason": "x"}) == []

    # Minor sweep: the first otherwise-passing verify with minors fails ONCE with sweep feedback;
    # the second identical call passes. Stubbed provider -- the snapshot write is best-effort.
    import asyncio

    class _StubProvider:
        async def exec_in_sandbox(self, _thread_id, _cmd):
            class _R:
                ok = True
                stdout = ""
                stderr = ""
            return _R()

    _MINOR_SWEEP_DONE.clear()
    minor_report = {
        "overall_verdict": "minor_gaps",
        "divergence_findings": {
            "status": "present",
            "values": [{
                "severity": "minor", "plan_reference": "Plan Step 4",
                "description": "empty-state copy differs from wireframe", "proposed_resolution": "align the copy",
            }],
        },
    }
    first = asyncio.run(verify_adversarial_compliance("t1", minor_report, "r1", None, _StubProvider(), "claude"))
    assert not first.passed and MINOR_SWEEP_MARKER in first.feedback and "Plan Step 4" in first.feedback, first
    # The fix pass reads report["blocking_reasons"] -- the sweep must hand it the minors verbatim.
    assert first.report["blocking_reasons"] and MINOR_SWEEP_MARKER in first.report["blocking_reasons"][0], first.report
    second = asyncio.run(verify_adversarial_compliance("t1", minor_report, "r1", None, _StubProvider(), "claude"))
    assert second.passed, second
    # A different run on the same thread gets its own sweep.
    third = asyncio.run(verify_adversarial_compliance("t1", minor_report, "r2", None, _StubProvider(), "claude"))
    assert not third.passed, third
    # Blocking findings still block regardless of sweep state, and no sweep fires with zero minors.
    _MINOR_SWEEP_DONE.clear()
    clean = asyncio.run(verify_adversarial_compliance(
        "t2",
        {"overall_verdict": "conforms", "divergence_findings": absent_findings},
        "r1", None, _StubProvider(), "claude",
    ))
    assert clean.passed, clean
    _MINOR_SWEEP_DONE.clear()

    # Per-sub-check rows.
    def _statuses(result: Any) -> list[tuple[str, str]]:
        return [(r["id"], r["status"]) for r in result.checks]

    ok = [(ADV_REPORT.id, "passed"), (ADV_VERDICT.id, "passed"), (ADV_BLOCKING_FINDINGS.id, "passed")]
    assert _statuses(clean) == ok + [(ADV_MINOR_SWEEP.id, "passed")], clean.checks
    assert _statuses(first) == ok + [(ADV_MINOR_SWEEP.id, "failed")], first.checks
    assert "per-process" in first.checks[-1]["detail"], first.checks
    assert _statuses(second) == ok + [(ADV_MINOR_SWEEP.id, "passed")] and "per-process" in second.checks[-1]["detail"]
    empty = asyncio.run(verify_adversarial_compliance("t3", None, "r1", None, _StubProvider(), "claude"))
    assert _statuses(empty) == [(ADV_REPORT.id, "failed")], empty.checks  # later checks never reached
    shared = CheckLog("adversarial-compliance_verify", VERIFY_CHECKS, strict=True)
    blocked = asyncio.run(verify_adversarial_compliance(
        "t3", {**contradictory, "overall_verdict": "major_gaps"}, "r1", None, _StubProvider(), "claude", log=shared,
    ))
    assert _statuses(blocked) == [(ADV_REPORT.id, "passed"), (ADV_VERDICT.id, "failed"), (ADV_BLOCKING_FINDINGS.id, "failed")]
    assert "US-0003.2" in blocked.checks[2]["detail"] and len(shared.results()) == 3
    _MINOR_SWEEP_DONE.clear()

    # Every declared Check is referenced: tagged by the helper or logged here (text scan).
    import inspect
    import sys

    helper_src = inspect.getsource(_ac.evaluate_audit_checks)
    gate_src = inspect.getsource(sys.modules[__name__].verify_adversarial_compliance)
    for check in VERIFY_CHECKS:
        name = next((k for k, v in vars(_ac).items() if k.startswith("CHECK_") and v == check.id), None)
        var = next(k for k, v in globals().items() if v is check)
        referenced = helper_src.count(name) >= 2 if name else f"({var}" in gate_src
        assert referenced, f"{check.id} is declared but never recorded"

    # Task 13b: ADVERSARIAL_COMPLIANCE_HARD_RULES -- one line per real rejection branch in
    # evaluate_audit/verify_adversarial_compliance (see the constant's own comment for the count
    # breakdown).
    assert len(ADVERSARIAL_COMPLIANCE_HARD_RULES) == 5, len(ADVERSARIAL_COMPLIANCE_HARD_RULES)
    assert all(isinstance(r, str) and r.strip() for r in ADVERSARIAL_COMPLIANCE_HARD_RULES)

    print("adversarial_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
