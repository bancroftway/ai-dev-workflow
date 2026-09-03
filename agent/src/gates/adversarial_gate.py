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

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

# Verdicts that block. "minor_gaps" passes deliberately: the stage is meant to surface small
# divergences for the record without deadlocking a run over cosmetic drift, and a run that cannot
# ever pass its own audit teaches people to disable the audit.
BLOCKING_VERDICTS = frozenset({"major_gaps", "fails_to_conform"})
BLOCKING_SEVERITIES = frozenset({"critical", "major"})

# One bounded minor-sweep lap per run: the FIRST otherwise-passing verify that still carries minor
# findings fails once, so the stage's write-capable fix pass gets one shot at closing what is
# mechanically closeable, and the re-audit referees. Exactly once -- minors are where subjectivity
# lives, and a fix-until-zero-minors loop never converges (an adversarial auditor can always find
# one more). Keyed process-local per (thread_id, run_id): a process restart at worst repeats one
# bounded lap, same tolerance as every other in-memory per-run cache in this codebase.
_MINOR_SWEEP_DONE: set[tuple[str, str]] = set()
MINOR_SWEEP_MARKER = "[minor sweep]"


def _findings_from(entry: Any) -> list[dict[str, Any]]:
    """`divergence_findings`'s values, tolerating the DivergenceFindingPresence-wrapped shape
    (schemas_audit.py: `{"status": ..., "values": [...], "reason": ...}`), a legacy/degenerate bare
    list, or a missing/None field. `content_dict` here is a plain dict off the wire, never
    re-validated through the Pydantic wrapper after initial parse, so this reads defensively rather
    than assuming the typed shape."""
    if isinstance(entry, dict):
        return list(entry.get("values") or [])
    if isinstance(entry, list):
        return list(entry)
    return []


def evaluate_audit(report: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """(passed, reasons). Pure, so the routing logic is testable without a sandbox.

    An ABSENT or empty report fails: this stage's entire job is to produce a judgement, and "no
    report" previously sailed through as approval. Blocking on it is the difference between a gate
    and a formality.
    """
    if not report:
        return False, [
            "the adversarial audit produced no report at all -- this stage must return a "
            "plan_conformance_summary and an overall_verdict, and an empty report cannot be "
            "distinguished from an audit that never happened"
        ]

    reasons: list[str] = []
    verdict = str(report.get("overall_verdict") or "").strip()
    if not verdict:
        reasons.append("no overall_verdict was reported")
    elif verdict in BLOCKING_VERDICTS:
        reasons.append(f"overall_verdict is {verdict!r}")

    blocking = [
        finding
        for finding in _findings_from(report.get("divergence_findings"))
        if str(finding.get("severity") or "").lower() in BLOCKING_SEVERITIES
    ]
    for finding in blocking:
        # Feedback names the Plan reference and the evidence, not just a count -- a redraft needs to
        # know WHICH criterion diverged and how it was established, the same reason the coverage gate
        # reports per-line gaps rather than a bare percentage.
        evidence = "; ".join(str(e) for e in (finding.get("evidence") or [])) or "(no evidence cited)"
        reasons.append(
            f"[{finding.get('severity')}] {finding.get('plan_reference') or 'unknown plan reference'}: "
            f"{finding.get('description') or '(no description)'} -- evidence: {evidence}"
        )
    return not reasons, reasons


async def verify_adversarial_compliance(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: Any,
    _chat_provider: str,
) -> "VerificationResult":
    # _chat_provider (StageSpec.deterministic_verify's Ruling-4 addition) is unused: this check has
    # no chat-model dispatch call of its own.
    from ..graph import VerificationResult

    await _snapshot_findings(provider, thread_id, run_id, content_dict)

    passed, reasons = evaluate_audit(content_dict)
    if passed:
        findings = _findings_from(content_dict.get("divergence_findings"))
        minors = [f for f in findings if str(f.get("severity") or "").lower() == "minor"]
        if minors and (thread_id, run_id) not in _MINOR_SWEEP_DONE:
            _MINOR_SWEEP_DONE.add((thread_id, run_id))
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
            )
        return VerificationResult(
            passed=True,
            feedback=(
                f"adversarial audit verdict {content_dict.get('overall_verdict')!r} with "
                f"{len(findings)} divergence finding(s), none critical/major"
            ),
            report={"overall_verdict": content_dict.get("overall_verdict"), "divergence_count": len(findings)},
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

    print("adversarial_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
