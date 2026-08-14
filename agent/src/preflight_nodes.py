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

from langchain_core.runnables import RunnableConfig

from . import git_ops, repo_files, template_loader
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

logger = logging.getLogger(__name__)

MANIFEST_PATH = ".ai-dev-workflow/manifest.json"

def _guidance_sentinel(key: str) -> str:
    """One idempotency marker per ecosystem's AGENTS.md paragraph.

    Byte-identical to the original hardcoded dotnet sentinel for key="dotnet" -- deliberately, so
    a repo onboarded before this was generalized doesn't get a second .NET paragraph appended.
    """
    return f"<!-- ai-dev-workflow:{key}-guidance -->"


_TECH_STACK_SENTINEL = _guidance_sentinel("tech-stack")

_TECH_STACK_PARAGRAPH = f"""
{_TECH_STACK_SENTINEL}
## Before generating, modifying, or reviewing any code

Read `.ai-dev-workflow/tech-stack.md` first and follow the technology stack, architecture, and
coding conventions documented there. It is kept up to date automatically as this repo's stack is
analyzed.
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
    manifest.update(updates)
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

    # Captured before anything is written, so app_discovery's reject path can put the tree back
    # exactly as it arrived. Same reference-point technique as StageSpec.capture_baseline_commit.
    head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")

    # manifest.json absence is the canonical "never onboarded before" signal -- gates whether
    # build_graph()'s conditional edge routes into brownfield-baseline's brownfield sub-flow. Read once, here, and
    # routed on from state later: app discovery writes to this file mid-run, so a fresh read at
    # the branch point would always report "onboarded".
    manifest_exists = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH) is not None
    return {
        "manifest_exists": manifest_exists,
        "run_baseline_commit": head.stdout.strip() if head.ok else None,
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

# Foreign ESLint configs. If a repo already lints its own way, this pipeline defers to it entirely
# rather than fighting it with a second config file -- eslint.config.mjs is excluded from the list
# because that is our own destination (handled by the version-stamp path instead).
_FOREIGN_ESLINT_CONFIGS = (
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)

# Base ESLint dev-dependencies, always installed for a node repo. Versions are otherwise left to
# npm (unlike NuGet, where Directory.Build.props must name one) so each repo's own lockfile and
# dependency policy decide -- with one exception.
#
# eslint is pinned to ^9 because it is not optional: with a bare "eslint", npm resolves the latest
# major, and eslint-plugin-react-hooks / eslint-plugin-jsx-a11y still peer-depend on ^8||^9. The
# whole install then fails ERESOLVE and the repo gets no lint config at all (observed, not
# theorised). Bump this only after the framework plugins have caught up.
_NODE_BASE_DEV_DEPS = (
    "eslint@^9",
    "@eslint/js",
    "globals",
    "typescript-eslint",
    "eslint-plugin-security",
    "eslint-plugin-sonarjs",
)

# Framework overlays. eslint.config.mjs imports each of these optionally, so a repo only ever gets
# the plugins matching frameworks it actually uses -- one template, four stacks.
_FRAMEWORK_DEV_DEPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("react", "next", "next.js", "remix", "preact"), ("eslint-plugin-react-hooks", "eslint-plugin-jsx-a11y")),
    (("angular",), ("angular-eslint",)),
    (("vue", "nuxt"), ("eslint-plugin-vue",)),
)


@dataclass(frozen=True)
class _Ecosystem:
    key: str
    files: tuple[tuple[str, str], ...]
    """(bundled template path, destination filename relative to this ecosystem's root)."""
    guidance: str
    """AGENTS.md paragraph body, appended once and guarded by _guidance_sentinel(key)."""
    needs_node_install: bool = False
    """True for ecosystems whose config file cannot run until packages are installed into the
    target repo -- the config is then written ONLY after a successful install (see _apply_node)."""
    dev_deps: tuple[str, ...] = field(default=())


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

_NODE = _Ecosystem(
    key="node",
    files=(("node/eslint.config.mjs", "eslint.config.mjs"),),
    needs_node_install=True,
    dev_deps=_NODE_BASE_DEV_DEPS,
    guidance="""## JavaScript / TypeScript

This repository has a shared `eslint.config.mjs` -- see that file's own header comment for the
conventions it enforces and the process for changing it. The build runs
`eslint . --max-warnings=0` plus a strict `tsc --noEmit`, so a lint warning or a type error is a
build failure, not advice.
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


def _node_dev_deps(frameworks: list[str]) -> tuple[str, ...]:
    """Base ESLint packages plus one overlay per detected framework. Pure -- see the self-check."""
    deps = list(_NODE_BASE_DEV_DEPS)
    lowered = " ".join(f.lower() for f in frameworks)
    for names, extra in _FRAMEWORK_DEV_DEPS:
        if any(name in lowered for name in names):
            deps.extend(d for d in extra if d not in deps)
    return tuple(deps)


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


_LOCKFILE_BY_MANAGER = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}


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


async def _apply_node(
    provider: SandboxProvider, thread_id: str, root: str, tech_stack: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Node/TS conventions. Install first, write second -- never the reverse.

    An eslint.config.mjs whose plugins were never installed fails with "Cannot find package
    '@eslint/js'", which is a *config* error: no amount of the agent fixing its own code clears
    it, and quality-remediation's build_ok short-circuit escalates to a human. So a failed install leaves the repo
    exactly as it arrived, and the failure is recorded instead.
    """
    for name in _FOREIGN_ESLINT_CONFIGS:
        if await repo_files.read_repo_file(provider, thread_id, _join_root(root, name)) is not None:
            return [], {"skipped": f"repo has its own {name}"}

    manager = await _detect_package_manager(provider, thread_id, root)
    if manager is None:
        return [], {"skipped": f"no package.json at root {root!r}"}

    config_path = _join_root(root, "eslint.config.mjs")
    existing = await repo_files.read_repo_file(provider, thread_id, config_path)
    if existing is not None and _is_ours(existing):
        current = _template_version(template_loader.load_template("node/eslint.config.mjs"))
        if _template_version(existing) >= current:
            return [], {"skipped": "already current"}
    elif existing is not None:
        return [], {"skipped": "repo has its own eslint.config.mjs"}

    deps = _node_dev_deps([str(item) for item in (tech_stack.get("frameworks") or [])])
    add = "add -D" if manager in ("pnpm", "yarn") else "install -D"
    prefix = f"cd {shlex.quote(root)} && " if root else ""
    install = await provider.exec_in_sandbox(
        thread_id, f"{prefix}{manager} {add} --no-audit --no-fund {' '.join(deps)} 2>&1"
    )
    if not install.ok:
        return [], {"install_failed": manager, "error": (install.stderr or install.stdout or "")[-500:]}

    written = await _write_if_outdated(provider, thread_id, config_path, "node/eslint.config.mjs")
    paths = [p for p in (written, _join_root(root, "package.json"), _join_root(root, _LOCKFILE_BY_MANAGER[manager])) if p]
    return paths, {"installed": manager, "dev_deps": list(deps)}


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
            if ecosystem.needs_node_install:
                paths, outcome = await _apply_node(provider, thread_id, root, tech_stack)
            else:
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
    # The one runnable check this module earns: _applicable_ecosystems and _node_dev_deps are the
    # only non-trivial pure logic here, and every failure mode they have is a real bug (a wrong
    # path silently misses projects; an unsafe root used to be able to kill the run).
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

    assert _node_dev_deps([]) == _NODE_BASE_DEV_DEPS
    assert "eslint-plugin-jsx-a11y" in _node_dev_deps(["Next.js"])
    assert "angular-eslint" in _node_dev_deps(["Angular"])
    assert "eslint-plugin-vue" in _node_dev_deps(["Nuxt"])
    assert "angular-eslint" not in _node_dev_deps(["React"])
    # No duplicates when two frameworks map to overlapping overlays.
    react_next = _node_dev_deps(["React", "Next.js"])
    assert len(react_next) == len(set(react_next)), react_next

    assert _template_version("aidw-template-version: 7") == 7
    assert _template_version("no stamp here") == 0
    assert _is_ours("... DO NOT MODIFY THIS FILE DURING FEATURE WORK ...")
    assert not _is_ours("# someone's own eslint config")

    print("preflight_nodes self-check: ok")
