"""Shared infra-vs-content failure classification.

Used wherever a raw process/tool-output string needs to be told apart from a genuine content or
gate failure: run-failure escalation payloads (graph.py, rebuild.py, e2e_nodes.py,
test_hardening_nodes.py) and diagram_gate's mermaid render check. One shared marker list instead
of each call site independently guessing -- started from diagram_gate.py's own
_INFRA_FAILURE_MARKERS (a headless-Chromium/Puppeteer dependency failure), generalized to the
sandbox connect-handshake timeout (sandbox/provider.py) and the GitHub-side Copilot session errors
observed live (agent/e2e_run*.log: "No server is currently available to service your request").
"""

from __future__ import annotations

from typing import Literal

FailureType = Literal["gate_exhausted", "infra_transient", "quota_exhausted"]

# Substrings (checked case-insensitively) that mean "the environment broke," not "the content is
# wrong."
_INFRA_FAILURE_MARKERS = (
    "command not found",
    "cannot find module",
    "failed to launch the browser process",
    "error while loading shared libraries",
    "did not complete a connect handshake",
    "no server is currently available",
    "connection closed with no response",
    # rebuild.py's fix_node tags its own stderr_tail with this exact prefix when an infra_retry
    # exhaustion meant the fix lap never actually ran -- a real marker, not a guess, so it belongs
    # here alongside the others rather than relying solely on whatever the raw exception said.
    "infra failure, fix lap not attempted",
)

# Substrings that specifically mean "the model's own request quota/rate limit was hit" -- reported
# as quota_exhausted rather than the more generic infra_transient, since a human reading the run
# failure benefits from knowing "wait for quota to reset" versus "the sandbox/network glitched."
_QUOTA_FAILURE_MARKERS = (
    "rate limit",
    "quota",
    "429",
    "too many requests",
)


def classify_failure(detail: str) -> FailureType:
    """Best-effort classification of a raw error/stderr string. Defaults to gate_exhausted (a real
    content/verification failure) when nothing infra-shaped is recognized -- the safer default,
    since mislabeling a genuine defect as infra would hide it from the user rather than the
    reverse."""
    lowered = (detail or "").lower()
    if any(marker in lowered for marker in _QUOTA_FAILURE_MARKERS):
        return "quota_exhausted"
    if any(marker in lowered for marker in _INFRA_FAILURE_MARKERS):
        return "infra_transient"
    return "gate_exhausted"


def _demo() -> None:
    """`cd agent && uv run python -m src.failure_classification`."""
    assert classify_failure("bash: mmdc: command not found") == "infra_transient"
    assert classify_failure(
        "RuntimeError: sandbox at localhost:9 did not complete a connect handshake within 60.0s"
    ) == "infra_transient"
    assert classify_failure("GitHub returned: No server is currently available to service your request.") == "infra_transient"
    assert classify_failure("[infra failure, fix lap not attempted] RuntimeError: session closed unexpectedly") == "infra_transient"
    assert classify_failure("429 Too Many Requests -- quota exceeded") == "quota_exhausted"
    assert classify_failure("AC-12 has no failing test") == "gate_exhausted"
    assert classify_failure("") == "gate_exhausted"
    print("failure_classification self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
