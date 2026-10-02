"""Requirements PRD stage: the round's file-based PRD merge, its deterministic checks, and rendering.

Each round's requirements delta is merged into the durable PRD (`01-requirements-prd.md`) by a model
that EDITS a scratch copy in place and declares, line by line, which prior requirements this delta
removed or changed -- and whether the delta said so outright ("explicit") or only implied it
("implied": a new requirement that contradicts or narrows an old one). The declared changes become
`PC-n` ids the Specification must account for (gates/ledger_sync_checks.check_prd_changes_addressed).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from . import git_ops, repo_files, workflow_persistence
from .gates.checks import Check, CheckLog
from .schemas import PrdChangesFile
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState, VerificationResult

# The round's scratch files. Seeded by hydrate_prd_round, edited by the model, read by verify.
PRD_DIR = ".ai-dev-workflow/prd"
BASE_PATH = f"{PRD_DIR}/base.md"  # the PRD as it stood when this round began
DRAFT_PATH = f"{PRD_DIR}/draft-prd.md"  # the model's edited copy, without Revision History
CHANGES_PATH = f"{PRD_DIR}/changes.json"  # schemas.PrdChangesFile: summary + declared changes
DELTA_PATH = f"{PRD_DIR}/delta.md"  # this round's requirements text, for verify's delta_quote check
# {message_id, approved_sha, base, delta}: which round the files belong to, plus the trusted copies of
# the round's base PRD and requirements text -- verify judges against these, never against base.md /
# delta.md, which the model can edit by mistake (base.md is an identical twin of the file it edits).
SEED_PATH = f"{PRD_DIR}/seed.json"

# The PRD's fixed structure (also stated in prompts/requirements_prd_draft.md). Protocol, not config.
REQUIRED_HEADINGS = (
    "Overview / Problem Statement",
    "Goals & Success Metrics",
    "Users / Personas",
    "Functional Requirements",
    "Non-Functional Requirements",
    "Out of Scope",
    "Open Questions & Assumptions",
)
HISTORY_HEADING = "## Revision History"
# Sections whose lines are requirements: a removed or changed line here must be declared. Overview,
# goals and open questions are narrative that legitimately gets reworded every round.
TRACKED_SECTIONS = frozenset({
    "Users / Personas", "Functional Requirements", "Non-Functional Requirements", "Out of Scope",
})
SKELETON = "# <Product name>\n\n" + "\n".join(f"## {h}\n" for h in REQUIRED_HEADINGS)

# How alike a rewritten line must be to the old one to count as "modified" rather than "removed"
# (difflib ratio). Only labels the change for the reviewer -- what must be declared doesn't use it.
_MODIFIED_SIMILARITY = 0.5
# Lines that stand in for an empty section ("None." -- the prompt asks for it). Not requirements.
_PLACEHOLDERS = frozenset({"none", "none.", "none yet", "none yet.", "n/a", "n/a.", "na", "tbd", "tbd.", "-"})
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TYPOGRAPHIC = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"})


def _lines(text: str) -> list[str]:
    """Lines as the Read tool numbers them: only a newline ends one (str.splitlines also splits on
    U+2028 and friends, which would shift every later line number). A trailing CR is dropped."""
    return [line.rstrip("\r") for line in text.split("\n")]


def _normalize(text: str) -> str:
    """Comparison key for free text: typographic quotes/dashes made plain, whitespace collapsed,
    lowercased."""
    return " ".join(text.translate(_TYPOGRAPHIC).split()).lower()


def _key(line: str) -> str:
    """A requirement line's identity: its text without the list marker. A marker change, a
    renumbered list or reflowed whitespace is not a change to the requirement."""
    return _normalize(_LIST_MARKER.sub("", line))

PRD_DRAFT_FILES = Check(
    "prd.draft_files", "Draft PRD and change list are readable",
    f"Checks {DRAFT_PATH} has content and {CHANGES_PATH} is valid: a one-line summary of the round plus "
    "a list of declared changes. Every later check reads these two files.",
    "blocking",
)
PRD_STRUCTURE = Check(
    "prd.structure", "PRD keeps its required sections",
    "Checks the draft keeps every required section heading, in order, and leaves out Revision History "
    "(the pipeline appends that itself, so earlier entries can never be lost).",
    "collected",
)
PRD_CHANGES_GROUNDED = Check(
    "prd.changes_grounded", "Declared changes point at real requirements",
    "Checks each declared change names real requirement lines of the previous PRD and quotes the words "
    "in this round's requirements that drive it, so every change traces back to what the user wrote.",
    "collected",
)
PRD_CHANGES_DECLARED = Check(
    "prd.changes_declared", "Every changed requirement is declared",
    "Checks every requirement the merge removed or rewrote is declared as a change, and nothing "
    "unchanged is. A requirement can't silently disappear or drift from one round to the next, and each "
    "declared change becomes an item the Specification must account for.",
    "collected",
)
VERIFY_CHECKS: tuple[Check, ...] = (PRD_DRAFT_FILES, PRD_STRUCTURE, PRD_CHANGES_GROUNDED, PRD_CHANGES_DECLARED)


def strip_history(prd: str) -> str:
    """Everything before the Revision History heading. Line numbers match the full document's, so a
    line number in base.md means the same line here. Pure."""
    lines = _lines(prd)
    for index, line in enumerate(lines):
        if line.strip() == HISTORY_HEADING:
            lines = lines[:index]
            break
    text = "\n".join(lines).rstrip()
    return f"{text}\n" if text else ""


def history_bullets(prd: str) -> list[str]:
    """The Revision History section's non-blank lines, verbatim (sub-bullets keep their indent). Pure."""
    bullets: list[str] = []
    inside = False
    for line in _lines(prd):
        if line.strip() == HISTORY_HEADING:
            inside = True
        elif inside and line.startswith("## "):
            break
        elif inside and line.strip():
            bullets.append(line.rstrip())
    return bullets


