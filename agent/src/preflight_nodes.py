"""brownfield-baseline plain (non-LLM) nodes: scaffold_node (the true entry point of a fresh run) and the
tech-stack stage's idempotency short-circuit / post-audit hook.

Kept as a separate module from graph.py (not just more functions in it) since these are a
self-contained unit with their own imports (repo_files, template_loader, git_ops) -- matches the
existing file-size-hygiene convention of one concern per module (workflow_persistence.py,
git_ops.py, model_config.py are all similarly scoped).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ValidationError

from . import config as workflow_config
from . import config_inventory, git_ops, model_config, repo_files, repo_scan, repo_test_config, session_store, tech_stack_signals, template_loader, workflow_persistence
from .chat_model import ainvoke_structured, get_chat_model_for_thread
from .markdown_render import render_tech_stack_markdown
from .prompt_loader import load_prompt, load_prompt_pair, render_prompt
from .schemas import TECH_STACK_EXTRACT_EXAMPLE, DotnetStatus, PresenceList, TechStack
from .schemas_app_discovery import DiscoveredApp
from .schemas_session import SessionTitleResponse
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

logger = logging.getLogger(__name__)

MANIFEST_PATH = ".ai-dev-workflow/manifest.json"


class ManifestAppCheck(BaseModel, extra="allow"):
    """app_discovery.app_check_record_node's block, plus exit's greenfield re-record."""

    checked_at: str | None = None
    run_id: str | None = None
    suitable: bool | None = None
    evidence_fingerprint: str | None = None
    apps: list[DiscoveredApp] = []


class Manifest(BaseModel, extra="allow"):
    """The one schema every manifest.json writer is validated against, greenfield and brownfield
    alike. extra='allow' + all-optional keys: this is a shape guard (a wrong-typed app_check or
    apps list fails loudly in the logs), not a presence gate -- presence of the fields a merge
    actually needs (apps, test_command, coverage_commands) is enforced by exit's deterministic
    verify, the only point where the complete picture exists."""

    onboarded: bool | None = None
    run_id: str | None = None
    timestamp: str | None = None
    toolchain: dict[str, Any] | None = None
    app_check: ManifestAppCheck | None = None
    test_command: str | None = None
    coverage_commands: list[dict[str, Any]] | None = None
    requirements_content_hash: str | None = None
    approval_hashes: dict[str, Any] | None = None
    metrics_summary: dict[str, Any] | None = None
    merge_readiness: dict[str, Any] | None = None

def _guidance_sentinel(key: str) -> str:
    """One idempotency marker per ecosystem's AGENTS.md paragraph.

    Byte-identical to the original hardcoded dotnet sentinel for key="dotnet" -- deliberately, so
    a repo onboarded before this was generalized doesn't get a second .NET paragraph appended.
    """
    return f"<!-- ai-dev-workflow:{key}-guidance -->"


def _session_title(raw_requirements_text: str, run_id: str) -> str:
    """First non-empty line of the run's requirements text, truncated to 80 chars -- what shows
    up as the row's title in /select's session history. Falls back to a run-id-stamped
    placeholder for the (rare) case a run reaches scaffold with no requirements text at all."""
    for line in raw_requirements_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    return f"(untitled run {run_id})"


async def _generate_session_title(thread_id: str, raw_requirements_text: str, run_id: str, provider: str) -> str:
    """LLM-generated session title, shown in the session-list UI -- same get_chat_model_for_thread
    / ainvoke_structured mechanism every draft node already uses (GitHub Copilot-backed), no
    separate LLM integration. Falls back to _session_title's first-line heuristic on empty input
    or any failure -- title generation must never block scaffold.

    `provider` is threaded in from scaffold_node's own `state["provider"]` (this thread's pinned
    org provider) rather than read here, since this helper has no `state` of its own -- same
    per-run pinning every other model_config.get_model_name call site relies on (Ruling 2)."""
    text = (raw_requirements_text or "").strip()
    if not text:
        return _session_title(raw_requirements_text, run_id)
    try:
        system, human_template = load_prompt_pair("session_title")
        human = render_prompt(human_template, requirements_text=text[:4000])
        model = get_chat_model_for_thread(
            thread_id,
            "session-title",
            "draft",
            provider=provider,
            # Task 3b (Part 2 Ruling 10) fix-round-3: `run_id` is already this function's own
            # parameter (used above for the fallback title) -- just wasn't forwarded here.
            run_id=run_id,
            model_name=model_config.get_model_name("session-title", "draft", provider),
            sandbox=sandbox_registry.get(thread_id),
        )
        response = await ainvoke_structured(
            model, [SystemMessage(content=system), HumanMessage(content=human)], SessionTitleResponse
        )
        title = response.title.strip()
        return title[:80] if title else _session_title(raw_requirements_text, run_id)
    except Exception:
        logger.warning(
            "session title generation failed for thread_id=%s; using heuristic fallback", thread_id, exc_info=True
        )
        return _session_title(raw_requirements_text, run_id)


_TECH_STACK_SENTINEL = _guidance_sentinel("tech-stack")

_TECH_STACK_PARAGRAPH = f"""
{_TECH_STACK_SENTINEL}
## Before generating, modifying, or reviewing any code

Read `.ai-dev-workflow/tech-stack.md` first and follow the technology stack, architecture, and
repository layout documented there -- including for a from-scratch (greenfield) repository, where
this file describes the stack and layout to scaffold the application into.
"""

_MEMORY_SENTINEL = _guidance_sentinel("repo-memory")

_MEMORY_PARAGRAPH = f"""
{_MEMORY_SENTINEL}
## Repo memory

`.ai-dev-workflow/memory.md` is this repository's durable memory: build quirks, commands that
work, config traps -- lessons previous agent runs paid for. Read it before starting work; append
a dated bullet when you learn something durable and repo-specific the code itself doesn't say.
Never write secrets or task narration into it.
"""

TECH_STACK_MD_PATH = ".ai-dev-workflow/tech-stack.md"
# One truth, derived from the stage-file numbering -- see workflow_persistence.
TECH_STACK_APPROVED_JSON_PATH = workflow_persistence.TECH_STACK_APPROVED_PATH


async def update_manifest(
    provider: SandboxProvider, thread_id: str, updates: dict[str, Any]
) -> dict[str, Any]:
    """Read-modify-write of manifest.json -- the only sanctioned way to touch it.

    Every writer goes through here (brownfield_write_manifest_node, app_discovery.app_check_record_node,
    exit_nodes.exit_finalize_node) because the file is co-owned: brownfield-baseline owns `onboarded`, app discovery
    owns `app_check`, exit owns the run/approval/metrics summary. A wholesale overwrite by any one
    of them silently deletes the others' keys -- which is exactly the bug exit_finalize_node had,
    dropping `onboarded` at the end of every run and re-triggering brownfield onboarding on the
    next one.

    A malformed manifest is replaced rather than raised on, matching
    hydrate_tech_stack_from_repo_file's same tolerance: this runs mid-pipeline, and a hand-edited
    file should not take a run down.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH)
    try:
        manifest = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        logger.warning("manifest.json is not valid JSON for thread_id=%s; rewriting it", thread_id)
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    # app_check is itself co-owned (app discovery writes suitable/evidence_fingerprint, exit's
    # greenfield re-record writes apps) -- deep-merge that one key so a partial update can't
    # clobber the fingerprint that next-run hydration depends on. Everything else is scalar-owned.
    if isinstance(updates.get("app_check"), dict) and isinstance(manifest.get("app_check"), dict):
        updates = {**updates, "app_check": {**manifest["app_check"], **updates["app_check"]}}
    manifest.update(updates)
    try:
        Manifest.model_validate(manifest)
    except ValidationError:
        # Shape guard only: log loudly, still write -- this runs mid-pipeline and a malformed
        # value must not take the run down (same tolerance as the JSON-decode path above).
        logger.warning("manifest.json failed schema validation for thread_id=%s", thread_id, exc_info=True)
    await repo_files.write_repo_file(provider, thread_id, MANIFEST_PATH, json.dumps(manifest, indent=2) + "\n")
    return manifest


async def scaffold_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """The true entry point of a from-scratch run (graph.py's module docstring definition of a
    "run"), deliberately kept read-mostly: it resets the workflow action ledger (fresh per
    session, per the user's explicit choice), records the pre-run HEAD, and reports whether this
    repo has been onboarded before.

    The repo-visible writes (AGENTS.md, .github/copilot-instructions.md) live in
    scaffold_finalize_node instead, which runs only after app discovery has confirmed the
    workflow actually applies here -- committing onboarding files into a repository we are about
    to reject would be rude, and undoing them afterwards is worse than never writing them.

    A no-op when no sandbox is registered for this thread yet -- mirrors _persist_if_sandboxed's
    same tolerance in graph.py, since scaffold_node runs unconditionally on every fresh intake,
    including threads that predate sandboxing.
    """
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {}

    provider = get_sandbox_provider()
    await repo_files.reset_ledger(provider, thread_id)
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "scaffold", "node": "scaffold", "action": "ran"})

    # Session row lives in SQL now (session_store.py) -- owner/repo/user_login/source_branch/
    # work_branch were already written once at provision time (sessions_api.provision_session);
    # this just refreshes the live run_id/title/status, independent of the git baseline capture
    # below (no more commit-ordering constraint against app_discovery's reject-path reset).
    run_id = state.get("run_id", "unknown")
    raw_requirements_text = state.get("raw_requirements_text") or ""
    await session_store.touch_run(
        thread_id,
        run_id=run_id,
        title=await _generate_session_title(thread_id, raw_requirements_text, run_id, state["provider"]),
    )

    # Captured before anything else is written, so app_discovery's reject path can put the tree
    # back exactly as it arrived. Same reference-point technique as StageSpec.capture_baseline_commit.
    head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")

    # manifest.json absence is the canonical "never onboarded before" signal -- gates whether
    # build_graph()'s conditional edge routes into brownfield-baseline's brownfield sub-flow. Read once, here, and
    # routed on from state later: app discovery writes to this file mid-run, so a fresh read at
    # the branch point would always report "onboarded".
    manifest_exists = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH) is not None
    return {
        "manifest_exists": manifest_exists,
        "run_baseline_commit": head.stdout.strip() if head.ok else None,
        # scaffold runs exactly and only on the proceed path (a blank reattach ping ENDs at
        # intake), so this is where a previous run's terminal failure record gets cleared --
        # keeping the failure pill visible across reloads but not across a real re-run.
        "run_failure": None,
        # Live cost restarts with the run (the sandbox ledger it sums from is reset just above).
        "token_usage_running": None,
    }


