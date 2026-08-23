"""SandboxProvider interface: the seam between the agent and per-session sandboxes.

Both copilot_chat_model.py and claude_chat_model.py drive their sandbox purely through one-shot
`exec` calls (docker exec / az container exec -- see cli_agent_exec.py) -- there is no persistent
server and no long-lived connection between the agent process and the sandbox for either provider.
Copilot's own JSON-RPC/TCP session mechanism (RuntimeConnection.for_uri, a real `copilot --server`
process, and the connect handshake this module used to perform) was fully retired by Task 3's
CLI-exec rewrite; nothing on either sandbox backend publishes or listens on a port anymore.
wait_for_cli_ready() below is the one readiness check every SandboxProvider.provision() now polls
before returning: exec a version-check command until the sandbox's CLI tooling actually responds,
rather than trusting the container's "running" state (which flips true well before
bootstrap.sh/toolchain setup has finished).
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

_READY_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class SandboxSession:
    """Bookkeeping a SandboxProvider hands back after provision().

    session_id/host are real and load-bearing (exec_in_sandbox's dispatch key, and diagnostic
    logging). port/connection_token are inert leftovers of the retired TCP/for_uri connection
    scheme described in this module's own docstring -- always a dummy/empty value now, kept only
    because removing them ripples into copilot_chat_model.py/claude_chat_model.py's own
    `sandbox: SandboxSession | None` field.
    """

    session_id: str
    host: str
    port: int
    connection_token: str


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SandboxProvider(abc.ABC):
    """Provisions, tracks, and tears down per-session sandboxes.

    One sandbox per session_id (which callers derive from (owner, repo, branch, user) per the
    architecture plan's Section A) -- calling provision() again for a session_id that already
    has a live sandbox returns the existing one rather than starting a second.
    """

    @abc.abstractmethod
    async def provision(
        self,
        *,
        session_id: str,
        repo_clone_url: str,
        branch: str,
        work_branch: str,
        git_user_token: str,
        runtime_auth_token: str,
        image: str | None = None,
    ) -> SandboxSession:
        """Start (or reuse) the sandbox for session_id and return how to reach it.

        repo_clone_url/branch/git_user_token are used once, inside the sandbox, for the single
        git clone -- never written to a long-lived env var visible to arbitrary child processes
        (plan Section C.4's ordering guarantee). work_branch is this session's own unique git
        branch (agent/src/branch_naming.py, computed once at session creation and never
        recomputed here) -- passed straight through as the sandbox's WORK_BRANCH env var.
        runtime_auth_token is the active provider's own secret -- the shared Copilot PAT
        (agent/src/graph.py's GITHUB_TOKEN) or an Anthropic API key, whichever chat_model.PROVIDER
        currently selects -- written into the sandbox as COPILOT_GITHUB_TOKEN or ANTHROPIC_API_KEY
        (never both real) for its own coding-agent CLI to authenticate with. (Was documented here
        as COPILOT_SDK_AUTH_TOKEN, the correct name only for the old, fully-retired SDK-based
        `copilot --server` process -- task-12-report.md BUG B / task-12b traced this docstring as
        the design doc for what turned out to be a real defect in local_docker.py/azure_aci.py's
        own env-var name. Fixed once to plain GITHUB_TOKEN, per the real Copilot CLI's own
        documented env vars -- then corrected again, same task's fix-round-1, to
        COPILOT_GITHUB_TOKEN specifically: `gh`, git credential helpers, and any repo-supplied
        script all read a plain GITHUB_TOKEN ambiently with zero extra config, which would silently
        hand the shared fleet PAT to arbitrary repo-supplied tooling running under --no-ask-user.
        COPILOT_GITHUB_TOKEN authenticates the same CLI identically -- it is one of the same three
        documented names -- without that ambient exposure.)
        """

    @abc.abstractmethod
    async def terminate(self, session_id: str) -> None:
        """Tear down the sandbox for session_id. No-op if it isn't running."""

    async def discard_workspace(self, session_id: str) -> None:
        """Delete any persistent workspace state for session_id (explicit user close only --
        never called by idle reaping). Default no-op for providers without one."""

    @abc.abstractmethod
    async def touch(self, session_id: str) -> None:
        """Reset the idle-timeout clock for session_id's sandbox. No-op if it isn't running."""

    @abc.abstractmethod
    async def list_active(self) -> list[str]:
        """Return the session_ids of currently-running sandboxes."""

    @abc.abstractmethod
    async def exec_in_sandbox(self, session_id: str, command: str) -> ExecResult:
        """Run a shell command inside session_id's sandbox and return its result.

        This is the persistence layer's (workflow_persistence.py, git_ops.py) only channel to the
        working tree -- per the architecture plan's Section B, file I/O and git operations must
        happen wherever the clone actually lives (inside the sandbox), not in the agent's own
        process. `command` is run via `sh -c`, so the caller is responsible for shell-safe
        quoting (workflow_persistence.py base64-encodes file content for exactly this reason).
        """


async def wait_for_cli_ready(
    exec_fn: Callable[[str], Awaitable[tuple[int, str, str]]], version_command: str
) -> None:
    """Block until the CLI tool in the sandbox is ready to accept commands.

    exec_fn is a thin provider-specific wrapper (docker exec / az container exec) that executes
    a command in the sandbox and returns (returncode, stdout, stderr). version_command is the
    caller-supplied version-check command for whichever CLI is actually active (e.g. "claude
    --version" or "copilot --version") -- no default here, deliberately: a fallback would silently
    check the wrong binary for any caller that forgot to pass one, exactly the bug this parameter
    replaces. This function polls exec_fn(version_command) every 0.5s up to _READY_TIMEOUT_SECONDS.
    Once the command succeeds (returncode == 0), returns; otherwise raises RuntimeError on timeout
    with the last error observed.
    """
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    last_error: str | None = None

    while time.monotonic() < deadline:
        try:
            returncode, _, stderr = await exec_fn(version_command)
            if returncode == 0:
                return
            last_error = f"returncode {returncode}: {stderr}"
        except Exception as exc:
            last_error = str(exc)
        await asyncio.sleep(0.5)

    raise RuntimeError(
        f"CLI tool in sandbox did not become ready within {_READY_TIMEOUT_SECONDS}s "
        f"(last error: {last_error})"
    )
