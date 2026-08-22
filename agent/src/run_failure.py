"""Shared terminal-run-failure recorder.

Consolidates what were four independent near-duplicate implementations (graph.py's stage-level
make_escalate_node, rebuild.py's make_escalate_node, e2e_nodes.py's e2e_escalate_node,
test_hardening_nodes.py's test_hardening_exit_escalate_node), each building a `run_failure`
payload and calling git_ops.record_run_failure directly with no shared vocabulary for WHY a run
died. Kept as its own module (not folded into graph.py) so rebuild.py/e2e_nodes.py/
test_hardening_nodes.py can import it without a circular import back through graph.py, which
already imports all three of them.
"""

from __future__ import annotations

from typing import Any

from . import git_ops
from .failure_classification import classify_failure


async def record_run_failure_and_reset(
    thread_id: str,
    run_id: str | None,
    *,
    payload: dict[str, Any],
    detail_for_classification: str,
    default_failure_type: str = "gate_exhausted",
) -> dict[str, Any]:
    """Records `payload` as the run's terminal failure, after adding a `failure_type` field
    alongside whatever `type` the call site already set. `type` keeps its existing meaning at every
    call site (WHICH ceiling was hit -- cannot_verify / rebuild_cap_exceeded / e2e_cap_exceeded /
    ...); `failure_type` answers a different question (WHY: a real gate-verified defect vs. a
    quota/timeout/infra failure) so the two are surfaced as separate fields rather than overloading
    one, in src/lib/workflow-types.ts's EscalationPayload and rendered distinctly in MetricsBar.tsx.

    `classify_failure(detail_for_classification)` is tried first (it can still positively identify
    `quota_exhausted` from a "rate limit"/"429" string even when the call site's own default is
    `gate_exhausted`), but a caller that already KNOWS this escalation is infra-caused by
    construction -- e.g. graph.py's make_draft_escalate_node, which is only ever reached via the
    infra_retry-exhaustion path, never a genuine content failure -- should pass
    `default_failure_type="infra_transient"` rather than trust classify_failure's string-marker
    guess alone. A Copilot RuntimeError's message does not always contain one of the known infra
    substrings, and silently falling through to the generic `gate_exhausted` default in that case
    would throw away certain structural knowledge for an uncertain string match."""
    classified = classify_failure(detail_for_classification)
    failure_type = classified if classified != "gate_exhausted" else default_failure_type
    full_payload = {**payload, "failure_type": failure_type}
    await git_ops.record_run_failure(thread_id, full_payload, run_id)
    return full_payload


def _demo() -> None:
    """Pure self-check of the classification wiring -- the git_ops.record_run_failure half needs a
    sandbox. `cd agent && uv run python -m src.run_failure`."""
    from .failure_classification import classify_failure as _classify

    detail = "RuntimeError: sandbox at localhost:9 did not complete a connect handshake within 60.0s"
    assert _classify(detail) == "infra_transient"
    detail = "AC-12 has no failing test"
    assert _classify(detail) == "gate_exhausted"
    print("run_failure self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
