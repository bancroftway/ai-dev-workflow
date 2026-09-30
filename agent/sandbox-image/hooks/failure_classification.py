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
    # Transient network/DNS failures reaching the provider API. Every one of these classified as
    # gate_exhausted before, i.e. "the generated content is wrong" -- so a dropped connection was
    # billed to the model's content-retry budget exactly the way the missing mermaid config and the
    # session-limit message were (observed live, run 026dee4f: the internet dropped mid-fix and the
    # rebuild fix lap was charged for it). Node's errno spellings are what the Claude CLI actually
    # surfaces; "can't reach the api server" is its own human-readable wrapper around them.
    "enotfound",
    "econnreset",
    "econnrefused",
    "etimedout",
    "eai_again",
    "socket hang up",
    "can't reach the api server",
    "cannot reach the api server",
)

# Substrings that specifically mean "the model's own request quota/rate limit was hit" -- reported
# as quota_exhausted rather than the more generic infra_transient, since a human reading the run
# failure benefits from knowing "wait for quota to reset" versus "the sandbox/network glitched."
_QUOTA_FAILURE_MARKERS = (
    "rate limit",
    "quota",
    "429",
    "too many requests",
    # The Claude CLI's own wording for a subscription cap, which contains neither "rate limit" nor
    # "quota": `You've hit your session limit - resets 2am (UTC)`, plus the usage-cap variant.
    # Without these the message fell through to gate_exhausted, so rebuild.py counted an unrunnable
    # lap against its retry budget and burned all four cycles in under two minutes on a condition
    # no amount of redrafting could fix (observed live, run 026dee4f: five approved stages lost at
    # the r_ac_to_tests gate).
    "session limit",
    "usage limit",
    "weekly limit",
    # The GENERAL form, added after "weekly limit" slipped through a list that already had
    # "session limit" and "usage limit": the Claude CLI phrases every cap as "You've hit your
    # <period> limit - resets <when>", so enumerating periods is a losing game -- each new one
    # misclassifies a provider outage as the model failing its own gate, which is what
    # `gate_exhausted` means and is the single most misleading verdict this module can produce.
    # Matching the sentence stem catches whatever period comes next without another incident.
    "hit your",
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
    # The Claude CLI's exact subscription-cap wording -- neither phrase contains "rate limit" or
    # "quota", so both need their own markers or a run dies as if its content were at fault.
    assert classify_failure(
        "reported an error (stop_reason='stop_sequence'): \"You've hit your session limit - resets 2am (UTC)\""
    ) == "quota_exhausted"
    assert classify_failure("Claude usage limit reached") == "quota_exhausted"
    # Every period the CLI phrases as "You've hit your <period> limit". Enumerating them failed
    # twice (session, then weekly), so the stem is what is actually asserted here.
    assert classify_failure("You've hit your weekly limit - resets Aug 29, 2am (UTC)") == "quota_exhausted"
    assert classify_failure("You've hit your 5-hour limit - resets at noon") == "quota_exhausted"
    # Network drops are infra, never a content verdict -- see _INFRA_FAILURE_MARKERS.
    assert classify_failure(
        "API Error: Can't reach the API server - check your internet or DNS (ENOTFOUND)"
    ) == "infra_transient"
    assert classify_failure("getaddrinfo EAI_AGAIN api.anthropic.com") == "infra_transient"
    assert classify_failure("read ECONNRESET") == "infra_transient"
    # ...but a genuine content failure that merely mentions a network-ish word must NOT be excused.
    assert classify_failure("AC-3: the retry banner never appears after a failed fetch") == "gate_exhausted"
    assert classify_failure("AC-12 has no failing test") == "gate_exhausted"
    assert classify_failure("") == "gate_exhausted"
    print("failure_classification self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
