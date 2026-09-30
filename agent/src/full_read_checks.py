"""Pure, dependency-free (stdlib only) whole-file-read coverage check -- extracted from
`claude_chat_model.py` (2026-09-29) specifically so the sandbox's own same-turn Stop hook
(`sandbox-image/hooks/check-full-read-stop.mjs`) can run the REAL check by shelling out to
`python3` on this ONE file, instead of a hand-ported JavaScript reimplementation (`coversWholeFile`)
drifting from it -- that hook's own header used to say "no automated drift guard for this one", the
exact drift risk `gates/coverage_parsing.py`/`gates/test_quality_checks.py`/
`gates/wireframe_linkage_checks.py` were each extracted to close.

Deliberately NOT everything `read_full_file_reads` does: that function is impure (it awaits
`provider.exec_in_sandbox` to fetch a Claude session's own transcript from OUTSIDE the sandbox --
real async I/O with no business running inside a per-turn Stop hook, unlike this module's own
target). Only the pure tail of it -- given the (start, end) line ranges a transcript's Read tool_use
calls already resolved to, does their union cover the whole file with no gap? -- is provably pure (a
list of tuples and an int in, a bool out, confirmed by this file's own self-check), so it is the ONE
piece this pipeline ships into `/opt/aidw-hooks/` and calls directly.

check-full-read-stop.mjs computes its own (start, end) ranges from the turn's OWN transcript file,
already sitting on disk inside the sandbox (`input.transcript_path` -- no sandbox-exec round trip
needed there at all) -- that transcript-parsing logic stays in the `.mjs` file exactly as before
(see that hook's own header); only the interval-union coverage check itself moves here.

`claude_chat_model.py` imports `_covers_whole_file` UNCHANGED (this is a pure code-move, not a
fork); its own self-check still exercises it through `read_full_file_reads`' own tests, proving
this extraction changed no behavior.

CLI mode (`python3 full_read_checks.py --check-hook`, stdin JSON: `{"ranges": [[start, end], ...],
"total_lines": int}`, stdout JSON: `{"covered": bool}`) is what the Stop hook actually invokes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _covers_whole_file(ranges: list[tuple[int, int]], total_lines: int) -> bool:
    """Pure interval-union check: do these 1-indexed, inclusive (start, end) line ranges together
    cover [1, total_lines] with no gap? Split out from read_full_file_reads so this logic has a
    sandbox-free self-check (ponytail: non-trivial branch/loop logic needs one runnable check).
    """
    sorted_ranges = sorted(ranges)
    covered_through = 0
    for start, end in sorted_ranges:
        if start > covered_through + 1:
            break  # gap in coverage -- union stops advancing here, whatever follows can't close it
        covered_through = max(covered_through, end)
    return covered_through >= total_lines


def evaluate(ranges: list[list[int] | tuple[int, int]], total_lines: int) -> dict[str, bool]:
    """The Stop hook's actual question, as one JSON-friendly call: {"covered": bool}. A thin
    wrapper so the CLI entrypoint below has no logic of its own to drift from what a caller
    importing this module gets."""
    return {"covered": _covers_whole_file([tuple(r) for r in ranges], total_lines)}


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.full_read_checks`
    """Self-check: no sandbox, no DB -- every function here is pure.

    Also re-proves the sandbox-image STAGING COPY (sandbox-image/hooks/full_read_checks.py, what
    the Dockerfile actually COPYs into /opt/aidw-hooks/ -- see check-full-read-stop.mjs's own
    header for why a physical copy exists at all: Docker's build context there is
    agent/sandbox-image, which cannot COPY a path outside itself) is byte-identical to THIS file.
    Skipped, not failed, when the staged copy is absent -- this file is also imported standalone by
    claude_chat_model.py in contexts (packaging, a checkout without sandbox-image/) where that path
    was never expected to exist."""
    staged_copy = Path(__file__).resolve().parents[1] / "sandbox-image" / "hooks" / "full_read_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/full_read_checks.py hooks/full_read_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    # Same cases claude_chat_model.py's own self-check used to exercise directly against
    # `_covers_whole_file` (unchanged here -- this extraction is a pure code-move, not a fork).
    assert _covers_whole_file([(1, 2000)], 1500), "one call whose window exceeds the file must cover it"
    assert not _covers_whole_file([], 1500), "no matching Read calls at all must not count as covered"
    assert not _covers_whole_file([(500, 2000)], 1500), "a read that skips the start of the file is incomplete"
    assert _covers_whole_file([(1, 2000), (2001, 3000)], 3000), "two adjoining reads must union to full coverage"
    assert not _covers_whole_file([(1, 1000), (1500, 3000)], 3000), "a gap between reads must not count as covered"
    assert _covers_whole_file([(2001, 3000), (1, 2000)], 3000), "out-of-order calls must still union correctly"
    assert _covers_whole_file([(1, 2000), (1, 2000)], 2000), "a repeated identical read must not double-count wrongly"

    assert evaluate([[1, 2000], [2001, 3000]], 3000) == {"covered": True}
    assert evaluate([], 1500) == {"covered": False}

    print("full_read_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"ranges": [...], "total_lines": int} on stdin,
        # JSON result on stdout. Deliberately the ONLY thing this branch does -- no sandbox access,
        # no transcript I/O; the hook already parsed its own transcript and hands the ranges over
        # as data.
        payload = json.loads(sys.stdin.read())
        json.dump(evaluate(payload.get("ranges") or [], payload.get("total_lines") or 0), sys.stdout)
    else:
        _demo()
