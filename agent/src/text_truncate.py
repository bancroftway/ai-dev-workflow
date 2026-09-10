"""Shared head+tail truncation for captured command/log output that feeds back into a model.

A plain `text[-N:]` throws away the START of long output -- for a multi-error compiler log or a
multi-item findings list, that's exactly where the first (often most actionable) items live, so
every fix/redraft lap only ever sees the same trailing subset. Keeping both ends with an explicit
"chars omitted" marker (the shape `gates/diagram_gate.py` already used before this was extracted)
fixes that without going unbounded.
"""

from __future__ import annotations


def truncate_middle(text: str, head_chars: int, tail_chars: int) -> str:
    threshold = head_chars + tail_chars
    if len(text) <= threshold:
        return text
    omitted = len(text) - threshold
    return f"{text[:head_chars]}\n...[{omitted} chars omitted]...\n{text[-tail_chars:]}"


def _demo() -> None:
    """`cd agent && uv run python -m src.text_truncate`."""
    short = "a" * 50
    assert truncate_middle(short, 40, 40) == short

    long_text = "HEAD" + "x" * 1000 + "TAIL"
    result = truncate_middle(long_text, 4, 4)
    assert result.startswith("HEAD\n")
    assert result.endswith("\nTAIL")
    assert "chars omitted" in result

    # Exact boundary: len == threshold must NOT truncate.
    boundary = "b" * 80
    assert truncate_middle(boundary, 40, 40) == boundary
    assert truncate_middle(boundary + "b", 40, 40) != boundary + "b"

    print("text_truncate: ok")


if __name__ == "__main__":
    _demo()
