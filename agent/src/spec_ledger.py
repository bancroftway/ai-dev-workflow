"""P2's stable ID registry (US-####/AC-####.#), persisted at spec/ledger.json (repo root) --
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
from typing import Any, Literal

# What a REAL ledger id looks like. The renumbering guards below only fire when the draft's own
# `id` field is itself ledger-shaped: schemas.py documents `id` as a same-response placeholder
# that is "ignored when existing_us_id is set", so a placeholder like 'draft-story-1' alongside a
# valid citation is the DOCUMENTED contract, not an attempted renumbering. Enforcing equality for
# placeholders made every revision round fail by construction (observed live: three verify cycles
# burned on 'draft-story-1' != 'US-0001').
_REAL_ID_RE = re.compile(r"^US-\d+(\.\d+)?$")

from . import repo_files
from .sandbox.provider import SandboxProvider

LEDGER_PATH = "spec/ledger.json"
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
    entries: list[dict[str, Any]], draft_user_stories: list[dict[str, Any]], run_id: str
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

    Stories/ACs present in `entries` as active/revised but not touched by this draft are marked
    retired (never deleted) -- the previous approved spec's story that's simply absent from this
    draft.
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

    if reasons:
        return LedgerSyncResult(passed=False, reasons=reasons, updated_entries=entries)

    for entry in updated:
        if entry["id"] not in touched_ids and entry.get("status") in ("active", "revised"):
            entry["status"] = "retired"

    return LedgerSyncResult(passed=True, reasons=[], updated_entries=updated)
