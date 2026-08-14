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
import io
import json
import logging
import secrets
import socket
import tarfile
import time
from dataclasses import dataclass, field

from . import registry
from ..telemetry import traced_exec
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


# Package-cache volume, mounted at the image's own AIDW_CACHE_DIR. Scoped per repo *owner* rather
# than globally or per session: a global volume would let one customer's repo write package
# tarballs a different customer's session later reads, and a per-session volume caches nothing.
# Only the cache is shared -- /opt/aidw/tools (executables) deliberately stays in the container's
# writable layer, since an executable on PATH is what would actually carry an attack across
# sessions. See the Dockerfile's /opt/aidw section.
# ponytail: no eviction -- these grow without bound. Add a size check plus `npm cache verify` /
# `mise cache clear` on provision if a dev machine ever notices.
_CACHE_DIR_IN_CONTAINER = "/opt/aidw/cache"
_CACHE_VOLUME_PREFIX = "aidw-cache-"

# Workspace volume, mounted at /workspace (the PARENT of the clone) so the entrypoint can
# `rm -rf /workspace/repo` on a corrupt clone without fighting a mount point. This is what lets
# the work tree -- and any commits the broken-push era would have lost -- survive container
# removal (reprovision's rm -f, the idle reaper, an agent crash). Docker creates it on first use
# and chowns the volume root to match the image's vscode-owned /workspace; NEVER pre-create it
# with `docker volume create` -- a pre-created volume root stays root-owned and every clone then
# fails EACCES.
# ponytail: per-session and unbounded in count, no eviction -- explicit DELETE /sessions removes
# its volume, idle reaps keep it (that durability is the point). `docker volume ls -f
# name=aidw-ws-` and prune by hand if a dev machine notices.
_WS_VOLUME_PREFIX = "aidw-ws-"

# One-shot clone-credential drop for the entrypoint (see _inject_git_token). Lives in the
# vscode-owned home dir, NOT the persistent /workspace volume.
_GIT_TOKEN_DIR_IN_CONTAINER = "/home/vscode"
_GIT_TOKEN_FILENAME = ".aidw-git-token"


def _cache_volume_name(repo_clone_url: str) -> str:
    """Docker volume name for this repo's owner, derived from the clone URL.

    Falls back to a single "shared" volume only when no owner can be parsed -- never to a
    per-customer guess.
    """
    trimmed = repo_clone_url.rstrip("/").removesuffix(".git")
    parts = [segment for segment in trimmed.split("/") if segment]
    owner = parts[-2] if len(parts) >= 2 else ""
    slug = "".join(char if char.isalnum() or char in "-_" else "-" for char in owner.lower())
    return f"{_CACHE_VOLUME_PREFIX}{slug or 'shared'}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _run_docker(*args: str, input_bytes: bytes | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=input_bytes)
    return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


