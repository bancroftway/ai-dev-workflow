"""Git operations against a sandbox's own clone (architecture plan Section B.2/B.3).

There is no local working tree on the agent's own host -- every operation here runs inside the
per-session sandbox via SandboxProvider.exec_in_sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from .sandbox.provider import SandboxProvider

from .repo_files import validate_repo_relative_path

logger = logging.getLogger(__name__)

_COMMIT_AUTHOR_NAME = "ai-dev-workflow"
_COMMIT_AUTHOR_EMAIL = "ai-dev-workflow@users.noreply.github.com"
# Duplicated as literals in agent/sandbox-image/entrypoint.sh's own COMMIT_AUTHOR_NAME/EMAIL (its
# scaffold-mode initial commit) -- two different runtimes (Python here, bash there), not worth new
# env-var plumbing for a value that never changes (Task 3, Task 10 sweep item #4). If this value
# ever needs to change, change it in both places.

# Per-thread push credentials + outcome, agent-memory only (never a container env var -- the
# clone token is deliberately destroyed after clone, see entrypoint.sh). The token re-arrives on
# every provision call; an agent restart forces reprovision, so absence just skips pushing.
_PUSH_TOKENS: dict[str, str] = {}
_LAST_PUSH: dict[str, dict[str, Any]] = {}

# Serializes git index writes: the background repo scan (repo_scan_baseline overlap) commits
# concurrently with the tech-stack/brownfield chain's commits into the SAME working tree, and two
# concurrent `git commit`s fail on .git/index.lock. Guards commit_paths/commit_all BODIES only --
# never wrap commit_ai_dev_workflow (it calls commit_paths; asyncio locks are non-reentrant).
_GIT_INDEX_LOCK = asyncio.Lock()


def set_push_token(thread_id: str, token: str) -> None:
    if token:
        _PUSH_TOKENS[thread_id] = token


def get_last_push(thread_id: str) -> dict[str, Any] | None:
    return _LAST_PUSH.get(thread_id)


def get_push_token(thread_id: str) -> str | None:
    """The same clone/push credential push_head uses -- open_pull_request reuses it rather than
    threading a second copy of the user's GitHub token through exit_finalize_node."""
    return _PUSH_TOKENS.get(thread_id)


async def open_pull_request(
    *, owner: str, repo: str, source_branch: str, work_branch: str, title: str, body: str, token: str
) -> str | None:
    """Opens a GitHub PR (work_branch -> source_branch) via a plain REST call -- no SDK dependency
    needed for one POST. Idempotent against GitHub's 422 "already exists" response: fetches and
    returns the existing PR's URL instead of treating it as an error (exit_finalize_node's hook can
    legitimately fire more than once for the same session, e.g. a resumed thread whose exit stage
    was already approved). Any other failure: log-and-continue, return None -- a hiccup here must
    never flip an otherwise gate-passing session to failed."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                json={"title": title, "body": body, "head": work_branch, "base": source_branch},
            )
        except httpx.HTTPError:
            logger.warning("open_pull_request request failed for %s/%s", owner, repo, exc_info=True)
            return None

        if resp.status_code == 201:
            return resp.json().get("html_url")

        if resp.status_code == 422:
            try:
                existing = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls",
                    headers=headers,
                    params={"head": f"{owner}:{work_branch}", "base": source_branch, "state": "all"},
                )
                if existing.status_code == 200 and existing.json():
                    return existing.json()[0].get("html_url")
            except httpx.HTTPError:
                logger.warning("open_pull_request existing-PR lookup failed for %s/%s", owner, repo, exc_info=True)
                return None

        logger.warning(
            "open_pull_request failed for %s/%s %s->%s: %s %s",
            owner, repo, work_branch, source_branch, resp.status_code, resp.text[:300],
        )
        return None


async def delete_remote_branch(*, owner: str, repo: str, branch: str, token: str) -> bool:
    """Deletes a branch ref via a plain REST call -- no sandbox/clone needed, so this works even
    for a long-idle session whose container was already reaped. Uses the CURRENT caller's live
    GitHub token (forwarded fresh per delete request), never the in-memory _PUSH_TOKENS cache:
    that cache is agent-restart-fragile by design (see its own module comment) and would silently
    no-op on exactly the old sessions a user is most likely to be cleaning up.

    404 (branch already gone, e.g. deleted via a merged PR) counts as success -- the caller's goal
    ("this branch should not exist") is already satisfied. Any other failure is log-and-continue,
    returned as False: a session's DB row should still be deletable even if GitHub is unreachable
    or the token lacks permission, since the row is this app's own bookkeeping, not GitHub's."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.delete(
                f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}", headers=headers
            )
        except httpx.HTTPError:
            logger.warning("delete_remote_branch request failed for %s/%s@%s", owner, repo, branch, exc_info=True)
            return False
    if resp.status_code in (204, 404):
        return True
    logger.warning(
        "delete_remote_branch failed for %s/%s@%s: %s %s", owner, repo, branch, resp.status_code, resp.text[:300]
    )
    return False