def _sections(lines: list[str]) -> list[str | None]:
    """The `## ` section each line sits in (None before the first one)."""
    current: str | None = None
    out: list[str | None] = []
    for line in lines:
        if line.startswith("## "):
            current = line[3:].strip()
        out.append(current)
    return out


def _requirement_lines(lines: list[str]) -> set[int]:
    """0-based indexes of the requirement lines: non-blank, non-heading, non-placeholder lines inside
    a tracked section."""
    sections = _sections(lines)
    return {
        i for i, line in enumerate(lines)
        if sections[i] in TRACKED_SECTIONS and line.strip() and not line.startswith("#")
        and _key(line) not in _PLACEHOLDERS
    }


def _changed_lines(base_lines: list[str], draft_lines: list[str]) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """({1-based base line: (kind, replacement)}, added lines). A base requirement is unchanged when
    the same requirement (same `_key`) still sits in the same section of the draft -- wherever it
    moved to, whatever marker it now has. Each remaining old line is paired with its most similar
    leftover new line in the same section ("modified"); an old line with no close match was
    "removed"; a leftover new line no old line pairs with was added."""
    base_sections, draft_sections = _sections(base_lines), _sections(draft_lines)
    pool: dict[tuple[str | None, str], list[int]] = {}
    for j in sorted(_requirement_lines(draft_lines)):
        pool.setdefault((draft_sections[j], _key(draft_lines[j])), []).append(j)
    unmatched_old = []
    for i in sorted(_requirement_lines(base_lines)):
        bucket = pool.get((base_sections[i], _key(base_lines[i])))
        if bucket:
            bucket.pop(0)
        else:
            unmatched_old.append(i)
    leftover_new = sorted(j for bucket in pool.values() for j in bucket)
    pairs = sorted(
        (
            (difflib.SequenceMatcher(None, _key(base_lines[i]), _key(draft_lines[j])).ratio(), i, j)
            for i in unmatched_old for j in leftover_new if base_sections[i] == draft_sections[j]
        ),
        reverse=True,
    )
    paired: dict[int, int] = {}
    used_new: set[int] = set()
    for ratio, i, j in pairs:
        if ratio >= _MODIFIED_SIMILARITY and i not in paired and j not in used_new:
            paired[i] = j
            used_new.add(j)
    changed = {
        i + 1: ("modified", draft_lines[paired[i]].strip()) if i in paired else ("removed", "")
        for i in unmatched_old
    }
    added = [draft_lines[j].strip() for j in leftover_new if j not in used_new]
    return changed, added


def check_structure(draft: str) -> list[str]:
    """The draft keeps every required heading, in order, and has no Revision History. Pure."""
    problems: list[str] = []
    lines = [line for line in _lines(draft) if line.strip()]
    if not lines or not lines[0].startswith("# "):
        problems.append(f"{DRAFT_PATH} must start with the product's `# <Product name>` title line")
    headings = [line[3:].strip() for line in _lines(draft) if line.startswith("## ")]
    missing = [h for h in REQUIRED_HEADINGS if h not in headings]
    if missing:
        problems.append(
            f"{DRAFT_PATH} is missing section(s): {', '.join(f'## {h}' for h in missing)} -- keep every "
            "required heading exactly as written, even when a section has nothing new"
        )
    present = [h for h in headings if h in REQUIRED_HEADINGS]
    if present != [h for h in REQUIRED_HEADINGS if h in present]:
        problems.append(f"{DRAFT_PATH}'s sections are out of order -- keep them in the required order")
    if HISTORY_HEADING[3:] in headings:
        problems.append(
            f"{DRAFT_PATH} contains `{HISTORY_HEADING}` -- leave it out; the pipeline appends this round's "
            "entry to the existing history itself"
        )
    return problems


