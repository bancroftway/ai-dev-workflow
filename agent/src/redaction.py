"""Shared secret-shaped-string scrubber (Part 2 Task 5: redaction at the point of capture).

Extracted, not reinvented, from telemetry.py's own `traced_exec` -- which has redacted long
base64-ish runs out of sandbox exec commands before they become OTEL span attributes since before
this task existed (git_ops.push_head embeds its credential-helper script as exactly one such
blob). Tasks 1/3/4 built the first human-facing event log in this codebase that carries raw
captured tool-call input/output (RunEvent.payload/summary -- durable via run_event_store, live via
run_event_stream) -- nothing scrubbed THAT content before this task. Same detector, same 40-char
threshold, now reachable from both call sites instead of living only inside telemetry.py.

Detector: a contiguous run of base64-ish characters, >=40 long. 40 is deliberate (telemetry.py's
own original comment, preserved here verbatim): a classic 40-char `ghp_`-style token sits exactly
at that threshold -- never raise it.
"""

from __future__ import annotations

import re
from typing import Any

SECRET_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def redact_text(text: str) -> str:
    """Replace every contiguous secret-shaped run in `text` with a fixed marker."""
    return SECRET_RUN.sub("<redacted>", text)


def redact_value(value: Any) -> Any:
    """Recursively apply redact_text to every string inside `value`.

    `value` is normally a RunEvent.payload dict -- both providers' _translate_intermediate_events
    build it from whatever the CLI's own JSONL happened to carry (a tool's raw `input`/`command`, a
    tool_result's raw `content`/output), arbitrarily nested and not just one flat string. Dicts and
    lists are walked structurally (real payloads are json.loads output, which never produces a
    tuple, so that type isn't handled separately); every other leaf type (int, float, bool, None)
    passes through unchanged -- there is nothing string-shaped to scrub in a bool or a None, and
    coercing one into a different type would corrupt the payload for no security benefit.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def _demo() -> None:
    """`cd agent && .venv/Scripts/python.exe -m src.redaction`."""
    token = "ghp_" + "x1Y2z3" * 8  # 52 chars, base64-ish -- comfortably over the 40-char threshold

    # The boundary the module docstring calls out explicitly: 39 chars must pass through untouched,
    # 40 must be redacted. Never let this drift without noticing.
    assert redact_text("a" * 39) == "a" * 39, "39 chars must NOT be redacted"
    assert redact_text("a" * 40) == "<redacted>", "40 chars MUST be redacted"

    # A realistic sentence: the secret-shaped run is scrubbed, surrounding plain text is not.
    assert redact_text(f"Authorization: Bearer {token} sent") == "Authorization: Bearer <redacted> sent"
    # Two independent runs in one string both get caught.
    assert redact_text(f"{token} and also {token}") == "<redacted> and also <redacted>"
    # Plain text with nothing secret-shaped is untouched.
    assert redact_text("git status") == "git status"

    # redact_value walks nested dict/list structure -- the actual shape a RunEvent.payload takes (a
    # tool's `input` dict, a tool_result's `content` list) -- scrubbing every string leaf while
    # leaving keys, structure, and non-string leaves (bool/int/None) alone.
    nested = {
        "name": "Bash",
        "input": {"command": f"echo {token}", "description": "ordinary text"},
        "result": ["prefix", token, {"nested_again": token}],
        "is_error": False,
        "retries": 0,
        "detail": None,
    }
    scrubbed = redact_value(nested)
    assert scrubbed == {
        "name": "Bash",
        "input": {"command": "echo <redacted>", "description": "ordinary text"},
        "result": ["prefix", "<redacted>", {"nested_again": "<redacted>"}],
        "is_error": False,
        "retries": 0,
        "detail": None,
    }
    assert token not in str(scrubbed), "token must not survive anywhere in the scrubbed structure"

    print("redaction self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && <venv>/python -m src.redaction
    _demo()
