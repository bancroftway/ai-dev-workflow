"""Pure, dependency-free (stdlib `re`/`json` only) specification-ledger sync checks -- extracted
from `spec_ledger.py`'s `sync_ledger` (2026-09-29, Task 11) specifically so the sandbox's own
same-turn Stop hook (`sandbox-image/hooks/check-ledger-sync-stop.mjs`) can run the REAL checks by
shelling out to `python3` on this ONE file, instead of a hand-ported JavaScript reimplementation
drifting from it -- the exact drift risk `narrative_format_checks.py` (Task 8) and
`wireframe_linkage_checks.py` (Task 9) were each extracted to close, applied here to `sync_ledger`'s
own citation/retirement/dedup rules -- graph.py's own `SPECIFICATION_HARD_RULES` comment calls the
13 core existing_us_id/existing_ac_id/retired_us_ids/retired_ac_ids branches this gate's "dominant
rejection mode" (13 of 17 hard rules) -- and, until now, every one of them had zero Stop-hook
equivalent.

Why a subprocess, not a straight import: same reasoning as narrative_format_checks.py's own header
-- `spec_ledger.py` (and everything upstream of it -- `graph.py`, `sandbox/provider.py`, this
pipeline's DB drivers) lives entirely outside the sandbox and is never copied into the image; even
if it were, importing that module pulls in a dependency graph with no business running inside a
per-turn Stop hook.

`spec_ledger.py`/`graph.py` import every name below UNCHANGED (this is a pure code-move, not a
fork); `spec_ledger.py`'s own `_demo()` self-check (which still exercises `sync_ledger` end-to-end)
is the proof this extraction changed no behavior.

Five things live here, matching this task's own brief:
  1. The citation/retirement validity checks `sync_ledger` calls inline for each of
     existing_us_id/existing_ac_id (unknown/wrong-parent/retired/renumbering),
     retired_us_ids/retired_ac_ids (unknown/wrong-kind/revise-and-retire-contradiction), and
     bug_affected_ac_ids (unknown/wrong-kind/not-live/reopen-and-retire-contradiction) --
     `check_existing_us_id_citation`/`check_existing_ac_id_citation`/`check_retired_us_id`/
     `check_retired_ac_id`/`check_bug_affected_ac_id` below. The mutation these guard (flipping
     ledger entry status, resolving a story/AC's real id) stays in spec_ledger.py itself -- the
     authoritative, stateful ledger writer this module has no business duplicating (same division
     of labor as wireframe_linkage_checks.check_retired_step_ids's own docstring).
  2. `find_duplicate_by_text` -- the exact-text citation-drop dedup check, moved byte-for-byte from
     spec_ledger.py's own (private) `_find_duplicate_by_text`, renamed without its leading
     underscore now that it is a shared export rather than a spec_ledger.py-private helper.
     Confirmed distinct from check-citation-drop-stop.mjs's own deliberately-different mass-count
     heuristic -- NOT touched or merged with that hook; they stay intentionally separate.
  3. `check_fully_reviewed_completeness` -- the run_id-dependent completeness sweep ("every live
     entry stamped last_reviewed_run_id==run_id"). The ONE check here that needs Task 5's
     AIDW_RUN_ID env var on the hook side -- see check-ledger-sync-stop.mjs's own header for how it
     reads that (audit-turn-scoped, piggybacking on the existing AIDW_AUDIT_FULL_READ_FILE gate
     check-full-read-stop.mjs already uses for the identical "is this genuinely audit's own turn on
     the specification stage" question).
  4. `check_empty_draft` -- graph.py's `_verify_specification_ledger` empty-draft rejection.
  5. `find_open_questions` -- graph.py's open-clarifying-question gate backstop (the filter half;
     message-building stays in graph.py, which also needs the raw dicts for its own
     `report={"open_questions": [...]}` field).

Plus `check_ledger_sync_draft`, a validation-ONLY (no mutation) replica of sync_ledger's own
citation loop over a WHOLE draft, built from the SAME per-item functions in (1)/(2) above -- needed
because the Stop hook has no `sync_ledger` of its own to call into. Only this loop STRUCTURE is a
second copy; the RULES it calls can never drift, because every branch below calls the identical
per-item function spec_ledger.py's own `sync_ledger` calls (same convention as
wireframe_linkage_checks.py's own `run_citation_validity_checks`/`check_ac_id_citation` split --
that module's own header has the full reasoning for why a shared loop-driver, unlike a shared rule,
is an acceptable second copy).

CLI mode (`python3 ledger_sync_checks.py --check-hook`, stdin JSON: `{"ledger_entries": [...],
"specification": {...}, "run_id": "..." | null}`, stdout JSON: `{"empty_draft_problems": [...],
"open_questions": [...], "citation_problems": [...], "completeness_problems": [...]}`) is what the
Stop hook actually invokes -- see `_run_check_hook_cli` below for the exact contract.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Same file `spec_ledger.DRAFT_SPEC_PATH` names -- duplicated here (not imported: spec_ledger.py
# imports THIS module, so the reverse import would be circular), same "confirmed byte-identical, no
# automated cross-file guard beyond this module's own _demo()" posture check-narrative-format-stop.
# mjs's own hardcoded copy of this exact string already has. `_demo()` below adds a same-process
# drift-guard assertion against the real constant (a local import well after both modules are fully
# loaded, so it introduces no cycle -- same trick claude_chat_model.py's own _demo() uses for
# config.AUDIT_FULL_READ_FILE_BY_STAGE).
DRAFT_SPEC_PATH = ".ai-dev-workflow/spec/draft-specification.json"

# What a REAL ledger id looks like -- moved verbatim from spec_ledger.py's own module-level
# `_REAL_ID_RE` (used only by the two renumbering guards below, now living here with them). See
# spec_ledger.py's own history comment on this regex for why placeholders are deliberately exempt.
_REAL_ID_RE = re.compile(r"^US-\d+(\.\d+)?$")


def _find(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    """Private, narrower copy of spec_ledger.py's own `_find` -- not imported from there (spec_
    ledger.py imports THIS module, so the reverse import would be circular), same precedent as
    narrative_format_checks.py's own `_normalize_role_text`. Pure."""
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None