async def push_head(provider: SandboxProvider, thread_id: str) -> None:
    """Pushes HEAD (this session's own unique work branch) to origin, log-and-continue.

    Plain --force, not --force-with-lease: branch-per-session restores "exactly one writer per
    branch" (WS0's single shared `ai-dev-workflow` branch gave that up, which is what forced the
    lease workaround this replaced -- see git history if that reasoning is ever needed again).
    Force (of some kind) is still needed because two pipeline paths legitimately `git reset --hard`
    this session's own work branch (finding-cluster's upgrade revert, app-discovery's reject
    cleanup) -- but with no other writer to protect against, there's nothing left for a lease to
    compare against.

    The token transits the container as a one-shot credential-helper file in /tmp, deleted in the
    same shell invocation.
    # ponytail: token readable inside the container for ~seconds per push; upgrade path = git
    # bundle relayed through the agent host if the sandbox trust model ever tightens.
    """
    token = _PUSH_TOKENS.get(thread_id)
    if not token:
        _LAST_PUSH[thread_id] = {"ok": False, "error": "no push token for this session (reprovision to enable pushing)", "at": datetime.now(timezone.utc).isoformat()}
        logger.info("push skipped for thread_id=%s: no token retained", thread_id)
        return
    helper_path = f"/tmp/aidw-cred-{uuid.uuid4().hex}.sh"
    helper_script = f"#!/bin/sh\necho username=x-access-token\necho password={token}\n"
    helper_b64 = base64.b64encode(helper_script.encode("utf-8")).decode("ascii")
    command = (
        f"printf %s {shlex.quote(helper_b64)} | base64 -d > {helper_path} && chmod 700 {helper_path} && "
        f"git -c credential.helper={helper_path} push --force --quiet -u origin HEAD; "
        f"rc=$?; rm -f {helper_path}; exit $rc"
    )
    result = await provider.exec_in_sandbox(thread_id, command)
    _LAST_PUSH[thread_id] = {
        "ok": result.ok,
        "error": None if result.ok else (result.stderr or result.stdout or "push failed")[-500:],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if not result.ok:
        # Log-and-continue, same policy as workflow persistence: a dead remote or missing push
        # permission must never nullify local progress. The failure IS surfaced -- get_last_push
        # feeds the streamed state's last_push and the frontend warning chip.
        logger.warning("git push failed for thread_id=%s: %s", thread_id, _LAST_PUSH[thread_id]["error"])


async def record_run_failure(
    thread_id: str, payload: dict[str, Any], run_id: str | None = None
) -> dict[str, Any]:
    """Durably records a terminal run failure ({stage, type, ...detail}) and returns the payload.

    Escalations no longer pause for a human -- the graph ENDs with `run_failure` set, so this is
    the last chance to leave a trace: a ledger row and the session row closed as "failed" (SQL,
    session_store.py -- unlike the ledger write below, this isn't a git commit, so it survives
    even when the commit that follows fails).
    No-ops (payload-only) when the sandbox is gone -- every `cannot_verify` failure happens
    exactly then. Best-effort by design: a failed write must never mask the failure itself.
    """
    from .sandbox import registry as sandbox_registry  # local: keep git_ops's import surface flat

    if sandbox_registry.get(thread_id) is None:
        return payload
    from . import repo_files  # local import mirrors the module-level one-way dependency
    from . import session_store  # local: keeps git_ops's import surface flat, same as sandbox_registry above

    provider = _get_provider()
    try:
        await repo_files.append_ledger_entry(
            provider, thread_id, {"stage": payload.get("stage"), "node": "run_failure", **payload}
        )
        await session_store.close_session(thread_id, run_id=run_id, status="failed", failure=payload)
        await commit_ai_dev_workflow(provider, thread_id, f"ai-dev-workflow: run failed at {payload.get('stage')}")
    except Exception:  # noqa: BLE001 -- best-effort trace; the failure payload is what matters
        logger.warning("failed to durably record run_failure for thread_id=%s", thread_id, exc_info=True)
    return payload


def _get_provider() -> SandboxProvider:
    from .sandbox import get_sandbox_provider  # local: sandbox/factory imports nothing from here

    return get_sandbox_provider()


# Every way git says "you asked me to commit, and there was nothing to commit". All of them are
# no-ops, not failures. The third one -- "nothing added to commit but untracked files present" --
# cost real work before it was handled: git emits it when the requested paths are clean but some
# unrelated file is untracked, and treating it as an error aborted the whole persist step, leaving
# state.json un-updated while the stage's artifact sat on the branch with nothing referencing it.
# The next resume then silently re-ran that stage.
_EMPTY_COMMIT_PHRASES = (
    "nothing to commit",
    "no changes added to commit",
    "nothing added to commit",
)


def is_empty_commit_output(combined_output: str) -> bool:
    """True when git's output means "nothing to commit". Case-insensitive; pure."""
    lowered = combined_output.lower()
    return any(phrase in lowered for phrase in _EMPTY_COMMIT_PHRASES)


async def commit_paths(provider: SandboxProvider, thread_id: str, paths: list[str], message: str) -> None:
    """Stage and commit exactly the given repo-relative paths.

    Commits automatically on every stage transition (plan Section B.3) -- local-only, never
    pushes. Pushing on an explicit user action is a separate, not-yet-built piece; committing
    locally is a safety net on its own (nothing is lost if the sandbox dies) regardless.

    Generalizes the original .ai-dev-workflow/-only commit helper (kept below as a thin wrapper,
    `commit_ai_dev_workflow`) so pipeline stages that touch source/config paths outside
    .ai-dev-workflow/ (AGENTS.md, Directory.Build.props, .ai-dev-workflow/spec/ledger.json, source files a
    codegen stage wrote, CHANGELOG.md, etc.) have one shared commit primitive instead of each
    stage reinventing the git-add-and-commit shell command.
    """
    if not paths:
        return
    # shlex.quote (not manual backslash-escaping, the prior approach) closes a real
    # command-injection gap found by automated security review: a path or commit message
    # containing "$", "`", or a stray quote could otherwise break out of the double-quoted shell
    # string below. Paths are additionally validated against the same repo-relative allowlist
    # write_repo_file uses -- a path is data (from a stage's own write, some of it model-reported),
    # never something that should reach `git add` as anything but a literal path argument.
    for path in paths:
        validate_repo_relative_path(path)
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    command = (
        f"git add -- {quoted_paths} && "
        f"git -c user.name={shlex.quote(_COMMIT_AUTHOR_NAME)} -c user.email={shlex.quote(_COMMIT_AUTHOR_EMAIL)} "
        f"commit -m {shlex.quote(message)} --quiet"
    )
    async with _GIT_INDEX_LOCK:
        result = await provider.exec_in_sandbox(thread_id, command)
        if result.ok:
            await push_head(provider, thread_id)
            return
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    # Two idempotent shapes: a fully clean tree ("nothing to commit"), and -- since the background
    # repo scan overlaps the tech-stack/brownfield chain -- the requested paths already committed
    # by a broader .ai-dev-workflow commit while some OTHER file is dirty ("no changes added to
    # commit", with the dirty file listed as not staged).
    if is_empty_commit_output(combined_output):
        return  # idempotent: caller ran but produced no actual file changes
    raise RuntimeError(f"git commit failed: {result.stderr or result.stdout}")


async def commit_ai_dev_workflow(provider: SandboxProvider, thread_id: str, message: str) -> None:
    """Stage and commit .ai-dev-workflow/ only -- thin wrapper over commit_paths() kept so every
    existing call site (graph.py's audit/gate/auto_approve nodes) is untouched."""
    await commit_paths(provider, thread_id, [".ai-dev-workflow"], message)


# Build output, dependency trees and test scratch, none of which belong in a reviewable branch.
# `.ai-dev-workflow/` is deliberately absent: that folder IS the deliverable.
_GITIGNORE_ENTRIES = (
    "node_modules/",
    ".next/",
    "out/",
    "dist/",
    "build/",
    "bin/",
    "obj/",
    "coverage/",
    ".nyc_output/",
    "TestResults/",
    # Both the directory and the loose files: Playwright writes `test-results/`, while the pipeline's
    # own test-run agents write `test-results-0.json` beside it. A directory-only pattern let
    # `apps/web/test-results-0.json` be committed as if it were source.
    "test-results*",
    "*.trx",
    "playwright-report/",
    "blob-report/",
    "playwright/.cache/",
    # ASP.NET's conventional runtime-data directory. The generated app persists its counter to
    # `App_Data/counter.json` and its store creates the file on demand, so tracking it puts live
    # application state into every diff.
    "App_Data/",
    "agent-work/",
    ".env.local",
    ".DS_Store",
    "*.user",
    # Python: the union list is ecosystem-neutral (a `.venv/` entry in a dotnet repo costs nothing),
    # and generated_ignore_entries can never derive these itself -- `.py` is in _AUTHORED_SUFFIXES,
    # so `__pycache__/` contents would read as authored files.
    ".venv/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    "htmlcov/",
    ".coverage",
)
_GITIGNORE_HEADER = "# Managed by ai-dev-workflow: build output and scratch must not reach the review branch."
# Suffixes a stage plausibly AUTHORED: source, and the manifests/configs that declare a project.
# Deliberately generous -- a false "authored" costs one over-specific ignore entry, a false
# "generated" silently deletes a whole app tree from the branch.
_AUTHORED_SUFFIXES = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro",
    ".py", ".cs", ".fs", ".vb", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".swift",
    ".razor", ".cshtml", ".html", ".css", ".scss", ".sass", ".sql", ".sh", ".md",
    ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".csproj", ".fsproj", ".vbproj", ".sln", ".slnx", ".props", ".targets",
)
# `.json` is NOT a suffix above on purpose: half the JSON in a built tree is toolchain output
# (`test-results-0.json`, `coverage-summary.json`, `*.nuget.g.props` siblings). These names are the
# authored ones -- a project manifest, a lockfile that belongs in review, a settings file.
_AUTHORED_NAMES = (
    "package.json", "package-lock.json", "tsconfig*.json", "jsconfig*.json", "appsettings*.json",
    "angular.json", "nx.json", "turbo.json", "global.json", "nuget.config", "composer.json",
    "deno.json", "requirements*.txt", "*.lock", "Dockerfile*", "Makefile",
    # Dot-files and per-stack odds and ends a stage legitimately writes. Observed live on the run
    # right after the collapse fix: `apps/web/.gitignore`, `apps/api/api.http` and
    # `apps/api/Properties/` (launchSettings.json) were still "detected as generated" and kept out
    # of the branch -- the app the reviewer sees must contain everything the stage actually wrote.
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig", ".npmrc", ".nvmrc",
    "*.env.example", "*.http", "launchSettings.json", "*.csx", "*.ps1", "*.bat",
)
_GENERATED_DIR_NAMES = frozenset(entry.strip("/") for entry in _GITIGNORE_ENTRIES if entry.endswith("/"))
_GENERATED_FILE_PATTERNS = tuple(entry for entry in _GITIGNORE_ENTRIES if not entry.endswith("/"))


