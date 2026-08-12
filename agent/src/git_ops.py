"""Git operations against a sandbox's own clone (architecture plan Section B.2/B.3).

There is no local working tree on the agent's own host -- every operation here runs inside the
per-session sandbox via SandboxProvider.exec_in_sandbox.
"""

from __future__ import annotations

from .sandbox.provider import SandboxProvider

_COMMIT_AUTHOR_NAME = "ai-dev-workflow"
_COMMIT_AUTHOR_EMAIL = "ai-dev-workflow@users.noreply.github.com"


async def commit_ai_dev_workflow(provider: SandboxProvider, thread_id: str, message: str) -> None:
    """Stage and commit .ai-dev-workflow/ only.

    Commits automatically on every stage transition (plan Section B.3) -- local-only, never
    pushes. Pushing on an explicit user action is a separate, not-yet-built piece; committing
    locally is a safety net on its own (nothing is lost if the sandbox dies) regardless.
    """
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    command = (
        "git add .ai-dev-workflow && "
        f'git -c user.name="{_COMMIT_AUTHOR_NAME}" -c user.email="{_COMMIT_AUTHOR_EMAIL}" '
        f'commit -m "{safe_message}" --quiet'
    )
    result = await provider.exec_in_sandbox(thread_id, command)
    if result.ok:
        return
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    if "nothing to commit" in combined_output:
        return  # idempotent: persist_state ran but produced no actual file changes
    raise RuntimeError(f"git commit failed: {result.stderr or result.stdout}")