def _normalize_text(text: str) -> str:
    """Private copy of spec_ledger.py's own `_normalize_text` (same circular-import reasoning as
    `_find` above): whitespace/case-insensitive comparison key for `find_duplicate_by_text` below."""
    return " ".join(text.split()).strip().lower()


def check_existing_us_id_citation(
    existing_us_id: str, draft_id: Any, entries: list[dict[str, Any]]
) -> str | None:
    """existing_us_id citation validity (spec_ledger.sync_ledger, moved verbatim): must exist in the
    ledger, must not be retired (ids are never reused), and the draft's own `id` field must not
    disagree with it (an attempted renumbering) -- checked in that exact order, matching
    sync_ledger's own inline `continue` ordering, so the FIRST violated reason is always the one
    returned. None means the citation is valid. Pure."""
    entry = _find(entries, existing_us_id)
    if entry is None or entry.get("kind") != "user_story":
        return f"existing_us_id {existing_us_id!r} does not exist in the ledger"
    if entry.get("status") == "retired":
        return f"existing_us_id {existing_us_id!r} refers to a retired story -- ids are never reused"
    if draft_id is not None and _REAL_ID_RE.match(str(draft_id)) and draft_id != existing_us_id:
        return (
            f"draft's own id {draft_id!r} does not match its cited existing_us_id "
            f"{existing_us_id!r} -- do not renumber an existing story"
        )
    return None


def check_existing_ac_id_citation(
    existing_ac_id: str, draft_id: Any, resolved_us_id: str, entries: list[dict[str, Any]]
) -> str | None:
    """existing_ac_id citation validity (spec_ledger.sync_ledger, moved verbatim): must exist, must
    belong to the SAME parent story it is nested under in this draft (`resolved_us_id` -- the
    caller's already-resolved parent story id), must not be retired, and the draft's own `id` field
    must not disagree with it. Same ordering discipline as check_existing_us_id_citation above.
    Pure."""
    entry = _find(entries, existing_ac_id)
    if entry is None or entry.get("kind") != "acceptance_criterion":
        return f"existing_ac_id {existing_ac_id!r} does not exist in the ledger"
    if entry.get("parent_us_id") != resolved_us_id:
        return (
            f"existing_ac_id {existing_ac_id!r} belongs to a different user story than "
            f"{resolved_us_id!r}"
        )
    if entry.get("status") == "retired":
        return f"existing_ac_id {existing_ac_id!r} refers to a retired AC -- ids are never reused"
    if draft_id is not None and _REAL_ID_RE.match(str(draft_id)) and draft_id != existing_ac_id:
        return (
            f"draft's own id {draft_id!r} does not match its cited existing_ac_id "
            f"{existing_ac_id!r} -- do not renumber an existing AC"
        )
    return None