def _looks_authored(path: str) -> bool:
    """Whether this path is source/manifest a stage could have written (never toolchain output).

    Extension alone is not enough in either direction: a build directory is full of `.js` and
    `.json` (`.next/server/app/page.js`, `obj/project.assets.json`). So anything living UNDER a
    known output directory is generated whatever it is named -- the static ignore list already
    enumerates those directories, so this reuses it rather than growing a second list.
    """
    segments = path.split("/")
    if any(segment in _GENERATED_DIR_NAMES for segment in segments[:-1]):
        return False
    name = segments[-1]
    if any(fnmatch.fnmatch(name, pattern) for pattern in _GENERATED_FILE_PATTERNS):
        return False
    if path.lower().endswith(_AUTHORED_SUFFIXES):
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _AUTHORED_NAMES)
_GITIGNORE_ENSURED: set[str] = set()


def generated_ignore_entries(untracked: list[str], tracked: list[str]) -> list[str]:
    """.gitignore entries covering files that appeared from building/booting, given the tracked set.

    `_GITIGNORE_ENTRIES` is an enumerated list, and an enumerated list is always one stack behind:
    it needed widening twice in a single session (`test-results/` missed the loose
    `test-results-0.json`; `App_Data/` was missing entirely), and a Python or Rust target would bring
    `__pycache__/`, `.pytest_cache/`, `target/` next. This derives the answer instead.

    The rule needs no per-stack knowledge: by the time the app has been built and booted, every
    source file a stage authored is already committed, so anything still untracked was produced by
    the toolchain.

    The entry is the generated directory sitting immediately INSIDE the deepest ancestor that does
    contain tracked files -- so `apps/web/.next/cache/turbopack/x.meta` collapses to `apps/web/.next/`
    while `apps/web/` itself is never ignored. When no ancestor contains tracked files at all, only
    the exact file is ignored, never a directory: a subtree with nothing committed in it might be
    source that simply has not been committed yet, and ignoring it would hide real work. That is
    deliberately conservative -- a first draft of this walked to the highest untracked ancestor and
    would have ignored an entire `services/` tree.

    `.ai-dev-workflow/` is excluded unconditionally: it is the deliverable, and parts of it are
    legitimately untracked mid-run.
    """
    tracked_dirs: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        for i in range(1, len(parts)):
            tracked_dirs.add("/".join(parts[:i]))

    # Directories that still hold an AUTHORED untracked file. The docstring's premise -- "by the
    # time the app has been built, every authored file is already committed" -- is false for the one
    # stage that matters: minimal-code-to-green writes a whole app tree and nothing commits it until
    # the rebuild node runs, so the collapse below would ignore that tree wholesale. Observed live
    # (nextjs-dotnet, thread 70bbfc95): `apps/` held one tracked file (Directory.Build.props from
    # scaffold), so the brand-new `apps/web/` and `apps/api.Tests/` collapsed to exactly those two
    # ignore entries -- the frontend was never committed, the coverage gate reported
    # "declared_frontend_missing", and the stage burned all 12 laps rebuilding a tree the gate could
    # not see. So authored files are never ignored, and the collapse walks DOWN past any directory
    # that holds one (`apps/web/` -> `apps/web/.next/`).
    authored_dirs: set[str] = set()
    for path in untracked:
        clean = path.strip().strip("/")
        if not _looks_authored(clean):
            continue
        parts = clean.split("/")
        for i in range(1, len(parts)):
            authored_dirs.add("/".join(parts[:i]))

    entries: set[str] = set()
    for path in untracked:
        clean = path.strip().strip("/")
        # `.gitignore` is this function's own output file. It is untracked on the first pass, sits at
        # the repo root next to tracked files, and so used to be "detected as generated" and added to
        # itself -- observed live, line 23 of the branch's own .gitignore was `.gitignore`.
        if not clean or clean.startswith(".ai-dev-workflow") or clean == ".gitignore":
            continue
        if _looks_authored(clean):
            continue
        parts = clean.split("/")
        # Deepest ancestor that holds tracked files; the entry is the one segment below it.
        deepest = -1
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in tracked_dirs:
                deepest = i
        if deepest == -1:
            entries.add(clean)  # nothing committed anywhere above it -- ignore the file only
            continue
        for i in range(deepest + 1, len(parts)):
            candidate = "/".join(parts[:i])
            if candidate not in authored_dirs:
                entries.add(candidate if candidate == clean else f"{candidate}/")
                break
        else:
            entries.add(clean)  # every directory above it holds authored work -- ignore this file only
    return sorted(entries)