def evaluate_round(base: str, draft: str, delta: str, changes_raw: str) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """The PRD stage's whole deterministic verdict, pure: ({check id: problems}, content). A check
    with an empty list passed; an unreadable draft or change list stops after the first check. On a
    pass, `content` holds the derived changes (`PC-n` ids, kind, basis, prior/new text), the
    round's summary, and a unified diff of the requirements text."""
    problems: dict[str, list[str]] = {PRD_DRAFT_FILES.id: []}
    if not draft.strip():
        problems[PRD_DRAFT_FILES.id].append(f"{DRAFT_PATH} is empty -- edit the seeded PRD in place")
        return problems, {}
    try:
        declared = PrdChangesFile.model_validate_json(changes_raw)
    except ValidationError as exc:
        problems[PRD_DRAFT_FILES.id].append(
            f"{CHANGES_PATH} is not a valid change list ({{summary, changes: [{{prior_lines, basis, delta_quote, "
            f"note}}]}}, with a non-blank summary): {exc.errors()[0].get('msg', exc)}"
        )
        return problems, {}

    problems[PRD_STRUCTURE.id] = check_structure(draft)

    base_lines = _lines(strip_history(base))
    draft_lines = _lines(draft)
    requirement = _requirement_lines(base_lines)
    delta_key = _normalize(delta)
    grounded: list[str] = []
    declared_by: dict[int, int] = {}
    for number, change in enumerate(declared.changes, start=1):
        for line_no in change.prior_lines:
            if line_no in declared_by and declared_by[line_no] != number:
                grounded.append(
                    f"change {number}: line {line_no} of {BASE_PATH} is declared in more than one change "
                    f"(also change {declared_by[line_no]}) -- each changed line belongs to exactly one change"
                )
            declared_by.setdefault(line_no, number)
        if not change.prior_lines:
            grounded.append(f"change {number} in {CHANGES_PATH} names no prior_lines -- name the {BASE_PATH} line(s) it changes")
        for line_no in change.prior_lines:
            if not 1 <= line_no <= len(base_lines):
                grounded.append(
                    f"change {number}: line {line_no} is not a requirement line of {BASE_PATH} "
                    f"(it has {len(base_lines)} lines before Revision History)"
                )
            elif line_no - 1 not in requirement:
                grounded.append(
                    f"change {number}: line {line_no} of {BASE_PATH} ({base_lines[line_no - 1].strip()!r}) is not a "
                    f"requirement line -- only lines under {', '.join(sorted(TRACKED_SECTIONS))} are declared"
                )
        quote = _normalize(change.delta_quote)
        if not quote or quote not in delta_key:
            grounded.append(
                f"change {number}: delta_quote {change.delta_quote!r} does not appear in this round's requirements "
                f"text ({DELTA_PATH}) -- quote the exact words that drive this change"
            )
    problems[PRD_CHANGES_GROUNDED.id] = grounded

    changed, added = _changed_lines(base_lines, draft_lines)
    declared_lines = {n for c in declared.changes for n in c.prior_lines if n - 1 in requirement}
    problems[PRD_CHANGES_DECLARED.id] = [
        f"line {n} of {BASE_PATH} ({base_lines[n - 1].strip()!r}) was {changed[n][0]} in {DRAFT_PATH}, but no "
        f"change in {CHANGES_PATH} declares it -- declare it (quoting the words of this round's requirements "
        "that drive it, explicit or implied), or restore the line exactly"
        for n in sorted(set(changed) - declared_lines)
    ] + [
        f"{CHANGES_PATH} declares line {n} of {BASE_PATH} ({base_lines[n - 1].strip()!r}), but {DRAFT_PATH} leaves "
        "it unchanged -- drop that declaration, or make the change"
        for n in sorted(declared_lines - set(changed))
    ]

    derived = []
    for number, change in enumerate(declared.changes, start=1):
        lines_here = [n for n in sorted(change.prior_lines) if n in changed]
        derived.append({
            # Keyed to the round's base line (fixed for the whole round), so a re-merge after a
            # correction keeps the same id for the same requirement -- a stale citation can never
            # land on a different change.
            "id": f"PC-L{min(change.prior_lines) if change.prior_lines else 0}",
            "kind": "modified" if any(changed[n][0] == "modified" for n in lines_here) else "removed",
            "basis": change.basis,
            "prior_lines": sorted(change.prior_lines),
            "prior_text": " / ".join(base_lines[n - 1].strip() for n in sorted(change.prior_lines) if 1 <= n <= len(base_lines)),
            "new_text": " / ".join(changed[n][1] for n in lines_here if changed[n][1]),
            "delta_quote": change.delta_quote,
            "note": change.note,
        })
    diff = "\n".join(difflib.unified_diff(
        base_lines, draft_lines, fromfile="previous PRD", tofile="this round", lineterm="", n=1,
    ))
    return problems, {"summary": declared.summary, "changes": derived, "diff": diff, "added": added}