def check_retired_us_id(us_id: str, entries: list[dict[str, Any]], touched_ids: set[str]) -> str | None:
    """retired_us_ids citation validity (spec_ledger.sync_ledger, moved verbatim): must exist, must
    be a user_story id (not an acceptance criterion), and must not also be revised in this same
    draft (`touched_ids` -- revise or retire, never both). Deliberately does NOT check whether the
    entry is already retired -- re-naming an already-gone id is a harmless no-op in sync_ledger's own
    mutation phase, not a validation failure. Pure."""
    entry = _find(entries, us_id)
    if entry is None:
        return f"retired_us_ids cites {us_id!r}, which does not exist in the ledger"
    if entry.get("kind") != "user_story":
        return f"retired_us_ids cites {us_id!r}, which is not a user story id"
    if us_id in touched_ids:
        return (
            f"retired_us_ids cites {us_id!r}, but this draft also revises it via "
            "existing_us_id -- a story cannot be both revised and retired in the same draft"
        )
    return None


def check_retired_ac_id(ac_id: str, entries: list[dict[str, Any]], touched_ids: set[str]) -> str | None:
    """retired_ac_ids citation validity -- same shape as check_retired_us_id above, moved verbatim
    from spec_ledger.sync_ledger. Pure."""
    entry = _find(entries, ac_id)
    if entry is None:
        return f"retired_ac_ids cites {ac_id!r}, which does not exist in the ledger"
    if entry.get("kind") != "acceptance_criterion":
        return f"retired_ac_ids cites {ac_id!r}, which is not an acceptance criterion id"
    if ac_id in touched_ids:
        return (
            f"retired_ac_ids cites {ac_id!r}, but this draft also revises it via "
            "existing_ac_id -- an AC cannot be both revised and retired in the same draft"
        )
    return None


def check_bug_affected_ac_id(
    bug_ac_id: str, entries: list[dict[str, Any]], retired_ac_id_set: set[str]
) -> str | None:
    """bug_affected_ac_ids citation validity (spec_ledger.sync_ledger, moved verbatim): must exist,
    must be an acceptance_criterion id, must be LIVE (active/revised/deferred -- a retired or
    nonexistent id cannot be reopened), and must not also be named in retired_ac_ids in this same
    draft (reopening and retiring the same id is a contradiction). Pure."""
    entry = _find(entries, bug_ac_id)
    if entry is None:
        return f"bug_affected_ac_ids cites {bug_ac_id!r}, which does not exist in the ledger"
    if entry.get("kind") != "acceptance_criterion":
        return f"bug_affected_ac_ids cites {bug_ac_id!r}, which is not an acceptance criterion id"
    if entry.get("status") not in ("active", "revised", "deferred"):
        return (
            f"bug_affected_ac_ids cites {bug_ac_id!r}, which is not a live criterion "
            f"(status={entry.get('status')!r}) -- a retired/nonexistent id cannot be reopened"
        )
    if bug_ac_id in retired_ac_id_set:
        return (
            f"bug_affected_ac_ids cites {bug_ac_id!r}, but this draft also retires it via "
            "retired_ac_ids -- a criterion cannot be both reopened by a bug and removed in the same draft"
        )
    return None


