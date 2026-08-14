"""SandboxProvider interface: the seam between the agent and per-session sandboxes.

copilot_chat_model.py constructs a CopilotClient against RuntimeConnection.for_uri(host:port,
connection_token=...) once it has a SandboxSession -- see the architecture plan's Section C.1,
which is grounded in the SDK's own client.py: connecting via for_uri skips _start_cli_server
entirely, so the *sandbox itself* must already be running `copilot --headless --auth-token-env
COPILOT_SDK_AUTH_TOKEN --port <N>` with COPILOT_SDK_AUTH_TOKEN/COPILOT_CONNECTION_TOKEN set in its
own environment before the agent ever connects.
"""

from __future__ import annotations

import abc
import asyncio
import json
import socket
import time
from dataclasses import dataclass

_READY_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class SandboxSession:
    """Everything copilot_chat_model.py needs to build a RuntimeConnection.for_uri(...) call."""

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
        git_user_token: str,
        copilot_auth_token: str,
        image: str | None = None,
    ) -> SandboxSession:
        """Start (or reuse) the sandbox for session_id and return how to reach it.

        repo_clone_url/branch/git_user_token are used once, inside the sandbox, for the single
        git clone -- never written to a long-lived env var visible to arbitrary child processes
        (plan Section C.4's ordering guarantee). copilot_auth_token is the shared Copilot PAT
        (agent/src/graph.py's GITHUB_TOKEN) the sandbox's own `copilot --server` process
        authenticates with; it is a no-op if passed to the agent's own CopilotClient once that
        client connects via for_uri (see this package's docstring).
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


async def wait_for_copilot_ready(host: str, port: int, connection_token: str) -> None:
    """Block until the sandbox's copilot --server has actually completed its own startup, not
    just bound its listening socket.

    Shared by every SandboxProvider implementation (LocalDockerProvider, AzureContainerInstanceProvider,
    ...) -- a bare TCP connect is NOT sufficient here, confirmed empirically
    (scratchpad/repro_warmup_window.py from the investigation this fixes): the CLI prints
    "listening on port" and accepts TCP connections within milliseconds, but its JSON-RPC layer
    takes several seconds longer to finish initializing. Any `connect` request that arrives before
    that finishes gets the connection dropped with zero response -- which is exactly what caused
    ~2/3 of CopilotChatModel session creations to fail with ProcessExitedError when this check only
    verified bare socket connectivity. Performing the real `connect` handshake as the readiness
    check (with the actual connection_token, not a placeholder) means provision() only returns
    once a caller connecting the same way the agent will is actually guaranteed to succeed.
    """
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    last_error: str | None = None
    message = {
        "jsonrpc": "2.0",
        "id": "readiness-check",
        "method": "connect",
        "params": {"token": connection_token},
    }
    body = json.dumps(message, separators=(",", ":")).encode()
    payload = f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0) as sock:
                sock.sendall(payload)
                response = sock.recv(4096)
        except OSError as exc:
            last_error = str(exc)
            await asyncio.sleep(0.3)
            continue
        if not response:
            last_error = "connection closed with no response (server not ready yet)"
            await asyncio.sleep(0.3)
            continue
        # Any well-formed JSON-RPC response -- success or an error frame like
        # AUTHENTICATION_FAILED -- proves the RPC layer itself is live, which is all this check
        # needs to confirm. A malformed/partial frame means it's still warming up.
        try:
            _, _, raw_body = response.partition(b"\r\n\r\n")
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            last_error = f"unparseable response during warmup: {response[:200]!r}"
            await asyncio.sleep(0.3)
            continue
        if "result" in parsed or "error" in parsed:
            return
        last_error = f"unexpected response shape: {parsed!r}"
        await asyncio.sleep(0.3)
    raise RuntimeError(
        f"sandbox at {host}:{port} did not complete a connect handshake within "
        f"{_READY_TIMEOUT_SECONDS}s (last error: {last_error})"
    )
