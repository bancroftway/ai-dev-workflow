"""P0 plain (non-LLM) nodes: scaffold_node (the true entry point of a fresh run) and the
tech-stack stage's idempotency short-circuit / post-audit hook.

Kept as a separate module from graph.py (not just more functions in it) since these are a
self-contained unit with their own imports (repo_files, template_loader, git_ops) -- matches the
existing file-size-hygiene convention of one concern per module (workflow_persistence.py,
git_ops.py, model_config.py are all similarly scoped).
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from . import git_ops, repo_files, template_loader
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

logger = logging.getLogger(__name__)

MANIFEST_PATH = ".ai-dev-workflow/manifest.json"

_DOTNET_GUIDANCE_SENTINEL = "<!-- ai-dev-workflow:dotnet-guidance -->"

_DOTNET_GUIDANCE_PARAGRAPH = f"""
{_DOTNET_GUIDANCE_SENTINEL}
## .NET

This repository uses .NET. A shared `Directory.Build.props` has been placed at the solution
root -- see that file's own header comment for the conventions it enforces and the process for
changing it.
"""


async def scaffold_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """The true entry point of a from-scratch run (graph.py's module docstring definition of a
    "run"): creates .ai-dev-workflow/ and .github/ if missing, writes AGENTS.md and a thin
    copilot-instructions.md pointer if missing (never overwriting a human-authored one), and
    resets the workflow action ledger (fresh per session, per the user's explicit choice).

    A no-op when no sandbox is registered for this thread yet -- mirrors _persist_if_sandboxed's
    same tolerance in graph.py, since scaffold_node runs unconditionally on every fresh intake,
    including threads that predate sandboxing.
    """
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

    copilot_instructions = await repo_files.read_repo_file(provider, thread_id, ".github/copilot-instructions.md")
    if copilot_instructions is None:
        await repo_files.write_repo_file(
            provider,
            thread_id,
            ".github/copilot-instructions.md",
            template_loader.load_template("github/copilot-instructions.md"),
        )
        written_paths.append(".github/copilot-instructions.md")

    await repo_files.reset_ledger(provider, thread_id)
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "scaffold", "node": "scaffold", "action": "ran"})

    if written_paths:
        await git_ops.commit_paths(
            provider, thread_id, [*written_paths, ".ai-dev-workflow"], "ai-dev-workflow: scaffold"
        )

    # manifest.json absence is the canonical "never onboarded before" signal -- gates whether
    # build_graph()'s conditional edge routes into P0's brownfield sub-flow before tech-stack.
    manifest_exists = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH) is not None
    return {"manifest_exists": manifest_exists}


_MIGRATION_GLOBS = "*.sql,*.prisma,migrations/*,Migrations/*,schema.rb,*.dbml"
_ROUTE_HINT_GLOBS = "routes.*,*Controller.cs,*.routes.ts,urls.py,routes/*"


async def p0_baseline_context_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Deterministic pre-scan for P0's brownfield draft: greps for schema/migration/route files
    and hands their content as grounding context, rather than trusting the model to explore
    unaided (reduces ER-diagram hallucination risk)."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {"p0_context": ""}
    provider = get_sandbox_provider()
    find_cmd = " -o ".join(f"-iname {shlex.quote(g)}" for g in (_MIGRATION_GLOBS + "," + _ROUTE_HINT_GLOBS).split(","))
    result = await provider.exec_in_sandbox(thread_id, f"find . \\( {find_cmd} \\) -not -path '*/node_modules/*' 2>/dev/null | head -50")
    paths = [p.strip() for p in (result.stdout or "").splitlines() if p.strip()]
    chunks: list[str] = []
    for path in paths[:20]:
        content = await repo_files.read_repo_file(provider, thread_id, path.lstrip("./"))
        if content:
            chunks.append(f"--- {path} ---\n{content[:2000]}")
    return {"p0_context": "\n\n".join(chunks)[:20000] or "(no schema/migration/route files found)"}


async def p0_write_manifest_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Ratification approval is what flips manifest.json from absent to present -- the literal
    mechanism, per the plan's own design. Deterministic, runs right after P0 brownfield's gate."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {"manifest_exists": True}
    provider = get_sandbox_provider()
    manifest = {"onboarded": True, "run_id": state.get("run_id", "unknown")}
    await repo_files.write_repo_file(provider, thread_id, MANIFEST_PATH, json.dumps(manifest, indent=2) + "\n")
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


async def apply_dotnet_conventions_if_applicable(
    thread_id: str, tech_stack: dict[str, Any], _state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_audit_hook for the tech-stack stage: writes Directory.Build.props (from the
    bundled template) at the reported solution root if not already present, and appends one
    idempotent .NET guidance paragraph to AGENTS.md -- both deterministic, driven by the audited
    TechStack object's own fields, never by the skill (which never writes files itself).
    """
    if not tech_stack.get("dotnet_detected"):
        return

    solution_root = tech_stack.get("dotnet_solution_root")
    if solution_root is None:
        # Low/no confidence -- skip the write rather than guess a path (MSBuild's props discovery
        # walks *up* from each project; a wrongly-placed file silently misses projects or pulls in
        # unrelated directories). The audit node's own audit_findings is where this gets surfaced.
        return

    props_path = f"{solution_root}/Directory.Build.props" if solution_root else "Directory.Build.props"
    written_paths: list[str] = []

    existing_props = await repo_files.read_repo_file(provider, thread_id, props_path)
    if existing_props is None:
        await repo_files.write_repo_file(
            provider, thread_id, props_path, template_loader.load_template("dotnet/Directory.Build.props")
        )
        written_paths.append(props_path)

    agents_md = await repo_files.read_repo_file(provider, thread_id, "AGENTS.md")
    if agents_md is not None and _DOTNET_GUIDANCE_SENTINEL not in agents_md:
        await repo_files.write_repo_file(provider, thread_id, "AGENTS.md", agents_md.rstrip() + "\n" + _DOTNET_GUIDANCE_PARAGRAPH)
        written_paths.append("AGENTS.md")

    if written_paths:
        await repo_files.append_ledger_entry(
            provider, thread_id, {"stage": "tech-stack", "node": "post_audit_hook", "wrote": written_paths}
        )
        await git_ops.commit_paths(provider, thread_id, written_paths, "ai-dev-workflow: apply .NET conventions")