async def _append_missing_gitignore_entries(
    provider: SandboxProvider, thread_id: str, candidates: list[str], header: str
) -> list[str]:
    """Append whichever of `candidates` .gitignore doesn't already have, under a `header` comment
    line, and return them (empty when nothing was missing). Appended, never overwritten: a repo
    (or a stage) may have its own entries that matter. Shared by ignore_generated_files and
    ensure_gitignore below."""
    existing = await provider.exec_in_sandbox(thread_id, "cat .gitignore 2>/dev/null || true")
    current = {line.strip() for line in str(existing.stdout or "").splitlines()}
    missing = [entry for entry in candidates if entry not in current]
    if missing:
        block = "\n".join([header, *missing])
        await provider.exec_in_sandbox(thread_id, f"printf '%s\\n' {shlex.quote(block)} >> .gitignore")
    return missing


async def ignore_generated_files(provider: SandboxProvider, thread_id: str) -> list[str]:
    """Append ignore entries for anything the toolchain produced but nothing tracks. Returns them.

    Runs on every `commit_all` -- deliberately without the once-per-thread guard `ensure_gitignore`
    uses, because generated files appear progressively (a build here, a test run there, a booted app
    later), not all at once at the first commit.
    """
    listing = await provider.exec_in_sandbox(
        thread_id, "git ls-files --others --exclude-standard && echo --- && git ls-files"
    )
    raw = str(listing.stdout or "")
    if "---" not in raw:
        return []
    untracked_block, tracked_block = raw.split("---", 1)
    untracked = [line.strip() for line in untracked_block.splitlines() if line.strip()]
    tracked = [line.strip() for line in tracked_block.splitlines() if line.strip()]
    if not untracked:
        return []

    candidates = generated_ignore_entries(untracked, tracked)
    missing = await _append_missing_gitignore_entries(
        provider, thread_id, candidates,
        "# Detected as generated: untracked after build/boot, nothing authored it.",
    )
    if not missing:
        return []
    logger.info(
        "gitignore: %d generated path(s) detected and ignored: %s",
        len(missing),
        ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""),
    )
    return missing