def find_duplicate_by_text(
    entries: list[dict[str, Any]], kind: str, text: str, exclude_ids: set[str]
) -> dict[str, Any] | None:
    """An already-tracked, still-live entry of the same `kind` ("user_story"/"acceptance_criterion")
    whose own title/description text is an EXACT match (after `_normalize_text`) for `text` --
    evidence this "new" entry is really an already-numbered one whose citation got dropped, not
    genuinely new content.

    Moved byte-for-byte from spec_ledger.py's own (private) `_find_duplicate_by_text` -- see
    spec_ledger.sync_ledger's own docstring/history comment for the full root-cause story
    (income-investor run d2392db9: 3 of 8 specification laps spent re-discovering already-numbered
    stories the draft had re-emitted with a null citation). An EXACT text match (not fuzzy) keeps
    this zero-false-positive: two genuinely different stories essentially never share
    byte-identical title/description text, so this only fires on the real regression. Pure."""
    text_key = _normalize_text(text)
    if not text_key:
        return None
    for entry in entries:
        if (
            entry.get("kind") == kind
            and entry.get("id") not in exclude_ids
            and entry.get("status") in ("active", "revised", "deferred")
            and _normalize_text(entry.get("title" if kind == "user_story" else "description", "")) == text_key
        ):
            return entry
    return None


def duplicate_story_reason(story: dict[str, Any], dup: dict[str, Any]) -> str:
    """The exact rejection message sync_ledger emits when a 'new' story (existing_us_id: null) is
    word-for-word identical to an already-tracked one -- factored out so spec_ledger.sync_ledger and
    this module's own `check_ledger_sync_draft` (below) never maintain two copies of this message."""
    return (
        f"story {story.get('id')!r} (title {story.get('title')!r}) is word-for-word "
        f"identical to already-tracked {dup['id']!r} but cites existing_us_id: null -- "
        f"this looks like a dropped citation, not new content: cite {dup['id']!r} via "
        "existing_us_id instead (copied character-for-character), or if it genuinely is "
        "new content, reword it so it isn't identical to an existing story"
    )


def duplicate_ac_reason(ac: dict[str, Any], dup: dict[str, Any]) -> str:
    """Same as duplicate_story_reason above, for a 'new' acceptance criterion."""
    return (
        f"criterion {ac.get('id')!r} (description {ac.get('description')!r}) is "
        f"word-for-word identical to already-tracked {dup['id']!r} but cites "
        f"existing_ac_id: null -- this looks like a dropped citation, not new "
        f"content: cite {dup['id']!r} via existing_ac_id instead (copied "
        "character-for-character), or if it genuinely is new content, reword it so "
        "it isn't identical to an existing criterion"
    )


def check_fully_reviewed_completeness(entries: list[dict[str, Any]], run_id: str) -> str | None:
    """The run_id-dependent completeness sweep (file-based-editing plan, Part 3's review-depth
    safety net) -- moved verbatim from spec_ledger.sync_ledger's own `fully_reviewed` handling.

    Pure predicate ONLY: does not perform the stamping mutation itself (sync_ledger's own job,
    since it mutates `entries` in place) -- called AFTER that stamping, so a lap that just proved a
    full read already has its stamps in `entries` by the time this runs. Any still-live
    (active/revised/deferred) user_story/acceptance_criterion entry not stamped
    `last_reviewed_run_id == run_id` fails. Returns the exact rejection message, or None if complete.
    Pure."""
    not_reviewed = [
        e["id"]
        for e in entries
        if e.get("kind") in ("user_story", "acceptance_criterion")
        and e.get("status") in ("active", "revised", "deferred")
        and e.get("last_reviewed_run_id") != run_id
    ]
    if not not_reviewed:
        return None
    return (
        "the AUDIT session did not prove it read the ENTIRE draft file this lap "
        f"({len(not_reviewed)} still-live stories/criteria unconfirmed). The audit pass "
        f"must view the whole {DRAFT_SPEC_PATH} -- one full Read, or offset/limit reads "
        "that together cover every line -- before resubmitting. No content change is "
        "implied."
    )


