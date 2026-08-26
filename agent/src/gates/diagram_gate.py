"""P3's deterministic diagram-validation gate: renders every ImplementationPlan.diagrams entry to
SVG via the mermaid CLI (`mmdc`) inside the sandbox, using rendering itself as the syntax check --
a non-zero exit is a concrete, machine-checkable failure fed back to the draft node, exactly like
every other deterministic_verify in this pipeline. Never trusts the LLM's own claim that Mermaid
source is valid.

Known limitation, stated plainly: mmdc needs a headless Chromium (via Puppeteer) inside the
sandbox image (agent/sandbox-image/Dockerfile installs it) -- this is a heavier, more
failure-prone dependency than any other gate in this pipeline, and unlike brownfield-baseline/P1/P2's gates, this
one has not been exercised against a rebuilt sandbox image end-to-end. A render failure caused by
a broken/missing Chromium install (not a real diagram syntax problem) is distinguished from a
genuine syntax failure where possible (see _looks_like_infra_failure) so it can be surfaced
differently, but this distinction itself is unverified in practice.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import git_ops, repo_files
from ..failure_classification import classify_failure
from ..sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from ..graph import VerificationResult

DIAGRAMS_DIR = ".ai-dev-workflow/plan/diagrams"
WIREFRAMES_DIR = ".ai-dev-workflow/plan/wireframes"

MAX_WIREFRAMES = 6
MAX_WIREFRAME_BYTES = 30 * 1024

# Trust-boundary checks on model-emitted wireframe HTML. This denylist is hygiene for the
# committed artifact, NOT the security boundary -- the frontend confines every wireframe (both
# thumbnail and full-size) to an empty-`sandbox` iframe, whose null origin and script ban hold
# even against markup these regexes miss. The on\w+= check is anchored inside a tag (after
# `<tag ` and before its `>`) so prose like "conversion=..." never false-positives.
_WIREFRAME_FORBIDDEN = (
    (re.compile(r"<\s*script\b", re.IGNORECASE), "contains a <script> tag"),
    (re.compile(r"<[a-zA-Z][^>]*\son\w+\s*=", re.IGNORECASE), "contains an inline on*= event handler"),
    (re.compile(r"""(?:src|href|action|data|xlink:href)\s*=\s*["']?\s*(?:https?:)?//""", re.IGNORECASE), "references an external URL"),
    (re.compile(r"""(?:src|href|action|data|xlink:href)\s*=\s*["']?\s*(?:javascript|vbscript|data|file)\s*:""", re.IGNORECASE), "uses a dangerous URL scheme (javascript:/vbscript:/data:/file:)"),
    (re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.IGNORECASE), "references an external URL (css url())"),
    (re.compile(r"@import\b", re.IGNORECASE), "uses @import (external stylesheet)"),
    (re.compile(r"<\s*(?:iframe|object|embed|base|form)\b", re.IGNORECASE), "contains an embedding/navigation element (iframe/object/embed/base/form)"),
    (re.compile(r"""<\s*meta\b[^>]*http-equiv""", re.IGNORECASE), "contains <meta http-equiv> (refresh/CSP override)"),
)


def check_wireframe(screen: str, html_source: str) -> str | None:
    """Returns a rejection reason, or None if the wireframe is acceptable. Pure -- self-checkable
    without a sandbox."""
    if not _SAFE_DIAGRAM_NAME_RE.match(screen or ""):
        return f"screen name {screen!r} must match {_SAFE_DIAGRAM_NAME_RE.pattern} (letters, digits, _, - only)"
    if len(html_source.encode("utf-8")) > MAX_WIREFRAME_BYTES:
        return f"wireframe {screen!r} exceeds {MAX_WIREFRAME_BYTES // 1024} KB -- simplify it"
    lowered = html_source.lower()
    if "<html" not in lowered and "<body" not in lowered and "<div" not in lowered:
        return f"wireframe {screen!r} does not look like an HTML page"
    for pattern, reason in _WIREFRAME_FORBIDDEN:
        if pattern.search(html_source):
            return f"wireframe {screen!r} {reason} -- wireframes must be fully self-contained (inline CSS only)"
    return None