async def ensure_gitignore(provider: SandboxProvider, thread_id: str) -> list[str]:
    """Guarantee a .gitignore covering build output, and untrack anything it already caught.

    Runs from `commit_all`, which is the `git add -A` that caused the problem: with no .gitignore in
    a generated repo, one run put **893 of 997 tracked paths** on the review branch as `.next/`,
    `bin/` and `obj/` artifacts. A pull request that is 90% build output is not reviewable, so this
    is a merge-readiness fix, not tidiness.

    Returns the paths it untracked (empty when there was nothing to do). Once per thread per
    process: the git plumbing is cheap but not free, and nothing re-dirties it mid-run.
    """
    if thread_id in _GITIGNORE_ENSURED:
        return []
    _GITIGNORE_ENSURED.add(thread_id)

    missing = await _append_missing_gitignore_entries(provider, thread_id, _GITIGNORE_ENTRIES, _GITIGNORE_HEADER)

    # `git ls-files -i -c --exclude-standard` = tracked files that the ignore rules now match.
    # Listed BEFORE removing them -- listing afterwards returns the empty set by construction, which
    # would make the log claim nothing was ever wrong.
    listed = await provider.exec_in_sandbox(thread_id, "git ls-files -i -c --exclude-standard")
    untracked = [line.strip() for line in str(listed.stdout or "").splitlines() if line.strip()]
    if untracked:
        # xargs, not one argv: this list was 893 entries on the branch that motivated the fix.
        await provider.exec_in_sandbox(
            thread_id,
            "git ls-files -z -i -c --exclude-standard | xargs -0 -r git rm -r --cached --quiet --",
        )
    if missing or untracked:
        logger.info(
            "gitignore: added %d entr(ies), untracked %d already-committed path(s)",
            len(missing),
            len(untracked),
        )
    return untracked