def check_empty_draft(specification: dict[str, Any]) -> str | None:
    """graph.py's `_verify_specification_ledger` empty-draft rejection, moved verbatim: reject only
    when EVERY field a real submission could touch is empty (user_stories, retired_us_ids,
    retired_ac_ids, bug_affected_ac_ids) -- a deletion-only ticket (retire something, add nothing
    new) or a wording-unchanged bug-reopen ticket (bug_affected_ac_ids only) legitimately has no
    stories/criteria to submit. Returns the exact rejection message, or None if the draft has real
    content. Pure."""
    if (
        specification.get("user_stories")
        or specification.get("retired_us_ids")
        or specification.get("retired_ac_ids")
        or specification.get("bug_affected_ac_ids")
    ):
        return None
    return (
        f"{DRAFT_SPEC_PATH} has nothing in it -- no new/revised stories or "
        "criteria, no retirements, no bug-affected ids. This response is metadata ABOUT "
        "the specification, not the specification itself. Use your file tools to "
        "actually write this ticket's real delta, then resubmit."
    )


def find_open_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """graph.py's `_verify_specification_ledger` open-clarifying-question gate backstop, moved
    verbatim (the filter half; message-building stays in graph.py, which also needs the raw dicts
    for its own `report={"open_questions": [...]}` field). Returns every question dict whose
    status=='open' (empty = none open, i.e. the gate passes). Pure."""
    return [q for q in questions if isinstance(q, dict) and q.get("status") == "open"]


def check_ledger_sync_draft(
    entries: list[dict[str, Any]],
    draft_user_stories: list[dict[str, Any]],
    retired_ac_ids: list[str] | None = None,
    retired_us_ids: list[str] | None = None,
    bug_affected_ac_ids: list[str] | None = None,
) -> list[str]:
    """Stop-hook-ONLY, validation-only replica of spec_ledger.sync_ledger's citation/retirement/
    dedup control flow -- see this module's own header for why this loop is a second copy (the
    RULES it calls, above, are not). NOT imported by spec_ledger.py itself (which has its own,
    mutation-carrying version of this exact loop, unchanged) -- this is purely what
    check-ledger-sync-stop.mjs shells out to for a same-turn nudge; the real spec_ledger.sync_ledger
    (host-side, run at verify time) remains the sole authority.

    Mirrors sync_ledger's greenfield leniency (an empty starting ledger treats every citation as
    "new", never a bad citation) and its "excludes ids touched earlier this same draft" dedup
    discipline via a local shadow copy of `entries` that grows exactly the way sync_ledger's own
    `updated` list does: a brand-new story/AC gets a synthetic placeholder id appended (never a real
    `allocate_next_id` call -- this hook has no business minting real ledger ids, and a citation to
    a same-draft placeholder can never collide with a real id, so equality-based lookups downstream
    stay correct) purely so a LATER item in the same draft sees an EARLIER one's existence/kind/
    parent the same way sync_ledger's own single pass would. Pure."""
    reasons: list[str] = []
    shadow = [dict(e) for e in entries]
    touched_ids: set[str] = set()
    ledger_was_empty = not entries
    counter = 0

    for story in draft_user_stories:
        existing_us_id = None if ledger_was_empty else story.get("existing_us_id")
        if existing_us_id is not None:
            problem = check_existing_us_id_citation(existing_us_id, story.get("id"), shadow)
            if problem:
                reasons.append(problem)
                continue
            resolved_us_id = existing_us_id
        else:
            dup = None if ledger_was_empty else find_duplicate_by_text(
                shadow, "user_story", story.get("title", ""), touched_ids
            )
            if dup is not None:
                reasons.append(duplicate_story_reason(story, dup))
                continue
            counter += 1
            resolved_us_id = f"__new_us_{counter}__"
            shadow.append({
                "id": resolved_us_id, "kind": "user_story", "status": "active",
                "title": story.get("title", ""),
            })
        touched_ids.add(resolved_us_id)

        for ac in story.get("acceptance_criteria") or []:
            existing_ac_id = ac.get("existing_ac_id")
            if existing_ac_id is not None:
                problem = check_existing_ac_id_citation(existing_ac_id, ac.get("id"), resolved_us_id, shadow)
                if problem:
                    reasons.append(problem)
                    continue
                resolved_ac_id = existing_ac_id
            else:
                ac_dup = None if ledger_was_empty else find_duplicate_by_text(
                    shadow, "acceptance_criterion", ac.get("description", ""), touched_ids
                )
                if ac_dup is not None:
                    reasons.append(duplicate_ac_reason(ac, ac_dup))
                    continue
                counter += 1
                resolved_ac_id = f"__new_ac_{counter}__"
                shadow.append({
                    "id": resolved_ac_id, "kind": "acceptance_criterion", "parent_us_id": resolved_us_id,
                    "status": "active", "description": ac.get("description", ""),
                })
            touched_ids.add(resolved_ac_id)

    for us_id in retired_us_ids or []:
        problem = check_retired_us_id(us_id, shadow, touched_ids)
        if problem:
            reasons.append(problem)

    for ac_id in retired_ac_ids or []:
        problem = check_retired_ac_id(ac_id, shadow, touched_ids)
        if problem:
            reasons.append(problem)

    retired_ac_id_set = set(retired_ac_ids or [])
    for bug_ac_id in bug_affected_ac_ids or []:
        problem = check_bug_affected_ac_id(bug_ac_id, shadow, retired_ac_id_set)
        if problem:
            reasons.append(problem)

    return reasons