@dataclass(frozen=True)
class DiagramRenderOutcome:
    name: str
    ok: bool
    is_infra_failure: bool
    stderr_tail: str


def _looks_like_infra_failure(stderr: str) -> bool:
    # Delegates to the repo-wide classifier (failure_classification.py) instead of a
    # gate-local marker list, so this gate, the sandbox connect-handshake retry, and every
    # escalate node's failure_type tagging agree on what "infra, not content" means.
    return classify_failure(stderr) == "infra_transient"


# mmdc names a genuine source problem in one of these shapes. Anything else it fails on -- a
# missing puppeteer config, no browser binary, a crashed Chromium -- is environmental, and telling
# the draft node to "fix your Mermaid" for it is unactionable: the model rewrites correct source
# every lap until max_verify_cycles runs out. Observed live (blazor-dotnet s01): the config file
# named by _render_one's own -p flag did not exist in the image, classify_failure called that
# `gate_exhausted` rather than infra, and the plan stage thrashed on syntax feedback for diagrams
# that rendered fine the moment the file was created.
_MERMAID_SYNTAX_MARKERS = re.compile(
    r"parse error|syntax error|expecting|unrecognized text|no diagram type detected", re.IGNORECASE
)


def _mermaid_error_summary(output: str) -> str:
    """The actionable mermaid parse error ('Parse error on line N ... Expecting ...') is at the
    TOP of mmdc's output; the tail is a useless puppeteer JS stack. Feeding the tail back to the
    draft node burned three verify cycles live -- the model never saw what was wrong. Keep the
    first meaningful lines, drop stack frames."""
    lines = [l.strip() for l in output.splitlines() if l.strip() and not l.lstrip().startswith("at ")]
    return " | ".join(lines[:10])[:700]


_SAFE_DIAGRAM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _render_one(provider: SandboxProvider, thread_id: str, diagram: dict[str, Any]) -> DiagramRenderOutcome:
    name = diagram.get("name") or "diagram"
    if not _SAFE_DIAGRAM_NAME_RE.match(name):
        # diagram["name"] is model-reported (PlanDiagram.name) -- a real command-injection gap,
        # found by automated security review, if it were interpolated unquoted into the mmdc
        # shell command below without validation first. Rejected as a render failure (not a
        # syntax problem -- the draft node's feedback should ask for a filename-safe name), never
        # silently sanitized/truncated, so the failure is visible rather than papered over.
        return DiagramRenderOutcome(
            name=name,
            ok=False,
            is_infra_failure=False,
            stderr_tail=f"diagram name {name!r} must match {_SAFE_DIAGRAM_NAME_RE.pattern} (letters, digits, _, - only)",
        )

    source = diagram.get("mermaid_source") or ""
    # Mermaid has NO backslash escapes -- \" inside a quoted label is always a parse error, and
    # models emit it habitually (observed live: three redraft laps could not shake it). The
    # sequence is never meaningful, so rewriting it to mermaid's own quote entity is lossless.
    source = source.replace('\\"', "#quot;")
    mmd_path = f"{DIAGRAMS_DIR}/{name}.mmd"
    svg_path = f"{DIAGRAMS_DIR}/{name}.svg"

    await repo_files.write_repo_file(provider, thread_id, mmd_path, source)

    # -p points mmdc at a bundled no-sandbox Puppeteer config (see the Dockerfile) -- required to
    # run headless Chromium as a non-root container user without --cap-add=SYS_ADMIN. Paths are
    # shell-quoted even though `name` is now validated above -- DIAGRAMS_DIR is a fixed constant,
    # but quoting is cheap defense-in-depth against a future change to that constant.
    command = (
        f"npx --yes @mermaid-js/mermaid-cli -i {shlex.quote(mmd_path)} -o {shlex.quote(svg_path)} "
        f"-p /opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json 2>&1"
    )
    result = await provider.exec_in_sandbox(thread_id, command)
    # HEAD as well as tail. _mermaid_error_summary below takes the first meaningful lines because
    # that is where mmdc puts the actionable "Parse error on line N ... Expecting ..." -- but a
    # plain `[-2000:]` threw that away before the summariser ever ran, leaving it to summarise the
    # puppeteer stack this file already documents as useless. Keeping both ends means the parse
    # error survives on a long output AND the tail is still there for a failure that only shows up
    # at the end (a crash, a non-zero exit message).
    raw_output = result.stdout or result.stderr or ""
    stderr_tail = (
        raw_output
        if len(raw_output) <= 4000
        else f"{raw_output[:2000]}\n...[{len(raw_output) - 4000} chars omitted]...\n{raw_output[-2000:]}"
    )
    # Infra unless mmdc actually named a source problem -- see _MERMAID_SYNTAX_MARKERS. The
    # classify_failure call stays as the first test so this gate keeps agreeing with the rest of
    # the pipeline on the transient failures it already recognizes.
    is_infra = not result.ok and (
        _looks_like_infra_failure(stderr_tail) or not _MERMAID_SYNTAX_MARKERS.search(stderr_tail)
    )
    return DiagramRenderOutcome(
        name=name, ok=result.ok, is_infra_failure=is_infra, stderr_tail=stderr_tail
    )


