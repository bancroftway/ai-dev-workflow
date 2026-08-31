"""P2's stable ID registry (US-####/AC-####.#), persisted at .ai-dev-workflow/spec/ledger.json --
distinct from .ai-dev-workflow/ledger.jsonl (repo_files.py's workflow ACTION log). This one is
cumulative across the repo's entire lifetime of using this tool: ids are never reused, even after
a story is retired, and a story/AC's *meaning* stays associated with its id across every revision
so P4's test names, test-hardening's flake tickets, and metrics-report's traceability matrix can all cite it forever.

Deliberately independent of graph.py (no VerificationResult import, no SandboxProvider I/O
coupling beyond the two thin load/save helpers below) so sync_ledger's actual allocation/
validation logic -- the part that matters -- is a plain, easily-testable function of two lists in,
one result out. graph.py's own _verify_specification_ledger wraps this as a deterministic_verify.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

# What a REAL ledger id looks like. The renumbering guards below only fire when the draft's own
# `id` field is itself ledger-shaped: schemas.py documents `id` as a same-response placeholder
# that is "ignored when existing_us_id is set", so a placeholder like 'draft-story-1' alongside a
# valid citation is the DOCUMENTED contract, not an attempted renumbering. Enforcing equality for
# placeholders made every revision round fail by construction (observed live: three verify cycles
# burned on 'draft-story-1' != 'US-0001').
_REAL_ID_RE = re.compile(r"^US-\d+(\.\d+)?$")

from . import repo_files
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

# Under .ai-dev-workflow/ on purpose: the pipeline never writes outside its own directory in a
# target repo (a top-level spec/ folder appearing in someone's repo was reported as a bug). The
# spec/ subdirectory keeps this ID registry visually apart from ledger.jsonl, the action log.
LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json"
SCHEMA_VERSION = 1

EntryStatus = Literal["active", "retired", "revised", "deferred"]
EntryKind = Literal["user_story", "acceptance_criterion"]

# Per-AC execution provenance, written by exactly three pipeline-owned sites (never the model):
# apply_tracking_resets_hook (spec approval -- clears them when the requirement really changed),
# stamp_plan_links_hook (plan approval -- plan_step_ids), and metrics_nodes.metrics_compute_node
# (healthy runs only -- coded_*/tested_*/test_ids). Absent fields mean "never coded/tested";
# schema_version stays 1 because every reader tolerates missing keys.
TRACKING_FIELDS = ("plan_step_ids", "coded_run_id", "coded_at", "tested_run_id", "tested_at", "test_ids")

# Two-phase reset marker: sync_ledger (verify time, persisted BEFORE the human gate) only marks a
# genuinely-reworded AC with the run id that reworded it; the destructive TRACKING_FIELDS clear
# happens in apply_tracking_resets_hook, which fires only on spec APPROVAL. A rejected or
# abandoned draft therefore never destroys delivered-work stamps -- sync_ledger drops stale
# markers (from runs that never reached approval) on its next pass.
PENDING_RESET_FIELD = "pending_reset_run_id"


@dataclass(frozen=True)
class LedgerSyncResult:
    passed: bool
    reasons: list[str]
    updated_entries: list[dict[str, Any]]


async def load_ledger(provider: SandboxProvider, thread_id: str) -> list[dict[str, Any]]:
    raw = await repo_files.read_repo_file(provider, thread_id, LEDGER_PATH)
    if raw is None:
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return []
    entries = doc.get("entries")
    return entries if isinstance(entries, list) else []


async def save_ledger(provider: SandboxProvider, thread_id: str, entries: list[dict[str, Any]]) -> None:
    doc = {"schema_version": SCHEMA_VERSION, "entries": entries}
    await repo_files.write_repo_file(provider, thread_id, LEDGER_PATH, json.dumps(doc, indent=2) + "\n")


async def hydrate_ticket_mode_context(
    thread_id: str, _state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.draft_prompt_context_from_repo_file for the specification stage.

    Signals ticket-mode framing when LEDGER_PATH already has entries -- a second-or-later ticket
    running against a project that already has an approved baseline, as opposed to a from-scratch
    first pass. UNLIKE preflight_nodes.hydrate_tech_stack_from_repo_file (StageSpec.
    hydrate_from_repo_file's only user today), a non-None return here never short-circuits the
    stage to "approved" -- see draft_prompt_context_from_repo_file's own docstring in graph.py.
    make_draft_node merges the returned dict into a prompt-only copy of this stage's StageState so
    _build_specification_prompt can append one extra instruction segment; a real draft, a real
    (differently-configured) audit, and the human gate all still run in full either way (Global
    Constraint, docs/superpowers/plans/part-3-tickets-tasks.md: "hydrate checks decide how much a
    stage's draft has to do, never whether audit or the human gate run").

    Returns None (no extra segment -- ordinary from-scratch framing) when the ledger is empty or
    absent. Re-reads the file on every draft call rather than caching the result on GraphState --
    it's one cheap read, and unlike StageSpec.capture_baseline_commit this signal has no "must
    stay stable across this run's retry cycles" requirement to protect.
    """
    entries = await load_ledger(provider, thread_id)
    return {"ticket_mode_baseline": True} if entries else None


def own_ac_ids_from_specification(specification: dict[str, Any] | None) -> set[str]:
    """This ticket's own approved Specification's AC ids (schemas.Specification shape: {id, kind,
    ...} nested under user_stories[].acceptance_criteria[]) -- the ledger-resolved ids sync_ledger
    already wrote back onto the draft in place, so these are real US-####.# ids, not placeholders.

    Shared by hydrate_ac_to_tests_ticket_mode_context below (decides whether the ledger holds
    another ticket's ACs too) and ac_coverage_gate.check_ac_coverage (Ruling 7: scopes its own
    coverage check down to THIS ticket's ACs) -- one computation, so the two can never answer "this
    ticket's own AC ids" two different, possibly-diverging ways.
    """
    specification = specification or {}
    return {
        ac.get("id")
        for story in (specification.get("user_stories") or [])
        for ac in (story.get("acceptance_criteria") or [])
    }


