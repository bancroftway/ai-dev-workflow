"""Docker Desktop/Engine-backed SandboxProvider for local development (plan Section E).

Mirrors the production Azure Container Apps sessions-pool provider's shape -- same
SandboxSession, same sandbox-image startup contract -- so switching SANDBOX_PROVIDER between
"local" and "azure" (see get_sandbox_provider()) is pure wiring, not a behavior change in
copilot_chat_model.py.

Explicit limitation, not parity (plan Section E): `docker run` publishes a real host TCP port
with nothing in between, so RuntimeConnection.for_uri("localhost:<port>") trivially succeeds here
regardless of whether Azure's sessions-pool proxy can carry the same raw-socket JSON-RPC protocol.
This provider working end-to-end is not evidence the Azure path will work.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field

from .provider import ExecResult, SandboxProvider, SandboxSession, wait_for_copilot_ready

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "ai-dev-workflow-sandbox:latest"
DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_COPILOT_PORT_IN_CONTAINER = 3000
_REAP_POLL_SECONDS = 60.0
_CONTAINER_NAME_PREFIX = "ai-dev-workflow-sandbox-"
# Matches entrypoint.sh's WORKSPACE_DIR -- the clone's own root, so persistence code can address
# ".ai-dev-workflow/..." paths without needing to know the sandbox's directory layout otherwise.
WORKSPACE_DIR_IN_CONTAINER = "/workspace/repo"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _run_docker(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


@dataclass
class _RunningSandbox:
    container_id: str
    host_port: int
    connection_token: str
    last_active: float = field(default_factory=time.monotonic)


class LocalDockerProvider(SandboxProvider):
    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._image = image
        self._idle_timeout_seconds = idle_timeout_seconds
        self._sandboxes: dict[str, _RunningSandbox] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

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
        async with self._lock:
            existing = self._sandboxes.get(session_id)
            if existing is not None:
                existing.last_active = time.monotonic()
                return SandboxSession(session_id, "localhost", existing.host_port, existing.connection_token)

            host_port = _free_port()
            connection_token = secrets.token_urlsafe(32)
            container_name = f"{_CONTAINER_NAME_PREFIX}{session_id}"

            # Best-effort cleanup of a stale container from a previous, uncleanly-terminated run
            # under the same session_id -- `docker run --name` fails outright if it's still around.
            await _run_docker("rm", "-f", container_name)

            returncode, container_id, stderr = await _run_docker(
                "run",
                "-d",
                "--rm",
                "--name",
                container_name,
                "-p",
                f"{host_port}:{_COPILOT_PORT_IN_CONTAINER}",
                "-e",
                f"REPO_CLONE_URL={repo_clone_url}",
                "-e",
                f"REPO_BRANCH={branch}",
                "-e",
                f"GIT_USER_TOKEN={git_user_token}",
                "-e",
                f"COPILOT_SDK_AUTH_TOKEN={copilot_auth_token}",
                "-e",
                f"COPILOT_CONNECTION_TOKEN={connection_token}",
                "-e",
                f"COPILOT_SERVER_PORT={_COPILOT_PORT_IN_CONTAINER}",
                image or self._image,
            )
            if returncode != 0:
                raise RuntimeError(f"docker run failed for session {session_id!r}: {stderr}")

            try:
                await wait_for_copilot_ready("localhost", host_port, connection_token)
            except Exception:
                await _run_docker("stop", container_id)
                raise

            self._sandboxes[session_id] = _RunningSandbox(container_id, host_port, connection_token)
            self._ensure_reaper_running()
            logger.info("Provisioned sandbox session_id=%s container=%s port=%d", session_id, container_id[:12], host_port)
            return SandboxSession(session_id, "localhost", host_port, connection_token)

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            sandbox = self._sandboxes.get(session_id)
            if sandbox is not None:
                sandbox.last_active = time.monotonic()

    async def terminate(self, session_id: str) -> None:
        async with self._lock:
            sandbox = self._sandboxes.pop(session_id, None)
        if sandbox is None:
            return
        logger.info("Terminating sandbox session_id=%s container=%s", session_id, sandbox.container_id[:12])
        await _run_docker("stop", sandbox.container_id)

    async def list_active(self) -> list[str]:
        async with self._lock:
            return list(self._sandboxes.keys())

    async def exec_in_sandbox(self, session_id: str, command: str) -> ExecResult:
        async with self._lock:
            sandbox = self._sandboxes.get(session_id)
        if sandbox is None:
            raise RuntimeError(f"no active sandbox for session_id={session_id!r}")

        returncode, stdout, stderr = await _run_docker(
            "exec", "-w", WORKSPACE_DIR_IN_CONTAINER, sandbox.container_id, "sh", "-c", command
        )
        return ExecResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def _ensure_reaper_running(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_idle_sandboxes())

    async def _reap_idle_sandboxes(self) -> None:
        """Docker Desktop has no idle-session GC of its own (plan Section E) -- this replicates
        the behavior Azure's sessions pool provides natively, so idle-cleanup logic gets
        exercised locally before it ever runs in production."""
        while True:
            await asyncio.sleep(_REAP_POLL_SECONDS)
            async with self._lock:
                now = time.monotonic()
                idle_session_ids = [
                    sid
                    for sid, sandbox in self._sandboxes.items()
                    if now - sandbox.last_active > self._idle_timeout_seconds
                ]
            for session_id in idle_session_ids:
                logger.info("Reaping idle sandbox session_id=%s", session_id)
                await self.terminate(session_id)