async def _inject_git_token(container_id: str, token: str) -> None:
    """docker cp a one-shot token file into the created-but-not-started container.

    The token never appears in the container's env (`docker inspect` shows nothing) and never
    touches host disk -- it travels as an in-memory tar over stdin. The entrypoint reads and
    deletes the file before anything repo-supplied can run.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = token.encode("utf-8")
        info = tarfile.TarInfo(_GIT_TOKEN_FILENAME)
        info.size = len(data)
        # docker cp ignores tar uid/gid (file lands root-owned), so 0644 is what lets the vscode
        # entrypoint read it. World-readable only between create and start -- no process is
        # running yet, and the entrypoint (PID 1) deletes it before anything repo-supplied runs.
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    returncode, _, stderr = await _run_docker(
        "cp", "-", f"{container_id}:{_GIT_TOKEN_DIR_IN_CONTAINER}", input_bytes=buf.getvalue()
    )
    if returncode != 0:
        raise RuntimeError(f"docker cp of git token failed: {stderr}")


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

            container_name = f"{_CONTAINER_NAME_PREFIX}{session_id}"

            # A container that survived an agent restart is reattached, not destroyed -- the old
            # path unconditionally `rm -f`'d it, throwing away the clone and every unpushed
            # commit, and raced --rm's autoremove into a name-conflict 502.
            reattached = await self._try_reattach(session_id, container_name, image or self._image)
            if reattached is not None:
                self._sandboxes[session_id] = reattached
                self._ensure_reaper_running()
                logger.info(
                    "Reattached sandbox session_id=%s container=%s port=%d",
                    session_id, reattached.container_id[:12], reattached.host_port,
                )
                return SandboxSession(session_id, "localhost", reattached.host_port, reattached.connection_token)

            host_port = _free_port()
            connection_token = secrets.token_urlsafe(32)

            # Best-effort cleanup of a stale (not-reattachable) container from a previous,
            # uncleanly-terminated run under the same session_id -- `docker create --name` fails
            # outright if it's still around.
            await _run_docker("rm", "-f", container_name)

            # create -> cp(token) -> start, not `docker run`: the clone credential rides in as a
            # one-shot file (see _inject_git_token) instead of an env var that would stay readable
            # forever via `docker inspect`.
            returncode, container_id, stderr = await _run_docker(
                "create",
                "--rm",
                "--name",
                container_name,
                "--label",
                f"aidw.session={session_id}",
                "-p",
                f"{host_port}:{_COPILOT_PORT_IN_CONTAINER}",
                # Created on demand by Docker; nothing pre-provisions it. The container works
                # identically without this mount (every cache path is an ENV in the image), so
                # this is purely a warm-start accelerator.
                "-v",
                f"{_cache_volume_name(repo_clone_url)}:{_CACHE_DIR_IN_CONTAINER}",
                # Workspace durability -- see _WS_VOLUME_PREFIX comment above.
                "-v",
                f"{_WS_VOLUME_PREFIX}{session_id}:/workspace",
                "-e",
                f"REPO_CLONE_URL={repo_clone_url}",
                "-e",
                f"REPO_BRANCH={branch}",
                # Suffixes the tool-owned work branch (entrypoint.sh) so two users on the same
                # repo+branch never share -- and force-push over -- one remote branch.
                "-e",
                f"AIDW_SESSION_ID={session_id}",
                "-e",
                f"COPILOT_SDK_AUTH_TOKEN={copilot_auth_token}",
                "-e",
                f"COPILOT_CONNECTION_TOKEN={connection_token}",
                "-e",
                f"COPILOT_SERVER_PORT={_COPILOT_PORT_IN_CONTAINER}",
                # Stamped into bootstrap.sh's toolchain report, so a "this repo needed a toolchain
                # the image lacks" finding is attributable to a specific image rather than to the
                # fleet in general.
                "-e",
                f"AIDW_IMAGE_REF={image or self._image}",
                image or self._image,
            )
            if returncode != 0:
                raise RuntimeError(f"docker create failed for session {session_id!r}: {stderr}")

            try:
                await _inject_git_token(container_id, git_user_token)
                returncode, _, stderr = await _run_docker("start", container_id)
                if returncode != 0:
                    raise RuntimeError(f"docker start failed for session {session_id!r}: {stderr}")
                await wait_for_copilot_ready("localhost", host_port, connection_token)
            except Exception:
                # --rm never armed for a never-started container; rm -f covers both cases. The
                # workspace volume survives (named volumes are never auto-removed).
                await _run_docker("rm", "-f", container_name)
                raise

            self._sandboxes[session_id] = _RunningSandbox(container_id, host_port, connection_token)
            self._ensure_reaper_running()
            logger.info("Provisioned sandbox session_id=%s container=%s port=%d", session_id, container_id[:12], host_port)
            return SandboxSession(session_id, "localhost", host_port, connection_token)

    async def _try_reattach(
        self, session_id: str, container_name: str, image_ref: str
    ) -> _RunningSandbox | None:
        """Rebuild bookkeeping for a container that survived an agent restart.

        None means the caller falls through to rm -f + fresh run. Declines when the container
        isn't running, is built from a different image than the current tag points at (a rebuild
        must not leave sessions on stale entrypoints -- AIDW_IMAGE_REF is just the tag string, so
        the image ID is what's compared), or fails the copilot liveness probe. The recovered
        connection token MUST be reused -- the running copilot server gates on the one in its env.
        """
        returncode, out, _ = await _run_docker("inspect", container_name)
        if returncode != 0:
            return None
        try:
            info = json.loads(out)[0]
            if not info["State"]["Running"]:
                return None
            rc_img, current_image_id, _ = await _run_docker(
                "image", "inspect", image_ref, "--format", "{{.Id}}"
            )
            if rc_img != 0 or info["Image"] != current_image_id:
                return None
            env = dict(e.split("=", 1) for e in info["Config"]["Env"] if "=" in e)
            token = env["COPILOT_CONNECTION_TOKEN"]
            port = int(info["NetworkSettings"]["Ports"][f"{_COPILOT_PORT_IN_CONTAINER}/tcp"][0]["HostPort"])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None
        try:
            await wait_for_copilot_ready("localhost", port, token)
        except Exception:  # noqa: BLE001 -- liveness probe; any failure means "don't reattach"
            return None
        return _RunningSandbox(info["Id"], port, token)

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            sandbox = self._sandboxes.get(session_id)
            if sandbox is not None:
                sandbox.last_active = time.monotonic()

    async def terminate(self, session_id: str) -> None:
        async with self._lock:
            sandbox = self._sandboxes.pop(session_id, None)
        # The reaper routes through here too; without this pop the ~40 registry.get() guards
        # across the pipeline kept seeing a phantom session after an idle reap.
        registry.pop(session_id)
        if sandbox is None:
            return
        logger.info("Terminating sandbox session_id=%s container=%s", session_id, sandbox.container_id[:12])
        await _run_docker("stop", sandbox.container_id)

    async def discard_workspace(self, session_id: str) -> None:
        # Explicit close is the one signal that means "discard my state"; idle reaps keep it.
        # terminate()'s `docker stop` hands removal to --rm's async autoremove, so force-remove
        # the container first and retry briefly -- a volume rm racing the autoremove fails with
        # "volume is in use" and used to leave the volume behind.
        await _run_docker("rm", "-f", f"{_CONTAINER_NAME_PREFIX}{session_id}")
        volume = f"{_WS_VOLUME_PREFIX}{session_id}"
        for _ in range(10):
            returncode, _, _ = await _run_docker("volume", "rm", "-f", volume)
            if returncode == 0:
                return
            await asyncio.sleep(1.0)
        logger.warning("workspace volume %s could not be removed (still in use?)", volume)

    async def list_active(self) -> list[str]:
        async with self._lock:
            return list(self._sandboxes.keys())

    @traced_exec
    async def exec_in_sandbox(self, session_id: str, command: str) -> ExecResult:
        async with self._lock:
            sandbox = self._sandboxes.get(session_id)
            if sandbox is not None:
                # Every exec is activity: keep the idle reaper from killing a live run.
                sandbox.last_active = time.monotonic()
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
