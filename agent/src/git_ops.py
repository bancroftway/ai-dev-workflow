"""Git operations against a sandbox's own clone (architecture plan Section B.2/B.3).

There is no local working tree on the agent's own host -- every operation here runs inside the
per-session sandbox via SandboxProvider.exec_in_sandbox.
"""

from __future__ import annotations

import shlex

from .sandbox.provider import SandboxProvider

from .repo_files import validate_repo_relative_path

_COMMIT_AUTHOR_NAME = "ai-dev-workflow"
_COMMIT_AUTHOR_EMAIL = "ai-dev-workflow@users.noreply.github.com"


async def commit_paths(provider: SandboxProvider, thread_id: str, paths: list[str], message: str) -> None:
    """Stage and commit exactly the given repo-relative paths.

    Commits automatically on every stage transition (plan Section B.3) -- local-only, never
    pushes. Pushing on an explicit user action is a separate, not-yet-built piece; committing
    locally is a safety net on its own (nothing is lost if the sandbox dies) regardless.

    Generalizes the original .ai-dev-workflow/-only commit helper (kept below as a thin wrapper,
    `commit_ai_dev_workflow`) so pipeline stages that touch source/config paths outside
    .ai-dev-workflow/ (AGENTS.md, Directory.Build.props, spec/ledger.json, source files a
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
    result = await provider.exec_in_sandbox(thread_id, command)
    if result.ok:
        return
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    if "nothing to commit" in combined_output:
        return  # idempotent: caller ran but produced no actual file changes
    raise RuntimeError(f"git commit failed: {result.stderr or result.stdout}")


async def commit_ai_dev_workflow(provider: SandboxProvider, thread_id: str, message: str) -> None:
    """Stage and commit .ai-dev-workflow/ only -- thin wrapper over commit_paths() kept so every
    existing call site (graph.py's audit/gate/auto_approve nodes) is untouched."""
    await commit_paths(provider, thread_id, [".ai-dev-workflow"], message)