def _demo() -> None:
    """Runnable check for the pure wireframe validator: `uv run python -m src.gates.diagram_gate`.

    Named `_demo` to match every other gate: graph.assert_gates_have_self_checks() enforces the one
    convention, so a new gate cannot ship without a check that can actually be run.
    """
    ok_html = "<html><body><style>body{font-family:sans-serif}</style><div>Login</div></body></html>"
    assert check_wireframe("login", ok_html) is None
    assert check_wireframe("bad name!", ok_html) is not None
    assert check_wireframe("s", "<div><script>alert(1)</script></div>") is not None
    assert check_wireframe("s", '<div onclick=go()>x</div>') is not None
    assert check_wireframe("s", '<img src="https://cdn.example.com/x.png">') is not None
    assert check_wireframe("s", '<div style="background:url(//evil)">x</div>') is not None
    assert check_wireframe("s", "<style>@import url(x)</style><div>x</div>") is not None
    # prose containing "conversion=" must NOT false-positive the on*= handler check
    assert check_wireframe("s", "<div>conversion=42%</div>") is None
    assert check_wireframe("s", "<div>" + "x" * MAX_WIREFRAME_BYTES + "</div>") is not None
    assert check_wireframe("s", "just words, no markup") is not None
    assert check_wireframe("s", '<a href="javascript:alert(1)">x</a>') is not None
    assert check_wireframe("s", '<a href = "JAVASCRIPT:alert(1)">x</a>') is not None
    assert check_wireframe("s", '<iframe srcdoc="<b>x</b>"></iframe>') is not None
    assert check_wireframe("s", '<meta http-equiv="refresh" content="0;url=x"><div>x</div>') is not None
    assert check_wireframe("s", '<object data="x"></object>') is not None
    assert check_wireframe("s", '<form action="/steal"><div>x</div></form>') is not None
    # a plain meta charset/viewport stays legal
    assert check_wireframe("s", '<meta charset="utf-8"><div>x</div>') is None
    # same-document anchors stay legal
    assert check_wireframe("s", '<a href="#section">jump</a><div id="section">x</div>') is None
    # Infra-vs-syntax split (_MERMAID_SYNTAX_MARKERS): a real mmdc parse error must stay a content
    # failure the draft node can act on, while an environment failure must NOT be fed back as
    # "fix your Mermaid" -- that is what burned the plan stage's whole cycle budget live.
    assert _MERMAID_SYNTAX_MARKERS.search("Parse error on line 3: ... Expecting 'SEMI'")
    assert _MERMAID_SYNTAX_MARKERS.search("No diagram type detected matching given configuration")
    assert not _MERMAID_SYNTAX_MARKERS.search(
        'Configuration file "/opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json" doesn\'t exist'
    )
    assert not _MERMAID_SYNTAX_MARKERS.search("Failed to launch the browser process! spawn ENOENT")
    print("diagram_gate wireframe self-check: all assertions passed")


