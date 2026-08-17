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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ValidationError

from . import git_ops, model_config, repo_files, repo_scan, session_store, template_loader
from .copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from .prompt_loader import load_prompt_pair, render_prompt
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


async def _generate_session_title(thread_id: str, raw_requirements_text: str, run_id: str) -> str:
    """LLM-generated session title, shown in the session-list UI -- same get_chat_model_for_thread
    / ainvoke_structured mechanism every draft node already uses (GitHub Copilot-backed), no
    separate LLM integration. Falls back to _session_title's first-line heuristic on empty input
    or any failure -- title generation must never block scaffold."""
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
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name("session-title", "draft"),
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
coding conventions documented there. It is kept up to date automatically as this repo's stack is
analyzed. If `.ai-dev-workflow/greenfield-stack.md` exists, this repository was started greenfield
-- follow that file's stack description and repository layout when scaffolding the application.
"""


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
        title=await _generate_session_title(thread_id, raw_requirements_text, run_id),
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
    elif _TECH_STACK_SENTINEL not in agents_md and ".ai-dev-workflow/tech-stack.md" not in agents_md:
        # A human-authored AGENTS.md is never overwritten -- but leaving it entirely alone means
        # the repo's own agents never learn that .ai-dev-workflow/tech-stack.md exists, which is
        # the file every convention this pipeline detects is written into. Append the pointer
        # only, guarded by its own sentinel (and by a plain substring check, so a hand-written
        # reference to the same path also counts as "already covered").
        await repo_files.write_repo_file(
            provider, thread_id, "AGENTS.md", agents_md.rstrip() + "\n" + _TECH_STACK_PARAGRAPH
        )
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
        repo_scan.start_background_scan(thread_id, provider)
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
    raw = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "tech-stack.approved.json exists but isn't valid JSON for thread_id=%s -- treating as "
            "needing a fresh run rather than failing hard (unlike HydrationError's contract "
            "elsewhere, this file isn't the sole source of truth for GraphState itself).",
            thread_id,
        )
        return None


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
    """A root is model-reported, and repo_files' path allowlist is deliberately narrow (no spaces,
    no traversal). An unusable root is skipped with a recorded reason rather than raised on -- a
    ValueError here would otherwise propagate out of the hook and take the whole run down."""
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
    `convention_roots` map where present, falling back to the repo root -- except .NET, which
    keeps its own two long-standing fields because eight other modules read them.
    """
    roots = tech_stack.get("convention_roots") or {}
    languages = [str(item).lower() for item in (tech_stack.get("languages") or [])]
    applicable: list[tuple[_Ecosystem, str]] = []

    if tech_stack.get("dotnet_detected"):
        solution_root = tech_stack.get("dotnet_solution_root")
        # None means the detector had low confidence. Skipping beats guessing: MSBuild discovers
        # props by walking *up* from each project, so a wrongly-placed file silently misses
        # projects or pulls in unrelated directories.
        if solution_root is not None:
            applicable.append((_DOTNET, str(solution_root)))

    if any(language in languages for language in ("typescript", "javascript")):
        applicable.append((_NODE, str(roots.get("node", ""))))

    if "python" in languages:
        applicable.append((_PYTHON, str(roots.get("python", ""))))

    return [(eco, root) for eco, root in applicable if _root_is_safe(root)]


async def _detect_package_manager(provider: SandboxProvider, thread_id: str, root: str) -> str | None:
    """npm / pnpm / yarn for this root, or None when there is no package.json to install into.

    Dispatching matters: running `npm install` in a pnpm repo drops a package-lock.json next to
    pnpm-lock.yaml and commits it, which quietly corrupts that repo's dependency management.
    """
    package_json = await repo_files.read_repo_file(provider, thread_id, _join_root(root, "package.json"))
    if package_json is None:
        return None
    try:
        declared = str(json.loads(package_json).get("packageManager") or "")
    except json.JSONDecodeError:
        declared = ""
    for name in ("pnpm", "yarn", "npm"):
        if declared.startswith(name):
            return name
    for lockfile, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm")):
        if await repo_files.read_repo_file(provider, thread_id, _join_root(root, lockfile)) is not None:
            return name
    return "npm"


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
    assert _applicable_ecosystems({}) == []
    assert _applicable_ecosystems({"languages": ["Rust"]}) == []

    dotnet_only = _applicable_ecosystems({"dotnet_detected": True, "dotnet_solution_root": "src"})
    assert [(e.key, r) for e, r in dotnet_only] == [("dotnet", "src")], dotnet_only
    assert _join_root("src", "Directory.Build.props") == "src/Directory.Build.props"

    # Repo root: "" is a legal solution root but an illegal repo-relative path -- the join, not
    # the validator, is what has to special-case it.
    root_level = _applicable_ecosystems({"dotnet_detected": True, "dotnet_solution_root": ""})
    assert [(e.key, r) for e, r in root_level] == [("dotnet", "")], root_level
    assert _join_root("", "Directory.Build.props") == "Directory.Build.props"

    # Low confidence (None) is not the same as the repo root ("").
    assert _applicable_ecosystems({"dotnet_detected": True, "dotnet_solution_root": None}) == []

    # A root the path allowlist rejects is dropped, not raised on.
    assert _applicable_ecosystems({"languages": ["Python"], "convention_roots": {"python": "My App"}}) == []
    assert _applicable_ecosystems({"languages": ["Python"], "convention_roots": {"python": "../etc"}}) == []

    polyglot = _applicable_ecosystems(
        {
            "languages": ["TypeScript", "Python", "C#"],
            "dotnet_detected": True,
            "dotnet_solution_root": "src",
            "convention_roots": {"node": "web", "python": "api"},
        }
    )
    assert [(e.key, r) for e, r in polyglot] == [("dotnet", "src"), ("node", "web"), ("python", "api")], polyglot

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

    print("preflight_nodes self-check: ok")
    assert _session_title("x" * 100, "abc123") == "x" * 80

    assert _template_version("aidw-template-version: 7") == 7
    assert _template_version("no stamp here") == 0
    assert _is_ours("... DO NOT MODIFY THIS FILE DURING FEATURE WORK ...")
    assert not _is_ours("# someone's own eslint config")

    print("preflight_nodes self-check: ok")
