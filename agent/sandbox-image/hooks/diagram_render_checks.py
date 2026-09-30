"""mermaid-cli render + infra-vs-syntax classification -- extracted from `diagram_gate.py`
(2026-09-30, Task 12) specifically so `check-diagram-render-stop.mjs` (a same-turn Stop hook,
already running INSIDE the sandbox that bakes in `@mermaid-js/mermaid-cli`/headless Chromium) can
shell out to the IDENTICAL invocation/classification `diagram_gate.py`'s own real gate
(`verify_plan_diagrams` -> `_render_one`) uses, instead of a hand-ported reimplementation of the
mmdc command/markers drifting from it -- same "duplicate the logic, or ship the real thing"
reasoning as `gates/wireframe_linkage_checks.py`'s own header.

Why a NEW sibling module, not folded into wireframe_linkage_checks.py: that module is
wireframe/AC-linkage-shaped (a list of dicts in, a list of strings out, fully side-effect-free);
this one actually spawns a subprocess (mmdc) and reads its output -- not the same contract, and
mixing the two would blur wireframe_linkage_checks.py's own "provably pure" self-check story.

Why `diagram_gate.py`'s own `_render_one` is NOT simply reused unchanged: it goes through
`provider.exec_in_sandbox` (an async, session/DB-aware remote-exec abstraction), because it runs
OUTSIDE the sandbox against a remote container over the wire. `check-diagram-render-stop.mjs` runs
INSIDE the sandbox already (same filesystem, same mmdc install) -- no `thread_id`/DB/`run_id`/live
LangGraph state needed at all, just `manifest.json`'s diagram entries and the `.mmd` files already
on disk (the model's own file edits already wrote them this turn; this module never writes one
itself). It shells out to THIS module's own `render_diagram` via `python3 -m` instead, which spawns
mmdc directly via `subprocess.run`. The command construction (argv) and infra-vs-syntax
classification below are the two pieces that would otherwise be a second, independently-maintained
copy -- both extracted here unchanged, and imported back into `diagram_gate.py` so its own
`_render_one` calls them too (see that function's own comments).

CLI mode (`python3 diagram_render_checks.py --render-hook`, stdin JSON: `{"diagrams": [{"name":
..., "mmd_path": ...}, ...]}` -- each `mmd_path` must already exist on disk; SVG output goes to a
scratch temp directory this module creates itself, never back into the repo checkout, so the
render hook can never trip a write-scope violation or leave a stray diff -- stdout JSON:
`{"failures": [{"name": ..., "is_infra_failure": ..., "stderr_tail": ...}, ...]}`, one entry per
diagram that failed to render; an empty list means every diagram rendered clean) is what the Stop
hook actually invokes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

try:  # package context (agent/src/gates) -- the normal import path for every in-process caller.
    from .wireframe_linkage_checks import SAFE_DIAGRAM_NAME_RE
except ImportError:  # standalone script context: staged alone into /opt/aidw-hooks/ (see the
    # Dockerfile's byte-identical staged-copy convention) -- a relative import has no parent
    # package to resolve against when this file is executed directly, so fall back to a flat
    # import of the sibling module Python's own script-directory sys.path entry already finds.
    from wireframe_linkage_checks import SAFE_DIAGRAM_NAME_RE  # type: ignore[no-redef]

try:
    from ..failure_classification import classify_failure
except ImportError:
    from failure_classification import classify_failure  # type: ignore[no-redef]

try:
    from ..text_truncate import truncate_middle
except ImportError:
    from text_truncate import truncate_middle  # type: ignore[no-redef]

# Fixed sandbox-image path (agent/sandbox-image/Dockerfile writes this file, historically around
# line 679) -- not an operator-tunable value (AGENTS.md's config.py criterion: a deploy operator
# never plausibly overrides an internal detail of what THIS image bakes in), same "plain constant"
# treatment as diagram_gate.py's own DIAGRAMS_DIR/WIREFRAMES_DIR.
MERMAID_PUPPETEER_CONFIG_PATH = "/opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json"

# Companion to config.py's DIAGRAM_ERROR_SUMMARY_HEAD/TAIL_CHARS/LINES_MAX/JOINED_CHARS -- read
# directly via os.environ, same reasoning as wireframe_linkage_checks.MAX_WIREFRAME_BYTES:
# config.py itself pulls in dependencies this sandbox-side module must not (see this file's own
# header). Same env var names as config.py's own reads -- two reads of the same env var, not a
# second knob (AGENTS.md's own rule) -- kept in sync by that identity, not by import.
DIAGRAM_ERROR_SUMMARY_HEAD_CHARS = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_HEAD_CHARS", "2000"))
DIAGRAM_ERROR_SUMMARY_TAIL_CHARS = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_TAIL_CHARS", "2000"))
DIAGRAM_ERROR_SUMMARY_LINES_MAX = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_LINES_MAX", "10"))
DIAGRAM_ERROR_SUMMARY_JOINED_CHARS = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_JOINED_CHARS", "700"))

# mmdc names a genuine source problem in one of these shapes. Anything else it fails on -- a
# missing puppeteer config, no browser binary, a crashed Chromium -- is environmental, and telling
# the draft node to "fix your Mermaid" for it is unactionable. Moved byte-for-byte from
# diagram_gate.py's private `_MERMAID_SYNTAX_MARKERS`.
MERMAID_SYNTAX_MARKERS = re.compile(
    r"parse error|syntax error|expecting|unrecognized text|no diagram type detected", re.IGNORECASE
)


@dataclass(frozen=True)
class RenderOutcome:
    name: str
    ok: bool
    is_infra_failure: bool
    stderr_tail: str


def looks_like_infra_failure(stderr: str) -> bool:
    # Delegates to the repo-wide classifier (failure_classification.py) instead of a gate-local
    # marker list, so this check, the sandbox connect-handshake retry, and every escalate node's
    # failure_type tagging agree on what "infra, not content" means. Moved byte-for-byte from
    # diagram_gate.py's private `_looks_like_infra_failure`.
    return classify_failure(stderr) == "infra_transient"


def mermaid_error_summary(output: str) -> str:
    """The actionable mermaid parse error ('Parse error on line N ... Expecting ...') is at the
    TOP of mmdc's output; the tail is a useless puppeteer JS stack. Moved byte-for-byte from
    diagram_gate.py's private `_mermaid_error_summary`. Pure."""
    lines = [l.strip() for l in output.splitlines() if l.strip() and not l.lstrip().startswith("at ")]
    return " | ".join(lines[:DIAGRAM_ERROR_SUMMARY_LINES_MAX])[:DIAGRAM_ERROR_SUMMARY_JOINED_CHARS]