async def scaffold_finalize_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """The write half of scaffolding, deferred until app discovery has accepted the repository:
    writes AGENTS.md and a thin copilot-instructions.md pointer if missing (never overwriting a
    human-authored one) and commits them."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {}

    provider = get_sandbox_provider()
    written_paths: list[str] = []

    agents_md = await repo_files.read_repo_file(provider, thread_id, "AGENTS.md")
    if agents_md is None:
        await repo_files.write_repo_file(
            provider, thread_id, "AGENTS.md", template_loader.load_template("agents-md/AGENTS.md")
        )
        written_paths.append("AGENTS.md")
    else:
        # A human-authored AGENTS.md is never overwritten -- but leaving it entirely alone means
        # the repo's own agents never learn about the pipeline's pointer files. Append each
        # missing paragraph only, guarded by its own sentinel (and by a plain substring check, so
        # a hand-written reference to the same path also counts as "already covered").
        appended = agents_md
        if _TECH_STACK_SENTINEL not in appended and ".ai-dev-workflow/tech-stack.md" not in appended:
            appended = appended.rstrip() + "\n" + _TECH_STACK_PARAGRAPH
        if _MEMORY_SENTINEL not in appended and ".ai-dev-workflow/memory.md" not in appended:
            appended = appended.rstrip() + "\n" + _MEMORY_PARAGRAPH
        if appended != agents_md:
            await repo_files.write_repo_file(provider, thread_id, "AGENTS.md", appended)
            written_paths.append("AGENTS.md")

    copilot_instructions = await repo_files.read_repo_file(provider, thread_id, ".github/copilot-instructions.md")
    if copilot_instructions is None:
        await repo_files.write_repo_file(
            provider,
            thread_id,
            ".github/copilot-instructions.md",
            template_loader.load_template("github/copilot-instructions.md"),
        )
        written_paths.append(".github/copilot-instructions.md")

    if await record_toolchain(provider, thread_id):
        written_paths.append(MANIFEST_PATH)

    if written_paths:
        await git_ops.commit_paths(
            provider, thread_id, [*written_paths, ".ai-dev-workflow"], "ai-dev-workflow: scaffold"
        )

    # Kick the ~100s baseline repo scan in the background so it overlaps the tech-stack ->
    # brownfield LLM chain instead of serializing behind it (repo_scan_baseline_node awaits it).
    # Guarded on baseline absence -- a returning thread must never be re-measured.
    if await repo_files.read_repo_file(provider, thread_id, repo_scan.BASELINE_PATH) is None:
        repo_scan.start_background_scan(
            thread_id, provider, chat_provider=state["provider"], run_id=state.get("run_id", "unknown")
        )
    return {}


TOOLCHAIN_REPORT_PATH = "agent-work/toolchain-bootstrap.json"


def _toolchain_log_path() -> Path:
    """Host-side sink for toolchain findings, and the only durable one that exists today.

    The manifest copy lives inside the target repo's clone, and this pipeline never pushes
    (git_ops.commit_paths is local-only) while sandboxes are --rm and idle-reaped -- so a record
    written *only* into the repo dies with the container. This file is what actually accumulates
    "which repos needed a toolchain the image doesn't ship", which is the question that decides
    what the next image should include.
    """
    return Path(os.environ.get("AIDW_TOOLCHAIN_LOG") or (Path(__file__).parent.parent / "agent-work" / "toolchain.jsonl"))


async def record_toolchain(provider: SandboxProvider, thread_id: str) -> bool:
    """Folds bootstrap.sh's report into its two sinks. Returns True when manifest.json changed.

    Split by write pattern, deliberately:
      - durable facts (which tools this repo needed, whether the image had them) -> manifest.json,
        rewritten only when that set actually changes, so a re-run produces no commit churn;
      - per-run metrics (that this session installed them at all) -> ledger.jsonl, which is fresh
        per session and already aggregated by metrics-report;
      - everything, appended -> the host-side JSONL above, which outlives the container.

    Best-effort throughout: telemetry must never be the reason a run fails.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, TOOLCHAIN_REPORT_PATH)
    if not raw:
        return False
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("toolchain report is not valid JSON for thread_id=%s -- ignoring", thread_id)
        return False

    tools = report.get("tools") or {}
    try:
        log_path = _toolchain_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"thread_id": thread_id, **report}) + "\n")
    except OSError:
        logger.warning("could not append to the host-side toolchain log", exc_info=True)

    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "scaffold", "node": "toolchain", "tools": tools}
    )

    if not tools:
        return False

    existing_raw = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH)
    try:
        existing = json.loads(existing_raw).get("toolchain") if existing_raw else None
    except json.JSONDecodeError:
        existing = None
    entry = {"image": report.get("image", "unknown"), "tools": tools}
    if existing == entry:
        # Same tools, same image, same outcomes -- rewriting would produce a commit whose only
        # content is "we ran again".
        return False
    await update_manifest(provider, thread_id, {"toolchain": entry})
    return True


_MIGRATION_GLOBS = "*.sql,*.prisma,migrations/*,Migrations/*,schema.rb,*.dbml"
_ROUTE_HINT_GLOBS = "routes.*,*Controller.cs,*.routes.ts,urls.py,routes/*"


async def brownfield_baseline_context_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Deterministic pre-scan for brownfield-baseline's brownfield draft: greps for schema/migration/route files
    and hands their content as grounding context, rather than trusting the model to explore
    unaided (reduces ER-diagram hallucination risk)."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {"brownfield_context": ""}
    provider = get_sandbox_provider()
    find_cmd = " -o ".join(f"-iname {shlex.quote(g)}" for g in (_MIGRATION_GLOBS + "," + _ROUTE_HINT_GLOBS).split(","))
    result = await provider.exec_in_sandbox(thread_id, f"find . \\( {find_cmd} \\) -not -path '*/node_modules/*' 2>/dev/null | head -50")
    paths = [p.strip() for p in (result.stdout or "").splitlines() if p.strip()]
    chunks: list[str] = []
    for path in paths[:20]:
        content = await repo_files.read_repo_file(provider, thread_id, path.lstrip("./"))
        if content:
            chunks.append(f"--- {path} ---\n{content[:2000]}")
    return {"brownfield_context": "\n\n".join(chunks)[:20000] or "(no schema/migration/route files found)"}


async def brownfield_write_manifest_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Ratification approval is what flips manifest.json from absent to present -- the literal
    mechanism, per the plan's own design. Deterministic, runs right after brownfield-baseline brownfield's gate."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {"manifest_exists": True}
    provider = get_sandbox_provider()
    await update_manifest(provider, thread_id, {"onboarded": True, "run_id": state.get("run_id", "unknown")})
    await git_ops.commit_paths(provider, thread_id, [MANIFEST_PATH], "ai-dev-workflow: ratify brownfield baseline")
    return {"manifest_exists": True}


def _merge_config_signals(
    tech_stack: dict[str, Any], scanned_auth: str, scanned_keys: list[str]
) -> tuple[dict[str, Any], bool]:
    """Task 5 fix #1's shared core: unions a config_inventory.inventory() scan into an
    already-produced TechStack dict's auth_kind/config_inventory fields via config_inventory.merge()
    (Task 4). Both resolve_tech_stack_submission's settle tail (_settle_tech_stack, below -- every
    settle-and-persist path: cache hit, fresh extraction, and the extraction-failure fallback) and
    hydrate_tech_stack_from_repo_file's backfill route through this one function, so there is
    structurally no path that can ship a TechStack without the union.

    `config_inventory` is PresenceList-shaped (Task 2), not a bare list -- unwrapped to its plain
    `values` via tech_stack_signals.presence_values for merge(), then rewrapped following
    PresenceList's own validation rules (non-empty values -> status="present"; empty ->
    status="absent" with a reason). Returns (possibly-unchanged tech_stack, changed) so a caller
    that only wants to backfill on real change (hydrate, to avoid rewriting an already-complete
    sidecar on every hydration) can skip its own write when nothing moved; a caller that always
    persists regardless (_settle_tech_stack) can just ignore the flag.
    """
    existing_auth = tech_stack.get("auth_kind") if isinstance(tech_stack.get("auth_kind"), str) else None
    existing_keys = tech_stack_signals.presence_values(tech_stack, "config_inventory")
    auth_kind, keys = config_inventory.merge(existing_auth or "none", existing_keys, scanned_auth, scanned_keys)
    if auth_kind == (existing_auth or "none") and keys == existing_keys:
        return tech_stack, False
    merged = dict(tech_stack)
    merged["auth_kind"] = auth_kind
    merged["config_inventory"] = (
        {"status": "present", "values": keys, "reason": ""}
        if keys
        else {"status": "absent", "values": [], "reason": "no config keys detected"}
    )
    return merged, True


_EXTRACTION_FAILURE_REASONS = frozenset({"extraction failed", "legacy sidecar, pre-typed-absence"})


