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

EntryStatus = Literal["active", "retired", "revised"]
EntryKind = Literal["user_story", "acceptance_criterion"]


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
            entry["status"] = "revised"
            entry["title"] = story.get("title", entry.get("title", ""))
            entry["last_revised_run_id"] = run_id
            resolved_us_id = existing_us_id
        else:
            resolved_us_id = allocate_next_id(updated, "user_story")
            updated.append(
                {
                    "id": resolved_us_id,
                    "kind": "user_story",
                    "status": "active",
                    "title": story.get("title", ""),
                    "first_seen_run_id": run_id,
                    "last_revised_run_id": run_id,
                }
            )

        story["id"] = resolved_us_id
        touched_ids.add(resolved_us_id)

        for ac in story.get("acceptance_criteria") or []:
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
                ac_entry["status"] = "revised"
                ac_entry["description"] = ac.get("description", ac_entry.get("description", ""))
                ac_entry["last_revised_run_id"] = run_id
                resolved_ac_id = existing_ac_id
            else:
                resolved_ac_id = allocate_next_id(updated, "acceptance_criterion", resolved_us_id)
                updated.append(
                    {
                        "id": resolved_ac_id,
                        "kind": "acceptance_criterion",
                        "parent_us_id": resolved_us_id,
                        "status": "active",
                        "description": ac.get("description", ""),
                        "first_seen_run_id": run_id,
                        "last_revised_run_id": run_id,
                    }
                )

            ac["id"] = resolved_ac_id
            touched_ids.add(resolved_ac_id)

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
        if entry.get("status") in ("active", "revised"):
            entry["status"] = "retired"
            entry["last_revised_run_id"] = run_id
            # Cascade: see this function's own docstring -- ac_coverage_gate only looks at an
            # AC's own status, never its parent's, so an orphaned "active" AC under a retired
            # story would still be treated as required coverage.
            for child in updated:
                if (
                    child.get("kind") == "acceptance_criterion"
                    and child.get("parent_us_id") == us_id
                    and child.get("status") in ("active", "revised")
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
        if entry.get("status") in ("active", "revised"):
            entry["status"] = "retired"
            entry["last_revised_run_id"] = run_id

    if reasons:
        return LedgerSyncResult(passed=False, reasons=reasons, updated_entries=entries)

    return LedgerSyncResult(passed=True, reasons=[], updated_entries=updated)


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

    print("spec_ledger self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.spec_ledger
    _demo()