async def commit_all(provider: SandboxProvider, thread_id: str, message: str) -> None:
    """Stage and commit EVERYTHING the pipeline's code-writing sessions changed (`git add -A`,
    .gitignore respected) and push. The artifact-only commit sites above deliberately never touch
    source files -- without this, the work branch the human reviews on GitHub would carry specs
    and scan reports but none of the generated code."""
    await ensure_gitignore(provider, thread_id)
    # After the static list, the derived sweep: generated files appear progressively across a run
    # (build, then test output, then a booted app's runtime state), so this runs on every commit.
    await ignore_generated_files(provider, thread_id)
    command = (
        "git add -A && "
        f"git -c user.name={shlex.quote(_COMMIT_AUTHOR_NAME)} -c user.email={shlex.quote(_COMMIT_AUTHOR_EMAIL)} "
        f"commit -m {shlex.quote(message)} --quiet"
    )
    async with _GIT_INDEX_LOCK:
        result = await provider.exec_in_sandbox(thread_id, command)
        if result.ok:
            await push_head(provider, thread_id)
            # Every code-writing stage funnels through here -- kick a display-only background
            # scan so the metrics bar reflects the new code at the next node boundary
            # (metrics_nodes.collect_live_refresh) instead of going stale until the next gate.
            from . import repo_scan  # local import mirrors the module-level one-way dependency

            repo_scan.start_background_refresh(thread_id, provider)
            return
    combined_output = f"{result.stdout}\n{result.stderr}"
    # Same shared phrase set as commit_paths: this site used to check only "nothing to commit", so
    # the two disagreed about what counts as an empty commit.
    if is_empty_commit_output(combined_output):
        return
    raise RuntimeError(f"git commit -A failed: {result.stderr or result.stdout}")


