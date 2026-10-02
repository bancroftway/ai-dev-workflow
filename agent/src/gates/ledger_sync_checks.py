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

Plus `story_delta`/`check_story_decisions` (2026-10-02): every story in the last-approved
specification must carry a declared decision (unchanged/modified/retired) that matches what the
draft actually does to it -- see check_story_decisions' own docstring -- and
`check_prd_changes_addressed`: every PRD change this round (`PC-n`) is accounted for by a decision.
Live here, not in spec_ledger.py, so the gate and the Stop hook judge with the one rule.

CLI mode (`python3 ledger_sync_checks.py --check-hook`, stdin JSON: `{"ledger_entries": [...],
"specification": {...}, "run_id": "..." | null, "approved_specification": {...} | null}`, stdout
JSON: `{"empty_draft_problems": [...], "open_questions": [...], "citation_problems": [...],
"completeness_problems": [...], "decision_problems": [...], "prd_change_problems": [...]}`, with
`"prd_change_ids": [...]` also accepted on stdin) is what the Stop hook actually
invokes -- see the `if __name__ == "__main__":` block below for the exact contract.
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

# workflow_persistence.SPECIFICATION_APPROVED_PATH, duplicated for the same reason (this module may
# import nothing project-local); `_demo()` guards it against the real constant AND against
# check-ledger-sync-stop.mjs, which reads this file to hand `check_story_decisions` its baseline.
APPROVED_SPEC_PATH = ".ai-dev-workflow/03-specification.approved.json"
# spec_ledger.PRD_CHANGES_SCRATCH_PATH, duplicated likewise: this round's PRD changes ({"changes":
# [{id, ...}]}), written by the host at every specification draft start; the hook hands the ids to
# check_prd_changes_addressed. Guarded by `_demo()` the same way.
PRD_CHANGES_SCRATCH_PATH = ".ai-dev-workflow/spec/prd-changes.json"

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
    retired_ac_ids, bug_affected_ac_ids, decided story_decisions rows) -- a deletion-only ticket (retire something, add nothing
    new) or a wording-unchanged bug-reopen ticket (bug_affected_ac_ids only) legitimately has no
    stories/criteria to submit. Returns the exact rejection message, or None if the draft has real
    content. Pure."""
    if (
        specification.get("user_stories")
        or specification.get("retired_us_ids")
        or specification.get("retired_ac_ids")
        or specification.get("bug_affected_ac_ids")
        # A no-op ticket's whole answer is its story decisions ("every story unchanged") -- real
        # content that must reach the no-new-work path. Seeded, still-undecided rows don't count.
        or any(isinstance(row, dict) and row.get("decision") for row in specification.get("story_decisions") or [])
    ):
        return None
    return (
        f"{DRAFT_SPEC_PATH} has nothing in it -- no new/revised stories or "
        "criteria, no retirements, no bug-affected ids, no decided story_decisions rows. This response is metadata ABOUT "
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
            existing_ac_id = None if ledger_was_empty else ac.get("existing_ac_id")
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


STORY_DECISIONS = ("unchanged", "modified", "retired")


def story_delta(
    prior_story: dict[str, Any],
    draft_story: dict[str, Any] | None,
    retired_us_ids: set[str],
    retired_ac_ids: set[str],
    bug_affected_ac_ids: set[str],
) -> str:
    """What this ticket's draft actually does to one story of the last-APPROVED specification --
    the single rule both the gate (graph.py's specification verify) and the same-turn Stop hook
    judge a declared `story_decisions` row against, so the two can never disagree.

    `prior_story` is the story as the approved specification holds it (SPECIFICATION_APPROVED_PATH,
    the same stable baseline spec_ledger.gate_change_status uses -- never the ledger, which is
    re-saved on every passing verify lap and would make a lap-1 change look "unchanged" on lap 2).
    `draft_story` is the draft's own story citing it via `existing_us_id`, or None when the draft
    doesn't cite it.

    "retired" wins over everything (a retired story whose criteria are also listed in
    retired_ac_ids is still just retired). "modified" means the requirement really changed: the
    title or narrative text differs (raw comparison, matching sync_ledger, which writes either
    change to the ledger), the story's or a criterion's deferred flag flipped, a criterion was
    added or reworded, or one of its criteria is retired or bug-reopened (both change what has to
    be delivered). Anything else -- including re-citing a carried-over story with its text
    unchanged, or a `ui_related`-only change (metadata, never a requirement change in sync_ledger
    either) -- is "unchanged". Pure."""
    if prior_story.get("id") in retired_us_ids:
        return "retired"
    prior_acs = {ac.get("id"): ac for ac in prior_story.get("acceptance_criteria") or []}
    if any(ac_id in retired_ac_ids or ac_id in bug_affected_ac_ids for ac_id in prior_acs):
        return "modified"
    if draft_story is None:
        return "unchanged"
    if (
        draft_story.get("title", "") != prior_story.get("title", "")
        or draft_story.get("narrative", "") != prior_story.get("narrative", "")
        or bool(draft_story.get("deferred")) != bool(prior_story.get("deferred"))
    ):
        return "modified"
    for ac in draft_story.get("acceptance_criteria") or []:
        prior_ac = prior_acs.get(ac.get("existing_ac_id"))
        if prior_ac is None:
            return "modified"  # a new criterion (or one created earlier this ticket, never approved)
        if (
            ac.get("description", "") != prior_ac.get("description", "")
            or bool(ac.get("deferred")) != bool(prior_ac.get("deferred"))
        ):
            return "modified"
    return "unchanged"


def check_story_decisions(approved_specification: dict[str, Any], specification: dict[str, Any]) -> list[str]:
    """Every story in the last-approved specification must have exactly one `story_decisions` row
    in this ticket's draft -- `{us_id, decision: unchanged|modified|retired, reason}` -- and the
    declared decision must equal what the draft actually does to it (`story_delta` above).

    Exists because a new requirement can contradict or change an existing story without naming
    it ("notes are permanent once saved" retires "delete a note"; "notes are capped at 500
    characters" modifies "create a note"). Before this check, silence meant "unchanged", so
    whether such an implied change was ever noticed was pure model judgment. Forcing a decision
    per story makes the model weigh every story against the new requirements, and matching the
    label to the draft catches the second half of the failure: a model that noticed ("modified:
    notes are now capped") but never made the change.

    Stories created during this ticket aren't in the approved specification, so they need no row.
    No approved specification (a first ticket) means nothing to classify. Returns one actionable
    message per problem, empty when every row is present and consistent. Pure."""
    prior_stories = [s for s in approved_specification.get("user_stories") or [] if s.get("id")]
    if not prior_stories:
        return []
    prior_by_id = {s["id"]: s for s in prior_stories}
    draft_by_id = {
        s.get("existing_us_id"): s for s in specification.get("user_stories") or [] if s.get("existing_us_id")
    }
    retired_us = set(specification.get("retired_us_ids") or [])
    retired_ac = set(specification.get("retired_ac_ids") or [])
    bug_ac = set(specification.get("bug_affected_ac_ids") or [])

    problems: list[str] = []
    seen: set[str] = set()
    for row in specification.get("story_decisions") or []:
        us_id = row.get("us_id") if isinstance(row, dict) else None
        if us_id not in prior_by_id:
            problems.append(
                f"story_decisions names {us_id!r}, which is not a live story in the approved "
                f"specification ({APPROVED_SPEC_PATH}) -- only classify stories that already exist there"
            )
            continue
        if us_id in seen:
            problems.append(f"story_decisions lists {us_id!r} more than once -- one row per story")
            continue
        seen.add(us_id)
        declared = row.get("decision")
        if declared is None:
            problems.append(
                f"story_decisions row for {us_id!r} has no decision -- set 'unchanged', 'modified' or 'retired'"
            )
            continue
        if declared not in STORY_DECISIONS:
            problems.append(
                f"story_decisions row for {us_id!r} has decision {declared!r} -- it must be one of "
                "'unchanged', 'modified' or 'retired'"
            )
            continue
        if not str(row.get("reason") or "").strip():
            problems.append(
                f"story_decisions row for {us_id!r} has no reason -- say in one line why, weighed "
                "against this ticket's requirements"
            )
        actual = story_delta(prior_by_id[us_id], draft_by_id.get(us_id), retired_us, retired_ac, bug_ac)
        if declared == actual:
            continue
        if actual == "retired":
            problems.append(
                f"{us_id!r} is declared {declared!r}, but the draft retires it (it is in retired_us_ids) "
                "-- set the decision to 'retired', or take it out of retired_us_ids"
            )
        elif declared == "retired":
            problems.append(
                f"{us_id!r} is declared 'retired', but the draft never retires it -- add it to "
                "retired_us_ids, or change the decision"
            )
        elif declared == "modified":
            problems.append(
                f"{us_id!r} is declared 'modified', but the draft changes nothing about it -- make the "
                "change (re-emit it citing existing_us_id with the new wording or criteria, add a "
                "criterion, or retire one of its criteria), or set the decision to 'unchanged'"
            )
        else:
            problems.append(
                f"{us_id!r} is declared 'unchanged', but the draft modifies it (its wording, a "
                "criterion, a deferral, or a retired/bug-reopened criterion) -- set the decision to "
                "'modified', or undo the change"
            )

    missing = [us_id for us_id in prior_by_id if us_id not in seen]
    if missing:
        problems.append(
            f"story_decisions has no row for {', '.join(missing)} -- every story in the approved "
            "specification needs exactly one: decide whether this ticket's requirements leave it "
            "unchanged, modify it, or retire it. A new requirement that contradicts, narrows or "
            "replaces an existing story is a change even when it never names that story."
        )
    return problems


def check_prd_changes_addressed(specification: dict[str, Any], prd_change_ids: list[str]) -> list[str]:
    """Every requirement the requirements-prd stage declared removed or modified this round (its
    `PC-n` ids) must be accounted for by the Specification: cited in the `prd_change_ids` of a
    story decision that is `modified` or `retired`, or listed in `prd_changes_without_story` with
    a reason (a PRD change no story covers, e.g. a requirement the specification never had).

    Binds the PRD's text-to-text conflict detection -- a separate model call from the one writing
    the specification -- to the specification itself, so "the PRD says deletion was removed
    (implied)" can't sit beside a "Delete a note: unchanged" decision. Citing a change only from an
    `unchanged` story doesn't count: that is exactly the contradiction this check exists to catch.
    Unknown ids are rejected. Returns one actionable message per problem. Pure."""
    known = list(dict.fromkeys(prd_change_ids))
    known_set = set(known)
    problems: list[str] = []
    addressed: set[str] = set()
    cited_by_unchanged: dict[str, str] = {}
    for row in specification.get("story_decisions") or []:
        if not isinstance(row, dict):
            continue
        for change_id in row.get("prd_change_ids") or []:
            if change_id not in known_set:
                problems.append(
                    f"story_decisions row for {row.get('us_id')!r} cites {change_id!r}, which is not one of this "
                    f"round's PRD changes ({', '.join(known) or 'there are none'})"
                )
            elif row.get("decision") in ("modified", "retired"):
                addressed.add(change_id)
            else:
                cited_by_unchanged.setdefault(change_id, row.get("us_id"))
    for row in specification.get("prd_changes_without_story") or []:
        change_id = row.get("change_id") if isinstance(row, dict) else None
        if change_id not in known_set:
            problems.append(
                f"prd_changes_without_story names {change_id!r}, which is not one of this round's PRD changes "
                f"({', '.join(known) or 'there are none'})"
            )
        elif not str(row.get("reason") or "").strip():
            problems.append(f"prd_changes_without_story row for {change_id!r} has no reason -- say why no story is affected")
        else:
            addressed.add(change_id)
    for change_id in known:
        if change_id in addressed:
            continue
        if change_id in cited_by_unchanged:
            problems.append(
                f"{change_id} is cited only by {cited_by_unchanged[change_id]!r}, which is declared 'unchanged' -- "
                "the PRD says this round removed or changed that requirement, so modify or retire the story it "
                "belongs to, or move it to prd_changes_without_story with a reason if no story is affected"
            )
        else:
            problems.append(
                f"{change_id} (a requirement this round's PRD merge removed or changed) is not accounted for -- "
                "cite it in the prd_change_ids of the story decision it modifies or retires, or list it in "
                "prd_changes_without_story with a reason if no story is affected"
            )
    return problems


def run_ledger_sync_checks(
    ledger_entries: list[dict[str, Any]],
    specification: dict[str, Any],
    run_id: str | None,
    approved_specification: dict[str, Any] | None = None,
    prd_change_ids: list[str] | None = None,
) -> dict[str, list[Any]]:
    """Everything check-ledger-sync-stop.mjs's CLI mode reports, computed once over the same
    inputs -- same "no drift" contract as wireframe_linkage_checks.run_all_checks. `run_id` is
    None whenever the hook isn't confident this is genuinely the audit's own turn on the
    specification stage (see check_fully_reviewed_completeness's own caller in the CLI below) --
    the completeness sweep is skipped entirely rather than guessed at. `approved_specification` is
    None whenever the hook isn't on the real specification stage (brownfield-spec builds the
    baseline, it never classifies one) or there is no approved specification yet -- the
    story-decisions check is skipped then."""
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
        "decision_problems": (
            check_story_decisions(approved_specification, specification) if approved_specification else []
        ),
        "prd_change_problems": check_prd_changes_addressed(specification, prd_change_ids or []),
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
    # A no-op ticket's whole draft is its story_decisions ("everything unchanged") -- that is a real
    # answer that must reach the no-new-work path, not an empty draft. Only the seeded, still
    # undecided rows mean nothing was done.
    assert check_empty_draft({"story_decisions": [{"us_id": "US-0001", "decision": "unchanged", "reason": "r"}]}) is None
    assert check_empty_draft({"story_decisions": [{"us_id": "US-0001", "decision": None, "reason": ""}]}) is not None

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
    # Greenfield leniency, AC LEVEL too (task review follow-up, 2026-09-29: this branch used to read
    # `ac.get("existing_ac_id")` unguarded, diverging from sync_ledger's own explicit
    # `ac["existing_ac_id"] = None` mutation on an empty ledger -- a new story's AC citing a
    # nonexistent existing_ac_id on a truly empty ledger was wrongly REJECTED here while the real
    # sync_ledger correctly passed it). A brand-new story with an AC citing an id that doesn't exist
    # anywhere must still pass cleanly when the ledger starts empty.
    assert check_ledger_sync_draft(
        [],
        [{
            "id": None, "existing_us_id": None, "title": "Export CSV",
            "acceptance_criteria": [
                {"id": None, "existing_ac_id": "US-0001.1", "description": "Produces a .csv file."}
            ],
        }],
    ) == [], "an AC citing a nonexistent existing_ac_id on an EMPTY ledger must never be rejected"
    # Retirement/bug-affected lists thread straight through to the per-item checks above.
    assert check_ledger_sync_draft(ledger, [], retired_us_ids=["US-9999"]) != []
    assert check_ledger_sync_draft(ledger, [], retired_ac_ids=["US-0001.1"], bug_affected_ac_ids=["US-0001.1"]) != [], (
        "reopen-and-retire contradiction must be caught even with no draft_user_stories at all"
    )

    # --- run_ledger_sync_checks: same shape the --check-hook CLI emits ---
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

    # --- story_delta: what the draft actually does to one approved story ---
    approved = {"user_stories": [
        {"id": "US-0001", "title": "Create a note", "narrative": "As a user, I want notes, so that I remember.",
         "deferred": False, "acceptance_criteria": [
             {"id": "US-0001.1", "description": "Saving stores the note.", "deferred": False, "ui_related": True},
         ]},
        {"id": "US-0002", "title": "Delete a note", "narrative": "As a user, I want to delete, so that I tidy.",
         "deferred": False, "acceptance_criteria": [
             {"id": "US-0002.1", "description": "Deleting removes the note.", "deferred": False},
         ]},
        {"id": "US-0003", "title": "Archive", "narrative": "As a user, I want archive, so that later.",
         "deferred": True, "acceptance_criteria": [
             {"id": "US-0003.1", "description": "Archived notes are hidden.", "deferred": True},
         ]},
    ]}
    create, delete, archive = approved["user_stories"]

    def recite(prior: dict[str, Any], **story_overrides: Any) -> dict[str, Any]:
        """A draft story re-citing `prior` with its text copied verbatim, then overridden."""
        story = {
            "id": prior["id"], "existing_us_id": prior["id"], "title": prior["title"],
            "narrative": prior["narrative"], "deferred": prior["deferred"],
            "acceptance_criteria": [
                {"id": ac["id"], "existing_ac_id": ac["id"], "description": ac["description"],
                 "deferred": ac["deferred"], "ui_related": ac.get("ui_related", False)}
                for ac in prior["acceptance_criteria"]
            ],
        }
        story.update(story_overrides)
        return story

    none: set[str] = set()
    assert story_delta(create, None, none, none, none) == "unchanged", "not cited, nothing named -- untouched"
    assert story_delta(delete, None, {"US-0002"}, none, none) == "retired"
    assert story_delta(delete, None, {"US-0002"}, {"US-0002.1"}, none) == "retired", (
        "a retired story whose criteria are ALSO listed in retired_ac_ids is still 'retired', not 'modified'"
    )
    assert story_delta(create, recite(create), none, none, none) == "unchanged", (
        "re-citing a carried-over story with its text unchanged is not a modification"
    )
    assert story_delta(create, recite(create, title="Create or edit a note"), none, none, none) == "modified"
    assert story_delta(create, recite(create, narrative="As a user, I want notes, so that I recall."), none, none, none) == "modified", (
        "a narrative-only change is a modification -- sync_ledger writes it to the ledger"
    )
    new_ac = recite(create)
    new_ac["acceptance_criteria"].append({"id": "ac-new", "existing_ac_id": None, "description": "Max 500 chars."})
    assert story_delta(create, new_ac, none, none, none) == "modified", "a new criterion under the story"
    reworded = recite(create)
    reworded["acceptance_criteria"][0]["description"] = "Saving stores the note, up to 500 chars."
    assert story_delta(create, reworded, none, none, none) == "modified", "a reworded criterion"
    ac_deferred = recite(create)
    ac_deferred["acceptance_criteria"][0]["deferred"] = True
    assert story_delta(create, ac_deferred, none, none, none) == "modified", "a criterion's deferred flag flipped"
    ui_only = recite(create)
    ui_only["acceptance_criteria"][0]["ui_related"] = False
    assert story_delta(create, ui_only, none, none, none) == "unchanged", "ui_related is metadata, not a requirement change"
    assert story_delta(create, None, none, {"US-0001.1"}, none) == "modified", (
        "retiring one of its criteria modifies the story even when the story itself isn't cited"
    )
    assert story_delta(create, None, none, none, {"US-0001.1"}) == "modified", (
        "a bug-reopened criterion modifies the story (its delivery stamps are cleared)"
    )
    assert story_delta(archive, recite(archive), none, none, none) == "unchanged", (
        "a parked story re-cited still deferred is unchanged"
    )
    assert story_delta(archive, recite(archive, deferred=False), none, none, none) == "modified", (
        "activating a deferred story flips its deferred flag"
    )

    # --- check_story_decisions: every live approved story declared, label matches the draft ---
    def decided(*rows: tuple[str, str | None, str]) -> list[dict[str, Any]]:
        return [{"us_id": us_id, "decision": decision, "reason": reason} for us_id, decision, reason in rows]

    good = {
        "user_stories": [], "retired_us_ids": ["US-0002"], "retired_ac_ids": [], "bug_affected_ac_ids": [],
        "story_decisions": decided(
            ("US-0001", "unchanged", "Creating notes is untouched by this ticket."),
            ("US-0002", "retired", "Notes are now permanent once saved, so deletion goes."),
            ("US-0003", "unchanged", "Archive stays parked."),
        ),
    }
    assert check_story_decisions(approved, good) == []
    assert check_story_decisions({"user_stories": []}, {"story_decisions": []}) == [], (
        "no approved baseline (first ticket) -- nothing to classify"
    )
    assert check_story_decisions({}, {}) == []

    missing = check_story_decisions(approved, {**good, "story_decisions": good["story_decisions"][:2]})
    assert missing and any("US-0003" in p for p in missing), "an unclassified live story is named"
    assert check_story_decisions(approved, {**good, "story_decisions": []}), "no decisions at all fails"

    dup_rows = good["story_decisions"] + decided(("US-0001", "unchanged", "again"))
    dup_problems = check_story_decisions(approved, {**good, "story_decisions": dup_rows})
    assert dup_problems and any("US-0001" in p and "more than once" in p for p in dup_problems)

    unknown_rows = good["story_decisions"] + decided(("US-0099", "unchanged", "?"))
    unknown_problems = check_story_decisions(approved, {**good, "story_decisions": unknown_rows})
    assert unknown_problems and any("US-0099" in p for p in unknown_problems)

    null_rows = decided(("US-0001", None, "x"), ("US-0002", "retired", "y"), ("US-0003", "unchanged", "z"))
    null_problems = check_story_decisions(approved, {**good, "story_decisions": null_rows})
    assert null_problems and any("US-0001" in p and "no decision" in p for p in null_problems)

    blank_rows = decided(("US-0001", "unchanged", "   "), ("US-0002", "retired", "y"), ("US-0003", "unchanged", "z"))
    blank_problems = check_story_decisions(approved, {**good, "story_decisions": blank_rows})
    assert blank_problems and any("US-0001" in p and "reason" in p for p in blank_problems)

    # The implied-removal case this whole check exists for: the model noticed the contradiction in
    # its reason but forgot to actually retire the story -- or declared it unchanged while retiring.
    said_unchanged = decided(("US-0001", "unchanged", "a"), ("US-0002", "unchanged", "b"), ("US-0003", "unchanged", "c"))
    lie = check_story_decisions(approved, {**good, "story_decisions": said_unchanged})
    assert lie and any("US-0002" in p and "retired" in p for p in lie), "declared unchanged, draft retires it"

    forgot = {**good, "retired_us_ids": []}
    forgot_problems = check_story_decisions(approved, forgot)
    assert forgot_problems and any("US-0002" in p and "retired" in p for p in forgot_problems), (
        "declared retired, but the draft never retires it"
    )

    # The implied-modification case: declared modified, but the draft changes nothing about it.
    no_change = {**good, "story_decisions": decided(
        ("US-0001", "modified", "Notes are now capped at 500 chars."),
        ("US-0002", "retired", "y"), ("US-0003", "unchanged", "z"),
    )}
    no_change_problems = check_story_decisions(approved, no_change)
    assert no_change_problems and any("US-0001" in p and "changes nothing" in p for p in no_change_problems)
    with_change = {**no_change, "user_stories": [new_ac]}
    assert check_story_decisions(approved, with_change) == [], "the same decision with the real change passes"

    # --- run_ledger_sync_checks threads the approved spec through to decision_problems ---
    assert run_ledger_sync_checks(ledger, {"user_stories": []}, run_id=None)["decision_problems"] == [], (
        "no approved specification handed over (brownfield-spec, or no baseline yet) -- check skipped"
    )
    hook_result = run_ledger_sync_checks(
        ledger, {**good, "story_decisions": said_unchanged}, run_id=None, approved_specification=approved,
    )
    assert hook_result["decision_problems"], "the hook reports the same decision problems the gate does"

    # --- check_prd_changes_addressed: every PRD change this round is accounted for ---
    linked = {
        "story_decisions": [
            {"us_id": "US-0001", "decision": "modified", "reason": "capped", "prd_change_ids": ["PC-2"]},
            {"us_id": "US-0002", "decision": "retired", "reason": "permanent", "prd_change_ids": ["PC-1"]},
        ],
        "prd_changes_without_story": [{"change_id": "PC-3", "reason": "only reworded the overview"}],
    }
    assert check_prd_changes_addressed(linked, ["PC-1", "PC-2", "PC-3"]) == []
    assert check_prd_changes_addressed({}, []) == [], "no PRD changes this round -- nothing to address"
    unaddressed = check_prd_changes_addressed(linked, ["PC-1", "PC-2", "PC-3", "PC-4"])
    assert unaddressed and any("PC-4" in p for p in unaddressed)
    # The case this exists for: the PRD said "removed (implied)", the spec cites it on a story it
    # keeps unchanged -- that is not addressing it.
    kept = {"story_decisions": [{"us_id": "US-0002", "decision": "unchanged", "reason": "x", "prd_change_ids": ["PC-1"]}]}
    kept_problems = check_prd_changes_addressed(kept, ["PC-1"])
    assert kept_problems and any("PC-1" in p and "unchanged" in p for p in kept_problems), kept_problems
    unknown_cited = check_prd_changes_addressed(
        {"story_decisions": [{"us_id": "US-0001", "decision": "modified", "reason": "x", "prd_change_ids": ["PC-9"]}]}, [],
    )
    assert unknown_cited and any("PC-9" in p for p in unknown_cited), "citing a change that doesn't exist"
    blank = check_prd_changes_addressed({"prd_changes_without_story": [{"change_id": "PC-1", "reason": " "}]}, ["PC-1"])
    assert blank and any("PC-1" in p and "reason" in p for p in blank)
    assert run_ledger_sync_checks(ledger, {"user_stories": []}, run_id=None, prd_change_ids=["PC-1"])["prd_change_problems"], (
        "the hook reports the same linkage problems the gate does"
    )
    assert run_ledger_sync_checks(ledger, {"user_stories": []}, run_id=None)["prd_change_problems"] == []

    # Drift guard: the approved-spec path the hook reads must be the one persistence writes, and the
    # .mjs must actually use this constant's literal value.
    from .. import workflow_persistence

    assert APPROVED_SPEC_PATH == workflow_persistence.SPECIFICATION_APPROVED_PATH
    hook_js = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "check-ledger-sync-stop.mjs"
    assert APPROVED_SPEC_PATH in hook_js.read_text(encoding="utf-8"), (
        f"check-ledger-sync-stop.mjs no longer reads {APPROVED_SPEC_PATH}"
    )
    assert PRD_CHANGES_SCRATCH_PATH == spec_ledger.PRD_CHANGES_SCRATCH_PATH
    assert PRD_CHANGES_SCRATCH_PATH in hook_js.read_text(encoding="utf-8"), (
        f"check-ledger-sync-stop.mjs no longer reads {PRD_CHANGES_SCRATCH_PATH}"
    )

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
            approved_specification=payload.get("approved_specification") or None,
            prd_change_ids=payload.get("prd_change_ids") or None,
        )
        json.dump(result, sys.stdout)
    else:
        _demo()
