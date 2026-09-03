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

import json

from .. import git_ops, repo_files, spec_ledger, workflow_persistence
from ..failure_classification import classify_failure
from ..sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from ..graph import VerificationResult

DIAGRAMS_DIR = ".ai-dev-workflow/plan/diagrams"
WIREFRAMES_DIR = ".ai-dev-workflow/plan/wireframes"


def wireframe_preview_url(owner: str, repo: str, branch: str, screen: str) -> str:
    """Rendered-HTML preview link for a committed wireframe. GitHub shows an .html blob as source;
    html-preview.github.io fetches the blob and renders it. Pure, so plan.md's link is testable."""
    return (
        "https://html-preview.github.io/?url="
        f"https://github.com/{owner}/{repo}/blob/{branch}/{WIREFRAMES_DIR}/{screen}.html"
    )

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


def _presence_values(entry: Any) -> list[dict[str, Any]]:
    """DiagramPresence/WireframePresence-shaped dict (schemas.py, Task 10) -> its `values` list.
    `content_dict` here is always freshly produced this run (stage["draft"] built from the current
    ImplementationPlan schema), so it always carries the wrapped shape -- the bare-list fallback is
    only for this module's own self-check fixtures and defense against a missing/None field."""
    if isinstance(entry, dict):
        return list(entry.get("values") or [])
    return list(entry or [])


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

def check_wireframe_ac_ids(
    wireframes: list[dict[str, Any]], ledger_entries: list[dict[str, Any]]
) -> list[str]:
    """Citation-validity only (user requirement 2026-08-31: 'the wireframes must indicate which
    US/AC they are fulfilling') -- each id a wireframe names must actually exist and be an
    acceptance criterion, same discipline as PlanStep.ac_ids. Deliberately NOT a coverage
    direction (no demand that every UI-touching AC have a wireframe, or that ui_related steps
    have one) -- that would be new scope beyond what was asked; this only catches an invented or
    mistyped id. Pure."""
    by_id = {e.get("id"): e for e in ledger_entries}
    problems: list[str] = []
    for wf in wireframes:
        screen = wf.get("screen") or "?"
        bad = [i for i in wf.get("ac_ids") or [] if by_id.get(i) is None or by_id[i].get("kind") != "acceptance_criterion"]
        if bad:
            problems.append(
                f"wireframe {screen!r}: cites {', '.join(bad)} which is not an acceptance criterion "
                "in the ledger -- copy ids exactly from the approved Specification"
            )
    return problems


def check_ui_wireframe_coverage(ui_related_ac_ids: set[str], wireframes: list[dict[str, Any]]) -> list[str]:
    """Coverage direction (user requirement 2026-09-01): every criterion the approved
    Specification marks ui_related must be cited by at least one wireframe's ac_ids -- a
    UI-facing requirement with zero wireframe evidence is exactly what this exists to catch. The
    caller only ever passes LIVE, non-deferred ids (see verify_plan_diagrams's own build of
    ui_related_ac_ids) -- nothing is demanded for scope not being built this ticket. Pure."""
    covered: set[str] = set()
    for wf in wireframes:
        covered.update(wf.get("ac_ids") or [])
    return [
        f"{ac_id}: marked ui_related in the Specification, but no wireframe's ac_ids cites it -- "
        "add a wireframe for the screen that satisfies it (or fix the Specification if ui_related "
        "is wrong for this criterion)"
        for ac_id in sorted(ui_related_ac_ids - covered)
    ]


