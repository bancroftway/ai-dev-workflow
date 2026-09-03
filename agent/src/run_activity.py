"""Process-local, in-memory, reference-counted "is a run actually executing right now" signal.

Mirrors sandbox/registry.py's module-level-dict pattern (SPECIFICATION.md Decision 4: small
internal tool, don't over-engineer). Refcounted, not boolean: an overlapping reattach/duplicate
run on the same session id must not have one finishing clear the other's active flag.

# ponytail: process-local refcount, single-instance only -- needs a shared store (Redis/DB row)
if this ever runs multi-worker; not needed today (docker-entrypoint.sh runs uvicorn with no
--workers flag), same caveat registry.py and checkpoint.py's AsyncSqliteSaver already carry.
"""

from __future__ import annotations

_counts: dict[str, int] = {}


def incr(session_id: str) -> None:
    key = session_id.lower()
    _counts[key] = _counts.get(key, 0) + 1


def decr(session_id: str) -> None:
    key = session_id.lower()
    count = _counts.get(key, 0) - 1
    if count > 0:
        _counts[key] = count
    else:
        _counts.pop(key, None)


def is_active(session_id: str) -> bool:
    return _counts.get(session_id.lower(), 0) > 0


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.run_activity`."""
    # Case-insensitive keys: the frontend mints lowercase UUIDs (crypto.randomUUID()) but SQL
    # Server round-trips UNIQUEIDENTIFIER uppercase (same reason session_store/run_event_store
    # normalize to lowercase on read) -- normalize here too so neither caller has to remember to.
    incr("ABC-123")
    assert is_active("abc-123") is True

    # Overlapping runs on the same id (reattach racing a still-finishing prior stream): one
    # decr() must not clear the other's active flag.
    incr("abc-123")
    decr("abc-123")
    assert is_active("abc-123") is True, "second incr() must survive the first decr()"

    # Decrement-to-zero cleanup: is_active flips false, and the dict doesn't leak the key.
    decr("abc-123")
    assert is_active("abc-123") is False
    assert "abc-123" not in _counts, "count should be popped at zero, not left at 0"

    # Extra/redundant decr() past zero is a safe no-op, never negative, never raises.
    decr("abc-123")
    assert is_active("abc-123") is False

    print("run_activity self-check passed")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.run_activity
    _demo()