def run_ledger_sync_checks(
    ledger_entries: list[dict[str, Any]], specification: dict[str, Any], run_id: str | None
) -> dict[str, list[Any]]:
    """Everything check-ledger-sync-stop.mjs's CLI mode reports, computed once over the same
    inputs -- same "no drift" contract as wireframe_linkage_checks.run_all_checks. `run_id` is
    None whenever the hook isn't confident this is genuinely the audit's own turn on the
    specification stage (see check_fully_reviewed_completeness's own caller in the CLI below) --
    the completeness sweep is skipped entirely rather than guessed at."""
    empty_draft = check_empty_draft(specification)
    completeness_problem = (
        check_fully_reviewed_completeness(ledger_entries, run_id) if run_id else None
    )
    return {
        "empty_draft_problems": [empty_draft] if empty_draft else [],
        "open_questions": find_open_questions(specification.get("questions") or []),
        "citation_problems": check_ledger_sync_draft(
            ledger_entries,
            specification.get("user_stories") or [],
            retired_ac_ids=specification.get("retired_ac_ids") or [],
            retired_us_ids=specification.get("retired_us_ids") or [],
            bug_affected_ac_ids=specification.get("bug_affected_ac_ids") or [],
        ),
        "completeness_problems": [completeness_problem] if completeness_problem else [],
    }


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.gates.ledger_sync_checks`
    """Self-check: no sandbox, no DB -- every function here is pure."""
    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "ledger_sync_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/ledger_sync_checks.py hooks/ledger_sync_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    # Drift guard: DRAFT_SPEC_PATH must equal the real spec_ledger.py constant it duplicates (see
    # this module's own header). Local import, well after both modules are fully loaded, so this
    # cannot introduce the cycle the module-level docstring explains this file must avoid.
    from .. import spec_ledger

    assert DRAFT_SPEC_PATH == spec_ledger.DRAFT_SPEC_PATH, (
        "ledger_sync_checks.DRAFT_SPEC_PATH has drifted from spec_ledger.DRAFT_SPEC_PATH"
    )

    ledger = [
        {"id": "US-0001", "kind": "user_story", "status": "active", "title": "Sign in"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "parent_us_id": "US-0001", "status": "active",
         "description": "Shows an error on a wrong password."},
        {"id": "US-0002", "kind": "user_story", "status": "retired", "title": "Old feature"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "parent_us_id": "US-0002", "status": "retired",
         "description": "Old criterion."},
    ]

    # --- check_existing_us_id_citation ---
    assert check_existing_us_id_citation("US-0001", "US-0001", ledger) is None
    assert check_existing_us_id_citation("US-0001", None, ledger) is None, "no draft id given -- nothing to compare"
    unknown = check_existing_us_id_citation("US-9999", None, ledger)
    assert unknown and "does not exist" in unknown
    retired_us = check_existing_us_id_citation("US-0002", None, ledger)
    assert retired_us and "retired" in retired_us and "never reused" in retired_us
    renumbered = check_existing_us_id_citation("US-0001", "US-0002", ledger)
    assert renumbered and "renumber" in renumbered
    # A same-response placeholder id (schemas.py: "ignored when existing_us_id is set") must NOT be
    # treated as a renumbering attempt -- only a REAL-shaped id disagreeing counts.
    assert check_existing_us_id_citation("US-0001", "draft-story-1", ledger) is None

    # --- check_existing_ac_id_citation ---
    assert check_existing_ac_id_citation("US-0001.1", "US-0001.1", "US-0001", ledger) is None
    unknown_ac = check_existing_ac_id_citation("US-9999.9", None, "US-0001", ledger)
    assert unknown_ac and "does not exist" in unknown_ac
    wrong_parent = check_existing_ac_id_citation("US-0001.1", None, "US-0002", ledger)
    assert wrong_parent and "different user story" in wrong_parent
    retired_ac = check_existing_ac_id_citation("US-0002.1", None, "US-0002", ledger)
    assert retired_ac and "retired" in retired_ac and "never reused" in retired_ac
    renumbered_ac = check_existing_ac_id_citation("US-0001.1", "US-0009.9", "US-0001", ledger)
    assert renumbered_ac and "renumber" in renumbered_ac

    # --- check_retired_us_id / check_retired_ac_id ---
    assert check_retired_us_id("US-0001", ledger, set()) is None
    assert check_retired_us_id("US-0002", ledger, set()) is None, "already-retired is a harmless no-op, not an error"
    missing_us = check_retired_us_id("US-9999", ledger, set())
    assert missing_us and "does not exist" in missing_us
    wrong_kind_us = check_retired_us_id("US-0001.1", ledger, set())
    assert wrong_kind_us and "not a user story id" in wrong_kind_us
    contradiction_us = check_retired_us_id("US-0001", ledger, {"US-0001"})
    assert contradiction_us and "cannot be both revised and retired" in contradiction_us

    assert check_retired_ac_id("US-0001.1", ledger, set()) is None
    missing_ac = check_retired_ac_id("US-9999.9", ledger, set())
    assert missing_ac and "does not exist" in missing_ac
    wrong_kind_ac = check_retired_ac_id("US-0001", ledger, set())
    assert wrong_kind_ac and "not an acceptance criterion id" in wrong_kind_ac
    contradiction_ac = check_retired_ac_id("US-0001.1", ledger, {"US-0001.1"})
    assert contradiction_ac and "cannot be both revised and retired" in contradiction_ac

    # --- check_bug_affected_ac_id ---
    assert check_bug_affected_ac_id("US-0001.1", ledger, set()) is None
    missing_bug = check_bug_affected_ac_id("US-9999.9", ledger, set())
    assert missing_bug and "does not exist" in missing_bug
    wrong_kind_bug = check_bug_affected_ac_id("US-0001", ledger, set())
    assert wrong_kind_bug and "not an acceptance criterion id" in wrong_kind_bug
    not_live_bug = check_bug_affected_ac_id("US-0002.1", ledger, set())
    assert not_live_bug and "not a live criterion" in not_live_bug
    contradiction_bug = check_bug_affected_ac_id("US-0001.1", ledger, {"US-0001.1"})
    assert contradiction_bug and "both reopened by a bug and removed" in contradiction_bug

    # --- find_duplicate_by_text / duplicate_*_reason ---
    dup = find_duplicate_by_text(ledger, "user_story", "  sign   IN  ", set())
    assert dup is not None and dup["id"] == "US-0001", "whitespace/case-insensitive exact match"
    assert find_duplicate_by_text(ledger, "user_story", "Sign in", {"US-0001"}) is None, "excluded ids never match"
    assert find_duplicate_by_text(ledger, "user_story", "Export CSV", set()) is None, "genuinely new text never matches"
    assert find_duplicate_by_text(ledger, "user_story", "", set()) is None, "blank text never matches"
    msg = duplicate_story_reason({"id": "draft-1", "title": "Sign in"}, dup)
    assert "US-0001" in msg and "identical" in msg and "existing_us_id" in msg
    ac_dup = find_duplicate_by_text(ledger, "acceptance_criterion", "Shows an error on a wrong password.", set())
    assert ac_dup is not None and ac_dup["id"] == "US-0001.1"
    ac_msg = duplicate_ac_reason({"id": "draft-1.1", "description": "Shows an error on a wrong password."}, ac_dup)
    assert "US-0001.1" in ac_msg and "identical" in ac_msg and "existing_ac_id" in ac_msg

    # --- check_fully_reviewed_completeness ---
    assert check_fully_reviewed_completeness(ledger, "run-1") is not None, "nothing stamped yet -- must fail"
    stamped = [dict(e) for e in ledger]
    for e in stamped:
        if e.get("status") in ("active", "revised", "deferred"):
            e["last_reviewed_run_id"] = "run-1"
    assert check_fully_reviewed_completeness(stamped, "run-1") is None, "every live entry stamped -- must pass"
    assert check_fully_reviewed_completeness(stamped, "run-2") is not None, "a DIFFERENT run_id's stamp doesn't count"

    # --- check_empty_draft ---
    assert check_empty_draft({"user_stories": [{"title": "x"}]}) is None
    assert check_empty_draft({"retired_us_ids": ["US-0001"]}) is None
    assert check_empty_draft({"bug_affected_ac_ids": ["US-0001.1"]}) is None
    empty_msg = check_empty_draft({})
    assert empty_msg is not None and "nothing in it" in empty_msg

    # --- find_open_questions ---
    assert find_open_questions([]) == []
    qs = [{"id": "q-a", "status": "answered"}, {"id": "q-b", "status": "open"}, "not-a-dict"]
    open_qs = find_open_questions(qs)
    assert len(open_qs) == 1 and open_qs[0]["id"] == "q-b"

    # --- check_ledger_sync_draft (the bulk hook-only driver) ---
    assert check_ledger_sync_draft(ledger, []) == []
    assert check_ledger_sync_draft(ledger, [{"existing_us_id": "US-9999", "id": None, "acceptance_criteria": []}]) != []
    # A brand-new story with a brand-new AC citing it as parent -- must pass with no reasons, and the
    # synthetic placeholder id must never collide with a real one.
    new_story = [{
        "id": None, "existing_us_id": None, "title": "Export CSV",
        "acceptance_criteria": [{"id": None, "existing_ac_id": None, "description": "Produces a .csv file."}],
    }]
    assert check_ledger_sync_draft(ledger, new_story) == []
    # Greenfield leniency: an EMPTY starting ledger never rejects any citation.
    assert check_ledger_sync_draft([], [{"existing_us_id": "US-1", "id": None, "acceptance_criteria": []}]) == []
    # Retirement/bug-affected lists thread straight through to the per-item checks above.
    assert check_ledger_sync_draft(ledger, [], retired_us_ids=["US-9999"]) != []
    assert check_ledger_sync_draft(ledger, [], retired_ac_ids=["US-0001.1"], bug_affected_ac_ids=["US-0001.1"]) != [], (
        "reopen-and-retire contradiction must be caught even with no draft_user_stories at all"
    )

    # --- run_ledger_sync_checks / _run_check_hook_cli: same shape the --check-hook CLI emits ---
    spec = {
        "user_stories": [], "questions": [{"id": "q-1", "status": "open", "question": "What timezone?"}],
    }
    result = run_ledger_sync_checks(ledger, spec, run_id=None)
    assert result["empty_draft_problems"] != [], "no stories/retirements/bug ids at all -- empty"
    assert len(result["open_questions"]) == 1 and result["open_questions"][0]["id"] == "q-1"
    assert result["citation_problems"] == []
    assert result["completeness_problems"] == [], "run_id=None must skip the completeness sweep entirely"

    result_with_run_id = run_ledger_sync_checks(ledger, {"user_stories": []}, run_id="run-1")
    assert result_with_run_id["completeness_problems"] != [], "run_id given, nothing stamped -- must fail"
    result_stamped = run_ledger_sync_checks(stamped, {"user_stories": []}, run_id="run-1")
    assert result_stamped["completeness_problems"] == []

    print("ledger_sync_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"ledger_entries": [...], "specification": {...},
        # "run_id": "..." | null} on stdin, JSON result on stdout. Deliberately the ONLY thing this
        # branch does -- no sandbox access beyond what the hook already read and handed over.
        payload = json.loads(sys.stdin.read())
        result = run_ledger_sync_checks(
            ledger_entries=payload.get("ledger_entries") or [],
            specification=payload.get("specification") or {},
            run_id=payload.get("run_id") or None,
        )
        json.dump(result, sys.stdout)
    else:
        _demo()
