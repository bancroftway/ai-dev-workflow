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

# File-based-editing plan, Part 1 sect. 1: the model's own sketchpad -- edited directly with real
# file tools across draft/audit laps, read back and validated by _verify_specification_ledger
# before anything downstream ever sees it. Sibling to LEDGER_PATH, never touched by
# workflow_persistence.persist_state (that keeps writing 03-specification.draft/approved.json as
# today, from the ledger-resolved content, not from this file directly).
DRAFT_SPEC_PATH = ".ai-dev-workflow/spec/draft-specification.json"

EntryStatus = Literal["active", "retired", "revised", "deferred"]
# "plan_step" added for the file-based-editing plan's Part 2: one shared ledger, not a second
# implementation -- sync_plan_ledger below reuses load_ledger/save_ledger/_find/allocate_next_id's
# sibling logic unchanged, just a simpler single-level sync (no parent/child, no deferred cascade,
# no placeholder/citation indirection -- PlanStep.id is trusted directly as the real id).
EntryKind = Literal["user_story", "acceptance_criterion", "plan_step"]

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

# "Resolved" provenance: a FOURTH tracking site, kept separate from TRACKING_FIELDS above because
# it fires on a different trigger (merge_ready going true in exit_nodes.exit_finalize_node) than
# TRACKING_FIELDS's "regression-clean run" -- folding it into that tuple would make the sites-list
# in TRACKING_FIELDS's own comment wrong. Written only by stamp_resolution() below. Cleared by
# apply_tracking_resets_hook on the same genuine-reword signal that clears TRACKING_FIELDS: once a
# requirement's wording really changed, its old "resolved" fact is no longer evidence of anything.
RESOLUTION_FIELDS = ("resolved_at", "resolved_run_id")


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
    thread_id: str, state: "GraphState", provider: SandboxProvider
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

    File-based-editing plan, Part 1 sect. 7 (migration bootstrap, folded into this existing hook
    rather than a new one): also seeds DRAFT_SPEC_PATH the first time this hook runs against a
    project that doesn't have it yet -- from the in-flight stage["draft"] if it's already
    full-shaped (an old-code run resuming post-upgrade), else from the last-approved specification
    (a new ticket in an existing project), else an empty Specification. Pure best-effort: seeding
    failure here never blocks drafting -- the model's own `create` on first view is the fallback
    this bootstrap merely tries to make unnecessary.
    """
    entries = await load_ledger(provider, thread_id)
    if await repo_files.read_repo_file(provider, thread_id, DRAFT_SPEC_PATH) is None:
        from . import workflow_persistence

        in_flight = ((state.get("stages") or {}).get("specification") or {}).get("draft")
        if isinstance(in_flight, dict) and in_flight.get("user_stories") is not None:
            seed: dict[str, Any] = in_flight
        else:
            approved_raw = await repo_files.read_repo_file(
                provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH
            )
            try:
                seed = json.loads(approved_raw) if approved_raw is not None else None
            except json.JSONDecodeError:
                seed = None
            if not isinstance(seed, dict):
                seed = {
                    "title": "", "summary": "", "work_kind": "feature", "user_stories": [],
                    "assumptions": {"status": "absent", "values": [], "reason": "Not yet drafted."},
                    "out_of_scope": {"status": "absent", "values": [], "reason": "Not yet drafted."},
                    "questions": [], "attachment_notes": [], "retired_ac_ids": [], "retired_us_ids": [],
                    "bug_affected_ac_ids": [],
                }
        await repo_files.write_repo_file(provider, thread_id, DRAFT_SPEC_PATH, json.dumps(seed, indent=2))
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


def _normalize_text(text: str) -> str:
    """Whitespace/case-insensitive comparison key for the citation-drop check below -- collapses
    the exact formatting noise (extra spaces, capitalization) a model's re-typed text could
    plausibly differ on while still being the same content, without doing any fuzzy/similarity
    matching that could false-positive two genuinely different stories into looking like a dup."""
    return " ".join(text.split()).strip().lower()


def _find_duplicate_by_text(
    entries: list[dict[str, Any]], kind: EntryKind, text: str, exclude_ids: set[str]
) -> dict[str, Any] | None:
    """An already-tracked, still-live entry of the same `kind` whose own title/description text is
    an EXACT match (after `_normalize_text`) for `text` -- evidence this "new" entry is really an
    already-numbered one whose citation got dropped, not genuinely new content.

    Root-caused 2026-09-17 (income-investor run d2392db9): 3 of 8 specification laps were spent
    almost entirely on the auditor manually re-discovering and re-citing already-numbered stories/
    criteria the draft had re-emitted with `existing_us_id`/`existing_ac_id: null` -- word-for-word
    identical to content the ledger already tracked under a real id (one lap's own audit note:
    "Restored the citation to the real ledger id (was null); no wording change" x30). The `else`
    branches below used to mint a brand-new id for ANY null citation with no cross-check at all,
    silently accepting -- and duplicating -- content the ledger already had. An EXACT text match
    (not fuzzy) keeps this zero-false-positive: two genuinely different stories essentially never
    share byte-identical title/description text, so this only fires on the real regression."""
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


# The full template regex, one capture group isolating the role text (non-greedy, stops at the
# FIRST ", I want" rather than the last) so the deny-list check below reads off this same match
# instead of a second, separately-maintained pattern. "a/an/the" -- not just "a/an" -- since "As
# the administrator, I want..." is a perfectly ordinary, grammatically-fine stakeholder phrasing;
# the article itself was never the problem, only whether the noun after it names a real
# stakeholder (the deny-list's job) or a wrong connective (the ", so that" requirement below).
# IGNORECASE for "As a"/"As An"/"I Want"/"So That" case variants; DOTALL because `.` doesn't match a
# newline by default and nothing guarantees a model never embeds one inside a narrative string --
# without it, a narrative with a stray newline would false-positive as a template violation it
# doesn't actually have.
_NARRATIVE_RE = re.compile(r"^As (?:a|an|the) (.+?), I want .+, so that .+$", re.IGNORECASE | re.DOTALL)

# Generic non-stakeholder role text observed live (income-investor run 1352296c, lap 1: "As the
# system, I need...", "As the Portfolio Optimizer...") -- a curated internal quality rule, not an
# operator-tunable runtime knob (AGENTS.md's own test: no deploy operator would plausibly want to
# override this independent of a code change), same precedent as skill_gate.py's _SKILL_ALIASES
# living as a plain module constant rather than in config.py. Deliberately generic
# software-engineering nouns only -- catches "the system"/"the algorithm" reliably across any
# project, but NOT a project-specific component name used as a role ("the Portfolio Optimizer"):
# that residual case has no fixed word list to catch it and still needs audit's own judgment, which
# the evidence shows it already handles.
_NON_STAKEHOLDER_ROLE_WORDS = frozenset({
    "system", "application", "api", "backend", "database", "algorithm", "function", "service",
    "platform", "scheduler", "job", "engine", "module", "process",
})


def check_narrative_format(user_stories: list[dict[str, Any]]) -> list[str]:
    """Every User Story's `narrative` must be 'As a <role>, I want <capability>, so that
    <benefit>' -- returns one actionable message per violation (empty list = all pass).

    Root-caused 2026-09-17 (income-investor run 1352296c): both specification prompts already
    state this template verbatim, yet 16 of lap 1's 19 story_changes were pure reformatting of
    violations audit had to catch by LLM judgment alone -- a fully mechanical rule with zero
    deterministic enforcement. Two checks, one regex: the structural 3-part shape (catches a
    dropped "that" directly), and a deny-list rejecting the specific generic-non-stakeholder
    pattern observed live ("the system", "the algorithm", ...). Honestly scoped: does not catch a
    project-specific component name used as a role -- see _NON_STAKEHOLDER_ROLE_WORDS' own
    docstring for why a fixed word list can't cover that case.
    """
    violations: list[str] = []
    for story in user_stories:
        narrative = story.get("narrative") or ""
        story_id = story.get("id") or story.get("existing_us_id") or "(no id)"
        match = _NARRATIVE_RE.match(narrative)
        if match is None:
            violations.append(
                f"{story_id}: narrative {narrative!r} does not match the required "
                "'As a <role>, I want <capability>, so that <benefit>' template (check for a "
                "missing 'that', or a role/capability/benefit segment that isn't actually present)."
            )
            continue
        role_text = _normalize_text(match.group(1)).removeprefix("a ").removeprefix("an ").strip()
        if role_text in _NON_STAKEHOLDER_ROLE_WORDS:
            violations.append(
                f"{story_id}: narrative {narrative!r} names '{match.group(1)}' as the role, which "
                "is not a real human or organizational stakeholder -- never the system itself, a "
                "module, a function, or a named system component. Name the actual person/role who "
                "wants this capability instead."
            )
    return violations


def sync_ledger(
    entries: list[dict[str, Any]],
    draft_user_stories: list[dict[str, Any]],
    run_id: str,
    retired_ac_ids: list[str] | None = None,
    retired_us_ids: list[str] | None = None,
    source_ticket_id: str | None = None,
    bug_affected_ac_ids: list[str] | None = None,
    fully_reviewed: bool | None = None,
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

    `source_ticket_id` (Tickets-view requirements-SOT work): stamped only onto entries CREATED by
    this call, so the elevated ledger doc can show which ticket introduced each requirement. Never
    backfilled onto a pre-existing entry revised/retired by a later ticket -- attribution names the
    entry's origin, not its most recent editor. None (the default) leaves new entries unstamped,
    same as every entry created before this field existed.

    `bug_affected_ac_ids` (file-based-editing plan, Part 5): the model's explicit declaration that
    a bug report is about these existing, LIVE acceptance criteria even though their wording is
    unchanged -- Specification.bug_affected_ac_ids, same category as retired_ac_ids, always
    validated the same fail-closed way: every id must already exist as a "acceptance_criterion"
    entry whose status is active/revised/deferred (a made-up, retired, or wrong-kind id fails the
    whole sync, same posture as a bad existing_ac_id/retired_ac_ids citation), and an id may not
    appear in BOTH bug_affected_ac_ids and retired_ac_ids in the same draft -- "this bug affects
    it" and "remove it" are contradictory claims, the same "revise or retire, never both" rule
    existing_ac_id/retired_ac_ids already enforce. Each valid id gets PENDING_RESET_FIELD stamped
    (independent of whether its text also changed this call) so apply_tracking_resets_hook clears
    its delivery stamps on the next spec approval, reopening it in eligible_ac_ids -- the ONLY
    other trigger for that field besides a genuine wording change (see the description-diff branch
    above). None/empty is the ordinary case (most drafts are not bug reports); never force-populate
    it just because a ticket is classified work_kind="bug" -- a genuinely no-op bug ticket (already
    fixed, duplicate) must be able to leave this empty too.

    `fully_reviewed` (file-based-editing plan, Part 3's review-depth safety net): `None` means "do
    not enforce this lap" (no audit role for the calling stage, or the calling provider cannot
    verify transcripts -- see claude_chat_model.read_full_file_reads' own fail-open contract) --
    entries are neither stamped nor checked. `True` means the caller has transcript evidence the
    audit session read the WHOLE draft file this lap: every entry (kind="user_story" or
    "acceptance_criterion") whose status is active/revised/deferred gets `last_reviewed_run_id`
    stamped to `run_id`, not just the ones this draft actually touched -- a single mechanical
    action over the whole live set. `False` means the caller expected evidence but the file was
    only partially read (or not read at all) this lap -- no stamping happens, but any entry
    already stamped `last_reviewed_run_id == run_id` from an EARLIER lap of this same run_id stays
    stamped (stamps persist across every lap of one ticket's stage, not per-lap). Either way (True
    or False), a completeness sweep then runs: any live entry whose `last_reviewed_run_id != run_id`
    fails the whole sync, same shape as the existing_ac_id/retired_ac_ids validation above -- so a
    ticket's first lap without a genuine full read can never pass, but once one lap proves the
    whole file was read, later same-run_id laps stay passing without re-demanding it.
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
            dup = None if ledger_was_empty else _find_duplicate_by_text(
                updated, "user_story", story.get("title", ""), touched_ids
            )
            if dup is not None:
                reasons.append(
                    f"story {story.get('id')!r} (title {story.get('title')!r}) is word-for-word "
                    f"identical to already-tracked {dup['id']!r} but cites existing_us_id: null -- "
                    f"this looks like a dropped citation, not new content: cite {dup['id']!r} via "
                    "existing_us_id instead (copied character-for-character), or if it genuinely is "
                    "new content, reword it so it isn't identical to an existing story"
                )
                continue
            story_deferred = bool(story.get("deferred"))
            resolved_us_id = allocate_next_id(updated, "user_story")
            new_entry = {
                "id": resolved_us_id,
                "kind": "user_story",
                "status": "deferred" if story_deferred else "active",
                "title": story.get("title", ""),
                "first_seen_run_id": run_id,
                "last_revised_run_id": run_id,
            }
            if source_ticket_id is not None:
                new_entry["source_ticket_id"] = source_ticket_id
            updated.append(new_entry)

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
                # Metadata, not a requirement-wording change -- synced independently of the
                # description/PENDING_RESET_FIELD dance above, never bumps last_revised_run_id.
                ac_entry["ui_related"] = ac.get("ui_related", ac_entry.get("ui_related", False))
                resolved_ac_id = existing_ac_id
            else:
                ac_dup = None if ledger_was_empty else _find_duplicate_by_text(
                    updated, "acceptance_criterion", ac.get("description", ""), touched_ids
                )
                if ac_dup is not None:
                    reasons.append(
                        f"criterion {ac.get('id')!r} (description {ac.get('description')!r}) is "
                        f"word-for-word identical to already-tracked {ac_dup['id']!r} but cites "
                        f"existing_ac_id: null -- this looks like a dropped citation, not new "
                        f"content: cite {ac_dup['id']!r} via existing_ac_id instead (copied "
                        "character-for-character), or if it genuinely is new content, reword it so "
                        "it isn't identical to an existing criterion"
                    )
                    continue
                resolved_ac_id = allocate_next_id(updated, "acceptance_criterion", resolved_us_id)
                new_ac_entry = {
                    "id": resolved_ac_id,
                    "kind": "acceptance_criterion",
                    "parent_us_id": resolved_us_id,
                    "status": "deferred" if ac_deferred else "active",
                    "description": ac.get("description", ""),
                    "ui_related": ac.get("ui_related", False),
                    "first_seen_run_id": run_id,
                    "last_revised_run_id": run_id,
                }
                if source_ticket_id is not None:
                    new_ac_entry["source_ticket_id"] = source_ticket_id
                updated.append(new_ac_entry)

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

    retired_ac_id_set = set(retired_ac_ids or [])
    for bug_ac_id in bug_affected_ac_ids or []:
        entry = _find(updated, bug_ac_id)
        if entry is None:
            reasons.append(f"bug_affected_ac_ids cites {bug_ac_id!r}, which does not exist in the ledger")
            continue
        if entry.get("kind") != "acceptance_criterion":
            reasons.append(f"bug_affected_ac_ids cites {bug_ac_id!r}, which is not an acceptance criterion id")
            continue
        if entry.get("status") not in ("active", "revised", "deferred"):
            reasons.append(
                f"bug_affected_ac_ids cites {bug_ac_id!r}, which is not a live criterion "
                f"(status={entry.get('status')!r}) -- a retired/nonexistent id cannot be reopened"
            )
            continue
        if bug_ac_id in retired_ac_id_set:
            reasons.append(
                f"bug_affected_ac_ids cites {bug_ac_id!r}, but this draft also retires it via "
                "retired_ac_ids -- a criterion cannot be both reopened by a bug and removed in the same draft"
            )
            continue
        entry[PENDING_RESET_FIELD] = run_id

    if reasons:
        return LedgerSyncResult(passed=False, reasons=reasons, updated_entries=entries)

    if fully_reviewed is not None:
        if fully_reviewed:
            for entry in updated:
                if entry.get("kind") in ("user_story", "acceptance_criterion") and entry.get("status") in (
                    "active", "revised", "deferred",
                ):
                    entry["last_reviewed_run_id"] = run_id
        not_reviewed = [
            e["id"]
            for e in updated
            if e.get("kind") in ("user_story", "acceptance_criterion")
            and e.get("status") in ("active", "revised", "deferred")
            and e.get("last_reviewed_run_id") != run_id
        ]
        if not_reviewed:
            return LedgerSyncResult(
                passed=False,
                reasons=[
                    "the audit session did not prove it read the ENTIRE draft file this lap -- these "
                    f"still-live ids were not confirmed reviewed: {not_reviewed}. View the whole file, "
                    "not a partial read, before resubmitting."
                ],
                updated_entries=entries,
            )

    return LedgerSyncResult(passed=True, reasons=[], updated_entries=updated)


def sync_plan_ledger(
    entries: list[dict[str, Any]],
    draft_plan_steps: list[dict[str, Any]],
    run_id: str,
    retired_step_ids: list[str] | None = None,
    fully_reviewed: bool | None = None,
) -> LedgerSyncResult:
    """File-based-editing plan, Part 2 sect. 5: plan's completeness gate, sharing sync_ledger's
    same ledger FILE (EntryKind="plan_step") rather than a second, independently-evolving
    implementation -- reuses load_ledger/save_ledger/_find unchanged.

    Deliberately simpler than sync_ledger's two-tier US/AC scheme: single-level (no parent/child,
    no deferred cascade -- plan steps have no deferred concept) and identity-direct.
    schemas.PlanStep.id (e.g. "PS-1") is trusted as the real ledger id directly -- there is no
    placeholder/citation indirection the way UserStory/AcceptanceCriterion need
    (existing_us_id/existing_ac_id vs. a same-response-scoped `id` placeholder); the model assigns
    a stable id itself and keeps citing that same id on every later revision, so "found by this
    id" IS "cites this existing entry."

    Every entry in `draft_plan_steps` (schemas.PlanStep, .model_dump()'d) is either a revision of
    an existing ledger entry sharing its own `id` (status flips to "revised"; `last_revised_run_id`
    bumps only when `description`/`ac_ids` actually changed, same "real change only" discipline as
    sync_ledger's title/description bump) or a brand-new one (status "active"). A step id that
    collides with a non-plan-step entry, or refers to an already-retired step (ids are never
    reused), fails the whole sync -- same fail-closed posture as sync_ledger's own citation checks.

    `retired_step_ids` names ids no longer in the plan -- the ONLY way a step's status becomes
    "retired" (explicit-only retirement, same Ruling 3 reasoning sync_ledger's own docstring
    explains: a step silently absent from this draft simply keeps its current status).

    `fully_reviewed`: identical contract to sync_ledger's own parameter -- see its docstring --
    scoped to kind="plan_step" entries here instead of user_story/acceptance_criterion.

    Deliberately does NOT compute the "missing_live" completeness sweep (which still-live steps
    from the LAST-APPROVED plan are absent from this draft) -- that lives at the call site
    (gates/diagram_gate.py's verify_plan_diagrams), mirroring exactly how graph.py's
    _verify_specification_ledger computes its own missing_live sweep around sync_ledger rather than
    inside it. This function only validates citation/retirement mechanics, same division of labor
    as sync_ledger.
    """
    updated = [dict(e) for e in entries]
    reasons: list[str] = []
    touched_ids: set[str] = set()

    for step in draft_plan_steps:
        step_id = step.get("id")
        if not step_id:
            reasons.append("a plan step is missing its own id")
            continue
        entry = _find(updated, step_id)
        if entry is not None and entry.get("kind") != "plan_step":
            reasons.append(f"plan step id {step_id!r} collides with a non-plan-step ledger entry")
            continue
        if entry is not None and entry.get("status") == "retired":
            reasons.append(f"plan step id {step_id!r} refers to a retired step -- ids are never reused")
            continue
        new_description = step.get("description", "")
        new_ac_ids = sorted(step.get("ac_ids") or [])
        # "step_kind" (feature/infrastructure), not "kind" -- this ledger entry's own `kind` field
        # already means EntryKind ("plan_step"); PlanStep.kind is a different axis entirely and
        # would silently collide/overwrite it if stored under the same key.
        new_step_kind = step.get("kind", "feature")
        new_ui_related = bool(step.get("ui_related", False))
        new_removes_ids = sorted(step.get("removes_ids") or [])
        if entry is None:
            updated.append({
                "id": step_id,
                "kind": "plan_step",
                "status": "active",
                "description": new_description,
                "ac_ids": new_ac_ids,
                "step_kind": new_step_kind,
                "ui_related": new_ui_related,
                "removes_ids": new_removes_ids,
                "first_seen_run_id": run_id,
                "last_revised_run_id": run_id,
            })
        else:
            entry["status"] = "revised"
            changed = (
                new_description != entry.get("description")
                or new_ac_ids != sorted(entry.get("ac_ids") or [])
                or new_step_kind != entry.get("step_kind", "feature")
                or new_removes_ids != sorted(entry.get("removes_ids") or [])
            )
            entry["description"] = new_description
            entry["ac_ids"] = new_ac_ids
            entry["step_kind"] = new_step_kind
            # Metadata, not content -- synced independently, never bumps last_revised_run_id (same
            # "ui_related is metadata" discipline sync_ledger already applies to AC.ui_related).
            entry["ui_related"] = new_ui_related
            entry["removes_ids"] = new_removes_ids
            if changed:
                entry["last_revised_run_id"] = run_id
        touched_ids.add(step_id)

    for step_id in retired_step_ids or []:
        entry = _find(updated, step_id)
        if entry is None:
            reasons.append(f"retired_step_ids cites {step_id!r}, which does not exist in the ledger")
            continue
        if entry.get("kind") != "plan_step":
            reasons.append(f"retired_step_ids cites {step_id!r}, which is not a plan step id")
            continue
        if step_id in touched_ids:
            reasons.append(
                f"retired_step_ids cites {step_id!r}, but this draft also revises it -- a step "
                "cannot be both revised and retired in the same draft"
            )
            continue
        if entry.get("status") in ("active", "revised"):
            entry["status"] = "retired"
            entry["last_revised_run_id"] = run_id

    if reasons:
        return LedgerSyncResult(passed=False, reasons=reasons, updated_entries=entries)

    if fully_reviewed is not None:
        if fully_reviewed:
            for entry in updated:
                if entry.get("kind") == "plan_step" and entry.get("status") in ("active", "revised"):
                    entry["last_reviewed_run_id"] = run_id
        not_reviewed = [
            e["id"]
            for e in updated
            if e.get("kind") == "plan_step"
            and e.get("status") in ("active", "revised")
            and e.get("last_reviewed_run_id") != run_id
        ]
        if not_reviewed:
            return LedgerSyncResult(
                passed=False,
                reasons=[
                    "the audit session did not prove it read the ENTIRE steps.json file this lap -- "
                    f"these still-live step ids were not confirmed reviewed: {not_reviewed}. View the "
                    "whole file, not a partial read, before resubmitting."
                ],
                updated_entries=entries,
            )

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
    old: dict[str, Any] | None, new: dict[str, Any], *, reopened: bool = False
) -> Literal["new", "modified", "unchanged", "deferred", "activated", "reopened"]:
    """Per-GATE change classification for the review UI: what changed versus the specification
    the human last actually APPROVED -- not the ledger's own rolling pre-sync state, and not
    change_status's per-RUN classification (both wrong here, for different reasons):

    - change_status (above) is per-RUN: every gate-rejection redraft shares one run_id, so an
      in-session rewording badged "new" forever (observed live 2026-08-31, S3 soft-delete
      revision).
    - An EARLIER version of this function compared against the ledger's pre-sync state instead,
      reasoning that the ledger only persists on a passing verify and a passing verify is what
      reaches the gate. That reasoning breaks across a reject-and-redraft cycle that happens
      BEFORE the human ever approves anything: sync_ledger (and the ledger save) already ran
      during the FIRST draft's verify, so the ledger enters the SECOND draft's verify already
      populated -- a story re-cited unchanged from that never-approved first draft then compared
      "unchanged" against it, even though the human had never seen it as anything but a fresh
      "new" story (observed live 2026-08-31: a 6-story spec redrafted once before approval badged
      only the ONE genuinely-added story "new" and silently dropped the badge from the other
      five).

    The specification a human last approved is the only content guaranteed NOT to change across
    any number of reject-before-approval redraft cycles (nothing is written there until a real
    approval), so it is the correct, stable baseline. `old`/`new` are `{"text": str, "deferred":
    bool}` shapes lifted straight from Specification content (previously-approved vs current
    draft) -- never ledger entries, which have no comparable "was this deferred before" signal of
    their own beyond the ledger's independent bookkeeping. Pure.

    old=None means this id did not exist in the previously-approved specification at all (a
    genuinely new ticket, or a first-ever approval where nothing has been approved yet).

    `reopened` (file-based-editing plan, Part 5): True when the caller has determined this id is
    named in this run's `bug_affected_ac_ids` -- a bug report about a criterion whose wording was
    always correct. Checked only for a textually-UNCHANGED id; a real wording change still reports
    "modified" even if also bug-reopened (the wording delta is the more informative signal). Without
    this, a bug-affected AC would badge "unchanged" -- the exact opposite of what a human reviewer
    or a downstream prompt scanning this one field for "what's new" needs to see."""
    if old is None:
        return "new"
    old_deferred, new_deferred = bool(old.get("deferred")), bool(new.get("deferred"))
    if old_deferred and not new_deferred:
        return "activated"
    if new_deferred:
        return "deferred"
    if old.get("text") != new.get("text"):
        return "modified"
    return "reopened" if reopened else "unchanged"


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


def stamp_resolution(entries: list[dict[str, Any]], run_id: str, now_iso: str) -> bool:
    """Mutates `entries` with resolution stamps; returns whether anything changed. Pure.

    Called ONLY from exit_nodes.exit_finalize_node, guarded on `merge_ready` -- delivery
    (stamp_delivery, above) fires on a merely regression-clean run, but verify_exit_readiness can
    still force merge_ready=False afterward (missing screenshots, no test command, unverified
    auth). "Resolved" must lag "coded/tested" by that one more gate, or a run that never actually
    reaches a mergeable state would still claim its criteria resolved.

    Unlike stamp_delivery, deliberately NOT scoped to the calling ticket's own AC ids: an AC
    delivered by an earlier run in the same lineage must still resolve here, once merge-readiness
    is finally reached by whichever run gets there -- "resolved" is a whole-ledger fact, not a
    per-ticket one. Do not "fix" this back to scoped.

    AC pass first, then a story bubble-up pass in the same call so a story whose last child is
    resolved by THIS call's AC pass resolves in the same run rather than lagging one run behind.
    """
    changed = False
    for entry in entries:
        if (
            entry.get("kind") == "acceptance_criterion"
            and entry.get("status") in ("active", "revised")
            and entry.get("coded_run_id")
            and entry.get("tested_run_id")
            and not entry.get("resolved_at")
        ):
            entry["resolved_at"] = now_iso
            entry["resolved_run_id"] = run_id
            changed = True
    for story in entries:
        if story.get("kind") != "user_story" or story.get("status") not in ("active", "revised") or story.get("resolved_at"):
            continue
        live_children = [
            e for e in entries
            if e.get("kind") == "acceptance_criterion"
            and e.get("parent_us_id") == story.get("id")
            and e.get("status") in ("active", "revised")
        ]
        if live_children and all(child.get("resolved_at") for child in live_children):
            story["resolved_at"] = now_iso
            story["resolved_run_id"] = run_id
            changed = True
    return changed


def _apply_tracking_resets(entries: list[dict[str, Any]], run_id: str) -> bool:
    """Pure second phase of the two-phase tracking reset (see PENDING_RESET_FIELD): clears
    TRACKING_FIELDS and RESOLUTION_FIELDS on any entry marked by THIS run's own sync, and cascades
    the RESOLUTION_FIELDS clear to the entry's parent story unconditionally (a resolved story's
    "all live children resolved" fact no longer holds once one of them is un-resolved). Markers
    from abandoned runs are dropped without clearing anything. Returns whether anything changed."""
    changed = False
    for entry in entries:
        marker = entry.get(PENDING_RESET_FIELD)
        if marker is None:
            continue
        if marker == run_id:
            for field in TRACKING_FIELDS:
                entry.pop(field, None)
            for field in RESOLUTION_FIELDS:
                entry.pop(field, None)
            parent = _find(entries, entry.get("parent_us_id")) if entry.get("parent_us_id") else None
            if parent is not None:
                for field in RESOLUTION_FIELDS:
                    parent.pop(field, None)
        entry.pop(PENDING_RESET_FIELD, None)
        changed = True
    return changed


async def apply_tracking_resets_hook(
    thread_id: str, content: dict[str, Any], state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_approve_hook for the specification stage: executes _apply_tracking_resets
    against the persisted ledger.

    ponytail: fires through _run_post_approve_hook, so a sandbox evicted at the gate or a raised
    save skips/loses the reset silently (logged) -- pre-existing hook ceiling, the next healthy
    sync re-marks a still-changed description.
    """
    del content  # the marker on the ledger entry, not the approved spec, is the authority
    run_id = state.get("run_id", "unknown")
    entries = await load_ledger(provider, thread_id)
    changed = _apply_tracking_resets(entries, run_id)
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
            "acceptance_criteria": [{"id": "draft-1.1", "existing_ac_id": None, "description": "Produces a .csv file.", "ui_related": True}],
        }
    ]
    result = sync_ledger([dict(e) for e in seed], other_ticket_draft, "run-2")
    assert result.passed, result.reasons
    us1 = next(e for e in result.updated_entries if e["id"] == "US-0001")
    ac1 = next(e for e in result.updated_entries if e["id"] == "US-0001.1")
    assert us1["status"] == "active", "an untouched, unnamed story must not be silently retired"
    assert ac1["status"] == "active", "an untouched, unnamed AC must not be silently retired"
    ac_new = next(e for e in result.updated_entries if e.get("description") == "Produces a .csv file.")
    assert ac_new["ui_related"] is True, "ui_related from the draft must persist on a new AC entry"

    # Provenance (Tickets-view requirements SOT work): a NEW entry stamps which ticket introduced
    # it; a later ticket revising a PRE-EXISTING entry must not steal or backfill its attribution.
    prov_draft = [
        {
            "id": "draft-2",
            "existing_us_id": None,
            "title": "Reset password",
            "acceptance_criteria": [{"id": "draft-2.1", "existing_ac_id": None, "description": "Emails a reset link."}],
        }
    ]
    prov_result = sync_ledger([dict(e) for e in seed], prov_draft, "run-11", source_ticket_id="ticket-A")
    assert prov_result.passed, prov_result.reasons
    new_us = next(e for e in prov_result.updated_entries if e.get("title") == "Reset password")
    new_ac = next(e for e in prov_result.updated_entries if e.get("description") == "Emails a reset link.")
    assert new_us["source_ticket_id"] == "ticket-A", "a new entry must be stamped with the ticket that introduced it"
    assert new_ac["source_ticket_id"] == "ticket-A"
    revise_draft = [
        {"id": "US-0001", "existing_us_id": "US-0001", "title": "Sign in (updated)", "acceptance_criteria": []}
    ]
    revised = sync_ledger([dict(e) for e in seed], revise_draft, "run-12", source_ticket_id="ticket-B")
    assert revised.passed, revised.reasons
    us_after_revise = next(e for e in revised.updated_entries if e["id"] == "US-0001")
    assert "source_ticket_id" not in us_after_revise, "revising a pre-existing entry must not backfill attribution"

    # Citation-drop detection (root-caused 2026-09-17, income-investor run d2392db9): a draft that
    # re-emits an already-tracked story/AC word-for-word but with existing_us_id/existing_ac_id
    # left null must be REJECTED, not silently minted as a duplicate.
    dropped_us_draft = [
        {"id": "draft-3", "existing_us_id": None, "title": "Sign in", "acceptance_criteria": []},
    ]
    dropped_us_result = sync_ledger([dict(e) for e in seed], dropped_us_draft, "run-13")
    assert not dropped_us_result.passed, "an identical-title story with a null citation must be rejected"
    assert any("US-0001" in r and "identical" in r for r in dropped_us_result.reasons), dropped_us_result.reasons

    dropped_ac_draft = [
        {
            "id": "US-0001", "existing_us_id": "US-0001", "title": "Sign in",
            "acceptance_criteria": [
                {"id": "draft-3.1", "existing_ac_id": None, "description": "Shows an error on a wrong password."}
            ],
        },
    ]
    dropped_ac_result = sync_ledger([dict(e) for e in seed], dropped_ac_draft, "run-14")
    assert not dropped_ac_result.passed, "an identical-description AC with a null citation must be rejected"
    assert any("US-0001.1" in r and "identical" in r for r in dropped_ac_result.reasons), dropped_ac_result.reasons

    # A case-/whitespace-only difference is still caught (normalized comparison, not exact bytes) --
    # but genuinely different text (the "Export CSV"/"Reset password" cases above) is NOT flagged,
    # already proven passing above.
    dropped_us_ws_draft = [
        {"id": "draft-4", "existing_us_id": None, "title": "  sign   IN  ", "acceptance_criteria": []},
    ]
    dropped_us_ws_result = sync_ledger([dict(e) for e in seed], dropped_us_ws_draft, "run-15")
    assert not dropped_us_ws_result.passed, "whitespace/case differences must not evade the duplicate check"

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
            # repo_files.read_repo_file's own 2026-09-17 fix reads these on the not-ok path
            # (is_expected_missing_file) -- every fake "not found" result here really does model a
            # plain missing file, never an unexpected failure, so match that real shape exactly.
            self.returncode = 0 if ok else 1
            self.stderr = "" if ok else "cat: file: No such file or directory"

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
                {"id": "US-0001.1", "existing_ac_id": "US-0001.1", "description": "Locks the account after 5 wrong passwords.", "ui_related": True}
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
    assert ac["ui_related"] is True, "ui_related syncs onto an existing AC entry independently of description tracking"
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

    # --- Resolution lifecycle ---
    # A live AC with both stamps set resolves; one missing a stamp does not; a retired AC never
    # resolves even fully delivered; a story only bubbles once EVERY live child is resolved.
    resolution_pool = [
        {"id": "US-0001", "kind": "user_story", "status": "active"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "parent_us_id": "US-0001", "status": "active",
         "coded_run_id": "r1", "tested_run_id": "r1"},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "parent_us_id": "US-0001", "status": "active",
         "coded_run_id": "r1"},  # never tested -- must not resolve, and must block the story bubble
        {"id": "US-0002", "kind": "user_story", "status": "active"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "parent_us_id": "US-0002", "status": "retired",
         "coded_run_id": "r1", "tested_run_id": "r1"},  # retired -- must never resolve
    ]
    assert stamp_resolution(resolution_pool, "r2", "t2")
    ac_resolved = next(e for e in resolution_pool if e["id"] == "US-0001.1")
    ac_unresolved = next(e for e in resolution_pool if e["id"] == "US-0001.2")
    story_blocked = next(e for e in resolution_pool if e["id"] == "US-0001")
    ac_retired = next(e for e in resolution_pool if e["id"] == "US-0002.1")
    assert ac_resolved["resolved_at"] == "t2" and ac_resolved["resolved_run_id"] == "r2"
    assert "resolved_at" not in ac_unresolved, "coded without tested must not resolve"
    assert "resolved_at" not in story_blocked, "story must not bubble while a live child is unresolved"
    assert "resolved_at" not in ac_retired, "a retired AC never resolves, however fully delivered"
    assert not stamp_resolution(resolution_pool, "r2", "t2"), "idempotent"

    # Once the last live child resolves, the story bubbles up in the SAME call.
    ac_unresolved["tested_run_id"] = "r1"
    assert stamp_resolution(resolution_pool, "r3", "t3")
    story_after = next(e for e in resolution_pool if e["id"] == "US-0001")
    assert story_after["resolved_at"] == "t3" and story_after["resolved_run_id"] == "r3"

    # Retirement never clobbers a prior resolution -- historical fact survives sync_ledger's own
    # retirement path (status/last_revised_run_id are the only fields it touches).
    resolved_then_retired = sync_ledger(
        [dict(e) for e in resolution_pool], [], "run-13", retired_us_ids=["US-0001"]
    )
    assert resolved_then_retired.passed, resolved_then_retired.reasons
    retired_ac = next(e for e in resolved_then_retired.updated_entries if e["id"] == "US-0001.1")
    assert retired_ac["status"] == "retired" and retired_ac["resolved_at"] == "t2", "resolved_at survives retirement"

    # A genuine re-word clears resolved_at on the AC AND cascades to clear it on the parent story.
    reworded_pool = [dict(e) for e in resolution_pool]
    next(e for e in reworded_pool if e["id"] == "US-0001.1")[PENDING_RESET_FIELD] = "run-14"
    assert _apply_tracking_resets(reworded_pool, "run-14")
    reworded_ac = next(e for e in reworded_pool if e["id"] == "US-0001.1")
    reworded_story = next(e for e in reworded_pool if e["id"] == "US-0001")
    assert "resolved_at" not in reworded_ac and "coded_run_id" not in reworded_ac
    assert "resolved_at" not in reworded_story, "un-resolving a child must cascade-clear the parent story"

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

    # gate_change_status: compares against the PREVIOUSLY APPROVED specification, never the
    # ledger's rolling pre-sync state -- the whole point is staying "new" across any number of
    # reject-before-approval redraft cycles, since nothing is written to the approved spec until
    # a real approval happens.
    old_story = {"text": "As a user, I want to delete a task.", "deferred": False}
    assert gate_change_status(None, {"text": "X", "deferred": False}) == "new"
    # Re-cited unchanged across a REJECTED, never-approved redraft: still "new", not "unchanged"
    # (the exact bug this rewrite fixes -- old_story stands in for "nothing has been approved
    # yet", so a second, third, Nth pre-approval redraft of the same never-approved content must
    # keep reporting "new" every time).
    assert gate_change_status(None, dict(old_story)) == "new"
    # Once something IS actually approved, re-citing it unchanged next time is "unchanged".
    assert gate_change_status(old_story, dict(old_story)) == "unchanged"
    assert gate_change_status(old_story, {**old_story, "text": "As a user, I want to soft-delete a task."}) == "modified"
    assert gate_change_status(old_story, {**old_story, "deferred": True}) == "deferred"
    assert gate_change_status({**old_story, "deferred": True}, {**old_story, "deferred": False}) == "activated"
    # Stays deferred both times: still reported "deferred" (the UI's own `deferred` prop already
    # forces the chip regardless, but the change classification should agree, not silently None).
    assert gate_change_status({**old_story, "deferred": True}, {**old_story, "deferred": True}) == "deferred"

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

    # --- bug_affected_ac_ids (Part 5) ---
    # A valid, live citation reopens the AC (PENDING_RESET_FIELD) even with wording unchanged.
    bug_seed = [dict(e) for e in coded_seed]  # US-0001.1 already has coded_run_id/tested_run_id
    bug_result = sync_ledger([dict(e) for e in bug_seed], [], "run-15", bug_affected_ac_ids=["US-0001.1"])
    assert bug_result.passed, bug_result.reasons
    bug_ac = next(e for e in bug_result.updated_entries if e["id"] == "US-0001.1")
    assert bug_ac[PENDING_RESET_FIELD] == "run-15", "a bug-reopened AC must be marked for tracking reset"
    assert bug_ac["coded_run_id"] == "run-1", "stamps survive until spec approval (two-phase, same as a reword)"

    # Fail-closed: an id that does not exist, or is not an acceptance_criterion, or is retired.
    assert not sync_ledger([dict(e) for e in seed], [], "run-16", bug_affected_ac_ids=["US-9999.9"]).passed
    assert not sync_ledger([dict(e) for e in seed], [], "run-16", bug_affected_ac_ids=["US-0001"]).passed
    retired_seed = [dict(e) for e in result2.updated_entries]  # US-0001.1 retired earlier in this demo
    assert not sync_ledger(retired_seed, [], "run-16", bug_affected_ac_ids=["US-0001.1"]).passed

    # An id cannot be both reopened and retired in the same draft.
    contradiction = sync_ledger(
        [dict(e) for e in seed], [], "run-17", retired_ac_ids=["US-0001.1"], bug_affected_ac_ids=["US-0001.1"]
    )
    assert not contradiction.passed, "reopening and retiring the same AC in one draft must be rejected"

    # gate_change_status: "reopened" only for a textually-unchanged id explicitly flagged; a real
    # wording change still reports "modified" even when also bug-reopened.
    assert gate_change_status(old_story, dict(old_story), reopened=True) == "reopened"
    assert gate_change_status(old_story, dict(old_story), reopened=False) == "unchanged"
    assert (
        gate_change_status(old_story, {**old_story, "text": "changed"}, reopened=True) == "modified"
    ), "a real wording change wins over reopened"
    assert gate_change_status(None, dict(old_story), reopened=True) == "new", "a brand-new id is never 'reopened'"

    # --- fully_reviewed / not_reviewed (Part 3's review-depth safety net) ---
    # None: no enforcement at all -- passes regardless of any stamps.
    none_result = sync_ledger([dict(e) for e in seed], [], "run-18", fully_reviewed=None)
    assert none_result.passed
    assert "last_reviewed_run_id" not in next(e for e in none_result.updated_entries if e["id"] == "US-0001")

    # True: stamps every live US/AC (not just touched ones) to this run_id, then passes.
    true_result = sync_ledger([dict(e) for e in seed], [], "run-19", fully_reviewed=True)
    assert true_result.passed, true_result.reasons
    for entry_id in ("US-0001", "US-0001.1"):
        stamped = next(e for e in true_result.updated_entries if e["id"] == entry_id)
        assert stamped["last_reviewed_run_id"] == "run-19", f"{entry_id} should be stamped by a full read"

    # False, first lap of a run_id with no prior stamp: rejected.
    false_result = sync_ledger([dict(e) for e in seed], [], "run-20", fully_reviewed=False)
    assert not false_result.passed, "a lap with incomplete evidence and no prior stamp must reject"

    # False, but an EARLIER lap of the SAME run_id already stamped everyone: still passes -- once
    # satisfied early in a ticket's redraft loop, later laps do not re-demand it.
    already_stamped_seed = [dict(e) for e in true_result.updated_entries]  # stamped run_id="run-19"
    later_lap = sync_ledger(already_stamped_seed, [], "run-19", fully_reviewed=False)
    assert later_lap.passed, "a stamp from an earlier lap of the same run_id must still satisfy later laps"

    # --- sync_plan_ledger (Part 2 sect. 5) ---
    plan_draft = [
        {"id": "PS-1", "description": "Build the login form.", "ac_ids": ["US-0001.1"], "kind": "feature"},
        {"id": "PS-2", "description": "Set up CI.", "ac_ids": [], "kind": "infrastructure"},
    ]
    plan_result = sync_plan_ledger([], plan_draft, "run-21")
    assert plan_result.passed, plan_result.reasons
    ps1 = next(e for e in plan_result.updated_entries if e["id"] == "PS-1")
    ps2 = next(e for e in plan_result.updated_entries if e["id"] == "PS-2")
    assert ps1["status"] == "active" and ps1["kind"] == "plan_step" and ps1["first_seen_run_id"] == "run-21"
    assert ps2["ac_ids"] == []
    # PlanStep.kind (feature/infrastructure) must round-trip under "step_kind" -- NOT "kind",
    # which this ledger entry's own EntryKind field already occupies ("plan_step").
    assert ps1["step_kind"] == "feature" and ps2["step_kind"] == "infrastructure"

    # Re-citing the SAME id and content: status flips to "revised", but last_revised_run_id does
    # NOT bump (identical content is not a real change) -- mirrors sync_ledger's own title/description discipline.
    unchanged_recite = sync_plan_ledger([dict(e) for e in plan_result.updated_entries], plan_draft, "run-22")
    assert unchanged_recite.passed, unchanged_recite.reasons
    ps1_recited = next(e for e in unchanged_recite.updated_entries if e["id"] == "PS-1")
    assert ps1_recited["status"] == "revised" and ps1_recited["last_revised_run_id"] == "run-21"

    # A genuine content change DOES bump last_revised_run_id.
    changed_draft = [{"id": "PS-1", "description": "Build the login form with SSO.", "ac_ids": ["US-0001.1"], "kind": "feature"}]
    changed_result = sync_plan_ledger([dict(e) for e in plan_result.updated_entries], changed_draft, "run-23")
    assert changed_result.passed, changed_result.reasons
    ps1_changed = next(e for e in changed_result.updated_entries if e["id"] == "PS-1")
    assert ps1_changed["last_revised_run_id"] == "run-23"

    # Explicit-only retirement: a step absent from the draft keeps its status; naming it retires it.
    retire_result = sync_plan_ledger([dict(e) for e in plan_result.updated_entries], [], "run-24", retired_step_ids=["PS-2"])
    assert retire_result.passed, retire_result.reasons
    assert next(e for e in retire_result.updated_entries if e["id"] == "PS-1")["status"] == "active", (
        "a step not named in this draft must keep its current status"
    )
    assert next(e for e in retire_result.updated_entries if e["id"] == "PS-2")["status"] == "retired"

    # Fail-closed: retiring a nonexistent id, or citing a retired id as if it were still live.
    assert not sync_plan_ledger([dict(e) for e in plan_result.updated_entries], [], "run-25", retired_step_ids=["PS-9999"]).passed
    assert not sync_plan_ledger(retire_result.updated_entries, [dict(plan_draft[1])], "run-26").passed, (
        "re-citing a retired plan step id must be rejected, not silently un-retire it"
    )

    # fully_reviewed for plan steps: same contract, scoped to kind="plan_step".
    plan_true = sync_plan_ledger([dict(e) for e in plan_result.updated_entries], [], "run-27", fully_reviewed=True)
    assert plan_true.passed
    assert all(
        e.get("last_reviewed_run_id") == "run-27"
        for e in plan_true.updated_entries
        if e.get("kind") == "plan_step" and e.get("status") in ("active", "revised")
    )
    plan_false = sync_plan_ledger([dict(e) for e in plan_result.updated_entries], [], "run-28", fully_reviewed=False)
    assert not plan_false.passed, "plan steps need the same full-read evidence before passing"

    # --- hydrate_ticket_mode_context's DRAFT_SPEC_PATH bootstrap (Part 1 sect. 7) ---
    import base64 as _base64
    import re as _re

    class _FakeBootstrapProvider:
        """Extends the read-only _FakeProvider pattern above with a minimal, real write-command
        simulation (write_repo_file's own base64-piped shell shape) so the bootstrap's actual
        write can be asserted on, not just trusted to not-raise."""

        def __init__(self, files: dict[str, str]) -> None:
            self._files = dict(files)
            self.writes: dict[str, str] = {}

        async def exec_in_sandbox(self, _thread_id: str, command: str):  # noqa: ANN201
            m = _re.search(r"echo (\S+) \| base64 -d > (\S+)", command)
            if m:
                target = m.group(2).strip("'\"")
                self.writes[target] = _base64.b64decode(m.group(1)).decode("utf-8")
                return _FakeReadResult(True, "")
            for path, content in self._files.items():
                if path in command:
                    return _FakeReadResult(True, content)
            return _FakeReadResult(False)

    # Case 1: DRAFT_SPEC_PATH absent, no in-flight draft, an approved spec exists -- seeds from it.
    approved_spec_json = json.dumps({"title": "Existing", "summary": "s", "user_stories": []})
    from . import workflow_persistence as _wp

    bootstrap_provider = _FakeBootstrapProvider({_wp.SPECIFICATION_APPROVED_PATH: approved_spec_json})
    ctx = asyncio.run(hydrate_ticket_mode_context("t", {"stages": {}}, bootstrap_provider))
    assert ctx is None, "empty ledger must not signal ticket-mode framing"
    assert DRAFT_SPEC_PATH in bootstrap_provider.writes, "should have seeded DRAFT_SPEC_PATH"
    assert json.loads(bootstrap_provider.writes[DRAFT_SPEC_PATH]) == json.loads(approved_spec_json), (
        "should have seeded DRAFT_SPEC_PATH from the last-approved specification"
    )

    # Case 2: an in-flight, full-shaped draft in state wins over the approved file.
    in_flight_draft = {"title": "In flight", "summary": "s", "user_stories": [{"id": "US-0001"}]}
    bootstrap_provider2 = _FakeBootstrapProvider({_wp.SPECIFICATION_APPROVED_PATH: approved_spec_json})
    asyncio.run(
        hydrate_ticket_mode_context(
            "t", {"stages": {"specification": {"draft": in_flight_draft}}}, bootstrap_provider2
        )
    )
    assert json.loads(bootstrap_provider2.writes[DRAFT_SPEC_PATH]) == in_flight_draft, (
        "an in-flight full-shaped draft must win over the approved file"
    )

    # Case 3: neither exists (brand-new ticket, brand-new project) -- seeds an empty Specification.
    bootstrap_provider3 = _FakeBootstrapProvider({})
    asyncio.run(hydrate_ticket_mode_context("t", {"stages": {}}, bootstrap_provider3))
    empty_seed = json.loads(bootstrap_provider3.writes[DRAFT_SPEC_PATH])
    assert empty_seed["user_stories"] == [] and empty_seed["title"] == ""

    # Case 4: DRAFT_SPEC_PATH already exists -- must NOT be overwritten (never clobber a live sketchpad).
    bootstrap_provider4 = _FakeBootstrapProvider(
        {DRAFT_SPEC_PATH: "already there", _wp.SPECIFICATION_APPROVED_PATH: approved_spec_json}
    )
    asyncio.run(hydrate_ticket_mode_context("t", {"stages": {}}, bootstrap_provider4))
    assert DRAFT_SPEC_PATH not in bootstrap_provider4.writes, "an existing sketchpad must never be overwritten"

    # check_narrative_format (root-caused 2026-09-17, income-investor run 1352296c).
    valid_story = {"id": "US-0001", "narrative": "As an administrator, I want to configure the risk-free rate, so that Sharpe/Sortino calculations use a current value."}
    assert check_narrative_format([valid_story]) == [], "a correctly-shaped narrative must pass"

    wrong_role_and_connective = {
        "id": "US-0002",
        "narrative": "As the system, I need to refresh the universe so I can keep data current.",
    }
    violations = check_narrative_format([wrong_role_and_connective])
    assert len(violations) == 1 and "US-0002" in violations[0], violations

    missing_that = {
        "id": "US-0003",
        "narrative": "As a user, I want to export the data, so I can share it with my team.",
    }
    violations = check_narrative_format([missing_that])
    assert len(violations) == 1 and "US-0003" in violations[0], violations

    # Honestly-scoped boundary: a project-specific component name used as a role is NOT caught --
    # no fixed word list can cover an arbitrary project's own component names, see
    # _NON_STAKEHOLDER_ROLE_WORDS' own docstring. This documents the scope, not a bug.
    project_specific_role = {
        "id": "US-0004",
        "narrative": "As the Portfolio Optimizer, I want to reuse the Evaluator function, so that scoring stays consistent.",
    }
    assert check_narrative_format([project_specific_role]) == [], (
        "a project-specific component name as role is a documented gap, not caught by the deny-list"
    )

    print("spec_ledger self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.spec_ledger
    _demo()
