"""Git operations against a sandbox's own clone (architecture plan Section B.2/B.3).

There is no local working tree on the agent's own host -- every operation here runs inside the
per-session sandbox via SandboxProvider.exec_in_sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any

from .sandbox.provider import SandboxProvider

from .repo_files import validate_repo_relative_path

logger = logging.getLogger(__name__)

_COMMIT_AUTHOR_NAME = "ai-dev-workflow"
_COMMIT_AUTHOR_EMAIL = "ai-dev-workflow@users.noreply.github.com"

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


async def push_head(provider: SandboxProvider, thread_id: str) -> None:
    """Pushes HEAD (the ai-dev-workflow/<branch>-<user> work branch) to origin, log-and-continue.

    --force (not --force-with-lease): the clone is --single-branch, so no remote-tracking ref for
    the work branch ever exists or updates -- a bare lease compares against "branch must not
    exist" and rejected literally every push as `stale info`. Force is needed at all because two
    pipeline paths legitimately `git reset --hard` the work branch (finding-cluster's upgrade
    revert, app-discovery's reject cleanup). The branch is tool-owned and user-scoped
    (entrypoint.sh suffixes it with the session id), so it has exactly one writer -- plain force
    is safe.

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


async def record_run_failure(thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Durably records a terminal run failure ({stage, type, ...detail}) and returns the payload.

    Escalations no longer pause for a human -- the graph ENDs with `run_failure` set, so this is
    the last chance to leave a trace: a ledger row plus a commit+push of .ai-dev-workflow/.
    No-ops (payload-only) when the sandbox is gone -- every `cannot_verify` failure happens
    exactly then. Best-effort by design: a failed write must never mask the failure itself.
    """
    from .sandbox import registry as sandbox_registry  # local: keep git_ops's import surface flat

    if sandbox_registry.get(thread_id) is None:
        return payload
    from . import repo_files  # local import mirrors the module-level one-way dependency

    provider = _get_provider()
    try:
        await repo_files.append_ledger_entry(
            provider, thread_id, {"stage": payload.get("stage"), "node": "run_failure", **payload}
        )
        await commit_ai_dev_workflow(provider, thread_id, f"ai-dev-workflow: run failed at {payload.get('stage')}")
    except Exception:  # noqa: BLE001 -- best-effort trace; the failure payload is what matters
        logger.warning("failed to durably record run_failure for thread_id=%s", thread_id, exc_info=True)
    return payload


def _get_provider() -> SandboxProvider:
    from .sandbox import get_sandbox_provider  # local: sandbox/factory imports nothing from here

    return get_sandbox_provider()


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
    if "nothing to commit" in combined_output or "no changes added to commit" in combined_output:
        return  # idempotent: caller ran but produced no actual file changes
    raise RuntimeError(f"git commit failed: {result.stderr or result.stdout}")


async def commit_ai_dev_workflow(provider: SandboxProvider, thread_id: str, message: str) -> None:
    """Stage and commit .ai-dev-workflow/ only -- thin wrapper over commit_paths() kept so every
    existing call site (graph.py's audit/gate/auto_approve nodes) is untouched."""
    await commit_paths(provider, thread_id, [".ai-dev-workflow"], message)


async def commit_all(provider: SandboxProvider, thread_id: str, message: str) -> None:
    """Stage and commit EVERYTHING the pipeline's code-writing sessions changed (`git add -A`,
    .gitignore respected) and push. The artifact-only commit sites above deliberately never touch
    source files -- without this, the work branch the human reviews on GitHub would carry specs
    and scan reports but none of the generated code."""
    command = (
        "git add -A && "
        f"git -c user.name={shlex.quote(_COMMIT_AUTHOR_NAME)} -c user.email={shlex.quote(_COMMIT_AUTHOR_EMAIL)} "
        f"commit -m {shlex.quote(message)} --quiet"
    )
    async with _GIT_INDEX_LOCK:
        result = await provider.exec_in_sandbox(thread_id, command)
        if result.ok:
            await push_head(provider, thread_id)
            return
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    if "nothing to commit" in combined_output:
        return
    raise RuntimeError(f"git commit -A failed: {result.stderr or result.stdout}")