def _looks_like_extraction_failure(tech_stack: dict[str, Any]) -> bool:
    """Task 5 fix #4's pure predicate: does this (already tech_stack_signals.load_tech_stack-
    normalized) TechStack dict carry the signature _extraction_failed_tech_stack's output leaves
    behind -- `summary` is raw (possibly truncated) markdown rather than an LLM-written sentence,
    AND every LLM-derived field sits at its bare absent/not-detected default with a generic reason
    (this fix's own "extraction failed" marker, or a genuinely ancient pre-typed-absence sidecar
    that coerces to the same shape).

    Deliberately distinguishes that from a genuinely EMPTY repo an LLM looked at and correctly
    reported nothing for: a real "nothing found" verdict carries its own specific reason text
    (e.g. "no framework found in package.json"), never the generic sentinel every field shares in
    a failed extraction. Also deliberately does not look at auth_kind/config_inventory -- those
    are populated by the deterministic config_inventory scan (fix #1), not the LLM, and
    legitimately vary independent of whether extraction itself succeeded.
    """
    summary = str(tech_stack.get("summary") or "")
    if not (summary.startswith("#") or len(summary) == 500):
        return False
    for field in ("languages", "frameworks", "package_managers", "testing_frameworks", "conventions"):
        entry = tech_stack.get(field) or {}
        if entry.get("status") != "absent" or entry.get("reason") not in _EXTRACTION_FAILURE_REASONS:
            return False
    dotnet = tech_stack.get("dotnet") or {}
    if dotnet.get("status") != "not_detected" or dotnet.get("reason") not in _EXTRACTION_FAILURE_REASONS:
        return False
    if tech_stack.get("convention_roots") or tech_stack.get("conventions_applied"):
        return False
    return True