def _plain(text: str) -> str:
    return " / ".join(_LIST_MARKER.sub("", part) for part in text.split(" / "))


def render_final_prd(draft: str, base: str, summary: str, changes: list[dict[str, Any]], date: str) -> str:
    """The approved PRD: the draft plus Revision History -- the base's entries byte-for-byte, then one
    entry for this round with one sub-bullet per declared change, implied ones naming the words that
    implied them. Rendered here, never by the model, so history is append-only by construction.
    Same inputs, same output (it can re-run on a resume). Pure."""
    entry = [f"- {date}: {summary.strip()}"]
    for change in changes:
        label = "Removed" if change["kind"] == "removed" else "Modified"
        if change.get("basis") == "implied":
            label += f' (implied by "{change["delta_quote"]}")'
        text = _plain(change["prior_text"])
        if change["kind"] == "modified" and change.get("new_text"):
            text += f" → {_plain(change['new_text'])}"
        entry.append(f"  - {label}: {text}")
    return f"{draft.rstrip()}\n\n{HISTORY_HEADING}\n" + "\n".join(history_bullets(base) + entry) + "\n"


async def hydrate_prd_round(thread_id: str, state: "GraphState", provider: SandboxProvider) -> dict[str, Any]:
    """StageSpec.draft_prompt_context_from_repo_file for requirements-prd: seeds the round's scratch
    files. A round lasts until a Specification is approved: a new submission starts a new round only
    when the approved specification has changed since this round was seeded (so a clarification
    answer, a new submission while the Specification is still unapproved, stays in the same round
    and keeps its PC-n changes). A new round snapshots the live PRD as its base; within a round,
    different requirements text (a clarification answer, or a correction at the Specification or
    Plan gate) re-seeds the draft from the round's own base -- never from the live PRD, which may
    already hold this round's first merge. The same text again is a later lap: the model's edits
    stay. Returns `prd_first_round` for the prompt."""
    message_id = str(state.get("consumed_message_id") or "")
    delta = (state.get("raw_requirements_text") or "").strip()
    approved = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH) or ""
    approved_sha = hashlib.sha256(approved.encode("utf-8")).hexdigest()
    seed = _load_seed(await repo_files.read_repo_file(provider, thread_id, SEED_PATH))
    same_round = "base" in seed and (seed.get("message_id") == message_id or seed.get("approved_sha") == approved_sha)
    if same_round:
        base = seed["base"]
        if seed.get("delta") == delta:
            return {"prd_first_round": not base.strip()}
    else:
        base = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.REQUIREMENTS_PRD_PATH) or ""
    await repo_files.write_repo_file(provider, thread_id, BASE_PATH, base)
    await repo_files.write_repo_file(provider, thread_id, DRAFT_PATH, strip_history(base) or SKELETON)
    await repo_files.write_repo_file(provider, thread_id, CHANGES_PATH, json.dumps({"summary": "", "changes": []}))
    await repo_files.write_repo_file(provider, thread_id, DELTA_PATH, delta)
    await repo_files.write_repo_file(provider, thread_id, SEED_PATH, json.dumps({
        "message_id": message_id, "approved_sha": approved_sha, "base": base, "delta": delta,
    }))
    return {"prd_first_round": not base.strip()}


def _load_seed(raw: str | None) -> dict[str, Any]:
    try:
        seed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return seed if isinstance(seed, dict) else {}


async def verify_requirements_prd(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None,
    provider: SandboxProvider, chat_provider: str, lap: int = 0, audit_ran_this_lap: bool = True,
    *, log: CheckLog | None = None,
) -> "VerificationResult":
    """StageSpec.deterministic_verify for requirements-prd: reads the round's files, runs
    evaluate_round, and on a pass replaces `content_dict` (the draft's metadata response, same
    object identity -- see the specification verify) with the round's content, including the final
    rendered PRD the post-approve hook writes."""
    from .graph import VerificationResult

    log = log or CheckLog("requirements-prd_verify", VERIFY_CHECKS)

    async def read(path: str) -> str:
        return await repo_files.read_repo_file(provider, thread_id, path) or ""

    # The seed's copies are the trusted base and requirements text; base.md/delta.md are only the
    # model's reading copies, put back here if an edit landed on them by mistake.
    seed = _load_seed(await repo_files.read_repo_file(provider, thread_id, SEED_PATH))
    if "base" in seed:
        base, delta = seed["base"], seed.get("delta", "")
        for path, trusted in ((BASE_PATH, base), (DELTA_PATH, delta)):
            if await read(path) != trusted:
                await repo_files.write_repo_file(provider, thread_id, path, trusted)
    else:
        base, delta = await read(BASE_PATH), await read(DELTA_PATH)
    draft = await read(DRAFT_PATH)
    problems, content = evaluate_round(base, draft, delta, await read(CHANGES_PATH))
    for check in VERIFY_CHECKS:
        if check.id in problems:
            if problems[check.id]:
                log.failed(check, "\n".join(problems[check.id]))
            else:
                log.passed(check)
    reasons = [p for check_problems in problems.values() for p in check_problems]
    checks = [r.to_dict() for r in log.results()]
    if reasons:
        return VerificationResult(False, "\n".join(f"- {r}" for r in reasons), {"reasons": reasons}, checks=checks)
    round_date = datetime.now(timezone.utc).date().isoformat()
    content["round_date"] = round_date
    content["prd_markdown"] = render_final_prd(draft, base, content["summary"], content["changes"], round_date)
    content_dict.clear()
    content_dict.update(content)
    return VerificationResult(
        True, f"PRD merge verified: {len(content['changes'])} declared change(s).",
        {"change_count": len(content["changes"])}, checks=checks,
    )