async def hydrate_ac_to_tests_ticket_mode_context(
    thread_id: str, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.draft_prompt_context_from_repo_file for the ac-to-tests stage.

    hydrate_ticket_mode_context's own sibling, but a coarser "the ledger has entries" check is
    wrong here: by the time ac-to-tests drafts, THIS ticket's own specification stage has already
    run sync_ledger and populated the ledger with its own ACs, so "entries exist" is trivially true
    even on a project's very first ticket. What actually matters is whether the ledger holds any
    ACTIVE Acceptance Criterion this ticket's own approved Specification doesn't itself list --
    i.e. a genuine multi-ticket project, not a first pass.

    Returns None (no extra segment) when every active ledger AC belongs to this ticket's own
    Specification. Never short-circuits drafting -- see draft_prompt_context_from_repo_file's own
    docstring on StageSpec; ac-to-tests still writes real tests for its own ACs either way, this
    only tells it not to also chase every other ticket's."""
    entries = await load_ledger(provider, thread_id)
    ledger_ac_ids = {
        e["id"] for e in entries if e.get("kind") == "acceptance_criterion" and e.get("status") in ("active", "revised")
    }
    specification = (state.get("stages") or {}).get("specification", {}).get("approved_content") or {}
    own_ac_ids = own_ac_ids_from_specification(specification)
    return {"ticket_mode_baseline": True} if (ledger_ac_ids - own_ac_ids) else None


def _next_us_number(entries: list[dict[str, Any]]) -> int:
    numbers = [int(e["id"].split("-")[1]) for e in entries if e.get("kind") == "user_story"]
    return (max(numbers) + 1) if numbers else 1


def _next_ac_number(entries: list[dict[str, Any]], parent_us_id: str) -> int:
    numbers = [
        int(e["id"].rsplit(".", 1)[-1])
        for e in entries
        if e.get("kind") == "acceptance_criterion" and e.get("parent_us_id") == parent_us_id
    ]
    return (max(numbers) + 1) if numbers else 1


def allocate_next_id(entries: list[dict[str, Any]], kind: EntryKind, parent_us_id: str | None = None) -> str:
    """Monotonic per kind, never reused even after retirement -- derived by scanning every entry
    ever recorded (retired ones are never physically removed from the list), not a separate
    counter field, so this is correct by construction rather than by keeping two things in sync.
    """
    if kind == "user_story":
        return f"US-{_next_us_number(entries):04d}"
    if parent_us_id is None:
        raise ValueError("acceptance_criterion allocation requires parent_us_id")
    return f"{parent_us_id}.{_next_ac_number(entries, parent_us_id)}"


def _find(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None


def sync_ledger(
    entries: list[dict[str, Any]],
    draft_user_stories: list[dict[str, Any]],
    run_id: str,
    retired_ac_ids: list[str] | None = None,
    retired_us_ids: list[str] | None = None,
) -> LedgerSyncResult:
    """The deterministic core of P2's ledger-sync gate.

    Every user story/AC in `draft_user_stories` (schemas.Specification.user_stories, already
    .model_dump()'d) must either cite an existing, non-retired id it's revising via its own
    `existing_us_id`/`existing_ac_id` field, or be new (that field is None). Never trusts the
    model's own free-text `id` field as authoritative -- on success, every story/AC's `id` is
    overwritten in place with the ledger-resolved id, so what actually gets shown to the human and
    persisted as the eventual approved content always carries real, ledger-backed ids.

    Fails (passed=False) the whole sync -- not a partial commit -- on any of: a cited id that
    doesn't exist in the ledger, a cited id that belongs to a retired entry (ids are never
    reused), an AC cited under the wrong parent user story, or the draft's own `id` field
    disagreeing with what it cited as `existing_*_id` (an attempted renumbering).

    Deferred scope (user requirement 2026-08-31): a draft story/AC carrying `deferred: true`
    (schemas.UserStory/AcceptanceCriterion) is fully specified but parked -- ledger status
    "deferred", excluded from eligible_ac_ids and every live-status gate filter, NOT crossed out.
    A story-level flag defers all of its criteria (including still-live children not re-emitted in
    this draft -- same cascade rationale as retirement). Re-citing a deferred entry without the
    flag promotes it back to "revised" and stamps `activated_run_id`, which change_status reports
    as "activated". Deferral is reversible parking; retirement stays the only terminal state.

    `retired_ac_ids`/`retired_us_ids` (schemas.Specification's own fields of the same name) name
    ledger entries this draft explicitly declares no longer belong -- the ONLY way an entry's
    status ever becomes `"retired"`. There is deliberately no "anything not re-cited this round
    gets retired" fallback (Ruling 3, docs/superpowers/plans/part-3-tickets-tasks.md). That
    unconditional auto-retire step used to run here, and it was safe only by accident: the
    greenfield-leniency branch below already made it a no-op whenever the ledger started empty,
    which was the only project state this function had ever actually run against in production
    (one draft in flight per repo, ever, before multi-ticket-per-project existed). The instant a
    second ticket's Specification stage ran against a project whose ledger already held a first
    ticket's stories, that same loop retired every one of them the moment this ticket's own
    (correctly narrower) draft failed to re-cite them -- a real, confirmed data-corruption bug,
    not a hypothetical one. Explicit-only retirement fixes this for every project state (empty or
    not) with no mode/flag to get wrong: a story silently absent from this ticket's draft, because
    it belongs to unrelated work, now simply keeps whatever status it already had.

    Retirement validation is fail-closed, the same posture as every existing_us_id/existing_ac_id
    citation above: a named id that doesn't exist in the ledger, that exists but is the wrong kind
    (a `US-####` id inside `retired_ac_ids` or vice versa -- almost certainly the two fields
    swapped), or that's named here AND cited as existing_us_id/existing_ac_id in this same
    response (revise and retire are contradictory instructions) all fail the whole sync and land
    in `reasons` -- silently ignoring a malformed retirement citation would hide exactly the kind
    of model mistake this ledger exists to catch. A named id that's already `"retired"` is a
    harmless no-op (still guarded to only flip `"active"`/`"revised"` entries): re-naming an
    already-gone id is not a mistake worth failing the sync over.

    Retiring a `user_story` entry also retires its own still-`"active"`/`"revised"`
    `acceptance_criterion` children, even ones not separately named in `retired_ac_ids`:
    `agent/src/gates/ac_coverage_gate.py`'s `check_ac_coverage` filters required coverage purely
    by each AC's OWN status and never looks at its parent story's status, so a retired story with
    orphaned still-`"active"` ACs would keep demanding coverage for criteria whose story is gone.
    This is a plain structural invariant (retiring a container retires its contents), not the
    supersession-lineage machinery Ruling 3 explicitly defers.
    """
    updated = [dict(e) for e in entries]
    reasons: list[str] = []
    touched_ids: set[str] = set()
    deferred_story_ids: set[str] = set()

    # Drop reset markers left by runs that never reached spec approval (rejected/abandoned drafts)
    # -- their stamp clears must never execute. This run's own markers are re-derived below.
    for entry in updated:
        if entry.get(PENDING_RESET_FIELD) not in (None, run_id):
            entry.pop(PENDING_RESET_FIELD, None)

    # Greenfield leniency: on an EMPTY ledger there is nothing an id citation could protect, and
    # models reliably hallucinate `existing_us_id: "US-1"` on a first run (observed live: three
    # verify cycles burned re-citing ids that never existed). Treat every citation as "new" then
    # instead of deadlocking the gate. A non-empty ledger keeps the strict fail-closed behavior.
    ledger_was_empty = not entries
    for story in draft_user_stories:
        existing_us_id = None if ledger_was_empty else story.get("existing_us_id")
        if ledger_was_empty:
            story["existing_us_id"] = None
            for ac in story.get("acceptance_criteria") or []:
                ac["existing_ac_id"] = None
        if existing_us_id is not None:
            entry = _find(updated, existing_us_id)
            if entry is None or entry.get("kind") != "user_story":
                reasons.append(f"existing_us_id {existing_us_id!r} does not exist in the ledger")
                continue
            if entry.get("status") == "retired":
                reasons.append(
                    f"existing_us_id {existing_us_id!r} refers to a retired story -- ids are never reused"
                )
                continue
            if story.get("id") is not None and _REAL_ID_RE.match(str(story["id"])) and story["id"] != existing_us_id:
                reasons.append(
                    f"draft's own id {story.get('id')!r} does not match its cited existing_us_id "
                    f"{existing_us_id!r} -- do not renumber an existing story"
                )
                continue
            # Deferred scope (user requirement 2026-08-31): a story marked deferred in the draft is
            # specified but parked -- not in the work queue, not crossed out. Citing a deferred
            # entry WITHOUT the flag promotes it back to live ("activated"), the delta flow that
            # builds just that slice.
            was_deferred = entry.get("status") == "deferred"
            story_deferred = bool(story.get("deferred"))
            entry["status"] = "deferred" if story_deferred else "revised"
            # last_revised_run_id bumps ONLY on a real title change: an identical re-cite is not a
            # revision, and stamping it polluted _diff_ledger/CHANGELOG with phantom "Revised"
            # rows and would misreport change_status as "modified".
            new_title = story.get("title", entry.get("title", ""))
            if new_title != entry.get("title"):
                entry["title"] = new_title
                entry["last_revised_run_id"] = run_id
            if story_deferred != was_deferred:
                entry["last_revised_run_id"] = run_id
                if was_deferred:
                    entry["activated_run_id"] = run_id
            resolved_us_id = existing_us_id
        else:
            story_deferred = bool(story.get("deferred"))
            resolved_us_id = allocate_next_id(updated, "user_story")
            updated.append(
                {
                    "id": resolved_us_id,
                    "kind": "user_story",
                    "status": "deferred" if story_deferred else "active",
                    "title": story.get("title", ""),
                    "first_seen_run_id": run_id,
                    "last_revised_run_id": run_id,
                }
            )

        story["id"] = resolved_us_id
        touched_ids.add(resolved_us_id)
        if story_deferred:
            deferred_story_ids.add(resolved_us_id)

        for ac in story.get("acceptance_criteria") or []:
            # A deferred story defers all of its criteria; an individual AC may also defer alone.
            ac_deferred = bool(ac.get("deferred")) or story_deferred
            existing_ac_id = ac.get("existing_ac_id")
            if existing_ac_id is not None:
                ac_entry = _find(updated, existing_ac_id)
                if ac_entry is None or ac_entry.get("kind") != "acceptance_criterion":
                    reasons.append(f"existing_ac_id {existing_ac_id!r} does not exist in the ledger")
                    continue
                if ac_entry.get("parent_us_id") != resolved_us_id:
                    reasons.append(
                        f"existing_ac_id {existing_ac_id!r} belongs to a different user story than "
                        f"{resolved_us_id!r}"
                    )
                    continue
                if ac_entry.get("status") == "retired":
                    reasons.append(
                        f"existing_ac_id {existing_ac_id!r} refers to a retired AC -- ids are never reused"
                    )
                    continue
                if ac.get("id") is not None and _REAL_ID_RE.match(str(ac["id"])) and ac["id"] != existing_ac_id:
                    reasons.append(
                        f"draft's own id {ac.get('id')!r} does not match its cited existing_ac_id "
                        f"{existing_ac_id!r} -- do not renumber an existing AC"
                    )
                    continue
                ac_was_deferred = ac_entry.get("status") == "deferred"
                ac_entry["status"] = "deferred" if ac_deferred else "revised"
                if ac_deferred != ac_was_deferred:
                    ac_entry["last_revised_run_id"] = run_id
                    if ac_was_deferred:
                        ac_entry["activated_run_id"] = run_id
                new_description = ac.get("description", ac_entry.get("description", ""))
                if new_description != ac_entry.get("description"):
                    # The requirement genuinely changed: mark it for a tracking-field reset at
                    # spec APPROVAL (two-phase -- see PENDING_RESET_FIELD) so its delivered code/
                    # tests are redone, and bump last_revised only for real changes (see the
                    # matching user-story comment above).
                    ac_entry["description"] = new_description
                    ac_entry["last_revised_run_id"] = run_id
                    ac_entry[PENDING_RESET_FIELD] = run_id
                elif ac_entry.get(PENDING_RESET_FIELD) == run_id:
                    # A later verify lap reverted the wording back to what the ledger already
                    # holds -- the pending reset no longer applies.
                    ac_entry.pop(PENDING_RESET_FIELD, None)
                resolved_ac_id = existing_ac_id
            else:
                resolved_ac_id = allocate_next_id(updated, "acceptance_criterion", resolved_us_id)
                updated.append(
                    {
                        "id": resolved_ac_id,
                        "kind": "acceptance_criterion",
                        "parent_us_id": resolved_us_id,
                        "status": "deferred" if ac_deferred else "active",
                        "description": ac.get("description", ""),
                        "first_seen_run_id": run_id,
                        "last_revised_run_id": run_id,
                    }
                )

            ac["id"] = resolved_ac_id
            touched_ids.add(resolved_ac_id)

    # Deferral cascades like retirement (same structural invariant: ac_coverage_gate/eligible_ac_ids
    # filter by an AC's OWN status): a deferred story's still-live children not re-emitted in this
    # draft park along with it, or the completeness/coverage machinery would keep demanding them.
    for child in updated:
        if (
            child.get("kind") == "acceptance_criterion"
            and child.get("parent_us_id") in deferred_story_ids
            and child.get("status") in ("active", "revised")
        ):
            child["status"] = "deferred"
            child["last_revised_run_id"] = run_id

    for us_id in retired_us_ids or []:
        entry = _find(updated, us_id)
        if entry is None:
            reasons.append(f"retired_us_ids cites {us_id!r}, which does not exist in the ledger")
            continue
        if entry.get("kind") != "user_story":
            reasons.append(f"retired_us_ids cites {us_id!r}, which is not a user story id")
            continue
        if us_id in touched_ids:
            reasons.append(
                f"retired_us_ids cites {us_id!r}, but this draft also revises it via "
                "existing_us_id -- a story cannot be both revised and retired in the same draft"
            )
            continue
        if entry.get("status") in ("active", "revised", "deferred"):
            entry["status"] = "retired"
            entry["last_revised_run_id"] = run_id
            # Cascade: see this function's own docstring -- ac_coverage_gate only looks at an
            # AC's own status, never its parent's, so an orphaned "active" AC under a retired
            # story would still be treated as required coverage.
            #
            # Safe by construction, not just in practice (Task 10 sweep item #5 -- recording the
            # proof here so a future reader doesn't have to re-derive it): this loop only runs for
            # a us_id that is NOT in touched_ids (the `if us_id in touched_ids: ... continue` guard
            # above already ruled out "revised AND retired in the same draft" for the story
            # itself). Revising one of THIS story's own ACs via existing_ac_id requires nesting
            # that AC inside a draft story block whose own existing_us_id resolves to this same
            # us_id -- which would add us_id to touched_ids and hit that same guard. So a story
            # reaching this cascade can never have one of its children simultaneously revised by
            # this same draft; any child already sitting in "revised" got there from an earlier
            # run, and flipping it to "retired" now is exactly the cascade this function's
            # docstring documents, not a live contradiction.
            for child in updated:
                if (
                    child.get("kind") == "acceptance_criterion"
                    and child.get("parent_us_id") == us_id
                    and child.get("status") in ("active", "revised", "deferred")
                ):
                    child["status"] = "retired"
                    child["last_revised_run_id"] = run_id

    for ac_id in retired_ac_ids or []:
        entry = _find(updated, ac_id)
        if entry is None:
            reasons.append(f"retired_ac_ids cites {ac_id!r}, which does not exist in the ledger")
            continue
        if entry.get("kind") != "acceptance_criterion":
            reasons.append(f"retired_ac_ids cites {ac_id!r}, which is not an acceptance criterion id")
            continue
        if ac_id in touched_ids:
            reasons.append(
                f"retired_ac_ids cites {ac_id!r}, but this draft also revises it via "
                "existing_ac_id -- an AC cannot be both revised and retired in the same draft"
            )
            continue
        if entry.get("status") in ("active", "revised", "deferred"):
            entry["status"] = "retired"
            entry["last_revised_run_id"] = run_id

    if reasons:
        return LedgerSyncResult(passed=False, reasons=reasons, updated_entries=entries)

    return LedgerSyncResult(passed=True, reasons=[], updated_entries=updated)


def upsert_questions(
    entries: list[dict[str, Any]], questions: list[dict[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    """Durable question provenance (user requirement 2026-08-31): merge the draft's FULL question
    ledger (schemas.SpecQuestion rows, .model_dump()'d) into the entries list as
    kind="clarifying_question" rows keyed by the model's stable question id. The draft's latest
    word wins on question text/status/answer; rows are never deleted -- answered and assumed
    history alongside the US/AC ids is exactly what lets every resolution trace back to the
    requirements document. Pure; mutates and returns `entries` for save_ledger."""
    by_id = {e.get("id"): e for e in entries if e.get("kind") == "clarifying_question"}
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        if not qid:
            continue
        row = by_id.get(qid)
        if row is None:
            row = {"kind": "clarifying_question", "id": qid, "raised_run_id": run_id}
            entries.append(row)
            by_id[qid] = row
        if q.get("question"):
            row["question"] = str(q["question"])
        row["status"] = q.get("status") or row.get("status") or "open"
        if q.get("answer"):
            row["answer"] = str(q["answer"])
        row["updated_run_id"] = run_id
    return entries


def change_status(
    entry: dict[str, Any], run_id: str
) -> Literal["new", "modified", "deleted", "unchanged", "deferred", "activated"]:
    """Derived per-run change classification -- deliberately computed, never stored: sync_ledger
    already stamps first_seen/last_revised (and retire/defer paths stamp last_revised), so a stored
    copy could only ever drift from these. "deleted" wins over "new" for an entry created and
    retired inside the same run's draft laps; "deferred" likewise wins for one created straight
    into the parked state, and "activated" (a deferred entry promoted back to live this run) wins
    over plain "modified".
    """
    if entry.get("status") == "retired" and entry.get("last_revised_run_id") == run_id:
        return "deleted"
    if entry.get("status") == "deferred" and entry.get("last_revised_run_id") == run_id:
        return "deferred"
    if entry.get("activated_run_id") == run_id:
        return "activated"
    if entry.get("first_seen_run_id") == run_id:
        return "new"
    if entry.get("last_revised_run_id") == run_id:
        return "modified"
    return "unchanged"


def gate_change_status(
    old_entry: dict[str, Any] | None, new_entry: dict[str, Any]
) -> Literal["new", "modified", "unchanged", "deferred", "activated"]:
    """Per-GATE change classification for the review UI: what changed versus the last draft the
    human actually saw. change_status (above) classifies per RUN, and every gate-rejection
    redraft shares one run_id -- so an in-session rewording badged "new" forever (observed live
    2026-08-31, S3 soft-delete revision). The ledger is only persisted on a PASSING verify, and a
    passing verify is exactly what reaches the gate -- so the ledger state loaded BEFORE this
    sync IS the last gated draft, and a plain content diff against it gives gate-relative
    semantics with no extra stamps to keep consistent. Pure.

    old_entry is the entry as it stood in the pre-sync ledger (None = allocated this cycle)."""
    if old_entry is None:
        return "new"
    old_status, new_status = old_entry.get("status"), new_entry.get("status")
    if old_status == "deferred" and new_status in ("active", "revised"):
        return "activated"
    if new_status == "deferred" and old_status != "deferred":
        return "deferred"
    text_key = "title" if new_entry.get("kind") == "user_story" else "description"
    if old_entry.get(text_key) != new_entry.get(text_key):
        return "modified"
    return "unchanged"


def eligible_ac_ids(entries: list[dict[str, Any]], own_ac_ids: set[str]) -> list[str]:
    """The work queue: this ticket's own ACs that are live and have never been delivered by a
    healthy run (no coded_run_id -- stamps are written only by metrics_compute on a
    regression-clean run, and cleared on spec approval when the requirement's wording really
    changed). Completed ACs are deliberately absent: gates must never send delivered work back
    for rework.
    """
    return [
        e["id"]
        for e in entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") in ("active", "revised")
        and e.get("id") in own_ac_ids
        and not e.get("coded_run_id")
    ]


def stamp_delivery(
    entries: list[dict[str, Any]],
    own_ac_ids: set[str],
    ac_execution: dict[str, Any] | None,
    run_id: str,
    now_iso: str,
) -> bool:
    """Mutates `entries` with delivery stamps; returns whether anything changed. Pure.

    Called ONLY from metrics_compute_node on a regression-clean run -- a failed run stamps
    nothing, so its work stays in the queue (stamping at minimal-code-to-green approval put a
    failed run's criteria beyond rework: empty work queue + still-failing tests, an infinite
    failure loop).

    `coded_*` stamps every eligible own-spec criterion (stamp-if-empty: the whole suite was green
    and gate-verified RED tests preceded it -- transitive, spec-scoped evidence). `tested_*`
    stamps per-criterion from the MEASURED eval (`per_ac[id].status == "pass"`; pass already
    implies not-flaky). `test_ids` is always refreshed on a pass -- freezing it would fossilize
    the first run's names and permanently trip the completed-AC protection after any legitimate
    rename.
    """
    per_ac = (ac_execution or {}).get("per_ac") or {}
    changed = False
    for entry in entries:
        if entry.get("kind") != "acceptance_criterion" or entry.get("status") not in ("active", "revised"):
            continue
        ac_id = entry.get("id")
        if ac_id not in own_ac_ids:
            continue
        if not entry.get("coded_run_id"):
            entry["coded_run_id"] = run_id
            entry["coded_at"] = now_iso
            changed = True
        row = per_ac.get(ac_id) or {}
        if row.get("status") == "pass":
            if not entry.get("tested_run_id"):
                entry["tested_run_id"] = run_id
                entry["tested_at"] = now_iso
                changed = True
            names = row.get("test_names") or []
            if names and entry.get("test_ids") != names:
                entry["test_ids"] = names
                changed = True
    return changed


async def apply_tracking_resets_hook(
    thread_id: str, content: dict[str, Any], state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_approve_hook for the specification stage: executes the second phase of the
    two-phase tracking reset (see PENDING_RESET_FIELD). Only markers stamped by THIS run's own
    sync are honored; markers from abandoned runs are dropped without clearing anything.

    ponytail: fires through _run_post_approve_hook, so a sandbox evicted at the gate or a raised
    save skips/loses the reset silently (logged) -- pre-existing hook ceiling, the next healthy
    sync re-marks a still-changed description.
    """
    del content  # the marker on the ledger entry, not the approved spec, is the authority
    run_id = state.get("run_id", "unknown")
    entries = await load_ledger(provider, thread_id)
    changed = False
    for entry in entries:
        marker = entry.get(PENDING_RESET_FIELD)
        if marker is None:
            continue
        if marker == run_id:
            for field in TRACKING_FIELDS:
                entry.pop(field, None)
        entry.pop(PENDING_RESET_FIELD, None)
        changed = True
    if changed:
        await save_ledger(provider, thread_id, entries)
        from . import git_ops

        await git_ops.commit_paths(
            provider, thread_id, [LEDGER_PATH], "ai-dev-workflow: spec approval -- tracking resets applied"
        )


async def stamp_plan_links_hook(
    thread_id: str, content: dict[str, Any], state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_approve_hook for the plan stage: records US/AC -> plan-step provenance
    (`plan_step_ids`) on live AC entries from the approved plan's own step citations. Overwrite
    semantics, so it is idempotent under resume re-fires; retired entries keep whatever historical
    links they had.
    """
    del state
    steps = content.get("plan_steps") or []
    links: dict[str, list[str]] = {}
    for step in steps:
        for ac_id in step.get("ac_ids") or []:
            links.setdefault(ac_id, []).append(step.get("id") or "?")
    entries = await load_ledger(provider, thread_id)
    changed = False
    for entry in entries:
        if entry.get("kind") != "acceptance_criterion" or entry.get("status") not in ("active", "revised"):
            continue
        new_links = sorted(set(links.get(entry["id"], [])))
        if new_links and entry.get("plan_step_ids") != new_links:
            entry["plan_step_ids"] = new_links
            changed = True
        elif not new_links and "plan_step_ids" in entry:
            entry.pop("plan_step_ids", None)
            changed = True
    if changed:
        await save_ledger(provider, thread_id, entries)
        from . import git_ops

        await git_ops.commit_paths(
            provider, thread_id, [LEDGER_PATH], "ai-dev-workflow: plan approval -- plan-step links recorded"
        )


def _demo() -> None:
    """Proves the actual bug Ruling 3 fixes, with real assertions -- not just "it compiles."

    `cd agent && uv run python -m src.spec_ledger`
    """
    seed = [
        {
            "id": "US-0001",
            "kind": "user_story",
            "status": "active",
            "title": "Sign in",
            "first_seen_run_id": "run-1",
            "last_revised_run_id": "run-1",
        },
        {
            "id": "US-0001.1",
            "kind": "acceptance_criterion",
            "parent_us_id": "US-0001",
            "status": "active",
            "description": "Shows an error on a wrong password.",
            "first_seen_run_id": "run-1",
            "last_revised_run_id": "run-1",
        },
    ]

    # THE BUG: an unrelated ticket's draft that cites neither existing id and names neither in
    # retired_ac_ids/retired_us_ids must leave both exactly as they were -- the old unconditional
    # auto-retire loop would have flipped both to "retired" here.
    other_ticket_draft = [
        {
            "id": "draft-1",
            "existing_us_id": None,
            "title": "Export CSV",
            "acceptance_criteria": [{"id": "draft-1.1", "existing_ac_id": None, "description": "Produces a .csv file."}],
        }
    ]
    result = sync_ledger([dict(e) for e in seed], other_ticket_draft, "run-2")
    assert result.passed, result.reasons
    us1 = next(e for e in result.updated_entries if e["id"] == "US-0001")
    ac1 = next(e for e in result.updated_entries if e["id"] == "US-0001.1")
    assert us1["status"] == "active", "an untouched, unnamed story must not be silently retired"
    assert ac1["status"] == "active", "an untouched, unnamed AC must not be silently retired"

    # THE FIX: naming a story in retired_us_ids DOES retire it, and cascades to its own AC.
    result2 = sync_ledger([dict(e) for e in seed], [], "run-3", retired_us_ids=["US-0001"])
    assert result2.passed, result2.reasons
    us1_after = next(e for e in result2.updated_entries if e["id"] == "US-0001")
    ac1_after = next(e for e in result2.updated_entries if e["id"] == "US-0001.1")
    assert us1_after["status"] == "retired"
    assert ac1_after["status"] == "retired", "retiring a story must cascade to its own ACs"

    # Fail-closed: a retirement citation that doesn't resolve is a validation failure, not a
    # silently-ignored no-op (this function's own documented choice -- see sync_ledger's docstring).
    result3 = sync_ledger([dict(e) for e in seed], [], "run-4", retired_ac_ids=["US-9999.9"])
    assert not result3.passed
    assert "US-9999.9" in result3.reasons[0]

    # hydrate_ac_to_tests_ticket_mode_context (Task 7a): cache hit vs. cache miss actually changes
    # its answer, not just that it runs. A minimal duck-typed fake stands in for SandboxProvider --
    # load_ledger only ever calls .exec_in_sandbox on it (via repo_files.read_repo_file).
    import asyncio

    class _FakeReadResult:
        def __init__(self, ok: bool, stdout: str = "") -> None:
            self.ok = ok
            self.stdout = stdout

    class _FakeProvider:
        def __init__(self, files: dict[str, str]) -> None:
            self._files = files

        async def exec_in_sandbox(self, _thread_id: str, command: str):  # noqa: ANN201
            for path, content in self._files.items():
                if path in command:
                    return _FakeReadResult(True, content)
            return _FakeReadResult(False)

    own_spec = {"user_stories": [{"acceptance_criteria": [{"id": "US-0002.1"}]}]}
    ticket_state = {"stages": {"specification": {"approved_content": own_spec}}}

    # MISS: every active ledger AC belongs to this ticket's own Specification (a first-ever
    # ticket, or a solo project) -- no reframe needed.
    ledger_only_own = json.dumps({"entries": [{"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"}]})
    assert asyncio.run(
        hydrate_ac_to_tests_ticket_mode_context("t", ticket_state, _FakeProvider({LEDGER_PATH: ledger_only_own}))
    ) is None

    # HIT: the ledger has an earlier ticket's AC too -- a genuine multi-ticket project.
    ledger_with_other_ticket = json.dumps(
        {
            "entries": [
                {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
                {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"},
            ]
        }
    )
    assert asyncio.run(
        hydrate_ac_to_tests_ticket_mode_context(
            "t", ticket_state, _FakeProvider({LEDGER_PATH: ledger_with_other_ticket})
        )
    ) == {"ticket_mode_baseline": True}

    # --- Tracking-field lifecycle (provenance work) ---
    coded_seed = [
        dict(seed[0]),
        {
            **seed[1],
            "coded_run_id": "run-1",
            "coded_at": "t1",
            "tested_run_id": "run-1",
            "tested_at": "t1",
            "test_ids": ["[US-0001.1] shows error"],
            "plan_step_ids": ["PS-2"],
        },
    ]
    recite_changed = [
        {
            "id": "US-0001",
            "existing_us_id": "US-0001",
            "title": "Sign in",
            "acceptance_criteria": [
                {"id": "US-0001.1", "existing_ac_id": "US-0001.1", "description": "Locks the account after 5 wrong passwords."}
            ],
        }
    ]
    r = sync_ledger([dict(e) for e in coded_seed], [dict(s) for s in recite_changed], "run-5")
    assert r.passed, r.reasons
    ac = next(e for e in r.updated_entries if e["id"] == "US-0001.1")
    # Two-phase reset: verify only MARKS; stamps survive until approval executes the clear.
    assert ac[PENDING_RESET_FIELD] == "run-5"
    assert ac["coded_run_id"] == "run-1", "stamps must survive until spec approval"
    assert ac["last_revised_run_id"] == "run-5"
    us = next(e for e in r.updated_entries if e["id"] == "US-0001")
    assert us["last_revised_run_id"] == "run-1", "identical title re-cite must not bump last_revised"

    # Identical re-cite: no marker, no bump -- completed work is never re-queued.
    recite_same = [
        {
            "id": "US-0001",
            "existing_us_id": "US-0001",
            "title": "Sign in",
            "acceptance_criteria": [
                {"id": "US-0001.1", "existing_ac_id": "US-0001.1", "description": "Shows an error on a wrong password."}
            ],
        }
    ]
    r2 = sync_ledger([dict(e) for e in coded_seed], [dict(s) for s in recite_same], "run-5")
    assert r2.passed, r2.reasons
    ac2 = next(e for e in r2.updated_entries if e["id"] == "US-0001.1")
    assert PENDING_RESET_FIELD not in ac2 and ac2["coded_run_id"] == "run-1"
    assert ac2["last_revised_run_id"] == "run-1", "identical re-cite must not bump last_revised"

    # A stale marker from an abandoned run is dropped by the next sync without clearing stamps.
    stale = [dict(coded_seed[0]), {**coded_seed[1], PENDING_RESET_FIELD: "run-dead"}]
    r3 = sync_ledger([dict(e) for e in stale], [], "run-6")
    assert r3.passed
    ac3 = next(e for e in r3.updated_entries if e["id"] == "US-0001.1")
    assert PENDING_RESET_FIELD not in ac3 and ac3["coded_run_id"] == "run-1"

    # change_status: full matrix, deleted wins over new for same-run create+retire.
    assert change_status({"status": "retired", "first_seen_run_id": "r", "last_revised_run_id": "r"}, "r") == "deleted"
    assert change_status({"status": "active", "first_seen_run_id": "r", "last_revised_run_id": "r"}, "r") == "new"
    assert change_status({"status": "revised", "first_seen_run_id": "r0", "last_revised_run_id": "r"}, "r") == "modified"
    assert change_status({"status": "active", "first_seen_run_id": "r0", "last_revised_run_id": "r0"}, "r") == "unchanged"
    assert change_status({"status": "retired", "first_seen_run_id": "r0", "last_revised_run_id": "r0"}, "r") == "unchanged"
    # Retire paths stamp last_revised (pre-existing behavior "deleted" depends on -- pin it).
    retired_now = next(e for e in result2.updated_entries if e["id"] == "US-0001.1")
    assert change_status(retired_now, "run-3") == "deleted"

    # eligible_ac_ids: excludes coded, retired, and other-ticket ids.
    pool = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active", "coded_run_id": "r1"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "revised"},
        {"id": "US-0001.3", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001", "kind": "user_story", "status": "active"},
    ]
    assert eligible_ac_ids(pool, {"US-0001.1", "US-0001.2", "US-0001.3"}) == ["US-0001.2"]

    # stamp_delivery: coded for eligible own ACs, tested per measured pass, test_ids refreshed,
    # never stamps retired/foreign entries, stamp-if-empty for run ids.
    delivery_pool = [
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "status": "revised",
         "coded_run_id": "r1", "coded_at": "t1", "tested_run_id": "r1", "tested_at": "t1",
         "test_ids": ["old name"]},
        {"id": "US-0001.3", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0009.1", "kind": "acceptance_criterion", "status": "active"},  # other ticket
    ]
    execution = {"per_ac": {
        "US-0001.1": {"status": "pass", "test_names": ["[US-0001.1] works"]},
        "US-0001.2": {"status": "pass", "test_names": ["new name"]},
        "US-0001.3": {"status": "pass", "test_names": ["zombie"]},
        "US-0009.1": {"status": "pass", "test_names": ["foreign"]},
    }}
    changed = stamp_delivery(delivery_pool, {"US-0001.1", "US-0001.2", "US-0001.3"}, execution, "r2", "t2")
    assert changed
    fresh = delivery_pool[0]
    assert fresh["coded_run_id"] == "r2" and fresh["tested_run_id"] == "r2" and fresh["test_ids"] == ["[US-0001.1] works"]
    already = delivery_pool[1]
    assert already["coded_run_id"] == "r1" and already["tested_run_id"] == "r1", "stamp-if-empty for run ids"
    assert already["test_ids"] == ["new name"], "test_ids always refreshed on a pass"
    assert "coded_run_id" not in delivery_pool[2], "retired entries never stamped"
    assert "coded_run_id" not in delivery_pool[3], "other tickets' entries never stamped"
    assert not stamp_delivery(delivery_pool, {"US-0001.1", "US-0001.2"}, execution, "r2", "t2"), "idempotent"

    # --- Deferred scope lifecycle ---
    # New story emitted deferred: parked from birth, cascades to its own new AC, out of the queue.
    deferred_draft = [
        {
            "id": "d-1",
            "existing_us_id": None,
            "title": "Delete a task",
            "deferred": True,
            "acceptance_criteria": [{"id": "d-1.1", "existing_ac_id": None, "description": "Deletes."}],
        }
    ]
    rd = sync_ledger([dict(e) for e in seed], deferred_draft, "run-7")
    assert rd.passed, rd.reasons
    dus = next(e for e in rd.updated_entries if e["kind"] == "user_story" and e["title"] == "Delete a task")
    dac = next(e for e in rd.updated_entries if e.get("parent_us_id") == dus["id"])
    assert dus["status"] == "deferred" and dac["status"] == "deferred"
    assert change_status(dus, "run-7") == "deferred", "born-deferred reports 'deferred', not 'new'"
    assert eligible_ac_ids(rd.updated_entries, {dac["id"]}) == [], "deferred ACs never enter the work queue"

    # Deferring an existing live story parks its non-re-emitted live children too (cascade).
    rd2 = sync_ledger(
        [dict(e) for e in seed],
        [{"id": "US-0001", "existing_us_id": "US-0001", "title": "Sign in", "deferred": True,
          "acceptance_criteria": []}],
        "run-8",
    )
    assert rd2.passed, rd2.reasons
    assert next(e for e in rd2.updated_entries if e["id"] == "US-0001")["status"] == "deferred"
    assert next(e for e in rd2.updated_entries if e["id"] == "US-0001.1")["status"] == "deferred", "defer cascades"

    # Promotion: re-citing a deferred entry WITHOUT the flag revives it as 'activated'.
    parked = rd2.updated_entries
    rp = sync_ledger(
        [dict(e) for e in parked],
        [{"id": "US-0001", "existing_us_id": "US-0001", "title": "Sign in", "acceptance_criteria": [
            {"id": "US-0001.1", "existing_ac_id": "US-0001.1", "description": "Shows an error on a wrong password."}
        ]}],
        "run-9",
    )
    assert rp.passed, rp.reasons
    pus = next(e for e in rp.updated_entries if e["id"] == "US-0001")
    pac = next(e for e in rp.updated_entries if e["id"] == "US-0001.1")
    assert pus["status"] == "revised" and pac["status"] == "revised"
    assert change_status(pus, "run-9") == "activated" and change_status(pac, "run-9") == "activated"
    assert eligible_ac_ids(rp.updated_entries, {"US-0001.1"}) == ["US-0001.1"], "promotion re-enters the queue"

    # A deferred entry can still be retired outright (feature cancelled from the PRD).
    rr = sync_ledger([dict(e) for e in parked], [], "run-10", retired_us_ids=["US-0001"])
    assert rr.passed, rr.reasons
    assert next(e for e in rr.updated_entries if e["id"] == "US-0001")["status"] == "retired"
    assert next(e for e in rr.updated_entries if e["id"] == "US-0001.1")["status"] == "retired"

    # gate_change_status: gate-relative badges -- an in-session rewording is 'modified' even
    # though run stamps say 'new' (the run-id bug this function exists to fix), and status
    # transitions win over text diffs.
    old_us = {"id": "US-0001", "kind": "user_story", "status": "revised", "title": "Delete a task"}
    assert gate_change_status(None, {"kind": "user_story", "status": "active", "title": "X"}) == "new"
    assert gate_change_status(old_us, {**old_us, "title": "Soft-delete a task"}) == "modified"
    assert gate_change_status(old_us, dict(old_us)) == "unchanged"
    assert gate_change_status(old_us, {**old_us, "status": "deferred"}) == "deferred"
    assert gate_change_status({**old_us, "status": "deferred"}, {**old_us, "status": "revised"}) == "activated"
    old_ac = {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active", "description": "a"}
    assert gate_change_status(old_ac, {**old_ac, "description": "b"}) == "modified"

    # Question ledger: upsert keeps history, latest draft wins, no deletes.
    q_entries: list[dict[str, Any]] = [{"kind": "user_story", "id": "US-0001", "status": "active"}]
    upsert_questions(q_entries, [{"id": "q-a", "question": "A?", "status": "open"}], "r1")
    assert any(e["kind"] == "clarifying_question" and e["id"] == "q-a" and e["status"] == "open" for e in q_entries)
    upsert_questions(
        q_entries,
        [{"id": "q-a", "question": "A?", "status": "answered", "answer": "per requirements: yes"},
         {"id": "q-b", "question": "B?", "status": "assumed", "answer": "assumed default"}],
        "r2",
    )
    q_rows = {e["id"]: e for e in q_entries if e["kind"] == "clarifying_question"}
    assert q_rows["q-a"]["status"] == "answered" and q_rows["q-a"]["answer"].startswith("per requirements")
    assert q_rows["q-a"]["raised_run_id"] == "r1" and q_rows["q-a"]["updated_run_id"] == "r2"
    assert q_rows["q-b"]["status"] == "assumed"
    assert len(q_rows) == 2 and any(e["kind"] == "user_story" for e in q_entries), "no deletes, other kinds intact"

    print("spec_ledger self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.spec_ledger
    _demo()