def build_mmdc_argv(
    mmd_path: str, svg_path: str, puppeteer_config_path: str = MERMAID_PUPPETEER_CONFIG_PATH
) -> list[str]:
    """The exact mmdc invocation, as an argv list -- ONE source of truth for both callers:
    diagram_gate.py's own `_render_one` (which shell-quotes this into a string for
    `provider.exec_in_sandbox`'s remote shell) and `render_diagram` below (which passes it straight
    to `subprocess.run`, no shell). Pure."""
    return [
        "npx", "--yes", "@mermaid-js/mermaid-cli",
        "-i", mmd_path, "-o", svg_path,
        "-p", puppeteer_config_path,
    ]


def classify_render_output(ok: bool, raw_output: str) -> tuple[bool, str]:
    """The PURE half of a render outcome -- given whether mmdc exited zero and its captured
    output, decide infra-vs-syntax and build the trimmed `stderr_tail`, exactly as
    diagram_gate.py's own `_render_one` does. Split out so a unit test (and diagram_gate.py itself)
    can exercise the classification without spawning mmdc. Returns (is_infra_failure, stderr_tail).
    Pure."""
    # HEAD as well as tail -- mmdc puts the actionable "Parse error on line N ... Expecting ..." at
    # the front; a plain tail-only slice would throw that away on a long output. See
    # text_truncate.truncate_middle's own docstring.
    stderr_tail = truncate_middle(raw_output, DIAGRAM_ERROR_SUMMARY_HEAD_CHARS, DIAGRAM_ERROR_SUMMARY_TAIL_CHARS)
    is_infra = not ok and (looks_like_infra_failure(stderr_tail) or not MERMAID_SYNTAX_MARKERS.search(stderr_tail))
    return is_infra, stderr_tail