def _demo() -> None:
    """`cd agent && uv run python -m src.git_ops`."""
    # The exact wording git produced on a live run, which the old narrower check missed.
    real_output = (
        "On branch ai-dev-workflow/50d64d0b\n"
        "Untracked files:\n  (use \"git add <file>...\" to include in what will be committed)\n"
        "\tapps/web/.next/lock\n\n"
        "nothing added to commit but untracked files present (use \"git add\" to track)\n"
    )
    assert is_empty_commit_output(real_output), "the live 'nothing added to commit' wording must be a no-op"
    assert is_empty_commit_output("nothing to commit, working tree clean")
    assert is_empty_commit_output("no changes added to commit (use \"git add\")")
    assert is_empty_commit_output("NOTHING TO COMMIT"), "must be case-insensitive"
    # A real failure must still raise -- this is the half that must not become permissive.
    assert not is_empty_commit_output("error: pathspec 'x' did not match any file(s) known to git")
    assert not is_empty_commit_output("fatal: could not read Username for 'https://github.com'")

    # .gitignore entries must cover the build output that actually polluted a branch (893 of 997
    # tracked paths), and must NOT cover the deliverable.
    for expected in (".next/", "bin/", "obj/", "node_modules/", "test-results*", "App_Data/"):
        assert expected in _GITIGNORE_ENTRIES, expected
    # The loose-file case that slipped through a directory-only pattern, and the runtime state file.
    import fnmatch

    for artifact in ("apps/web/test-results-0.json", "apps/api.Tests/x.trx"):
        name = artifact.rsplit("/", 1)[-1]
        assert any(fnmatch.fnmatch(name, pat) for pat in _GITIGNORE_ENTRIES if not pat.endswith("/")), artifact
    assert not any(".ai-dev-workflow" in entry for entry in _GITIGNORE_ENTRIES), (
        ".ai-dev-workflow/ is the deliverable and must never be ignored"
    )

    # --- derived generated-file detection ---------------------------------------------------------
    # A whole generated directory collapses to one entry, however many files it holds.
    tracked = [
        "apps/web/package.json",
        "apps/web/src/app/page.tsx",
        "apps/api/Program.cs",
        ".gitignore",
    ]
    next_build = [
        "apps/web/.next/BUILD_ID",
        "apps/web/.next/cache/turbopack/00000058.meta",
        "apps/web/.next/server/app/page.js",
    ]
    assert generated_ignore_entries(next_build, tracked) == ["apps/web/.next/"], generated_ignore_entries(
        next_build, tracked
    )

    # A loose file in a directory that DOES hold source is ignored by exact path -- ignoring
    # `apps/web/` because a test-result landed there would bury the whole app.
    loose = ["apps/web/test-results-0.json", "apps/web/test-results-1.json"]
    assert generated_ignore_entries(loose, tracked) == [
        "apps/web/test-results-0.json",
        "apps/web/test-results-1.json",
    ], generated_ignore_entries(loose, tracked)

    # The two real cases this session had to patch by hand, now derived rather than enumerated.
    assert generated_ignore_entries(["apps/api/App_Data/counter.json"], tracked) == ["apps/api/App_Data/"]

    # Stacks the static list has never seen. Each needs a tracked sibling, because the collapse is
    # anchored on "a directory that already holds committed source".
    polyglot = [*tracked, "services/api/main.py", "crates/engine/Cargo.toml", "apps/api.Tests/Api.Tests.csproj"]
    for path, expected in (
        ("services/api/__pycache__/app.cpython-312.pyc", "services/api/__pycache__/"),
        ("crates/engine/target/debug/engine", "crates/engine/target/"),
        ("apps/api.Tests/obj/Debug/net10.0/Api.Tests.dll", "apps/api.Tests/obj/"),
    ):
        got = generated_ignore_entries([path], polyglot)
        assert got == [expected], (path, got)

    # With NOTHING committed above it, only the file is ignored -- never a directory that might turn
    # out to be uncommitted source.
    orphan = generated_ignore_entries(["services/api/__pycache__/app.pyc"], tracked)
    assert orphan == ["services/api/__pycache__/app.pyc"], orphan

    # The deliverable is never ignored, however untracked it looks mid-run.
    assert generated_ignore_entries([".ai-dev-workflow/history/r1-screens/001-home.png"], tracked) == []
    assert generated_ignore_entries([], tracked) == []
    # A stray file at the repo root has no ancestor directory -- exact path, never "/".
    assert generated_ignore_entries(["stray.log"], tracked) == ["stray.log"]

    # The live kill (thread 70bbfc95): minimal-code-to-green had just written the whole app and
    # NOTHING under apps/ was committed except scaffold's Directory.Build.props. The collapse used to
    # produce ["apps/api.Tests/", "apps/web/"] -- the frontend never reached the branch, the coverage
    # gate reported declared_frontend_missing, and the stage burned all 12 laps. Authored files must
    # survive; only the toolchain output below them may be ignored.
    scaffold_only = ["apps/Directory.Build.props", ".gitignore"]
    fresh_app = [
        "apps/web/package.json",
        "apps/web/src/app/page.tsx",
        "apps/web/.next/BUILD_ID",
        "apps/web/node_modules/next/index.js",
        "apps/api.Tests/Api.Tests.csproj",
        "apps/api.Tests/CounterApiTests.cs",
        "apps/api.Tests/obj/project.assets.json",
        ".gitignore",
    ]
    got = generated_ignore_entries(fresh_app, scaffold_only)
    assert got == ["apps/api.Tests/obj/", "apps/web/.next/", "apps/web/node_modules/"], got
    assert ".gitignore" not in got, "this function's own output file must never ignore itself"

    print("git_ops self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