def check_plan_linkage(
    plan_steps: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    own_ac_ids: set[str],
    prior_steps_by_id: dict[str, dict[str, Any]],
    run_id: str | None = None,
) -> list[str]:
    """Pure both-direction US/AC <-> plan-step provenance check (the ledger is the authority):

    step side -- every step cites >=1 live AC id or is kind='infrastructure'; cited ids must
    exist, be acceptance criteria, and not ALL be retired (a step whose every criterion this
    Specification retires is a deleted feature's leftover and must be dropped); a NEW or CHANGED
    step (vs the prior approved plan) citing only completed criteria is rework the pipeline
    forbids -- verbatim carryovers are exempt because ticket mode requires restating them.

    coverage side -- every ELIGIBLE AC (this ticket's own, live, never delivered by a healthy
    run) is cited by >=1 step. Completed criteria need no step; this direction also defeats
    marking every step 'infrastructure' to dodge the step-side rule.

    removal side (user requirement 2026-08-31, brownfield/greenfield asymmetry): a criterion
    that was DELIVERED by an earlier healthy run (coded_run_id set) and RETIRED this round
    (last_revised_run_id == run_id) has real artifacts in the repo -- tests, implementation,
    UI, navigation -- so some step must name it (or its parent story) in `removes_ids`, and
    every removes_ids citation must resolve to a genuinely retired entry (a live id there is
    almost certainly ac_ids/removes_ids swapped). A criterion retired before anything was ever
    built (greenfield: no coded_run_id) demands NO removal step -- there is nothing to remove.
    run_id=None skips the demand direction (self-checks/legacy callers), never the validation.
    """
    problems: list[str] = []
    by_id = {e.get("id"): e for e in ledger_entries}
    cited_live: set[str] = set()
    removed_ids: set[str] = set()
    for step in plan_steps:
        step_id = step.get("id") or "?"
        for rid in step.get("removes_ids") or []:
            entry = by_id.get(rid)
            if entry is None:
                problems.append(f"{step_id}: removes_ids cites {rid!r}, which does not exist in the ledger")
                continue
            if entry.get("status") != "retired":
                problems.append(
                    f"{step_id}: removes_ids cites {rid!r}, which is NOT retired -- removal steps "
                    "only ever name retired scope (live work belongs in ac_ids)"
                )
                continue
            removed_ids.add(rid)
            # A story id in removes_ids covers all of its (retired) criteria.
            if entry.get("kind") == "user_story":
                removed_ids.update(
                    e["id"] for e in ledger_entries
                    if e.get("kind") == "acceptance_criterion" and e.get("parent_us_id") == rid
                )
    if run_id is not None:
        delivered_retired = [
            e["id"]
            for e in ledger_entries
            if e.get("kind") == "acceptance_criterion"
            and e.get("status") == "retired"
            and e.get("last_revised_run_id") == run_id
            and e.get("coded_run_id")
        ]
        for ac_id in delivered_retired:
            if ac_id not in removed_ids:
                problems.append(
                    f"{ac_id}: this criterion was DELIVERED by an earlier run and retired this "
                    "round -- its code/UI/navigation still exist, so a plan step must name it "
                    "(or its parent story) in removes_ids and describe the removal work"
                )
    for step in plan_steps:
        step_id = step.get("id") or "?"
        ac_ids = step.get("ac_ids") or []
        if not ac_ids:
            if step.get("kind") != "infrastructure":
                problems.append(
                    f"{step_id}: cites no acceptance criteria and is not kind='infrastructure' -- "
                    "every feature step must name the US-####.# ids it fulfils"
                )
            continue
        bad = [i for i in ac_ids if by_id.get(i) is None or by_id[i].get("kind") != "acceptance_criterion"]
        if bad:
            problems.append(
                f"{step_id}: cites {', '.join(bad)} which is not an acceptance criterion in the "
                "ledger -- copy ids exactly from the approved Specification"
            )
            continue
        live = [i for i in ac_ids if by_id[i].get("status") in ("active", "revised")]
        if not live:
            problems.append(
                f"{step_id}: every cited criterion ({', '.join(ac_ids)}) is retired or deferred -- "
                "this step implements scope that is out of this ticket; drop it from the plan"
            )
            continue
        # A step citing a MIX of live and non-live ids used to slip through here: only the `live`
        # subset was ever inspected below, so a retired/deferred id riding alongside a real one was
        # silently tolerated (found live 2026-08-31, user question: "does the gate strictly
        # enforce that no deleted or deferred AC is fulfilled by any plan item" -- it did not, for
        # exactly this mixed-citation shape). Flag every non-live id individually, not just the
        # all-non-live case above.
        non_live = [i for i in ac_ids if i not in live]
        if non_live:
            problems.append(
                f"{step_id}: cites {', '.join(non_live)}, which {'is' if len(non_live) == 1 else 'are'} "
                "retired or deferred -- ac_ids may only name LIVE criteria; a retired criterion's "
                "delivered artifacts belong in removes_ids instead, and deferred scope must not be "
                "planned at all"
            )
            continue
        prior = prior_steps_by_id.get(step.get("id") or "")
        carryover = prior is not None and prior.get("description") == step.get("description")
        if not carryover:
            undelivered = [i for i in live if not by_id[i].get("coded_run_id")]
            if not undelivered:
                problems.append(
                    f"{step_id}: is new/changed but cites only already-delivered criteria "
                    f"({', '.join(live)}) -- completed criteria are never re-planned; carry the "
                    "prior step over verbatim or drop it"
                )
                continue
        cited_live.update(live)
    for ac_id in spec_ledger.eligible_ac_ids(ledger_entries, own_ac_ids):
        if ac_id not in cited_live:
            problems.append(
                f"{ac_id}: this ticket's undelivered criterion is cited by no plan step -- every "
                "criterion awaiting delivery needs at least one step (ac_ids) that fulfils it"
            )
    return problems


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
    # check_plan_linkage: one assertion per rule, plus a passing plan.
    ledger = [
        {"id": "US-0001", "kind": "user_story", "status": "active"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0001.3", "kind": "acceptance_criterion", "status": "active", "coded_run_id": "r1"},
    ]
    own = {"US-0001.1", "US-0001.2", "US-0001.3"}
    ok_steps = [
        {"id": "PS-1", "description": "build it", "ac_ids": ["US-0001.1"], "kind": "feature"},
        {"id": "PS-2", "description": "wire CI", "ac_ids": [], "kind": "infrastructure"},
    ]
    assert check_plan_linkage(ok_steps, ledger, own, {}) == []
    # feature step with no citations
    assert any("PS-1" in p for p in check_plan_linkage(
        [{"id": "PS-1", "description": "x", "ac_ids": [], "kind": "feature"}], ledger, set(), {}))
    # unknown / non-AC id
    assert any("US-0009.9" in p for p in check_plan_linkage(
        [{"id": "PS-1", "description": "x", "ac_ids": ["US-0009.9"]}], ledger, set(), {}))
    assert any("not an acceptance criterion" in p for p in check_plan_linkage(
        [{"id": "PS-1", "description": "x", "ac_ids": ["US-0001"]}], ledger, set(), {}))
    # every cited AC retired => drop the step
    assert any("retired" in p for p in check_plan_linkage(
        [{"id": "PS-1", "description": "x", "ac_ids": ["US-0001.2"]}], ledger, set(), {}))
    # mixed live + retired citation: the retired id is individually flagged, not silently
    # tolerated because a live id happens to ride along in the same step (the exact gap a live
    # question surfaced 2026-08-31 -- see this block's own comment above).
    assert any("US-0001.2" in p and "US-0001.1" not in p for p in check_plan_linkage(
        [{"id": "PS-1", "description": "x", "ac_ids": ["US-0001.1", "US-0001.2"]}], ledger, set(), {}))
    # new step citing only delivered criteria => rework refused; verbatim carryover exempt
    rework = [{"id": "PS-9", "description": "redo it", "ac_ids": ["US-0001.3"]}]
    assert any("never re-planned" in p for p in check_plan_linkage(rework, ledger, set(), {}))
    assert check_plan_linkage(rework, ledger, set(), {"PS-9": {"id": "PS-9", "description": "redo it"}}) == []
    # coverage direction: undelivered own AC with no step fails; completed AC needs none
    assert any("US-0001.1" in p for p in check_plan_linkage(
        [{"id": "PS-2", "description": "ci", "ac_ids": [], "kind": "infrastructure"}], ledger, own, {}))
    # removal side (2026-08-31): delivered-then-retired demands a removes_ids citation; retired
    # with nothing built demands none; live ids in removes_ids are rejected; a parent story id
    # covers its children.
    removal_ledger = [
        {"id": "US-0004", "kind": "user_story", "status": "retired", "last_revised_run_id": "r2"},
        {"id": "US-0004.1", "kind": "acceptance_criterion", "parent_us_id": "US-0004",
         "status": "retired", "last_revised_run_id": "r2", "coded_run_id": "r1"},  # BUILT, removed
        {"id": "US-0004.2", "kind": "acceptance_criterion", "parent_us_id": "US-0004",
         "status": "retired", "last_revised_run_id": "r2"},  # never built -- greenfield removal
        {"id": "US-0005.1", "kind": "acceptance_criterion", "status": "active"},
    ]
    # No removal step for the delivered criterion => demanded.
    assert any("US-0004.1" in p and "removes_ids" in p for p in check_plan_linkage(
        [{"id": "PS-1", "description": "x", "ac_ids": ["US-0005.1"]}], removal_ledger, set(), {}, run_id="r2"))
    # Naming the AC directly satisfies it; the never-built sibling is never demanded.
    removal_ok = [
        {"id": "PS-1", "description": "build", "ac_ids": ["US-0005.1"]},
        {"id": "PS-2", "description": "remove delete-task UI/API", "ac_ids": [], "kind": "infrastructure",
         "removes_ids": ["US-0004.1"]},
    ]
    assert check_plan_linkage(removal_ok, removal_ledger, set(), {}, run_id="r2") == []
    # Parent story id covers its delivered child.
    removal_ok[1]["removes_ids"] = ["US-0004"]
    assert check_plan_linkage(removal_ok, removal_ledger, set(), {}, run_id="r2") == []
    # A live id in removes_ids is rejected.
    assert any("NOT retired" in p for p in check_plan_linkage(
        [{"id": "PS-2", "description": "x", "ac_ids": [], "kind": "infrastructure",
          "removes_ids": ["US-0005.1"]}], removal_ledger, set(), {}, run_id="r2"))
    # Retired in an EARLIER run (not this one) demands nothing new.
    assert check_plan_linkage(
        [{"id": "PS-1", "description": "build", "ac_ids": ["US-0005.1"]}],
        removal_ledger, set(), {}, run_id="r3") == []
    # plan.md preview link: html-preview.github.io over the branch's blob URL, exact shape.
    assert wireframe_preview_url("acme", "shop", "ai-dev-workflow/abc", "catalog") == (
        "https://html-preview.github.io/?url=https://github.com/acme/shop/blob/ai-dev-workflow/abc/"
        ".ai-dev-workflow/plan/wireframes/catalog.html"
    )
    from ..markdown_render import render_plan_markdown

    # check_wireframe_ac_ids (user requirement 2026-08-31): citation-validity only, no coverage
    # demand -- a real AC id passes, an invented/mistyped one is rejected.
    assert check_wireframe_ac_ids(
        [{"screen": "task-list", "ac_ids": ["US-0001.1"]}], ledger,
    ) == []
    assert any("bogus" in p for p in check_wireframe_ac_ids(
        [{"screen": "task-list", "ac_ids": ["bogus"]}], ledger,
    ))
    assert any("US-0001" in p for p in check_wireframe_ac_ids(
        [{"screen": "task-list", "ac_ids": ["US-0001"]}], ledger,  # a story id, not a criterion
    ))

    # check_ui_wireframe_coverage (user requirement 2026-09-01): a ui_related AC with no
    # wireframe citing it is flagged; one covered by ANY wireframe's ac_ids passes.
    assert check_ui_wireframe_coverage({"US-0001.1"}, []) and "US-0001.1" in check_ui_wireframe_coverage({"US-0001.1"}, [])[0]
    assert check_ui_wireframe_coverage({"US-0001.1"}, [{"screen": "task-list", "ac_ids": ["US-0001.1"]}]) == []
    assert check_ui_wireframe_coverage(set(), [{"screen": "task-list", "ac_ids": []}]) == []

    # wireframes is WireframePresence-shaped (schemas.py, Task 10), not a bare list.
    rendered = render_plan_markdown({"wireframes": {"status": "present", "values": [
        {"screen": "catalog", "html_source": "<html></html>", "preview_url": "https://html-preview.github.io/?url=x"},
        {"screen": "cart", "html_source": "<html></html>"},
    ]}})
    assert "- [catalog](plan/wireframes/catalog.html) -- [preview](https://html-preview.github.io/?url=x)" in rendered, rendered
    assert "- [cart](plan/wireframes/cart.html)\n" in rendered, rendered

    # _presence_values: the DiagramPresence/WireframePresence -> plain-list extraction this gate
    # relies on throughout verify_plan_diagrams.
    assert _presence_values({"status": "present", "values": [{"screen": "x"}], "reason": ""}) == [{"screen": "x"}]
    assert _presence_values({"status": "absent", "values": [], "reason": "no UI work"}) == []
    assert _presence_values(None) == []
    print("diagram_gate wireframe self-check: all assertions passed")


if __name__ == "__main__":
    _demo()


async def verify_plan_diagrams(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: SandboxProvider,
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

    # Provenance first: pure checks against the ledger, cheaper than any render, and a plan whose
    # steps aren't linked to this ticket's criteria is wrong regardless of its diagrams. The spec
    # read falls back to an empty own-set (coverage direction skipped) the same way
    # ac_coverage_gate's identical read does -- an infra hiccup must not manufacture a false gap.
    ledger_entries = await spec_ledger.load_ledger(provider, thread_id)
    own_ac_ids: set[str] = set()
    spec_doc: dict[str, Any] = {}
    raw_spec = await repo_files.read_repo_file(
        provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH
    )
    if raw_spec is not None:
        try:
            spec_doc = json.loads(raw_spec)
            own_ac_ids = spec_ledger.own_ac_ids_from_specification(spec_doc)
        except json.JSONDecodeError:
            pass
    prior_steps_by_id: dict[str, dict[str, Any]] = {}
    raw_prior_plan = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.PLAN_APPROVED_PATH)
    if raw_prior_plan is not None:
        try:
            prior_steps_by_id = {
                s.get("id"): s for s in (json.loads(raw_prior_plan).get("plan_steps") or []) if s.get("id")
            }
        except json.JSONDecodeError:
            pass
    # Every LIVE (not deferred, not retired -- retired entries are absent from spec_doc's own
    # user_stories entirely; a deferred one stays present with deferred=true) ui_related
    # criterion, straight from the approved Specification -- the same read already fetched above
    # for own_ac_ids, no ledger schema change needed. A criterion inside a deferred STORY is
    # deferred even if its own `deferred` field was left at the schema default (schemas.py's own
    # "automatically" cascade note), so both levels are checked.
    ui_related_ac_ids = {
        ac.get("id")
        for story in (spec_doc.get("user_stories") or [])
        if not story.get("deferred")
        for ac in (story.get("acceptance_criteria") or [])
        if ac.get("ui_related") and not ac.get("deferred")
    }
    # wireframes is WireframePresence-shaped (schemas.py, Task 10): `{"status", "values", "reason"}`
    # rather than a bare list -- extract its values once, reused by every check below.
    wireframes = _presence_values(content_dict.get("wireframes"))
    linkage_problems = (
        check_plan_linkage(content_dict.get("plan_steps") or [], ledger_entries, own_ac_ids, prior_steps_by_id, run_id=run_id)
        + check_wireframe_ac_ids(wireframes, ledger_entries)
        + check_ui_wireframe_coverage(ui_related_ac_ids, wireframes)
    )
    if linkage_problems:
        return VerificationResult(
            passed=False,
            feedback="; ".join(linkage_problems),
            report={"plan_linkage_failed": linkage_problems},
        )

    # Scope-lifecycle stamps for the Plan review UI (user requirement 2026-08-31, mirroring the
    # spec view's badges): each step inherits the strongest change classification of the criteria
    # it fulfils, straight from the approved Specification's own per-AC `change` stamps -- so a
    # reviewer sees which steps exist because of NEW scope, an UPDATE, or a promotion
    # ("activated"), and removal steps are recognizable by their removes_ids. Deterministic,
    # stamped in place (this verify's established contract -- see the wireframe preview_url
    # stamping below).
    ac_change_by_id = {
        ac.get("id"): ac.get("change")
        for story in (spec_doc.get("user_stories") or [])
        for ac in (story.get("acceptance_criteria") or [])
    }
    _CHANGE_PRIORITY = ["activated", "new", "modified", "deferred", "unchanged"]
    for step in content_dict.get("plan_steps") or []:
        changes = {ac_change_by_id.get(i) for i in (step.get("ac_ids") or [])}
        step["change"] = next((c for c in _CHANGE_PRIORITY if c in changes), None)

    # diagrams is DiagramPresence-shaped (schemas.py, Task 10) -- same extraction as wireframes
    # above (already computed; re-used here, not re-fetched from content_dict).
    diagrams = _presence_values(content_dict.get("diagrams"))

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
    if wireframes:
        # Stamp a rendered-preview link onto each wireframe entry. In-place mutation of
        # content_dict is this verify's established contract (it already rewrites ids/fields before
        # the gate), so the link lands in approved_content and plan.md. Repo/branch come from the
        # session row -- the only place they are durably known; unavailable (DB down, no row) means
        # plan.md keeps just the relative link, never a broken absolute one.
        try:
            from .. import session_store

            row = await session_store.get_session(thread_id)
        except Exception:  # noqa: BLE001 -- cosmetic link, never a gate failure
            row = None
        if row and row.get("owner") and row.get("repo") and row.get("work_branch"):
            for wf in wireframes:
                wf["preview_url"] = wireframe_preview_url(row["owner"], row["repo"], row["work_branch"], wf["screen"])

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