if __name__ == "__main__":
    _demo()


async def verify_plan_diagrams(
    thread_id: str, content_dict: dict[str, Any], _run_id: str, _baseline_commit: str | None, provider: SandboxProvider,
    _chat_provider: str,
) -> "VerificationResult":
    # _chat_provider (StageSpec.deterministic_verify's Ruling-4 addition) is unused: rendering a
    # mermaid diagram has no chat-model dispatch call of its own.
    from ..graph import VerificationResult  # local import: graph.py imports this module

    if not content_dict:
        # Reachable via the clarification-cycle safety cap: auto_approve_node promotes whatever
        # the last draft attempt produced straight to "approved" content, and a draft that never
        # got past a (headless-disallowed) clarifying-question response can leave that empty/None.
        # A crash here would kill the whole run; report it through the normal retry/escalate path
        # instead, same as any other failed verification.
        return VerificationResult(
            passed=False,
            feedback="Plan content is empty -- the draft never produced a real plan (safety-cap auto-approve after repeated clarification attempts). Draft a complete plan with no clarifying questions.",
            report={"plan_content": "empty"},
        )

    diagrams = content_dict.get("diagrams") or []
    wireframes = content_dict.get("wireframes") or []

    # Wireframes first: pure checks, no Chromium involved -- a broken wireframe should be cheap
    # feedback, not a render cycle. Same retry loop as diagram syntax failures.
    if len(wireframes) > MAX_WIREFRAMES:
        return VerificationResult(
            passed=False,
            feedback=f"{len(wireframes)} wireframes exceeds the cap of {MAX_WIREFRAMES} -- keep only the screens this plan actually changes.",
            report={"wireframes_rejected": "too_many"},
        )
    wireframe_errors = [
        err for wf in wireframes if (err := check_wireframe(wf.get("screen") or "", wf.get("html_source") or "")) is not None
    ]
    if wireframe_errors:
        return VerificationResult(passed=False, feedback="; ".join(wireframe_errors), report={"wireframes_failed": wireframe_errors})
    for wf in wireframes:
        await repo_files.write_repo_file(provider, thread_id, f"{WIREFRAMES_DIR}/{wf['screen']}.html", wf["html_source"])

    if not diagrams and not wireframes:
        return VerificationResult(passed=True, feedback="No diagrams or wireframes in this draft -- nothing to validate.", report={})

    outcomes = [await _render_one(provider, thread_id, diagram) for diagram in diagrams]
    failures = [o for o in outcomes if not o.ok]

    if not failures:
        commit_dirs = ([DIAGRAMS_DIR] if diagrams else []) + ([WIREFRAMES_DIR] if wireframes else [])
        await git_ops.commit_paths(provider, thread_id, commit_dirs, "ai-dev-workflow: render plan diagrams + wireframes")
        return VerificationResult(
            passed=True,
            feedback=f"All {len(diagrams)} diagram(s) rendered and {len(wireframes)} wireframe(s) validated.",
            report={"rendered": [o.name for o in outcomes], "wireframes": [wf["screen"] for wf in wireframes]},
        )

    infra_failures = [o for o in failures if o.is_infra_failure]
    if infra_failures:
        # Distinct from a real syntax problem -- the draft node retrying with "fix your Mermaid
        # syntax" feedback would be nonsensical here since the syntax was never actually checked.
        feedback = (
            f"Diagram rendering infrastructure failure (mermaid-cli/Chromium), not a diagram syntax "
            f"problem -- affected: {[o.name for o in infra_failures]}. First error: "
            f"{infra_failures[0].stderr_tail}"
        )
    else:
        feedback = "; ".join(f"{o.name}: {_mermaid_error_summary(o.stderr_tail)}" for o in failures)

    return VerificationResult(
        passed=False,
        feedback=feedback,
        report={"failed": [o.name for o in failures], "infra_failure": bool(infra_failures)},
    )