def render_diagram(name: str, mmd_path: str, svg_path: str) -> RenderOutcome:
    """Actually spawns mmdc -- the only non-pure function here. `mmd_path` must already exist on
    disk (this module never writes it -- see this module's own header); `svg_path`'s parent
    directory must already exist (the CLI wrapper below creates a scratch temp dir for it). No
    internal timeout -- the same single outer bound (the Stop hook's own bumped ~300s Stop-hook
    timeout, registered in the Dockerfile) governs this the same way it does every other
    potentially-slow hook in this image; see check-diagram-render-stop.mjs's own header."""
    if not SAFE_DIAGRAM_NAME_RE.match(name):
        # diagram["name"] is model-reported -- a real command-injection gap if it were
        # interpolated unquoted into an mmdc invocation without validation first. Rejected as a
        # render failure (not a syntax problem), never silently sanitized/truncated. Same guard
        # diagram_gate.py's own _render_one applies before building its command.
        return RenderOutcome(
            name=name, ok=False, is_infra_failure=False,
            stderr_tail=f"diagram name {name!r} must match {SAFE_DIAGRAM_NAME_RE.pattern} (letters, digits, _, - only)",
        )
    argv = build_mmdc_argv(mmd_path, svg_path)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        # npx/mmdc itself could not even start (missing binary, exec permission, etc.) -- infra,
        # never a syntax verdict; never blame the model's Mermaid for an environment problem that
        # never even reached mmdc.
        return RenderOutcome(name=name, ok=False, is_infra_failure=True, stderr_tail=str(exc))
    raw_output = proc.stdout or proc.stderr or ""
    is_infra, stderr_tail = classify_render_output(proc.returncode == 0, raw_output)
    return RenderOutcome(name=name, ok=proc.returncode == 0, is_infra_failure=is_infra, stderr_tail=stderr_tail)


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.gates.diagram_render_checks` (no sandbox, no
    mmdc install needed -- only the pure classify/argv halves are exercised; `render_diagram`
    itself needs a real mmdc binary and is exercised live by diagram_gate.py's own gate instead).

    Also re-proves the sandbox-image STAGING COPY is byte-identical to THIS file -- same
    drift-guard convention as wireframe_linkage_checks.py's own `_demo()`."""
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "diagram_render_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/diagram_render_checks.py hooks/diagram_render_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    assert build_mmdc_argv("a.mmd", "a.svg") == [
        "npx", "--yes", "@mermaid-js/mermaid-cli", "-i", "a.mmd", "-o", "a.svg",
        "-p", MERMAID_PUPPETEER_CONFIG_PATH,
    ]
    assert build_mmdc_argv("a.mmd", "a.svg", "/custom/config.json")[-1] == "/custom/config.json"

    assert MERMAID_SYNTAX_MARKERS.search("Parse error on line 3: ... Expecting 'SEMI'")
    assert MERMAID_SYNTAX_MARKERS.search("No diagram type detected matching given configuration")
    assert not MERMAID_SYNTAX_MARKERS.search(
        'Configuration file "/opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json" doesn\'t exist'
    )
    assert not MERMAID_SYNTAX_MARKERS.search("Failed to launch the browser process! spawn ENOENT")

    assert looks_like_infra_failure("bash: mmdc: command not found") is True
    assert looks_like_infra_failure("Parse error on line 3: ... Expecting 'SEMI'") is False

    is_infra, tail = classify_render_output(True, "all good")
    assert is_infra is False and tail == "all good"
    is_infra, _ = classify_render_output(False, "Parse error on line 1: Expecting 'SEMI', got 'EOF'")
    assert is_infra is False, "a real mmdc syntax marker on a non-zero exit is a content failure"
    is_infra, _ = classify_render_output(False, "Failed to launch the browser process! spawn ENOENT")
    assert is_infra is True, "a recognized infra marker is never blamed on the model"
    is_infra, _ = classify_render_output(False, "some unrecognized failure with no known marker at all")
    assert is_infra is True, "an unrecognized non-zero-exit shape defaults to infra, not a false syntax verdict"

    summary = mermaid_error_summary("Parse error on line 3\nExpecting SEMI\n    at Object.parse (/x.js:1:1)\n    at more stack")
    assert "Parse error" in summary and "at Object.parse" not in summary, "stack frames are dropped"

    # render_diagram: the unsafe-name guard needs no real mmdc/sandbox.
    outcome = render_diagram("bad name!", "x.mmd", "x.svg")
    assert outcome.ok is False and outcome.is_infra_failure is False and "must match" in outcome.stderr_tail

    print("diagram_render_checks self-check: all assertions passed")


def _run_render_hook_cli() -> None:
    payload = json.loads(sys.stdin.read())
    diagrams = payload.get("diagrams") or []  # [{"name": ..., "mmd_path": ...}, ...]
    svg_dir = tempfile.mkdtemp(prefix="aidw-diagram-render-")
    failures = []
    for d in diagrams:
        name = d.get("name") or "diagram"
        mmd_path = d.get("mmd_path") or ""
        svg_path = os.path.join(svg_dir, f"{name}.svg")
        outcome = render_diagram(name, mmd_path, svg_path)
        if not outcome.ok:
            failures.append({
                "name": outcome.name,
                "is_infra_failure": outcome.is_infra_failure,
                "stderr_tail": mermaid_error_summary(outcome.stderr_tail),
            })
    sys.stdout.write(json.dumps({"failures": failures}))


if __name__ == "__main__":
    if "--render-hook" in sys.argv:
        _run_render_hook_cli()
    else:
        _demo()