async def hydrate_tech_stack_from_repo_file(
    thread_id: str, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.hydrate_from_repo_file for the tech-stack stage: skip Copilot CLI drafting
    entirely and hydrate as pre-approved when tech-stack.approved.json already exists.

    Requires the .json sidecar specifically, not just tech-stack.md (the rendered .md isn't
    reliably round-trippable back into TechStack's typed fields) -- a bare .md with no .json
    (e.g. from a version of this tool predating the sidecar, or manual repo setup) is treated as
    "needs a fresh run," not "already done."
    """
    raw = await repo_files.read_repo_file(provider, thread_id, TECH_STACK_APPROVED_JSON_PATH)
    if raw is None:
        return None
    try:
        approved = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "tech-stack.approved.json exists but isn't valid JSON for thread_id=%s -- treating as "
            "needing a fresh run rather than failing hard (unlike HydrationError's contract "
            "elsewhere, this file isn't the sole source of truth for GraphState itself).",
            thread_id,
        )
        return None
    if not isinstance(approved, dict):
        return approved

    # Fix #4 (Task 5): repair a sidecar carrying the extraction-failure signature (fix #2's
    # typed-absence fallback, or an ancient sidecar that coerces the same way) by retrying
    # extraction once against tech-stack.md, which is still on disk. Self-limiting -- a renewed
    # failure (the retry itself raising) just leaves the existing sidecar as-is rather than
    # looping; no separate migration script for dead threads that will never hydrate again.
    if _looks_like_extraction_failure(tech_stack_signals.load_tech_stack(approved)):
        try:
            markdown = await repo_files.read_repo_file(provider, thread_id, TECH_STACK_MD_PATH)
            if markdown:
                repaired = await _extract_tech_stack(
                    thread_id, markdown, provider, state["provider"], state.get("run_id", "unknown")
                )
                approved = repaired
                await repo_files.write_repo_file(
                    provider, thread_id, TECH_STACK_APPROVED_JSON_PATH, json.dumps(approved, indent=2) + "\n",
                )
        except Exception:  # noqa: BLE001 -- one retry only; a renewed failure just keeps the old sidecar
            logger.warning(
                "tech-stack sidecar repair retry failed for thread_id=%s; leaving existing sidecar as-is",
                thread_id, exc_info=True,
            )

    # Backfill auth_kind/config_inventory for repos onboarded before these fields existed: they
    # otherwise never populate (hydration skips detection forever), so the deterministic scanner
    # fills them here (via the same config_inventory.merge() core resolve_tech_stack_submission's
    # own settle tail uses, fix #1) and the enriched sidecar is re-persisted. Best-effort -- a scan
    # failure just leaves the fields at their existing values. Gated on "something's actually
    # missing" (not unconditional, unlike the settle tail) so an already-complete sidecar isn't
    # rescanned on every hydration of an onboarded repo.
    existing_auth = approved.get("auth_kind") if isinstance(approved.get("auth_kind"), str) else None
    existing_keys = tech_stack_signals.presence_values(approved, "config_inventory")
    if existing_auth in (None, "none") or not existing_keys:
        try:
            scanned_auth, scanned_keys = await config_inventory.inventory(provider, thread_id)
            merged, changed = _merge_config_signals(approved, scanned_auth, scanned_keys)
            if changed:
                approved = merged
                await repo_files.write_repo_file(
                    provider, thread_id, TECH_STACK_APPROVED_JSON_PATH, json.dumps(approved, indent=2),
                )
        except Exception:  # noqa: BLE001
            logger.warning("auth_kind/config backfill failed for thread_id=%s", thread_id, exc_info=True)
    return approved


def _resolve_ticket_tech_stack_markdown(
    tech_stack_id: str | None, tech_stack_text: str | None, catalog: list[dict[str, Any]]
) -> str | None:
    """Pure decision behind _prefill_from_ticket_tech_stack_selection: given a project's own
    ticket-filing-time picker selection and the loaded catalog, what markdown (if any) should
    prefill the tech-stack draft. None means "nothing to prefill" -- the caller falls through to
    fresh detection exactly as if this fallback didn't exist.

    tech_stack_id (a catalog pick) wins over tech_stack_text (free-text description) when a
    project somehow has both -- mirrors the New Ticket form's own catalog-vs-"describe it myself"
    framing, a catalog id being the more specific of the two selections. Free text is still a real
    user selection, not a lesser one -- prefilled too, just clearly labeled as the user's own
    words rather than passed off as this tool's own markdown convention (a catalog entry's
    rendered file, or an LLM's detection draft)."""
    if tech_stack_id:
        catalog_entry = next((s for s in catalog if s["id"] == tech_stack_id), None)
        return catalog_entry["markdown"] if catalog_entry is not None else None
    if tech_stack_text:
        return f"# Tech Stack\n\n(As described by the user when filing this ticket.)\n\n{tech_stack_text}"
    return None


async def _prefill_from_ticket_tech_stack_selection(thread_id: str) -> dict[str, Any] | None:
    """Fallback half of prefill_tech_stack_from_repo_file (Phase E audit, B-Critical-1): the New
    Ticket form's tech-stack picker (dbo.projects.tech_stack_id/tech_stack_text, written by
    project_store.create_project at ticket-filing time) used to be written and read by NOTHING --
    a user picked a stack, Assign scaffolded an empty repo, and the tech-stack stage then detected
    against that empty repo with no knowledge the user had already answered, asking again at the
    gate. Only reached when the repo carries no tech-stack.md of its own (this function's only
    caller already checked that) -- a connected brownfield repo's committed stack always wins over
    a picker choice made at ticket-filing time, the same precedence hydrate_from_repo_file already
    gives tech-stack.approved.json over everything else.

    Local imports (project_store, app_discovery): app_discovery imports FROM this module
    (preflight_nodes.update_manifest) at module load time, so a top-level import back the other
    way would be circular; project_store has no such cycle but is kept local too, for symmetry --
    both are only ever needed once this function actually runs. The actual choice of WHAT to
    return lives in the pure, synchronously-testable _resolve_ticket_tech_stack_markdown above --
    this async half is pure I/O plumbing around it.
    """
    from . import project_store

    session = await session_store.get_session(thread_id)
    project_id = session.get("project_id") if session else None
    if not project_id:
        return None
    project = await project_store.get_project(project_id)
    if project is None:
        return None

    from . import app_discovery

    tech_stack_id = project.get("tech_stack_id")
    markdown = _resolve_ticket_tech_stack_markdown(
        tech_stack_id, project.get("tech_stack_text"), app_discovery.load_stack_catalog()
    )
    if markdown is None and tech_stack_id:
        logger.warning(
            "project %s picked tech_stack_id=%r at ticket-filing time, but no catalog entry has "
            "that id any more -- falling through to fresh detection instead of prefilling",
            project_id, tech_stack_id,
        )
    return {"markdown": markdown} if markdown is not None else None


async def prefill_tech_stack_from_repo_file(
    thread_id: str, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.prefill_from_repo_file for the tech-stack stage: tech-stack.md exists but
    approved.json doesn't (else hydrate_tech_stack_from_repo_file already short-circuited to
    approved above) -- show the file's own content in the Tech Stack tab, zero LLM calls, zero
    repo exploration. Unlike hydrate, the result becomes an UNAPPROVED draft that still passes
    through the human gate (make_draft_node), since the file's content was never reviewed through
    this tool before.

    Falls back (Phase E audit, B-Critical-1) to the New Ticket form's own tech-stack picker
    (dbo.projects.tech_stack_id/tech_stack_text) when the repo has no tech-stack.md of its own --
    see _prefill_from_ticket_tech_stack_selection. That fallback only ever fires for a project that
    actually recorded a picker choice (a "+ New Project" ticket); a Connect-Repository project's
    tech_stack_id/tech_stack_text stay NULL forever (project_store.py's own docstring), so this
    changes nothing for that flow. Same just_rejected guard as every other prefill_from_repo_file
    caller (make_draft_node) -- a human's gate rejection always redrafts for real, never re-serves
    this same prefill.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, TECH_STACK_MD_PATH)
    if raw is not None:
        return {"markdown": raw}
    ticket_prefill = await _prefill_from_ticket_tech_stack_selection(thread_id)
    if ticket_prefill is not None:
        return ticket_prefill
    # Greenfield short-circuit (2026-08-31): the deterministic app_discovery_pre scan already
    # proved there is no application code, so the LLM explore-and-draft pass has nothing to
    # discover -- its live output on an empty repo was literally "No application code found
    # yet" after ~60s of haiku. Serve that stub directly: the gate opens as soon as the sandbox
    # is ready. `greenfield_stub` keeps the canned-stack dropdown visible
    # (graph._build_tech_stack_interrupt_extra reads it -- a plain {"markdown": ...} draft would
    # otherwise read as "file existed, hide the dropdown").
    if tech_stack_signals.is_greenfield_repo(state):
        return {
            "markdown": (
                "# Tech Stack\n\n"
                "No application code found yet -- this is a greenfield repository.\n\n"
                "Pick a starting stack from the dropdown above, or describe your own stack here, "
                "then submit.\n"
            ),
            "greenfield_stub": True,
        }
    return None


_TECH_STACK_EXTRACT_PROMPT = load_prompt("tech_stack_extract")


# Content-hash cache for _extract_tech_stack results, one json file on the agent host (same
# disposable-run-plumbing lifecycle as agent/data/checkpoints.sqlite -- losing it just costs one
# LLM call per distinct markdown). Validated through TechStack on read so a stale/corrupt row can
# never feed downstream gates a wrong shape.
_EXTRACT_CACHE_PATH = Path(
    os.environ.get("AIDW_TECH_STACK_EXTRACT_CACHE", Path(__file__).parents[1] / "data" / "tech_stack_extract_cache.json")
)


def _extract_cache_key(markdown: str) -> str:
    import hashlib

    return hashlib.sha256(markdown.strip().encode("utf-8")).hexdigest()


def _extract_cache_get(markdown: str) -> dict[str, Any] | None:
    try:
        rows = json.loads(_EXTRACT_CACHE_PATH.read_text(encoding="utf-8"))
        raw = rows.get(_extract_cache_key(markdown))
        return None if raw is None else TechStack.model_validate(raw).model_dump(mode="json")
    except Exception:  # noqa: BLE001 -- cache is best-effort; any failure means "miss"
        return None


def _extract_cache_put(markdown: str, tech_stack: dict[str, Any]) -> None:
    try:
        rows: dict[str, Any] = {}
        if _EXTRACT_CACHE_PATH.exists():
            rows = json.loads(_EXTRACT_CACHE_PATH.read_text(encoding="utf-8"))
        rows[_extract_cache_key(markdown)] = tech_stack
        _EXTRACT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _EXTRACT_CACHE_PATH.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 -- never let cache bookkeeping break an approval
        logger.warning("tech-stack extract cache write failed", exc_info=True)


async def _extract_tech_stack(
    thread_id: str, markdown: str, provider: SandboxProvider, chat_provider: str, run_id: str
) -> dict[str, Any]:
    """One-shot structured extraction of the TechStack schema from already-human-approved
    markdown -- no repo exploration, no clarification loop, distinct "extract" role so this never
    shares (and clobbers) the draft session's own cached conversation (get_chat_model_for_thread's
    session cache is keyed by (thread_id, stage, role)).

    `chat_provider` (this thread's pinned org provider, "claude"/"copilot") is threaded in from
    resolve_tech_stack_submission's own `state["provider"]` -- named distinctly from `provider`
    (the pre-existing SandboxProvider connection object this function already took) to avoid
    colliding with it, same disambiguation chat_model.read_skill_invocations uses. `run_id`
    (Task 3b, Part 2 Ruling 10 fix-round-3) is threaded in the same way, from the same caller's
    `state["run_id"]` -- this function has no `state` of its own, same reason `chat_provider`
    isn't just read here directly."""
    model = get_chat_model_for_thread(
        thread_id,
        "tech-stack",
        "extract",
        provider=chat_provider,
        run_id=run_id,
        model_name=model_config.get_model_name("tech-stack", "extract", chat_provider),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=workflow_config.READ_ONLY_AVAILABLE_TOOLS,
    )
    response = await ainvoke_structured(
        model, [SystemMessage(content=_TECH_STACK_EXTRACT_PROMPT), HumanMessage(content=markdown)], TechStack,
        # Task 5 fix #2: cheap insurance against a transient failure taking this one-shot, no-retry-
        # loop extraction all the way down to the typed-absent fallback -- bumped from
        # ainvoke_structured's own default of 3, both for the normal extraction call and for
        # hydrate_tech_stack_from_repo_file's fix #4 repair retry (this is their only shared call).
        max_attempts=5,
        # Task 7: a worked example of the current TechStack shape (dotnet as one object,
        # convention_roots as a list, auth_kind/config_inventory included) -- this call has no
        # StageSpec of its own to carry draft_example, so the example is passed directly.
        example=TECH_STACK_EXTRACT_EXAMPLE,
    )
    return response.model_dump(mode="json")


def _extraction_failed_tech_stack(markdown: str) -> dict[str, Any]:
    """Task 5 fix #2: the extract-except fallback, factored out of resolve_tech_stack_submission so
    every LLM-derived field becomes an explicit typed-absence marker (Task 2's PresenceList/
    DotnetStatus shape) instead of the old bare `TechStack(summary=...)` -- which, now that those
    fields are required with no default, would itself raise ValidationError right here in the
    except block instead of degrading gracefully.

    Constructed via the actual TechStack class (not a hand-built dict) so Pydantic's own validators
    catch a shape mistake immediately, matching _extract_tech_stack's own model_dump(mode="json")
    pattern. auth_kind has no "extraction failed" option in its Literal -- "none" is the safe,
    conservative default, and fix #1's merge (which runs unconditionally afterward on every settle
    path, see _settle_tech_stack) still correctly overrides it with the deterministically-scanned
    value, so nothing is actually lost here beyond the LLM-only fields this function truly cannot
    know.
    """
    absent = PresenceList(status="absent", reason="extraction failed")
    return TechStack(
        summary=markdown.strip()[:500] or "(not extracted)",
        languages=absent,
        frameworks=absent,
        package_managers=absent,
        testing_frameworks=absent,
        conventions=absent,
        dotnet=DotnetStatus(status="not_detected", reason="extraction failed"),
        convention_roots=[],
        conventions_applied=[],
        auth_kind="none",
        config_inventory=absent,
    ).model_dump(mode="json")


async def _settle_tech_stack(
    thread_id: str, tech_stack: dict[str, Any], provider: SandboxProvider
) -> dict[str, Any]:
    """Task 5 fix #1's shared merge-then-persist tail for resolve_tech_stack_submission's TWO
    settle-and-persist points (a cache hit, and fresh-extraction/extraction-failure-fallback):
    scans this repo's own config_inventory deterministically and unions it into `tech_stack` (via
    _merge_config_signals) BEFORE either path writes tech-stack.approved.json.

    Putting this merge in only one of the two callers would leave the cache-hit path -- precisely
    the "unedited canned catalog pick" case the extraction cache exists for -- silently shipping
    without the auth/config union, reintroducing the exact bug this fix targets on that path; both
    of resolve_tech_stack_submission's return statements now go through this one function instead.
    """
    try:
        scanned_auth, scanned_keys = await config_inventory.inventory(provider, thread_id)
    except Exception:  # noqa: BLE001 -- the scan is best-effort, never a reason to lose the approval
        logger.warning("config inventory scan failed for thread_id=%s", thread_id, exc_info=True)
        scanned_auth, scanned_keys = "none", []
    merged, _changed = _merge_config_signals(tech_stack, scanned_auth, scanned_keys)
    await repo_files.write_repo_file(
        provider, thread_id, TECH_STACK_APPROVED_JSON_PATH, json.dumps(merged, indent=2) + "\n"
    )
    await git_ops.commit_paths(
        provider, thread_id, [TECH_STACK_APPROVED_JSON_PATH], "ai-dev-workflow: tech stack extracted"
    )
    return merged


def _select_tech_stack_markdown(resume_value: Any, draft: dict[str, Any] | None) -> str:
    """Pure decision behind resolve_tech_stack_submission: what text actually gets saved.

    The tab's Submit resolves with `{"markdown": edited_text}` in the normal case; a bare
    `resume_value` (e.g. headless auto-approve, `Command(resume=True)`) falls back to whatever
    the stage already had as its draft -- which itself is one of two shapes depending on how the
    draft was produced: `{"markdown": ...}` from prefill_tech_stack_from_repo_file (file already
    existed), or a raw TechStack-shaped dict from the LLM draft path (needs rendering to text)."""
    draft = draft or {}
    if isinstance(draft, dict) and "markdown" in draft:
        fallback = draft.get("markdown")
    else:
        fallback = render_tech_stack_markdown(draft)
    markdown = (resume_value.get("markdown") if isinstance(resume_value, dict) else "") or fallback or ""
    return markdown


async def resolve_tech_stack_submission(
    thread_id: str, resume_value: Any, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.resolve_from_interrupt for the tech-stack stage: the Tech Stack tab's Submit
    button resolves with `{"markdown": <edited text>}` -- this is what actually gets that edited
    text saved and turned into the structured TechStack every downstream gate reads, since
    make_gate_node's default behavior (approve stage["draft"] verbatim) has no way to see it.
    """
    stage = state["stages"]["tech-stack"]
    markdown = _select_tech_stack_markdown(resume_value, stage.get("draft"))

    # Written FIRST, unconditionally: the human's approved text must never be lost even if the
    # extraction pass below fails outright -- ainvoke_structured raises after exhausting its own
    # retries, and an uncaught exception here would otherwise crash the whole graph run through
    # make_gate_node, losing this write along with it.
    await repo_files.write_repo_file(provider, thread_id, TECH_STACK_MD_PATH, markdown)
    await git_ops.commit_paths(provider, thread_id, [TECH_STACK_MD_PATH], "ai-dev-workflow: tech stack saved")

    # Extraction cache (backlog item 1, 2026-08-31): an UNEDITED canned catalog pick is the same
    # markdown bytes every session, and its structured extraction is deterministic -- one agent-host
    # json file keyed by content hash skips the LLM call for every repeat. Edited text misses and
    # extracts normally; every cache failure falls through to the real call. Note the cache stores
    # the PURE extraction result only (before _settle_tech_stack's config_inventory union below) --
    # it's shared across every thread that produces this same markdown, so a merge keyed to one
    # thread's own repo scan must never be written back into it.
    cached = _extract_cache_get(markdown)
    if cached is not None:
        logger.info("tech-stack extraction served from cache for thread_id=%s", thread_id)
        return await _settle_tech_stack(thread_id, cached, provider)

    try:
        tech_stack = await _extract_tech_stack(
            thread_id, markdown, provider, state["provider"], state.get("run_id", "unknown")
        )
        _extract_cache_put(markdown, tech_stack)
    except Exception:
        logger.exception(
            "tech-stack extraction failed for thread_id=%s; approving with typed-absent LLM fields "
            "instead of failing the run -- a human already reviewed and approved the markdown, "
            "which is what matters; a failed best-effort JSON extraction is a quality loss, not a "
            "reason to crash. Every downstream consumer tolerates a sparse TechStack.",
            thread_id,
        )
        try:
            await repo_files.append_ledger_entry(
                provider, thread_id,
                {
                    "stage": "tech-stack", "node": "extract",
                    "error": "extraction failed; approved with typed-absent fallback",
                },
            )
        except Exception:  # noqa: BLE001 -- ledger bookkeeping must never break the approval either
            logger.warning(
                "failed to append tech-stack extraction-failure ledger entry for thread_id=%s",
                thread_id, exc_info=True,
            )
        tech_stack = _extraction_failed_tech_stack(markdown)

    return await _settle_tech_stack(thread_id, tech_stack, provider)


# ── Ecosystem convention table ────────────────────────────────────────────────────────────────
#
# The one idea this table encodes: an LLM writes better code when a *deterministic* tool tells it
# it is wrong. .NET has had that since the beginning (analyzers + TreatWarningsAsErrors => a
# violation is a compile error). Every entry below is the same trick for another ecosystem, and
# agent/src/rebuild.py is where each one is actually made fatal.

# Every template this module writes carries this line in its header. It, not a checksum list, is
# how an existing file is recognised as *ours* and therefore safe to replace on a version bump --
# a human-authored config never contains it, so a repo's own file is never clobbered.
_TEMPLATE_SENTINEL = "DO NOT MODIFY THIS FILE DURING FEATURE WORK"
_STAMP_RE = re.compile(r"aidw-template-version:\s*(\d+)")

@dataclass(frozen=True)
class _Ecosystem:
    key: str
    files: tuple[tuple[str, str], ...]
    """(bundled template path, destination filename relative to this ecosystem's root)."""
    guidance: str
    """AGENTS.md paragraph body, appended once and guarded by _guidance_sentinel(key)."""


_DOTNET = _Ecosystem(
    key="dotnet",
    files=(("dotnet/Directory.Build.props", "Directory.Build.props"),),
    guidance="""## .NET

This repository uses .NET. A shared `Directory.Build.props` has been placed at the solution
root -- see that file's own header comment for the conventions it enforces and the process for
changing it. Analyzer violations are build errors, not warnings: code that trips one does not
compile.
""",
)

# Node writes NOTHING into the target repo. The lint toolchain is baked into the sandbox image
# at /opt/aidw/lint (config + its own node_modules) and the rebuild gate runs it from there --
# installing lint devDependencies into a repo once re-resolved a pnpm workspace's peer graph,
# forked drizzle-orm into two incompatible instances, and broke the repo's own build. A repo
# with its own ESLint setup keeps its own lint contract (the gate defers, see rebuild.py).
_NODE = _Ecosystem(
    key="node",
    files=(),
    guidance="""## JavaScript / TypeScript

If this repository has no ESLint setup of its own, the pipeline lints it with a pipeline-owned
config (baked into the CI sandbox, never written into this repo) at `eslint --max-warnings=0`
strictness, plus a strict `tsc --noEmit` -- a lint warning or a type error is a build failure,
not advice. A repository that ships its own ESLint config keeps its own lint contract instead.
""",
)

_PYTHON = _Ecosystem(
    key="python",
    files=(("python/ruff.toml", "ruff.toml"), ("python/mypy.ini", "mypy.ini")),
    guidance="""## Python

This repository has a shared `ruff.toml` and `mypy.ini` -- see those files' own header comments
for the conventions they enforce and the process for changing them. The build runs `ruff check .`
and `mypy .`, so a lint or typing violation is a build failure, not advice.
""",
)


def _template_version(text: str) -> int:
    """Version stamped into a template's header, or 0 for one of ours that predates stamping."""
    match = _STAMP_RE.search(text)
    return int(match.group(1)) if match else 0


def _is_ours(text: str) -> bool:
    return _TEMPLATE_SENTINEL in text


def _join_root(root: str, name: str) -> str:
    """Root "" means the repo root itself. Kept as an explicit branch because
    repo_files.validate_repo_relative_path rejects both "" and a leading "/"."""
    return f"{root}/{name}" if root else name


def _root_is_safe(root: str) -> bool:
    """A root is model-reported, and repo_files' path allowlist rejects traversal, absolute paths and
    shell metacharacters (spaces ARE allowed -- see repo_files._SAFE_PATH_RE for why). An unusable
    root is skipped with a recorded reason rather than raised on -- a ValueError here would otherwise
    propagate out of the hook and take the whole run down."""
    if root == "":
        return True
    try:
        repo_files.validate_repo_relative_path(root)
    except ValueError:
        return False
    return True


def _applicable_ecosystems(tech_stack: dict[str, Any]) -> list[tuple[_Ecosystem, str]]:
    """(ecosystem, repo-relative root) for every ecosystem this repo should get conventions for.

    Pure function on the audited TechStack dict, which is what makes it the one thing in this
    module with a runnable self-check (see __main__ at the bottom). Roots come from the model's
    `convention_roots` list where an ecosystem entry is status="present", falling back to the repo
    root -- except .NET, which keeps its own top-level `dotnet` field because eight other modules
    read it.
    """
    languages = [str(item).lower() for item in tech_stack_signals.presence_values(tech_stack, "languages")]
    applicable: list[tuple[_Ecosystem, str]] = []

    if tech_stack_signals.dotnet_detected(tech_stack):
        solution_root = tech_stack_signals.dotnet_solution_root(tech_stack)
        # None means the detector had low confidence. Skipping beats guessing: MSBuild discovers
        # props by walking *up* from each project, so a wrongly-placed file silently misses
        # projects or pulls in unrelated directories.
        if solution_root is not None:
            applicable.append((_DOTNET, str(solution_root)))

    if any(language in languages for language in ("typescript", "javascript")):
        applicable.append((_NODE, str(tech_stack_signals.convention_root(tech_stack, "node") or "")))

    if "python" in languages:
        applicable.append((_PYTHON, str(tech_stack_signals.convention_root(tech_stack, "python") or "")))

    return [(eco, root) for eco, root in applicable if _root_is_safe(root)]


async def _write_if_outdated(
    provider: SandboxProvider, thread_id: str, path: str, template_path: str
) -> str | None:
    """Writes a bundled template to `path` when it is absent or an older version of our own file.
    Returns the path when written, None when nothing needed doing.

    A file that exists and does not carry _TEMPLATE_SENTINEL is treated as human-authored and left
    strictly alone -- that check, not the stamp, is what makes overwriting safe.
    """
    content = template_loader.load_template(template_path)
    existing = await repo_files.read_repo_file(provider, thread_id, path)
    if existing is not None:
        if not _is_ours(existing):
            return None
        if _template_version(existing) >= _template_version(content):
            return None
    await repo_files.write_repo_file(provider, thread_id, path, content)
    return path


async def apply_stack_conventions(
    thread_id: str, tech_stack: dict[str, Any], _state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_approve_hook for the tech-stack stage: writes each detected ecosystem's
    build-blocking convention files and appends one idempotent AGENTS.md paragraph per ecosystem.

    Deterministic throughout -- driven by the approved TechStack object's own fields, never by the
    skill, which is read-only by contract and never writes a file itself.

    Runs on the *approved* path rather than post-audit specifically so it still fires for a repo
    that hydrates its tech stack from disk and skips drafting entirely (see StageSpec's own
    post_approve_hook docstring); it must therefore be a no-op when everything is already current.

    Every ecosystem is applied inside its own try/except: one bad root or one npm failure must not
    cost the others their conventions, and must never take down the run.
    """
    # Seed the settings-page config table with this run's detected keys (value-empty rows the user
    # fills in). Additive-only + idempotent -- merge_detected never clobbers a user value -- so it
    # is safe on every run, including hydration-skipped ones. Best-effort: config seeding must never
    # fail the conventions hook.
    detected_keys = tech_stack_signals.presence_values(tech_stack, "config_inventory")
    if detected_keys:
        try:
            sess = await session_store.get_session(thread_id)
            if sess is not None:
                await repo_test_config.merge_detected(sess["owner"], sess["repo"], list(detected_keys))
        except Exception:  # noqa: BLE001
            logger.warning("could not seed repo_test_config for thread_id=%s", thread_id, exc_info=True)

    written_paths: list[str] = []
    outcomes: dict[str, Any] = {}

    for ecosystem, root in _applicable_ecosystems(tech_stack):
        try:
            # Config-file templates only (dotnet/python; node's files tuple is empty -- its lint
            # toolchain ships in the sandbox image and never touches the repo). No ecosystem
            # installs packages into the target repo anymore.
            paths = []
            for template_path, filename in ecosystem.files:
                written = await _write_if_outdated(
                    provider, thread_id, _join_root(root, filename), template_path
                )
                if written:
                    paths.append(written)
            outcome = {"wrote": paths} if paths else {"skipped": "already current"}
            written_paths.extend(paths)
            outcomes[ecosystem.key] = outcome

            agents_md = await repo_files.read_repo_file(provider, thread_id, "AGENTS.md")
            sentinel = _guidance_sentinel(ecosystem.key)
            if agents_md is not None and sentinel not in agents_md:
                await repo_files.write_repo_file(
                    provider,
                    thread_id,
                    "AGENTS.md",
                    f"{agents_md.rstrip()}\n\n{sentinel}\n{ecosystem.guidance}",
                )
                written_paths.append("AGENTS.md")
        except Exception as exc:  # noqa: BLE001 -- one ecosystem's failure is not the run's failure
            logger.warning(
                "Failed to apply %s conventions for thread_id=%s", ecosystem.key, thread_id, exc_info=True
            )
            outcomes[ecosystem.key] = {"error": str(exc)[:500]}

    # Task 5 fix #3: persist which ecosystems' conventions actually applied. This hook already
    # fires on every hydration short-circuit AND on the resume path (graph.py's two
    # `_run_post_approve_hook` call sites for an "already approved" tech-stack stage), so this
    # self-heals every already-onboarded repo's permanently-`[]` conventions_applied the next time
    # its pipeline runs -- no separate migration script needed. Read-modify-write against the
    # sidecar ON DISK (not the `tech_stack` argument this hook was handed), same pattern
    # hydrate_tech_stack_from_repo_file's own backfill uses -- `tech_stack` can be a checkpointed
    # value from a prior lap on the resume path, so writing through it directly would risk
    # persisting stale content over whatever's actually on disk.
    applied = sorted(key for key, outcome in outcomes.items() if "error" not in outcome)
    try:
        current_raw = await repo_files.read_repo_file(provider, thread_id, TECH_STACK_APPROVED_JSON_PATH)
        if current_raw is not None:
            current = json.loads(current_raw)
            if isinstance(current, dict) and current.get("conventions_applied") != applied:
                current["conventions_applied"] = applied
                await repo_files.write_repo_file(
                    provider, thread_id, TECH_STACK_APPROVED_JSON_PATH, json.dumps(current, indent=2) + "\n",
                )
    except Exception:  # noqa: BLE001 -- best-effort bookkeeping, never a reason to skip the ledger/commit below
        logger.warning("failed to persist conventions_applied for thread_id=%s", thread_id, exc_info=True)

    if not written_paths:
        # Recorded even when nothing was written: "we looked and everything was current" is a
        # different fact from "this hook never ran," and only the ledger can tell them apart.
        await repo_files.append_ledger_entry(
            provider, thread_id, {"stage": "tech-stack", "node": "post_approve_hook", "outcomes": outcomes}
        )
        return

    deduped = list(dict.fromkeys(written_paths))
    await repo_files.append_ledger_entry(
        provider,
        thread_id,
        {"stage": "tech-stack", "node": "post_approve_hook", "wrote": deduped, "outcomes": outcomes},
    )
    await git_ops.commit_paths(
        provider, thread_id, deduped, f"ai-dev-workflow: apply {'/'.join(sorted(outcomes))} conventions"
    )


if __name__ == "__main__":  # pragma: no cover -- `cd agent && python -m src.preflight_nodes`
    # The one runnable check this module earns: _applicable_ecosystems is the only non-trivial
    # pure logic here, and every failure mode it has is a real bug (a wrong path silently misses
    # projects; an unsafe root used to be able to kill the run).
    def _langs(*values: str) -> dict[str, Any]:
        return {"status": "present", "values": list(values)} if values else {"status": "absent", "reason": "test fixture"}

    def _dotnet(solution_root: str | None) -> dict[str, Any]:
        # solution_root=None is the "detected, low confidence" case; "" is a legitimate
        # repo-root-itself value. DotnetStatus requires a reason whenever root is blank.
        return {"status": "detected", "solution_root": solution_root, "reason": "" if solution_root else "test fixture"}

    def _roots(**by_ecosystem: str) -> list[dict[str, Any]]:
        return [{"ecosystem": eco, "status": "present", "root": root, "reason": ""} for eco, root in by_ecosystem.items()]

    def _full(**overrides: Any) -> dict[str, Any]:
        # A full, valid new-shape TechStack dict. _applicable_ecosystems now routes every read
        # through tech_stack_signals' shared helpers, which normalize legacy shape via
        # TechStack.model_validate -- that validates the WHOLE object, so a partial dict (e.g. just
        # {"dotnet": ...}) fails validation and silently falls back to "nothing detected" instead of
        # exercising the field under test. Every real caller already hands these helpers a full
        # TechStack dump, never a hand-picked subset -- this fixture matches that.
        base: dict[str, Any] = {
            "summary": "s",
            "languages": {"status": "absent", "reason": "test fixture"},
            "frameworks": {"status": "absent", "reason": "test fixture"},
            "package_managers": {"status": "absent", "reason": "test fixture"},
            "testing_frameworks": {"status": "absent", "reason": "test fixture"},
            "conventions": {"status": "absent", "reason": "test fixture"},
            "dotnet": {"status": "not_detected", "reason": "test fixture"},
            "convention_roots": [],
            "conventions_applied": [],
            "auth_kind": "none",
            "config_inventory": {"status": "absent", "reason": "test fixture"},
        }
        return {**base, **overrides}

    assert _applicable_ecosystems({}) == []
    assert _applicable_ecosystems(_full(languages=_langs("Rust"))) == []

    dotnet_only = _applicable_ecosystems(_full(dotnet=_dotnet("src")))
    assert [(e.key, r) for e, r in dotnet_only] == [("dotnet", "src")], dotnet_only
    assert _join_root("src", "Directory.Build.props") == "src/Directory.Build.props"

    # Repo root: "" is a legal solution root but an illegal repo-relative path -- the join, not
    # the validator, is what has to special-case it.
    root_level = _applicable_ecosystems(_full(dotnet=_dotnet("")))
    assert [(e.key, r) for e, r in root_level] == [("dotnet", "")], root_level
    assert _join_root("", "Directory.Build.props") == "Directory.Build.props"

    # Low confidence (None) is not the same as the repo root ("").
    assert _applicable_ecosystems(_full(dotnet=_dotnet(None))) == []

    # A root the path allowlist rejects is dropped, not raised on. Traversal, absolute paths and
    # shell metacharacters are all rejected -- the cases that actually matter, since these roots are
    # model-reported and end up inside container shell commands.
    def _py_roots(root: str) -> list[tuple[str, str]]:
        applicable = _applicable_ecosystems(_full(languages=_langs("Python"), convention_roots=_roots(python=root)))
        return [(eco.key, resolved) for eco, resolved in applicable]

    assert _py_roots("../etc") == []
    assert _py_roots("/etc") == []
    assert _py_roots("src; rm -rf /") == []
    assert _py_roots("a$(whoami)") == []
    # Spaces are NOT rejected, deliberately -- repo_files._SAFE_PATH_RE was widened for a real
    # generated Next.js chunk path, and shlex.quote (not the character class) is what makes these
    # shell-safe. This assertion exists so nobody "fixes" the class back and breaks that stage.
    assert _py_roots("My App") == [("python", "My App")]

    polyglot = _applicable_ecosystems(
        _full(
            languages=_langs("TypeScript", "Python", "C#"),
            dotnet=_dotnet("src"),
            convention_roots=_roots(node="web", python="api"),
        )
    )
    assert [(e.key, r) for e, r in polyglot] == [("dotnet", "src"), ("node", "web"), ("python", "api")], polyglot

    # An ecosystem recorded status="absent" must fall back to the repo root, same as no entry at
    # all -- a real repo whose convention_roots audit found no node.js work still needs "" (root
    # level) if the languages heuristic alone flags it (defensive: the two should agree in practice).
    absent_only = _applicable_ecosystems(
        _full(
            languages=_langs("TypeScript"),
            convention_roots=[{"ecosystem": "node", "status": "absent", "root": "", "reason": "no package.json"}],
        )
    )
    assert [(e.key, r) for e, r in absent_only] == [("node", "")], absent_only

    # Genuinely LEGACY on-disk shape (old dotnet_detected/dotnet_solution_root pair, dict-shaped
    # convention_roots) -- what every already-onboarded repo's tech-stack.approved.json actually
    # contains until the migration ships. Must normalize via load_tech_stack, not silently drop
    # the ecosystems this repo actually has.
    legacy = {
        "summary": "legacy sidecar",
        "languages": ["TypeScript", "Python"],
        "frameworks": [],
        "package_managers": [],
        "testing_frameworks": [],
        "conventions": [],
        "dotnet_detected": False,
        "dotnet_solution_root": None,
        "convention_roots": {"node": "web", "python": "api"},
        "conventions_applied": [],
        "auth_kind": "none",
        "config_inventory": [],
    }
    legacy_applicable = _applicable_ecosystems(legacy)
    assert [(e.key, r) for e, r in legacy_applicable] == [("node", "web"), ("python", "api")], legacy_applicable

    # Node writes nothing into the repo: no template files, only the AGENTS.md paragraph.
    assert _NODE.files == ()

    assert _session_title("  \nAdd login\nsecond line", "abc123") == "Add login"
    assert _session_title("", "abc123") == "(untitled run abc123)"

    # Manifest schema: a full manifest round-trips, unknown extra keys survive (extra="allow"),
    # a wrong-typed app_check fails validation (the log-and-write-anyway is update_manifest's job).
    good = {
        "onboarded": True, "run_id": "r1", "toolchain": {"image": "x"}, "future_key": {"kept": True},
        "app_check": {"suitable": True, "evidence_fingerprint": "sha256:x", "apps": [
            {"path": ".", "name": "app", "app_class": "web", "runtime": "node22", "start_command": "npm run dev"}
        ]},
        "test_command": "npx vitest run", "coverage_commands": [{"command": "x", "artifact": "y", "format": "istanbul"}],
    }
    validated = Manifest.model_validate(good)
    assert validated.app_check is not None and validated.app_check.apps[0].name == "app"
    assert validated.model_extra.get("future_key") == {"kept": True}
    try:
        Manifest.model_validate({"app_check": {"apps": "not-a-list"}})
        raise AssertionError("wrong-typed app_check must fail validation")
    except ValidationError:
        pass

    # candidates_to_apps: pre-LLM candidates map to valid DiscoveredApp dicts, one per path.
    from .app_discovery import candidates_to_apps
    apps = candidates_to_apps([
        {"path": "src/Api", "source": "src/Api/Api.csproj", "likely_class": "api", "runtime": "dotnet",
         "marker": "Sdk=Web", "start_command": "dotnet run --project src/Api", "port": 5001},
        {"path": "src/Api", "source": "src/Api/Program.cs", "likely_class": "api", "runtime": "dotnet", "marker": "web host"},
        {"path": ".", "likely_class": "not-a-class", "runtime": "node"},
    ])
    assert len(apps) == 2 and apps[0]["name"] == "Api" and apps[0]["start_command"] == "dotnet run --project src/Api"
    assert apps[1]["app_class"] == "unknown" and apps[1]["name"] == "app"
    assert ManifestAppCheck.model_validate({"apps": apps}).apps[0].port == 5001

    assert _session_title("x" * 100, "abc123") == "x" * 80

    # _select_tech_stack_markdown: the tab's edited text wins; a bare (non-dict) resume value
    # falls back to whatever the stage already had, rendering a raw TechStack draft to markdown
    # but using a prefilled {"markdown": ...} draft's text verbatim.
    assert _select_tech_stack_markdown({"markdown": "edited"}, {"markdown": "prefilled"}) == "edited"
    assert _select_tech_stack_markdown(True, {"markdown": "prefilled"}) == "prefilled"
    assert _select_tech_stack_markdown(True, {"summary": "S"}) == render_tech_stack_markdown({"summary": "S"})
    assert _select_tech_stack_markdown(True, None) == render_tech_stack_markdown({})

    assert _template_version("aidw-template-version: 7") == 7
    assert _template_version("no stamp here") == 0
    assert _is_ours("... DO NOT MODIFY THIS FILE DURING FEATURE WORK ...")
    assert not _is_ours("# someone's own eslint config")

    # _resolve_ticket_tech_stack_markdown (Phase E audit B-Critical-1): the New Ticket form's
    # picker selection must actually resolve to something, not just round-trip through the DB
    # unread -- this is the pure decision the write-only-picker fix rests on.
    fake_catalog = [{"id": "nextjs-fastapi", "title": "Next.js + FastAPI", "markdown": "# Next.js + FastAPI\n..."}]
    assert _resolve_ticket_tech_stack_markdown("nextjs-fastapi", None, fake_catalog) == "# Next.js + FastAPI\n..."
    # An id that no longer names a real catalog entry (e.g. the templates directory changed since
    # the ticket was filed) must fall through to None, not raise or fabricate content.
    assert _resolve_ticket_tech_stack_markdown("retired-stack-id", None, fake_catalog) is None
    # Free text is a real selection too, prefilled but clearly labeled as the user's own words.
    described = _resolve_ticket_tech_stack_markdown(None, "Rails + Postgres, no frontend yet", fake_catalog)
    assert described is not None and "Rails + Postgres" in described and "user" in described.lower(), described
    # A catalog id wins over free text when a project somehow has both.
    assert _resolve_ticket_tech_stack_markdown("nextjs-fastapi", "ignored text", fake_catalog) == "# Next.js + FastAPI\n..."
    # Neither field set (a Connect-Repository project, or a "+ New Project" ticket that picked
    # neither) -- nothing to prefill, the caller falls through to fresh detection unchanged.
    assert _resolve_ticket_tech_stack_markdown(None, None, fake_catalog) is None
    assert _resolve_ticket_tech_stack_markdown("", "", fake_catalog) is None

    # --- Task 5 fixes -------------------------------------------------------------------------
    # _merge_config_signals / _extraction_failed_tech_stack / _looks_like_extraction_failure are
    # pure and checked directly; resolve_tech_stack_submission / hydrate_tech_stack_from_repo_file
    # / apply_stack_conventions are exercised for real against a minimal in-memory fake
    # SandboxProvider (config_inventory.inventory and _extract_tech_stack -- the two genuinely
    # I/O-bound/LLM-bound calls -- are monkeypatched around their own real work, same convention
    # graph.py's own self-check uses for _persist_if_sandboxed/_run_post_approve_hook).
    import asyncio

    unchanged, changed = _merge_config_signals(
        _full(auth_kind="entra", config_inventory={"status": "present", "values": ["AzureAd:TenantId"], "reason": ""}),
        "entra", ["AzureAd:TenantId"],
    )
    assert changed is False and unchanged["auth_kind"] == "entra"
    merged, changed = _merge_config_signals(
        _full(auth_kind="none", config_inventory={"status": "absent", "values": [], "reason": "test fixture"}),
        "google", ["GOOG_CLIENT_ID"],
    )
    assert changed is True and merged["auth_kind"] == "google"
    assert merged["config_inventory"] == {"status": "present", "values": ["GOOG_CLIENT_ID"], "reason": ""}
    # Existing wins over scanned once it's already something specific; keys still union.
    kept, changed = _merge_config_signals(
        _full(auth_kind="custom", config_inventory={"status": "present", "values": ["A"], "reason": ""}),
        "google", ["A", "B"],
    )
    assert kept["auth_kind"] == "custom" and kept["config_inventory"]["values"] == ["A", "B"] and changed is True

    # _extraction_failed_tech_stack (fix #2): must itself be schema-valid -- Task 2's typed fields
    # are all required, so the OLD bare `TechStack(summary=...)` fallback would now raise
    # ValidationError right here instead of degrading gracefully.
    failed = _extraction_failed_tech_stack("# Tech Stack\n\nSome markdown that never got extracted.")
    TechStack.model_validate(failed)
    assert failed["auth_kind"] == "none"
    assert failed["languages"] == {"status": "absent", "values": [], "reason": "extraction failed"}
    assert failed["dotnet"] == {"status": "not_detected", "solution_root": None, "reason": "extraction failed"}
    assert failed["convention_roots"] == [] and failed["conventions_applied"] == []

    # _looks_like_extraction_failure (fix #4): the fallback's own signature is caught...
    assert _looks_like_extraction_failure(tech_stack_signals.load_tech_stack(failed)) is True
    # ...a genuine LLM verdict of "empty repo" -- same all-absent shape, but its OWN specific
    # reasons, not the generic sentinel -- must NOT be flagged for repair.
    genuinely_empty = _full(
        summary="An empty repository with no application code yet.",
        languages={"status": "absent", "values": [], "reason": "no source files found in the repository"},
        frameworks={"status": "absent", "values": [], "reason": "no source files found in the repository"},
        package_managers={"status": "absent", "values": [], "reason": "no source files found in the repository"},
        testing_frameworks={"status": "absent", "values": [], "reason": "no source files found in the repository"},
        conventions={"status": "absent", "values": [], "reason": "no source files found in the repository"},
        dotnet={"status": "not_detected", "reason": "no .csproj or .sln files found"},
    )
    assert _looks_like_extraction_failure(tech_stack_signals.load_tech_stack(genuinely_empty)) is False
    # A well-formed summary alone must not be flagged, even if fields happen to share the generic
    # reason text -- the summary check is the primary signal.
    well_formed_summary = {**failed, "summary": "A small Node/Express API with no auth."}
    assert _looks_like_extraction_failure(tech_stack_signals.load_tech_stack(well_formed_summary)) is False
    # A genuinely ancient pre-typed-absence sidecar coerces to the same reason text via
    # PresenceList/DotnetStatus's own legacy coercion -- also caught.
    legacy_failure_shape = {
        "summary": "#" * 500,
        "languages": [], "frameworks": [], "package_managers": [], "testing_frameworks": [], "conventions": [],
        "dotnet_detected": False, "dotnet_solution_root": None,
        "convention_roots": {}, "conventions_applied": [], "auth_kind": "none", "config_inventory": [],
    }
    assert _looks_like_extraction_failure(tech_stack_signals.load_tech_stack(legacy_failure_shape)) is True

    class _FakeExecResult:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

        @property
        def ok(self) -> bool:
            return self.returncode == 0

    class _FakeSandboxProvider:
        """Minimal in-memory SandboxProvider stand-in, self-check only: implements just enough of
        exec_in_sandbox's shell surface for repo_files.read/write_repo_file and
        append_ledger_entry to run against a dict instead of a real container (same duck-typing
        convention chat_model.py's own self-check _FakeSandboxProvider uses). Anything else
        (git add/commit, mkdir) is accepted as a no-op success -- only reads/writes are tracked."""

        def __init__(self, files: dict[str, str] | None = None):
            self.files: dict[str, str] = dict(files or {})

        async def exec_in_sandbox(self, thread_id: str, command: str):  # noqa: ARG002
            import base64 as _b64

            cat_match = re.search(r"cat (\S+) 2>/dev/null", command)
            if cat_match:
                path = cat_match.group(1)
                if path in self.files:
                    return _FakeExecResult(0, self.files[path])
                return _FakeExecResult(1, "", "no such file")
            write_match = re.search(r"echo (\S+) \| base64 -d (>>?) (\S+)$", command)
            if write_match:
                encoded, mode, path = write_match.groups()
                decoded = _b64.b64decode(encoded).decode("utf-8")
                self.files[path] = (self.files.get(path, "") + decoded) if mode == ">>" else decoded
            return _FakeExecResult(0)

    # resolve_tech_stack_submission, scenario A: the CACHE-HIT path must ALSO merge (fix #1) --
    # this is exactly the "unedited canned catalog pick" case the extraction cache exists for, and
    # the bug this fix targets was this path silently shipping without the auth/config union.
    cached_stack = _full(
        summary="A small Node/Express API.",
        languages={"status": "present", "values": ["TypeScript"], "reason": ""},
        auth_kind="none",
        config_inventory={"status": "absent", "values": [], "reason": "test fixture"},
    )
    real_extract_cache_get = _extract_cache_get
    real_inventory = config_inventory.inventory

    async def _fake_inventory_entra(provider, thread_id):  # noqa: ANN001, ARG001
        return "entra", ["AzureAd:TenantId"]

    globals()["_extract_cache_get"] = lambda markdown: dict(cached_stack)
    config_inventory.inventory = _fake_inventory_entra
    try:
        fake_provider_a = _FakeSandboxProvider()
        result_a = asyncio.run(
            resolve_tech_stack_submission(
                "demo-thread-a", {"markdown": "# Tech Stack\n\nUnedited canned catalog pick."},
                {"stages": {"tech-stack": {"draft": {}}}, "provider": "claude", "run_id": "demo-run-a"},
                fake_provider_a,
            )
        )
    finally:
        globals()["_extract_cache_get"] = real_extract_cache_get
        config_inventory.inventory = real_inventory

    assert result_a is not None
    assert result_a["auth_kind"] == "entra", "cache-hit path must merge the deterministic scan (fix #1)"
    assert result_a["config_inventory"] == {"status": "present", "values": ["AzureAd:TenantId"], "reason": ""}
    assert json.loads(fake_provider_a.files[TECH_STACK_APPROVED_JSON_PATH]) == result_a

    # resolve_tech_stack_submission, scenario B: the fresh-extraction path also merges, AND the
    # extraction cache stores the PURE extraction result (before the scan is unioned in) --
    # otherwise a later cache hit for a DIFFERENT repo/thread would ship THIS repo's own scan.
    fresh_extracted = _full(
        summary="A small Python service.",
        languages={"status": "present", "values": ["Python"], "reason": ""},
        auth_kind="none",
        config_inventory={"status": "present", "values": ["FOO_KEY"], "reason": ""},
    )
    cache_put_calls: list[tuple[str, dict[str, Any]]] = []
    real_extract_cache_get = _extract_cache_get
    real_extract_cache_put = _extract_cache_put
    real_extract_tech_stack = globals()["_extract_tech_stack"]
    real_inventory = config_inventory.inventory

    async def _fake_extract_b(thread_id, markdown, provider, chat_provider, run_id):  # noqa: ANN001, ARG001
        return dict(fresh_extracted)

    async def _fake_inventory_custom(provider, thread_id):  # noqa: ANN001, ARG001
        return "custom", ["BAR_KEY"]

    globals()["_extract_cache_get"] = lambda markdown: None
    globals()["_extract_cache_put"] = lambda markdown, tech_stack: cache_put_calls.append((markdown, dict(tech_stack)))
    globals()["_extract_tech_stack"] = _fake_extract_b
    config_inventory.inventory = _fake_inventory_custom
    try:
        fake_provider_b = _FakeSandboxProvider()
        result_b = asyncio.run(
            resolve_tech_stack_submission(
                "demo-thread-b", {"markdown": "# Tech Stack\n\nFreshly typed description."},
                {"stages": {"tech-stack": {"draft": {}}}, "provider": "claude", "run_id": "demo-run-b"},
                fake_provider_b,
            )
        )
    finally:
        globals()["_extract_cache_get"] = real_extract_cache_get
        globals()["_extract_cache_put"] = real_extract_cache_put
        globals()["_extract_tech_stack"] = real_extract_tech_stack
        config_inventory.inventory = real_inventory

    assert result_b is not None
    assert result_b["auth_kind"] == "custom", "extraction path must merge the deterministic scan too (fix #1)"
    assert result_b["config_inventory"]["values"] == ["FOO_KEY", "BAR_KEY"], "union, existing keys first"
    assert len(cache_put_calls) == 1 and cache_put_calls[0][1] == fresh_extracted, (
        "the cache must store the PURE extraction, not the per-thread merged result"
    )

    # resolve_tech_stack_submission, scenario C: total extraction failure -> typed-absent fallback
    # (fix #2) still merges (fix #1) and records a ledger entry, and the human's approved markdown
    # is never lost.
    real_extract_cache_get = _extract_cache_get
    real_extract_tech_stack = globals()["_extract_tech_stack"]
    real_inventory = config_inventory.inventory

    async def _raising_extract(thread_id, markdown, provider, chat_provider, run_id):  # noqa: ANN001, ARG001
        raise RuntimeError("simulated extraction failure")

    async def _fake_inventory_google(provider, thread_id):  # noqa: ANN001, ARG001
        return "google", ["GOOG_KEY"]

    globals()["_extract_cache_get"] = lambda markdown: None
    globals()["_extract_tech_stack"] = _raising_extract
    config_inventory.inventory = _fake_inventory_google
    try:
        fake_provider_c = _FakeSandboxProvider()
        markdown_c = "# Tech Stack\n\nWill fail to extract."
        result_c = asyncio.run(
            resolve_tech_stack_submission(
                "demo-thread-c", {"markdown": markdown_c},
                {"stages": {"tech-stack": {"draft": {}}}, "provider": "claude", "run_id": "demo-run-c"},
                fake_provider_c,
            )
        )
    finally:
        globals()["_extract_cache_get"] = real_extract_cache_get
        globals()["_extract_tech_stack"] = real_extract_tech_stack
        config_inventory.inventory = real_inventory

    assert fake_provider_c.files[TECH_STACK_MD_PATH] == markdown_c, "the human's approved text must never be lost"
    TechStack.model_validate(result_c)
    assert result_c["summary"].startswith("#"), "fallback summary is raw markdown -- fix #4's repair signature"
    assert result_c["auth_kind"] == "google", "fix #1's merge still runs on the extraction-failure path"
    assert result_c["config_inventory"]["values"] == ["GOOG_KEY"]
    assert "extraction failed" in fake_provider_c.files.get(repo_files.LEDGER_PATH, ""), (
        "the extraction failure must be recorded in the ledger (fix #2)"
    )

    # hydrate_tech_stack_from_repo_file, scenario D: a sidecar carrying the extraction-failure
    # signature gets repaired via one retry against the markdown still on disk (fix #4), and the
    # repair result is itself persisted.
    markdown_d = "# Tech Stack\n\nOriginal human-approved markdown."
    bad_sidecar = _extraction_failed_tech_stack(markdown_d)
    repaired_stack = _full(
        summary="A Go microservice with no auth.",
        languages={"status": "present", "values": ["Go"], "reason": ""},
    )
    real_extract_tech_stack = globals()["_extract_tech_stack"]
    real_inventory = config_inventory.inventory

    async def _fake_extract_repair(thread_id, markdown, provider, chat_provider, run_id):  # noqa: ANN001, ARG001
        assert markdown == markdown_d, "the repair retry must read the CURRENT tech-stack.md"
        return dict(repaired_stack)

    async def _fake_inventory_none(provider, thread_id):  # noqa: ANN001, ARG001
        return "none", []

    globals()["_extract_tech_stack"] = _fake_extract_repair
    config_inventory.inventory = _fake_inventory_none
    try:
        fake_provider_d = _FakeSandboxProvider(files={
            TECH_STACK_APPROVED_JSON_PATH: json.dumps(bad_sidecar),
            TECH_STACK_MD_PATH: markdown_d,
        })
        hydrated_d = asyncio.run(
            hydrate_tech_stack_from_repo_file(
                "demo-thread-d", {"provider": "claude", "run_id": "demo-run-d"}, fake_provider_d
            )
        )
    finally:
        globals()["_extract_tech_stack"] = real_extract_tech_stack
        config_inventory.inventory = real_inventory

    assert hydrated_d is not None and hydrated_d["summary"] == repaired_stack["summary"]
    assert json.loads(fake_provider_d.files[TECH_STACK_APPROVED_JSON_PATH])["summary"] == repaired_stack["summary"]

    # hydrate_tech_stack_from_repo_file, scenario E: a genuinely empty repo's sidecar must NOT be
    # repaired -- too loose a signature would fire a needless retry and could silently overwrite a
    # real "nothing found" verdict with a fabricated one.
    real_extract_tech_stack = globals()["_extract_tech_stack"]
    real_inventory = config_inventory.inventory

    async def _fail_if_called(thread_id, markdown, provider, chat_provider, run_id):  # noqa: ANN001, ARG001
        raise AssertionError("hydrate must not retry extraction for a genuinely empty repo")

    async def _fake_inventory_none2(provider, thread_id):  # noqa: ANN001, ARG001
        return "none", []

    globals()["_extract_tech_stack"] = _fail_if_called
    config_inventory.inventory = _fake_inventory_none2
    try:
        fake_provider_e = _FakeSandboxProvider(files={
            TECH_STACK_APPROVED_JSON_PATH: json.dumps(genuinely_empty),
            TECH_STACK_MD_PATH: "# Tech Stack\n\n(never read -- no repair should trigger)",
        })
        hydrated_e = asyncio.run(
            hydrate_tech_stack_from_repo_file(
                "demo-thread-e", {"provider": "claude", "run_id": "demo-run-e"}, fake_provider_e
            )
        )
    finally:
        globals()["_extract_tech_stack"] = real_extract_tech_stack
        config_inventory.inventory = real_inventory

    assert hydrated_e is not None and hydrated_e["summary"] == genuinely_empty["summary"]

    # apply_stack_conventions, scenario F: conventions_applied self-heals on every run (fix #3),
    # including a hydration/resume short-circuit that hands this hook a checkpointed `tech_stack`
    # -- the fix reads/writes TECH_STACK_APPROVED_JSON_PATH on disk, not that argument, precisely
    # so it can't persist stale content over whatever's actually there.
    tech_stack_f = _full(languages=_langs("Python"), convention_roots=_roots(python=""))
    fake_provider_f = _FakeSandboxProvider(files={TECH_STACK_APPROVED_JSON_PATH: json.dumps(tech_stack_f)})
    asyncio.run(apply_stack_conventions("demo-thread-f", tech_stack_f, {}, fake_provider_f))
    on_disk_f = json.loads(fake_provider_f.files[TECH_STACK_APPROVED_JSON_PATH])
    assert on_disk_f["conventions_applied"] == ["python"], "fix #3: conventions_applied must self-heal"
    assert "ruff.toml" in fake_provider_f.files and "mypy.ini" in fake_provider_f.files

    print("preflight_nodes self-check: ok")
