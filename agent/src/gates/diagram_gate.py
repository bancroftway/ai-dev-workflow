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
from typing import TYPE_CHECKING, Any, Callable

import json

from .. import chat_model, config, git_ops, repo_files, spec_ledger, workflow_persistence
from ..failure_classification import classify_failure
from ..sandbox.provider import SandboxProvider
from ..schemas import presence_values as _presence_values
from ..text_truncate import truncate_middle
from . import write_scope_gate

if TYPE_CHECKING:
    from ..graph import VerificationResult

DIAGRAMS_DIR = ".ai-dev-workflow/plan/diagrams"
WIREFRAMES_DIR = ".ai-dev-workflow/plan/wireframes"

# File-based-editing plan, Part 2 sect. 1: the model's own scratch sandbox, edited directly with
# real file tools across draft/audit laps -- mirrors Part 1's DRAFT_SPEC_PATH sketchpad, one level
# up (a directory of files, since plan's content -- steps + a manifest + raw wireframe/diagram
# sources -- doesn't fit one JSON file the way specification's does without re-introducing the
# same up-to-180KB-of-escaped-HTML corruption risk this design exists to avoid).
DRAFT_DIR = ".ai-dev-workflow/plan/_draft"
DRAFT_STEPS_PATH = f"{DRAFT_DIR}/steps.json"
DRAFT_MANIFEST_PATH = f"{DRAFT_DIR}/manifest.json"
DRAFT_WIREFRAMES_DIR = f"{DRAFT_DIR}/wireframes"
DRAFT_DIAGRAMS_DIR = f"{DRAFT_DIR}/diagrams"


def wireframe_preview_url(owner: str, repo: str, branch: str, screen: str) -> str:
    """Rendered-HTML preview link for a committed wireframe. GitHub shows an .html blob as source;
    html-preview.github.io fetches the blob and renders it. Pure, so plan.md's link is testable."""
    return (
        "https://html-preview.github.io/?url="
        f"https://github.com/{owner}/{repo}/blob/{branch}/{WIREFRAMES_DIR}/{screen}.html"
    )

MAX_WIREFRAMES = config.DIAGRAM_MAX_WIREFRAMES
MAX_WIREFRAME_BYTES = config.DIAGRAM_MAX_WIREFRAME_BYTES

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


def check_wireframe_has_ac_ids(wireframes: list[dict[str, Any]]) -> list[str]:
    """Every wireframe must cite >=1 ac_id. `Wireframe.ac_ids` (schemas.py) defaults to an empty
    list, and until now nothing rejected that: check_wireframe_ac_ids only validates ids a
    wireframe DOES cite are real, and check_ui_wireframe_coverage only checks the other direction
    (every ui_related AC has SOME wireframe). Neither stops a wireframe from citing nothing at
    all -- which would dodge the e2e stage's wireframe-coverage gate (e2e_nodes.py), which has
    nothing to match an AC-less screen against. Pure."""
    return [
        f"wireframe {wf.get('screen')!r}: cites no ac_ids -- every wireframe must name at least "
        "one acceptance criterion it is evidence for, or the e2e stage cannot verify this screen "
        "was actually built and tested"
        for wf in wireframes
        if not (wf.get("ac_ids") or [])
    ]


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


def check_dangling_visual_retirement(
    wireframe_refs: list[dict[str, Any]],
    diagram_refs: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    retired_wireframe_screens: set[str],
    retired_diagram_names: set[str],
) -> list[str]:
    """File-based-editing plan, Part 2 sect. 6 (gap found and closed, user-raised): a wireframe or
    `user_flow` diagram whose every cited AC is now retired is a deleted feature's leftover and
    must be named in retired_wireframe_screens/retired_diagram_names -- mirrors
    check_plan_linkage's own removal side for plan steps ("a step whose every criterion this
    Specification retires is a deleted feature's leftover and must be dropped"), applied to visual
    artifacts instead. A wireframe/diagram with a live citation, or with NO citations at all
    (caught separately by check_wireframe_has_ac_ids), is never flagged here. `er`/`architecture`
    diagrams are exempt -- whole-system views, not retired this way (schemas.ImplementationPlan's
    own retired_diagram_names docstring). Pure.
    """
    retired_ac_ids = {
        e["id"] for e in ledger_entries if e.get("kind") == "acceptance_criterion" and e.get("status") == "retired"
    }
    problems: list[str] = []
    for wf in wireframe_refs:
        ac_ids = wf.get("ac_ids") or []
        if ac_ids and all(i in retired_ac_ids for i in ac_ids) and wf.get("screen") not in retired_wireframe_screens:
            problems.append(
                f"wireframe {wf.get('screen')!r} cites only retired criteria ({', '.join(ac_ids)}) -- "
                "name it in retired_wireframe_screens or fix its citations"
            )
    for d in diagram_refs:
        if d.get("kind") != "user_flow":
            continue
        ac_ids = d.get("ac_ids") or []
        if ac_ids and all(i in retired_ac_ids for i in ac_ids) and d.get("name") not in retired_diagram_names:
            problems.append(
                f"diagram {d.get('name')!r} cites only retired criteria ({', '.join(ac_ids)}) -- "
                "name it in retired_diagram_names or fix its citations"
            )
    return problems


def _spec_changed_this_run(ledger_entries: list[dict[str, Any]], run_id: str) -> bool:
    """File-based-editing plan, Part 2 sect. 6: did THIS run genuinely change the spec (US/AC
    content), scoped to `EntryKind in ("user_story", "acceptance_criterion")` specifically --
    explicitly excludes `plan_step` entries (both kinds share one ledger file, Part 2 sect. 5) so a
    plan-only revision with no AC wording change never counts as "the spec changed" and force a
    diagram review that has nothing to do with it. Pure."""
    return any(
        e.get("kind") in ("user_story", "acceptance_criterion")
        and (e.get("first_seen_run_id") == run_id or e.get("last_revised_run_id") == run_id)
        for e in ledger_entries
    )