async def write_approved_prd(
    thread_id: str, content: dict[str, Any], state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_approve_hook: writes the approved PRD and this round's change record, then
    commits. Idempotent -- everything comes from the approved content, so a re-fire on a resume
    rewrites identical files."""
    await repo_files.write_repo_file(provider, thread_id, workflow_persistence.REQUIREMENTS_PRD_PATH, content["prd_markdown"])
    record = {
        "submission_id": state.get("consumed_message_id"), "round_date": content.get("round_date"),
        "summary": content.get("summary", ""), "changes": content.get("changes") or [],
        "added": content.get("added") or [], "diff": content.get("diff", ""),
    }
    await repo_files.write_repo_file(
        provider, thread_id, workflow_persistence.REQUIREMENTS_PRD_CHANGES_PATH, json.dumps(record, indent=2) + "\n"
    )
    await git_ops.commit_paths(
        provider, thread_id,
        [workflow_persistence.REQUIREMENTS_PRD_PATH, workflow_persistence.REQUIREMENTS_PRD_CHANGES_PATH],
        "ai-dev-workflow: requirements PRD merged",
    )


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.requirements_prd`
    """Self-check: the pure merge checks, rendering, and the round-seeding hook (fake file store)."""
    import asyncio

    from . import repo_files, workflow_persistence

    base = (
        "# Notes\n\n"
        "## Overview / Problem Statement\nA notes app.\n\n"
        "## Goals & Success Metrics\nWriters keep notes.\n\n"
        "## Users / Personas\n- Writer: keeps notes.\n\n"
        "## Functional Requirements\n"
        "- Users can create a note.\n"
        "- Users can delete a note.\n"
        "- Notes list newest first.\n\n"
        "## Non-Functional Requirements\n- Pages load in under 1s.\n\n"
        "## Out of Scope\n- Sharing.\n\n"
        "## Open Questions & Assumptions\n- None.\n\n"
        "## Revision History\n"
        "- 2026-09-01: First version.\n"
    )
    lines = base.splitlines()
    create_line = lines.index("- Users can create a note.") + 1
    delete_line = lines.index("- Users can delete a note.") + 1
    newest_line = lines.index("- Notes list newest first.") + 1
    overview_line = lines.index("A notes app.") + 1
    delta = "Notes are permanent once saved. Notes are capped at 500 characters."

    body = strip_history(base)
    assert "## Revision History" not in body and body.startswith("# Notes")
    assert body.splitlines() == lines[: len(body.splitlines())], "line numbers stay aligned with base.md"
    assert history_bullets(base) == ["- 2026-09-01: First version."]
    assert history_bullets(body) == []

    draft = (
        body.replace("- Users can create a note.\n", "- Users can create a note of up to 500 characters.\n")
        .replace("- Users can delete a note.\n", "")
        .replace("- Notes list newest first.\n", "- Notes list newest first.\n- Saved notes are permanent.\n")
    )

    def changes_doc(*changes: dict, summary: str = "Notes are now permanent and capped.") -> str:
        return json.dumps({"summary": summary, "changes": list(changes)})

    removal = {"prior_lines": [delete_line], "basis": "implied", "delta_quote": "Notes are permanent once saved", "note": ""}
    cap = {"prior_lines": [create_line], "basis": "explicit", "delta_quote": "capped at 500 characters", "note": ""}

    # --- a correct round: every changed line declared, every declaration real ---
    problems, content = evaluate_round(base, draft, delta, changes_doc(removal, cap))
    assert all(not p for p in problems.values()), problems
    assert list(problems) == [c.id for c in VERIFY_CHECKS], "every check evaluated, in order"
    pc1, pc2 = content["changes"]
    # Ids are keyed to the round's base line, so a re-merge in the same round keeps them stable.
    assert pc1["id"] == f"PC-L{delete_line}" and pc1["kind"] == "removed" and pc1["basis"] == "implied"
    assert pc1["prior_text"] == "- Users can delete a note." and pc1["new_text"] == ""
    assert pc2["id"] == f"PC-L{create_line}" and pc2["kind"] == "modified" and "500 characters" in pc2["new_text"]
    assert content["summary"] == "Notes are now permanent and capped."
    assert "-- Users can delete a note." in content["diff"], content["diff"]
    assert content["added"] == ["- Saved notes are permanent."], "requirement lines this round added"

    # --- silent drop: the deletion was made but never declared ---
    problems, _ = evaluate_round(base, draft, delta, changes_doc(cap))
    assert problems["prd.changes_declared"] and "Users can delete a note" in problems["prd.changes_declared"][0]

    # --- phantom: a declared line that didn't change ---
    phantom = {"prior_lines": [newest_line], "basis": "explicit", "delta_quote": "Notes are permanent", "note": ""}
    problems, _ = evaluate_round(base, draft, delta, changes_doc(removal, cap, phantom))
    assert any(f"line {newest_line}" in p for p in problems["prd.changes_declared"]), problems

    # --- edits that change no requirement are never flagged (each would burn a verify lap) ---
    def unflagged(edited: str, expect_added: list[str] | None = None, against: str = base) -> None:
        found, found_content = evaluate_round(against, edited, delta, changes_doc())
        assert not found["prd.changes_declared"], (found["prd.changes_declared"], edited)
        if expect_added is not None:
            assert found_content["added"] == expect_added, found_content["added"]

    unflagged(body.replace("- Users can create a note.", "* Users can create a note."))  # list marker
    unflagged(body.replace(
        "- Users can create a note.\n- Users can delete a note.\n",
        "- Users can delete a note.\n- Users can create a note.\n",
    ))  # reordered
    unflagged(body.replace("- Pages load in under 1s.", "-   Pages  load in under 1s."))  # whitespace
    unflagged(body.replace("A notes app.", "A small notes app."))  # an untracked section
    numbered_base = base.replace(
        "- Users can create a note.\n- Users can delete a note.\n- Notes list newest first.\n",
        "1. Users can create a note.\n2. Users can delete a note.\n3. Notes list newest first.\n",
    )
    numbered_draft = strip_history(numbered_base).replace(
        "2. Users can delete a note.\n3. Notes list newest first.\n", "2. Notes list newest first.\n",
    )
    numbered_delete = numbered_base.splitlines().index("2. Users can delete a note.") + 1
    found, _ = evaluate_round(numbered_base, numbered_draft, delta, changes_doc())
    assert len(found["prd.changes_declared"]) == 1 and f"line {numbered_delete}" in found["prd.changes_declared"][0], (
        "renumbering after a deletion flags only the deleted line"
    )
    placeholder_base = base.replace("- Pages load in under 1s.", "None.")
    unflagged(
        strip_history(placeholder_base).replace("None.", "- Notes save within 200ms.", 1),
        expect_added=["- Notes save within 200ms."], against=placeholder_base,
    )  # the "None." placeholder is not a requirement

    # --- ungrounded declarations ---
    made_up = {**removal, "delta_quote": "users asked to remove deletion"}
    problems, _ = evaluate_round(base, draft, delta, changes_doc(made_up, cap))
    assert problems["prd.changes_grounded"] and "delta_quote" in problems["prd.changes_grounded"][0]
    curly = "Notes are permanent once saved – they’re never deleted."
    problems, _ = evaluate_round(
        base, draft, curly, changes_doc({**removal, "delta_quote": "they're never deleted"}, {**cap, "delta_quote": "Notes"}),
    )
    assert not problems["prd.changes_grounded"], ("typographic quotes and dashes still match", problems)
    out_of_range = {**removal, "prior_lines": [9999]}
    problems, _ = evaluate_round(base, draft, delta, changes_doc(out_of_range, cap))
    assert any("9999" in p for p in problems["prd.changes_grounded"])
    untracked = {**removal, "prior_lines": [overview_line]}
    problems, _ = evaluate_round(base, draft, delta, changes_doc(untracked, cap))
    assert any(f"line {overview_line}" in p for p in problems["prd.changes_grounded"])
    twice = {**cap, "prior_lines": [create_line, delete_line]}
    problems, _ = evaluate_round(base, draft, delta, changes_doc(removal, twice))
    assert any(f"line {delete_line}" in p and "more than one" in p for p in problems["prd.changes_grounded"])
    problems, _ = evaluate_round(base, draft, delta, changes_doc(removal, cap, summary="  "))
    assert problems["prd.draft_files"], "a blank summary is a malformed changes file"

    # --- line numbers match the Read tool's: only a newline ends a line ---
    sep_base = base.replace("- Users can create a note.", "- Users can create a note (any length).")
    sep_lines = sep_base.split("\n")
    assert strip_history(sep_base).split("\n")[sep_lines.index("- Users can delete a note.")] == "- Users can delete a note."

    # --- structure ---
    problems, _ = evaluate_round(base, draft.replace("## Out of Scope\n", ""), delta, changes_doc(removal, cap))
    assert any("Out of Scope" in p for p in problems["prd.structure"])
    problems, _ = evaluate_round(base, draft + "\n## Revision History\n- x\n", delta, changes_doc(removal, cap))
    assert any("Revision History" in p for p in problems["prd.structure"])

    # --- unreadable files stop at the first row ---
    problems, content = evaluate_round(base, draft, delta, "not json")
    assert list(problems) == ["prd.draft_files"] and problems["prd.draft_files"] and content == {}
    problems, _ = evaluate_round(base, "   ", delta, changes_doc())
    assert list(problems) == ["prd.draft_files"]

    # --- a project's first round: no base, nothing to declare ---
    first_draft = SKELETON.replace("# <Product name>", "# Notes").replace(
        "## Functional Requirements\n", "## Functional Requirements\n- Users can create a note.\n"
    )
    problems, content = evaluate_round("", first_draft, delta, changes_doc())
    assert all(not p for p in problems.values()), problems
    problems, _ = evaluate_round("", first_draft, delta, changes_doc(removal))
    assert problems["prd.changes_grounded"] or problems["prd.changes_declared"], "nothing exists to change yet"

    # --- render: history kept byte-for-byte, one new bullet, conflicts called out ---
    _, content = evaluate_round(base, draft, delta, changes_doc(removal, cap))
    final = render_final_prd(draft, base, content["summary"], content["changes"], "2026-10-02")
    assert final == render_final_prd(draft, base, content["summary"], content["changes"], "2026-10-02"), "idempotent"
    assert final.startswith(draft.rstrip()) and final.endswith("\n")
    history = history_bullets(final)
    assert history[0] == "- 2026-09-01: First version.", history
    assert history[1] == "- 2026-10-02: Notes are now permanent and capped.", history
    assert '  - Removed (implied by "Notes are permanent once saved"): Users can delete a note.' in history, history
    assert any(h.startswith("  - Modified: Users can create a note. → ") for h in history), history
    assert strip_history(final) == draft.rstrip() + "\n", "re-stripping the final PRD gives back the draft"
    first_final = render_final_prd(first_draft, "", "First version.", [], "2026-10-02")
    assert history_bullets(first_final) == ["- 2026-10-02: First version."]

    # --- seeding: a round lasts until a Specification is approved ---
    store: dict[str, str] = {workflow_persistence.REQUIREMENTS_PRD_PATH: base}

    async def _read(_p, _t, path):  # noqa: ANN001, ANN202
        return store.get(path)

    async def _write(_p, _t, path, content):  # noqa: ANN001, ANN202
        store[path] = content

    real = (repo_files.read_repo_file, repo_files.write_repo_file)
    repo_files.read_repo_file, repo_files.write_repo_file = _read, _write
    try:
        round1 = {"consumed_message_id": "m1", "raw_requirements_text": delta}
        ctx = asyncio.run(hydrate_prd_round("t", round1, None))  # type: ignore[arg-type]
        assert ctx == {"prd_first_round": False}
        assert store[BASE_PATH] == base and store[DRAFT_PATH] == body and store[DELTA_PATH] == delta
        assert json.loads(store[CHANGES_PATH]) == {"summary": "", "changes": []}
        seed = json.loads(store[SEED_PATH])
        assert seed["base"] == base and seed["delta"] == delta and seed["message_id"] == "m1"

        store[DRAFT_PATH] = draft  # the model's edits this lap
        asyncio.run(hydrate_prd_round("t", round1, None))  # type: ignore[arg-type]
        assert store[DRAFT_PATH] == draft, "a later lap of the same round keeps the model's edits"

        # A correction at the spec or plan gate (same submission, different text): the live PRD
        # already holds this round's first merge -- re-merge from the round's own base.
        store[workflow_persistence.REQUIREMENTS_PRD_PATH] = "# already merged once\n"
        corrected = {**round1, "raw_requirements_text": "Notes are permanent once saved."}
        asyncio.run(hydrate_prd_round("t", corrected, None))  # type: ignore[arg-type]
        assert store[DRAFT_PATH] == body and store[BASE_PATH] == base
        assert store[DELTA_PATH] == "Notes are permanent once saved."

        # A clarification answer is a NEW submission, but no Specification was approved since the
        # round began: still the same round, re-merged from its base -- its PC-n changes survive.
        answered = {"consumed_message_id": "m2", "raw_requirements_text": delta + " Notes are plain text."}
        asyncio.run(hydrate_prd_round("t", answered, None))  # type: ignore[arg-type]
        assert store[BASE_PATH] == base and store[DRAFT_PATH] == body and json.loads(store[SEED_PATH])["base"] == base

        # A Specification approved since: the round is over, the next one starts from the live PRD.
        store[workflow_persistence.SPECIFICATION_APPROVED_PATH] = '{"title": "Notes"}'
        next_ticket = {"consumed_message_id": "m3", "raw_requirements_text": "Notes can be tagged."}
        asyncio.run(hydrate_prd_round("t", next_ticket, None))  # type: ignore[arg-type]
        assert store[BASE_PATH] == "# already merged once\n", "a new round snapshots the live PRD"

        # The model edits the read-only twin by mistake: verify judges against the seed's copy and
        # puts base.md back.
        store.clear()
        store[workflow_persistence.REQUIREMENTS_PRD_PATH] = base
        asyncio.run(hydrate_prd_round("t", round1, None))  # type: ignore[arg-type]
        store.update({DRAFT_PATH: draft, CHANGES_PATH: changes_doc(cap), BASE_PATH: draft, DELTA_PATH: "tampered"})
        tampered_dict: dict[str, Any] = {"readiness": True}
        tampered = asyncio.run(verify_requirements_prd("t", tampered_dict, "r", None, None, "claude"))  # type: ignore[arg-type]
        assert not tampered.passed and "Users can delete a note" in tampered.feedback, "the undeclared drop is still caught"
        assert store[BASE_PATH] == base and store[DELTA_PATH] == delta, "the read-only twins are restored"

        store.clear()
        first = asyncio.run(hydrate_prd_round("t", {"consumed_message_id": "m9", "raw_requirements_text": delta}, None))  # type: ignore[arg-type]
        assert first == {"prd_first_round": True} and store[DRAFT_PATH] == SKELETON and store[BASE_PATH] == ""

        # --- verify: rows per check; a pass replaces the draft's metadata with the round's content ---
        store.clear()
        store[workflow_persistence.REQUIREMENTS_PRD_PATH] = base
        asyncio.run(hydrate_prd_round("t", round1, None))  # type: ignore[arg-type]
        store.update({DRAFT_PATH: draft, CHANGES_PATH: changes_doc(removal, cap)})
        content_dict: dict[str, Any] = {"readiness": True, "summary": "turn summary"}
        verdict = asyncio.run(verify_requirements_prd("t", content_dict, "r", None, None, "claude"))  # type: ignore[arg-type]
        assert verdict.passed and [r["status"] for r in verdict.checks] == ["passed"] * 4, verdict.checks
        expected_ids = [f"PC-L{delete_line}", f"PC-L{create_line}"]
        assert "readiness" not in content_dict and [c["id"] for c in content_dict["changes"]] == expected_ids
        assert history_bullets(content_dict["prd_markdown"])[1].endswith("Notes are now permanent and capped.")

        store[CHANGES_PATH] = changes_doc(cap)  # the deletion made but not declared
        failed_dict: dict[str, Any] = {"readiness": True}
        failed = asyncio.run(verify_requirements_prd("t", failed_dict, "r", None, None, "claude"))  # type: ignore[arg-type]
        rows = {r["id"]: r["status"] for r in failed.checks}
        assert not failed.passed and rows["prd.changes_declared"] == "failed" and rows["prd.structure"] == "passed"
        assert "Users can delete a note" in failed.feedback and failed_dict == {"readiness": True}, "untouched on failure"

        # --- post-approve: writes the approved PRD and the round's change record ---
        committed: list[list[str]] = []

        async def _commit(_p, _t, paths, _message):  # noqa: ANN001, ANN202
            committed.append(paths)

        real_commit = git_ops.commit_paths
        git_ops.commit_paths = _commit  # type: ignore[assignment]
        try:
            asyncio.run(write_approved_prd("t", content_dict, {"consumed_message_id": "m1"}, None))  # type: ignore[arg-type]
        finally:
            git_ops.commit_paths = real_commit  # type: ignore[assignment]
        assert store[workflow_persistence.REQUIREMENTS_PRD_PATH] == content_dict["prd_markdown"]
        record = json.loads(store[workflow_persistence.REQUIREMENTS_PRD_CHANGES_PATH])
        assert record["submission_id"] == "m1" and [c["id"] for c in record["changes"]] == expected_ids
        assert record["added"] == ["- Saved notes are permanent."]
        assert committed == [[workflow_persistence.REQUIREMENTS_PRD_PATH, workflow_persistence.REQUIREMENTS_PRD_CHANGES_PATH]]
    finally:
        repo_files.read_repo_file, repo_files.write_repo_file = real

    print("requirements_prd self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