def _reopened_or_changed_ac_ids(ledger_entries: list[dict[str, Any]], run_id: str, bug_affected_ac_ids: set[str]) -> set[str]:
    """AC ids this run either genuinely changed (first-seen/last-revised this run_id) or reopened
    via bug_affected_ac_ids (wording unchanged, §Part5's 'reopened' case) -- the citation-scoped
    trigger set for wireframe/user_flow-diagram stale-review enforcement below."""
    changed = {
        e["id"]
        for e in ledger_entries
        if e.get("kind") == "acceptance_criterion"
        and (e.get("first_seen_run_id") == run_id or e.get("last_revised_run_id") == run_id)
    }
    return changed | bug_affected_ac_ids


def check_stale_visual_review(
    diagram_refs: list[dict[str, Any]],
    wireframe_refs: list[dict[str, Any]],
    prior_diagram_names: set[str],
    prior_wireframe_screens: set[str],
    ledger_entries: list[dict[str, Any]],
    run_id: str,
    bug_affected_ac_ids: set[str],
    diagrams_reviewed: list[dict[str, Any]],
    wireframes_reviewed: list[dict[str, Any]],
) -> list[str]:
    """File-based-editing plan, Part 2 sect. 6 (user decision 2026-09-16: full parity, no visual
    artifact may go stale silently) -- two trigger shapes, matched to what each kind has to scope
    by:

    - `er`/`architecture` (no ac_ids, whole-system view): BLANKET trigger -- if the spec changed
      at all this run, every one must appear in `diagrams_reviewed`. Justified by count (typically
      one or two per project).
    - `user_flow` diagrams and wireframes (both ac_ids-scoped): PER-ITEM trigger -- only a
      PRE-EXISTING item (already in the prior approved manifest; a brand-new one is exempt, its
      existence already proves it isn't stale) whose own cited ac_ids include one this run changed
      or bug-reopened must appear in `{diagrams,wireframes}_reviewed`. Scales to many items, unlike
      the blanket rule.

    Pure -- the mechanical `mermaid_source`/html diff-on-"revised" verification is a separate
    check (verify_plan_diagrams itself, which has the file content to diff).
    """
    reviewed_diagram_names = {r.get("name") for r in diagrams_reviewed}
    reviewed_wireframe_screens = {r.get("screen") for r in wireframes_reviewed}
    problems: list[str] = []

    if _spec_changed_this_run(ledger_entries, run_id):
        for d in diagram_refs:
            if d.get("kind") in ("er", "architecture") and d.get("name") not in reviewed_diagram_names:
                problems.append(
                    f"diagram {d.get('name')!r} was not reviewed even though the specification "
                    "changed this ticket -- confirm it's still accurate or revise it"
                )

    trigger_ac_ids = _reopened_or_changed_ac_ids(ledger_entries, run_id, bug_affected_ac_ids)
    for d in diagram_refs:
        if d.get("kind") != "user_flow" or d.get("name") not in prior_diagram_names:
            continue
        if set(d.get("ac_ids") or []) & trigger_ac_ids and d.get("name") not in reviewed_diagram_names:
            problems.append(
                f"diagram {d.get('name')!r} cites a criterion that changed this run but was not "
                "reviewed -- confirm it's still accurate or revise it"
            )
    for wf in wireframe_refs:
        if wf.get("screen") not in prior_wireframe_screens:
            continue
        if set(wf.get("ac_ids") or []) & trigger_ac_ids and wf.get("screen") not in reviewed_wireframe_screens:
            problems.append(
                f"wireframe {wf.get('screen')!r} cites a criterion that changed this run but was "
                "not reviewed -- confirm it's still accurate or revise it"
            )
    return problems


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
    return " | ".join(lines[:config.DIAGRAM_ERROR_SUMMARY_LINES_MAX])[:config.DIAGRAM_ERROR_SUMMARY_JOINED_CHARS]


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
    stderr_tail = truncate_middle(
        raw_output, config.DIAGRAM_ERROR_SUMMARY_HEAD_CHARS, config.DIAGRAM_ERROR_SUMMARY_TAIL_CHARS
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

    # check_wireframe_has_ac_ids: the mirror direction check_wireframe_ac_ids never covered -- a
    # wireframe that cites NOTHING is rejected (it would dodge e2e's wireframe-coverage gate
    # entirely, having nothing to match against), one that cites something real passes.
    assert check_wireframe_has_ac_ids([{"screen": "task-list", "ac_ids": ["US-0001.1"]}]) == []
    assert any("task-list" in p for p in check_wireframe_has_ac_ids(
        [{"screen": "task-list", "ac_ids": []}]
    ))
    assert any("task-list" in p for p in check_wireframe_has_ac_ids(
        [{"screen": "task-list"}]  # ac_ids key absent entirely, not just empty
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
    # relies on throughout verify_plan_diagrams. Now schemas.presence_values under this module's
    # own alias (final review fix wave consolidated 6 near-identical private copies into one) --
    # this module's own former version used `list(entry or [])` for the non-dict fallback rather
    # than an isinstance check; confirmed here that the shared isinstance-checked version still
    # returns the identical [] for both None and a missing field, the only two shapes this gate's
    # real callers (a freshly-produced ImplementationPlan dump) ever hand it.
    assert _presence_values({"status": "present", "values": [{"screen": "x"}], "reason": ""}) == [{"screen": "x"}]
    assert _presence_values({"status": "absent", "values": [], "reason": "no UI work"}) == []
    assert _presence_values(None) == []

    # Task 13b: PLAN_HARD_RULES -- one line per real rejection branch in verify_plan_diagrams
    # (see the constant's own comment for the count breakdown).
    assert len(PLAN_HARD_RULES) == 20, len(PLAN_HARD_RULES)
    assert all(isinstance(r, str) and r.strip() for r in PLAN_HARD_RULES)

    # --- File-based-editing plan, Part 2 sect. 6: check_dangling_visual_retirement ---
    retirement_ledger = [
        {"id": "US-0006.1", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0006.2", "kind": "acceptance_criterion", "status": "active"},
    ]
    # A wireframe citing ONLY retired criteria and not named retired -- flagged.
    assert any(
        "task-list" in p
        for p in check_dangling_visual_retirement(
            [{"screen": "task-list", "ac_ids": ["US-0006.1"]}], [], retirement_ledger, set(), set()
        )
    )
    # Same, but already named in retired_wireframe_screens -- not flagged.
    assert check_dangling_visual_retirement(
        [{"screen": "task-list", "ac_ids": ["US-0006.1"]}], [], retirement_ledger, {"task-list"}, set()
    ) == []
    # A live citation alongside a retired one is fine -- not flagged.
    assert check_dangling_visual_retirement(
        [{"screen": "task-list", "ac_ids": ["US-0006.1", "US-0006.2"]}], [], retirement_ledger, set(), set()
    ) == []
    # user_flow diagrams get the identical treatment; er/architecture never do (whole-system views).
    assert any(
        "old-flow" in p
        for p in check_dangling_visual_retirement(
            [], [{"name": "old-flow", "kind": "user_flow", "ac_ids": ["US-0006.1"]}], retirement_ledger, set(), set()
        )
    )
    assert check_dangling_visual_retirement(
        [], [{"name": "system-er", "kind": "er", "ac_ids": []}], retirement_ledger, set(), set()
    ) == [], "er/architecture diagrams are never retired this way"

    # --- check_stale_visual_review ---
    changed_ledger = [
        {"id": "US-0007.1", "kind": "acceptance_criterion", "status": "revised", "last_revised_run_id": "r9", "first_seen_run_id": "r1"},
        {"id": "US-0007.2", "kind": "acceptance_criterion", "status": "active", "first_seen_run_id": "r1", "last_revised_run_id": "r1"},
    ]
    # er/architecture blanket trigger: spec changed this run, diagram not in diagrams_reviewed -> flagged.
    assert any(
        "system-er" in p
        for p in check_stale_visual_review(
            [{"name": "system-er", "kind": "er", "ac_ids": []}], [], {"system-er"}, set(),
            changed_ledger, "r9", set(), [], [],
        )
    )
    # Confirmed current satisfies it.
    assert check_stale_visual_review(
        [{"name": "system-er", "kind": "er", "ac_ids": []}], [], {"system-er"}, set(),
        changed_ledger, "r9", set(),
        [{"name": "system-er", "action": "confirmed_current", "reason": "still accurate"}], [],
    ) == []
    # user_flow/wireframe per-item trigger: only a PRE-EXISTING item whose own cited AC changed is
    # demanded -- a brand-new one (not in the prior set) is exempt, and an untouched AC's item is
    # never demanded even though the spec changed elsewhere this run.
    assert check_stale_visual_review(
        [{"name": "brand-new-flow", "kind": "user_flow", "ac_ids": ["US-0007.1"]}], [], set(), set(),
        changed_ledger, "r9", set(), [], [],
    ) == [], "a diagram created THIS run is exempt -- its existence already proves it isn't stale"
    assert check_stale_visual_review(
        [], [{"screen": "untouched", "ac_ids": ["US-0007.2"]}], set(), {"untouched"},
        changed_ledger, "r9", set(), [], [],
    ) == [], "a wireframe whose own cited AC did not change this run is never demanded"
    assert any(
        "affected-flow" in p
        for p in check_stale_visual_review(
            [{"name": "affected-flow", "kind": "user_flow", "ac_ids": ["US-0007.1"]}], [], {"affected-flow"}, set(),
            changed_ledger, "r9", set(), [], [],
        )
    )
    # A bug-reopened (wording unchanged) AC also triggers the per-item wireframe/user_flow check.
    assert any(
        "reopened-screen" in p
        for p in check_stale_visual_review(
            [], [{"screen": "reopened-screen", "ac_ids": ["US-0007.2"]}], set(), {"reopened-screen"},
            changed_ledger, "r9", {"US-0007.2"}, [], [],
        )
    )

    print("diagram_gate wireframe self-check: all assertions passed")


# Task 13b: one line per DISTINCT rejection reason inside verify_plan_diagrams below, including
# every reason folded into check_plan_linkage/check_wireframe_ac_ids/check_ui_wireframe_coverage/
# check_wireframe/_render_one -- all defined in THIS file, so (unlike write_scope_gate.py's
# delegation to ac_coverage_gate.py) none of it is out-of-scope delegation. The 8
# _WIREFRAME_FORBIDDEN patterns are one rule ("must be self-contained/safe"), not eight -- same
# granularity write_scope_gate.py's _is_test_path regex family gets. A render failure classified
# as infrastructure (mmdc/Chromium broken, not a genuine Mermaid syntax error) is deliberately
# NOT a rule here -- the model cannot fix its own sandbox's browser install, and the gate itself
# never blames it for one (see _looks_like_infra_failure).
PLAN_HARD_RULES: tuple[str, ...] = (
    "Never submit empty plan content -- a draft that never produced a real plan (e.g. stuck on "
    "clarifying questions) is rejected outright.",
    "Every id you name in a step's removes_ids must actually exist in the spec ledger -- an "
    "invented or mistyped id is rejected.",
    "Every id you name in a step's removes_ids must be RETIRED, not live -- live/active scope "
    "belongs in ac_ids, never in removes_ids.",
    "If this ticket retires a criterion an earlier run already delivered (its code, tests, and "
    "UI already exist), some plan step must name it (or its parent story) in removes_ids and "
    "describe the removal work -- a delivered-then-retired criterion cannot just be dropped.",
    "Every feature step must cite at least one live US-####.# acceptance-criterion id in "
    "ac_ids, unless it is kind='infrastructure' -- a step with no citations and no "
    "infrastructure kind is rejected.",
    "Every acceptance-criterion id a step cites in ac_ids must be a real id copied verbatim "
    "from the approved Specification's ledger -- an invented or mistyped id is rejected.",
    "A step whose every cited criterion is retired or deferred implements out-of-ticket scope "
    "and must be dropped from the plan entirely.",
    "A step may not mix live and non-live (retired/deferred) criteria in ac_ids -- ac_ids may "
    "only name LIVE criteria; move a retired criterion's citation to removes_ids and drop "
    "deferred scope from the plan altogether.",
    "A new or materially changed step may not cite only already-delivered (completed) criteria "
    "-- completed criteria are never re-planned; either carry the prior step over verbatim "
    "(identical description) or drop it.",
    "Every one of this ticket's own undelivered, live criteria must be cited by ac_ids in at "
    "least one plan step -- an eligible criterion with no covering step is rejected.",
    "Every acceptance-criterion id a wireframe cites in its ac_ids must be a real id from the "
    "approved Specification's ledger, copied verbatim -- an invented or mistyped id is "
    "rejected.",
    "Every wireframe must cite at least one ac_id -- a wireframe with an empty ac_ids list is "
    "rejected, since the e2e stage has nothing to match it against.",
    "Every criterion the approved Specification marks ui_related must be cited by at least one "
    "wireframe's ac_ids -- a UI-facing requirement with zero wireframe evidence is rejected.",
    "Include at most 6 wireframes -- keep only the screens this plan actually changes.",
    "Every wireframe's screen name must be a plain filename (letters, digits, underscore, "
    "hyphen only).",
    "Every wireframe's HTML must stay under 30 KB -- simplify an oversized wireframe rather "
    "than submit it.",
    "Every wireframe must actually be an HTML page (contain an <html>, <body>, or <div> tag) "
    "-- prose with no markup is rejected.",
    "Every wireframe must be fully self-contained, inline-styled HTML with no <script> tags, "
    "inline on*= event handlers, external URLs or stylesheets, javascript:/vbscript:/data:/"
    "file: URLs, embedding/navigation elements (iframe/object/embed/base/form), or "
    "<meta http-equiv> overrides.",
    "Every diagram's name must be a plain filename (letters, digits, underscore, hyphen only).",
    "Every diagram's Mermaid source must actually render -- a real parse/syntax error must be "
    "fixed before this stage can pass (an infrastructure failure in the renderer itself is not "
    "held against you).",
)


async def _load_and_sync_plan_steps(
    provider: SandboxProvider,
    thread_id: str,
    run_id: str,
    stage_key: str,
    chat_provider: str,
    has_audit_role: bool,
    lap: int,
) -> tuple[list[dict[str, Any]] | None, list[str], list[dict[str, Any]], str | None]:
    """File-based-editing plan, Part 2 sect. 5/6: reads _draft/steps.json, validates each entry
    against schemas.PlanStep, computes `fully_reviewed` (Part 3's review-depth safety net,
    targeting the AUDIT session for `stage_key` -- `has_audit_role=False` skips this entirely, the
    audit-role carve-out a stage with no audit pass, e.g. a brownfield baseline pass, needs so it
    can ever pass verify at all), and calls sync_plan_ledger. Persists the ledger and writes the
    ledger-resolved content back to steps.json on success (mirrors Part 1's identical write-back
    for draft-specification.json).

    Returns (resolved_plan_steps_or_None-on-failure, problems, ledger_entries, infra_error) --
    ledger_entries is always returned, even on failure, so the caller's later checks (stale-review's
    spec-changed delta) can still compute against it. `infra_error` is None for every content
    verdict and a report["infra_error"] code when the platform could not produce the evidence a
    check needed (today: the audit session's transcript for the full-read proof) -- the caller
    routes that onto the infra-retry budget instead of the stage's own verify laps.

    `lap` is the stage's pre-increment verify_cycle_count, threaded from make_verify_node, so the
    audit session lookup below keys on the same `audit-{run_id}-{lap}` string make_audit_node used.
    """
    from ..schemas import PlanStep

    raw = await repo_files.read_repo_file(provider, thread_id, DRAFT_STEPS_PATH)
    ledger_entries = await spec_ledger.load_ledger(provider, thread_id)
    if raw is None:
        return None, [
            f"{DRAFT_STEPS_PATH} does not exist -- create it with your file tools, shaped "
            '{"plan_steps": [...], "retired_step_ids": [...]}.'
        ], ledger_entries, None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"{DRAFT_STEPS_PATH} is not valid JSON: {exc}. Fix it and resubmit."], ledger_entries, None
    if not isinstance(doc, dict):
        return None, [f"{DRAFT_STEPS_PATH} must be a JSON object with 'plan_steps'/'retired_step_ids' keys."], ledger_entries, None

    raw_steps = doc.get("plan_steps")
    retired_step_ids = doc.get("retired_step_ids") or []
    if not isinstance(raw_steps, list):
        return None, [f"{DRAFT_STEPS_PATH}'s 'plan_steps' must be a list."], ledger_entries, None
    validated: list[dict[str, Any]] = []
    for i, raw_step in enumerate(raw_steps):
        try:
            validated.append(PlanStep.model_validate(raw_step).model_dump())
        except Exception as exc:  # noqa: BLE001 -- surfaced as actionable feedback, never a crash
            return None, [
                f"{DRAFT_STEPS_PATH}'s plan_steps[{i}] does not match the PlanStep shape: {exc}"
            ], ledger_entries, None

    fully_reviewed: bool | None = None
    if has_audit_role:
        # Per-lap audit role key, not the bare "audit" label -- identical fix and reasoning to
        # graph.py's _verify_specification_ledger (2026-09-18, session 6244ef47): the cache is
        # keyed `audit-{run_id}-{lap}`, so the bare label returned None on every lap and this
        # gate's fail-closed branch rejected a plan the audit HAD fully read.
        audit_role = chat_model.lap_role("audit", run_id, lap)
        session_id = chat_model.get_session_id(thread_id, stage_key, audit_role, provider=chat_provider)
        evidence: bool | None = None
        if session_id is not None:
            total_lines = raw.count("\n") + 1
            evidence = await chat_model.read_full_file_reads(
                provider, thread_id, session_id, DRAFT_STEPS_PATH, total_lines, active_provider=chat_provider
            )
        # A provider that CAN prove this (Claude) but produced nothing to check -- no cached session
        # id for this lap's audit role, or its transcript unreadable -- is a platform fault, surfaced
        # as an infra_error verdict (spends config.VERIFY_INFRA_RETRY_CAP, never the stage's own
        # laps) rather than collapsing into fully_reviewed=False, which is indistinguishable from a
        # genuine partial read. None under a provider that structurally cannot verify transcripts
        # (Copilot) stays fail-open: sync_plan_ledger skips the check. A bool is real evidence.
        if evidence is None and chat_model.provider_can_verify_transcripts(chat_provider):
            return None, [
                chat_model.transcript_unreadable_feedback(stage_key, audit_role, session_id)
            ], ledger_entries, "audit_transcript_unreadable"
        fully_reviewed = evidence

    sync_result = spec_ledger.sync_plan_ledger(
        ledger_entries, validated, run_id, retired_step_ids=retired_step_ids, fully_reviewed=fully_reviewed,
    )
    if not sync_result.passed:
        return None, sync_result.reasons, ledger_entries, None
    await spec_ledger.save_ledger(provider, thread_id, sync_result.updated_entries)
    resolved = [
        e for e in sync_result.updated_entries
        if e.get("kind") == "plan_step" and e.get("status") in ("active", "revised")
    ]
    resolved_doc = {
        "plan_steps": [
            {
                "id": e["id"],
                "description": e.get("description", ""),
                "ac_ids": e.get("ac_ids") or [],
                "kind": e.get("step_kind", "feature"),
                "ui_related": e.get("ui_related", False),
                "removes_ids": e.get("removes_ids") or [],
            }
            for e in resolved
        ],
        "retired_step_ids": retired_step_ids,
    }
    await repo_files.write_repo_file(provider, thread_id, DRAFT_STEPS_PATH, json.dumps(resolved_doc, indent=2))
    return resolved_doc["plan_steps"], [], sync_result.updated_entries, None


async def _load_and_check_manifest(
    provider: SandboxProvider, thread_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """File-based-editing plan, Part 2 sect. 6: reads _draft/manifest.json, validates each entry
    against schemas.PlanDiagramRef/PlanWireframeRef, and checks disk-vs-manifest consistency --
    every file in _draft/wireframes//_draft/diagrams/ must be listed in manifest.json (current or
    explicitly retired), and vice versa (no ledger needed for this direction -- see the plan's own
    YAGNI reasoning: nothing downstream cites a "wireframe id" for cross-run provenance the way
    plan steps/ACs are). Returns (manifest-shaped dict or None on failure, problems).
    """
    from ..schemas import PlanDiagramRef, PlanWireframeRef

    raw = await repo_files.read_repo_file(provider, thread_id, DRAFT_MANIFEST_PATH)
    if raw is None:
        return None, [
            f"{DRAFT_MANIFEST_PATH} does not exist -- create it with your file tools, shaped "
            '{"wireframes": [...], "diagrams": [...], "retired_wireframe_screens": [...], '
            '"retired_diagram_names": [...]}.'
        ]
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"{DRAFT_MANIFEST_PATH} is not valid JSON: {exc}. Fix it and resubmit."]
    if not isinstance(doc, dict):
        return None, [f"{DRAFT_MANIFEST_PATH} must be a JSON object."]

    problems: list[str] = []
    wireframe_refs: list[dict[str, Any]] = []
    for i, wf in enumerate(doc.get("wireframes") or []):
        try:
            wireframe_refs.append(PlanWireframeRef.model_validate(wf).model_dump())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"manifest.json's wireframes[{i}] does not match the expected shape (screen/ac_ids): {exc}")
    diagram_refs: list[dict[str, Any]] = []
    for i, d in enumerate(doc.get("diagrams") or []):
        try:
            diagram_refs.append(PlanDiagramRef.model_validate(d).model_dump())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"manifest.json's diagrams[{i}] does not match the expected shape (name/kind/ac_ids): {exc}")
    if problems:
        return None, problems

    retired_wireframe_screens = set(doc.get("retired_wireframe_screens") or [])
    retired_diagram_names = set(doc.get("retired_diagram_names") or [])

    wf_ls = await provider.exec_in_sandbox(thread_id, f"ls {shlex.quote(DRAFT_WIREFRAMES_DIR)} 2>/dev/null")
    disk_wireframe_screens = {
        line.strip()[: -len(".html")] for line in (wf_ls.stdout or "").splitlines() if line.strip().endswith(".html")
    }
    manifest_wireframe_screens = {wf["screen"] for wf in wireframe_refs}
    for screen in sorted(disk_wireframe_screens - manifest_wireframe_screens - retired_wireframe_screens):
        problems.append(
            f"{DRAFT_WIREFRAMES_DIR}/{screen}.html exists on disk but is not listed in "
            "manifest.json's wireframes (or retired_wireframe_screens)"
        )
    for screen in sorted(manifest_wireframe_screens - disk_wireframe_screens):
        problems.append(f"manifest.json lists wireframe {screen!r} but {DRAFT_WIREFRAMES_DIR}/{screen}.html does not exist -- create it")

    dg_ls = await provider.exec_in_sandbox(thread_id, f"ls {shlex.quote(DRAFT_DIAGRAMS_DIR)} 2>/dev/null")
    disk_diagram_names = {
        line.strip()[: -len(".mmd")] for line in (dg_ls.stdout or "").splitlines() if line.strip().endswith(".mmd")
    }
    manifest_diagram_names = {d["name"] for d in diagram_refs}
    for name in sorted(disk_diagram_names - manifest_diagram_names - retired_diagram_names):
        problems.append(
            f"{DRAFT_DIAGRAMS_DIR}/{name}.mmd exists on disk but is not listed in manifest.json's "
            "diagrams (or retired_diagram_names)"
        )
    for name in sorted(manifest_diagram_names - disk_diagram_names):
        problems.append(f"manifest.json lists diagram {name!r} but {DRAFT_DIAGRAMS_DIR}/{name}.mmd does not exist -- create it")

    if problems:
        return None, problems
    return {
        "wireframes": wireframe_refs,
        "diagrams": diagram_refs,
        "retired_wireframe_screens": sorted(retired_wireframe_screens),
        "retired_diagram_names": sorted(retired_diagram_names),
    }, []


def make_verify_plan_diagrams(
    stage_key: str = "plan", has_audit_role: bool = True
) -> Callable[[str, dict[str, Any], str, str | None, SandboxProvider, str, int], Any]:
    """Factory, not a bare function (file-based-editing plan, Part 6 audit fix): Part 6's
    brownfield plan-pass reuses this exact verification logic under a DIFFERENT stage-key (not the
    real "plan" key, so the graph's own linear stage-chain doesn't misroute -- see graph.py's
    brownfield wiring) and with no audit role at all. A bare module-level function had no way to
    know which stage-key/audit-role it was running for -- StageSpec.deterministic_verify's own
    calling convention (make_verify_node) never passes the stage key through, so hardcoding
    "plan"/"audit" inline (as this function originally did) would look up the WRONG session, or
    the right session under the wrong provider policy, for any caller besides the real plan stage.

    `verify_plan_diagrams` below is `make_verify_plan_diagrams("plan")` -- the real plan
    StageSpec's own `deterministic_verify`, unchanged from every existing caller's perspective.
    """

    async def verify_plan_diagrams(
        thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None,
        provider: SandboxProvider, chat_provider: str, lap: int = 0,
    ) -> "VerificationResult":
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

        # File-based-editing plan, Part 2 sect. 1: commit the whole scratch dir on EVERY
        # invocation, pass or fail -- unlike specification's single sketchpad file (which already
        # rides along in every git_ops.commit_ai_dev_workflow call), _draft/ is new scratch with no
        # existing commit path, and losing it to a mid-run container swap would be a real
        # regression versus today's durably-persisted stage["draft"].
        await git_ops.commit_paths(provider, thread_id, [DRAFT_DIR], "ai-dev-workflow: plan draft scratch")

        # Part 2 sect. 7: the write-scope guard -- plan previously had zero write tools, so this is
        # a genuinely new risk (a plan-stage edit accidentally touching spec_ledger.json, approved
        # specs, or real application source). Same mechanism ac-to-tests already uses, a different
        # allowlist.
        write_scope = await write_scope_gate.check_write_scope(
            provider, thread_id, baseline_commit, run_id,
            is_in_scope=write_scope_gate.is_plan_scratch_path,
            is_pipeline_owned=write_scope_gate.is_plan_pipeline_owned,
        )
        if not write_scope.passed:
            return VerificationResult(
                passed=False,
                feedback=(
                    "These files are outside plan's write scope and could not be auto-reverted: "
                    f"{write_scope.violating_paths}. Only .ai-dev-workflow/plan/_draft/** may be "
                    "created or modified here."
                ),
                report={"violating_paths": write_scope.violating_paths, "changed_paths": write_scope.changed_paths},
            )

        # Provenance first: pure checks against the ledger, cheaper than any render, and a plan whose
        # steps aren't linked to this ticket's criteria is wrong regardless of its diagrams. The spec
        # read falls back to an empty own-set (coverage direction skipped) the same way
        # ac_coverage_gate's identical read does -- an infra hiccup must not manufacture a false gap.
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
        bug_affected_ac_ids = set(spec_doc.get("bug_affected_ac_ids") or [])

        prior_steps_by_id: dict[str, dict[str, Any]] = {}
        prior_wireframe_screens: set[str] = set()
        prior_diagram_names: set[str] = set()
        prior_diagrams_by_name: dict[str, str] = {}
        prior_wireframes_by_screen: dict[str, str] = {}
        raw_prior_plan = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.PLAN_APPROVED_PATH)
        if raw_prior_plan is not None:
            try:
                prior_plan_doc = json.loads(raw_prior_plan)
            except json.JSONDecodeError:
                prior_plan_doc = {}
            prior_steps_by_id = {
                s.get("id"): s for s in (prior_plan_doc.get("plan_steps") or []) if s.get("id")
            }
            for d in _presence_values(prior_plan_doc.get("diagrams")):
                if d.get("name"):
                    prior_diagram_names.add(d["name"])
                    prior_diagrams_by_name[d["name"]] = d.get("mermaid_source", "")
            for wf in _presence_values(prior_plan_doc.get("wireframes")):
                if wf.get("screen"):
                    prior_wireframe_screens.add(wf["screen"])
                    prior_wireframes_by_screen[wf["screen"]] = wf.get("html_source", "")

        # File-based-editing plan, Part 2 sect. 5/6: load+sync steps.json and manifest.json BEFORE
        # check_plan_linkage runs -- a new, earlier check, not a replacement (everything below this
        # point that already existed is unchanged).
        resolved_steps, step_problems, ledger_entries, step_infra_error = await _load_and_sync_plan_steps(
            provider, thread_id, run_id, stage_key, chat_provider, has_audit_role, lap,
        )
        if step_infra_error is not None:
            # Platform could not evaluate a check (see _load_and_sync_plan_steps): infra verdict,
            # same routing make_verify_node gives ac_coverage_gate's missing-artifact case.
            return VerificationResult(
                passed=False,
                feedback="\n\n".join(step_problems),
                report={"infra_error": step_infra_error, "step_problems": step_problems},
            )
        manifest, manifest_problems = await _load_and_check_manifest(provider, thread_id)
        if step_problems or manifest_problems:
            return VerificationResult(
                passed=False,
                feedback="\n\n".join(step_problems + manifest_problems),
                report={"step_problems": step_problems, "manifest_problems": manifest_problems},
            )
        assert resolved_steps is not None and manifest is not None  # guaranteed by the empty-problems check above

        wireframe_refs = manifest["wireframes"]
        diagram_refs = manifest["diagrams"]
        retired_wireframe_screens = set(manifest["retired_wireframe_screens"])
        retired_diagram_names = set(manifest["retired_diagram_names"])

        pre_check_problems = check_dangling_visual_retirement(
            wireframe_refs, diagram_refs, ledger_entries, retired_wireframe_screens, retired_diagram_names,
        )
        pre_check_problems += check_stale_visual_review(
            diagram_refs, wireframe_refs, prior_diagram_names, prior_wireframe_screens,
            ledger_entries, run_id, bug_affected_ac_ids,
            content_dict.get("diagrams_reviewed") or [], content_dict.get("wireframes_reviewed") or [],
        )

        # Read every diagram/wireframe's real content from its sidecar file -- needed both to
        # mechanically verify a claimed "revised" action actually changed the content (below) and
        # to inject the full ImplementationPlan-shaped content back into content_dict once
        # everything passes (Part 2 sect. 2's "critical property": the persisted shape stays
        # byte-for-byte identical to today, so downstream code never needs to change).
        diagram_sources: dict[str, str] = {}
        for d in diagram_refs:
            raw_src = await repo_files.read_repo_file(provider, thread_id, f"{DRAFT_DIAGRAMS_DIR}/{d['name']}.mmd")
            if raw_src is None:
                pre_check_problems.append(f"{DRAFT_DIAGRAMS_DIR}/{d['name']}.mmd could not be read")
                continue
            diagram_sources[d["name"]] = raw_src
        wireframe_sources: dict[str, str] = {}
        for wf in wireframe_refs:
            raw_src = await repo_files.read_repo_file(provider, thread_id, f"{DRAFT_WIREFRAMES_DIR}/{wf['screen']}.html")
            if raw_src is None:
                pre_check_problems.append(f"{DRAFT_WIREFRAMES_DIR}/{wf['screen']}.html could not be read")
                continue
            wireframe_sources[wf["screen"]] = raw_src

        # Mechanical verification of a claimed "revised" action (Part 2 sect. 6 item 4): the file
        # must actually differ from the last-approved version -- catches a false "revised" claim,
        # not just trusting the label. A diagram/wireframe with no prior version (new this run) is
        # trivially "revised" by virtue of not having existed; only checked when a prior exists.
        for record in content_dict.get("diagrams_reviewed") or []:
            name = record.get("name")
            if record.get("action") == "revised" and name in prior_diagrams_by_name:
                if diagram_sources.get(name, "") == prior_diagrams_by_name[name]:
                    pre_check_problems.append(
                        f"diagram {name!r} claims action='revised' but its mermaid_source is "
                        "byte-identical to the last-approved version -- either actually revise it "
                        "or claim 'confirmed_current' instead"
                    )
        for record in content_dict.get("wireframes_reviewed") or []:
            screen = record.get("screen")
            if record.get("action") == "revised" and screen in prior_wireframes_by_screen:
                if wireframe_sources.get(screen, "") == prior_wireframes_by_screen[screen]:
                    pre_check_problems.append(
                        f"wireframe {screen!r} claims action='revised' but its HTML is "
                        "byte-identical to the last-approved version -- either actually revise it "
                        "or claim 'confirmed_current' instead"
                    )

        if pre_check_problems:
            return VerificationResult(passed=False, feedback="\n\n".join(pre_check_problems), report={"pre_check_problems": pre_check_problems})

        # Inject the file-resolved FULL content back into content_dict, matching today's
        # ImplementationPlan shape exactly, before any of the existing logic below (all of it
        # unchanged from before this rewrite) ever sees it -- Part 2 sect. 2's "critical property".
        content_dict["plan_steps"] = resolved_steps
        content_dict["diagrams"] = {
            "status": "present" if diagram_refs else "absent",
            "values": [
                {"name": d["name"], "kind": d["kind"], "ac_ids": d.get("ac_ids") or [], "mermaid_source": diagram_sources[d["name"]]}
                for d in diagram_refs
            ],
            "reason": "" if diagram_refs else "No diagrams in this plan.",
        }
        content_dict["wireframes"] = {
            "status": "present" if wireframe_refs else "absent",
            "values": [
                {"screen": wf["screen"], "ac_ids": wf.get("ac_ids") or [], "html_source": wireframe_sources[wf["screen"]]}
                for wf in wireframe_refs
            ],
            "reason": "" if wireframe_refs else "No user-interface work in this plan.",
        }
        content_dict["retired_wireframe_screens"] = manifest["retired_wireframe_screens"]
        content_dict["retired_diagram_names"] = manifest["retired_diagram_names"]

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
        # Aggregated, not early-returned (2026-09-07 audit): these three check independent aspects of
        # the same content -- ledger/AC linkage, wireframe count, and per-wireframe HTML validity --
        # with no ordering dependency between them, so union them into one lap's feedback instead of
        # reporting only whichever hit first.
        linkage_problems = (
            check_plan_linkage(content_dict.get("plan_steps") or [], ledger_entries, own_ac_ids, prior_steps_by_id, run_id=run_id)
            + check_wireframe_ac_ids(wireframes, ledger_entries)
            + check_wireframe_has_ac_ids(wireframes)
            + check_ui_wireframe_coverage(ui_related_ac_ids, wireframes)
        )

        # Scope-lifecycle stamps for the Plan review UI (user requirement 2026-08-31, mirroring the
        # spec view's badges): each step inherits the strongest change classification of the criteria
        # it fulfils, straight from the approved Specification's own per-AC `change` stamps -- so a
        # reviewer sees which steps exist because of NEW scope, an UPDATE, or a promotion
        # ("activated"), and removal steps are recognizable by their removes_ids. Deterministic,
        # stamped in place (this verify's established contract -- see the wireframe preview_url
        # stamping below). Unconditional: harmless on a failing lap (the envelope this feeds is only
        # ever built on a PASS, graph.py's make_verify_node), so it doesn't need to wait on
        # linkage_problems.
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

        structural_problems = list(linkage_problems)
        wireframes_too_many = len(wireframes) > MAX_WIREFRAMES
        if wireframes_too_many:
            structural_problems.append(
                f"{len(wireframes)} wireframes exceeds the cap of {MAX_WIREFRAMES} -- keep only the screens this plan actually changes."
            )
        wireframe_errors = [
            err for wf in wireframes if (err := check_wireframe(wf.get("screen") or "", wf.get("html_source") or "")) is not None
        ]
        structural_problems.extend(wireframe_errors)

        # Wireframes are only written to the sandbox once individually valid and within the cap
        # (unchanged invariant) -- gated on wireframe_errors/count specifically, not on linkage
        # problems elsewhere in the same content, since a bad AC citation has nothing to do with
        # whether a given wireframe's own HTML is safe to persist.
        if wireframes and not wireframe_errors and not wireframes_too_many:
            for wf in wireframes:
                await repo_files.write_repo_file(provider, thread_id, f"{WIREFRAMES_DIR}/{wf['screen']}.html", wf["html_source"])
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

        # Diagrams and wireframes are independent artifacts (2026-09-07 audit, confirmed by reading
        # _render_one: it only ever touches diagram["name"]/["mermaid_source"], nothing wireframe-
        # related) -- render regardless of any structural problem found above, so a broken wireframe
        # and a failing diagram can both be reported in the same lap instead of costing two.
        outcomes = [await _render_one(provider, thread_id, diagram) for diagram in diagrams] if diagrams else []
        failures = [o for o in outcomes if not o.ok]
        infra_failures = [o for o in failures if o.is_infra_failure]

        if structural_problems or failures:
            feedback_parts = list(structural_problems)
            if failures:
                if infra_failures:
                    # Distinct from a real syntax problem -- the draft node retrying with "fix your
                    # Mermaid syntax" feedback would be nonsensical here since the syntax was never
                    # actually checked.
                    feedback_parts.append(
                        f"Diagram rendering infrastructure failure (mermaid-cli/Chromium), not a diagram syntax "
                        f"problem -- affected: {[o.name for o in infra_failures]}. First error: "
                        f"{infra_failures[0].stderr_tail}"
                    )
                else:
                    feedback_parts.append(
                        "; ".join(f"{o.name}: {_mermaid_error_summary(o.stderr_tail)}" for o in failures)
                    )
            report: dict[str, Any] = {}
            if linkage_problems:
                report["plan_linkage_failed"] = linkage_problems
            if wireframes_too_many:
                report["wireframes_rejected"] = "too_many"
            if wireframe_errors:
                report["wireframes_failed"] = wireframe_errors
            if failures:
                report["failed"] = [o.name for o in failures]
                report["infra_failure"] = bool(infra_failures)
            return VerificationResult(passed=False, feedback="\n\n".join(feedback_parts), report=report)

        if not diagrams and not wireframes:
            return VerificationResult(passed=True, feedback="No diagrams or wireframes in this draft -- nothing to validate.", report={})

        commit_dirs = ([DIAGRAMS_DIR] if diagrams else []) + ([WIREFRAMES_DIR] if wireframes else [])
        await git_ops.commit_paths(provider, thread_id, commit_dirs, "ai-dev-workflow: render plan diagrams + wireframes")
        return VerificationResult(
            passed=True,
            feedback=f"All {len(diagrams)} diagram(s) rendered and {len(wireframes)} wireframe(s) validated.",
            report={"rendered": [o.name for o in outcomes], "wireframes": [wf["screen"] for wf in wireframes]},
        )

    return verify_plan_diagrams


# The real plan StageSpec's own `deterministic_verify` -- unchanged reference for every existing
# caller (graph.py, self-checks below). Part 6's brownfield plan-pass calls
# make_verify_plan_diagrams("brownfield-plan", has_audit_role=False) instead.
verify_plan_diagrams = make_verify_plan_diagrams("plan")


if __name__ == "__main__":
    _demo()
