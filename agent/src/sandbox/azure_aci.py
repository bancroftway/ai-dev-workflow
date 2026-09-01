"""Azure Container Instances (ACI)-backed SandboxProvider for production (architecture plan
Section C/D).

Uses the `az` CLI via subprocess, matching LocalDockerProvider's shape -- `az` already works for
both local testing (interactive `az login`) and production (a Container App's managed identity
via `az login --identity`, or DefaultAzureCredential-equivalent ambient auth), so this avoids a
separate SDK dependency and its own auth wiring.

Grounded in a live validation session against a real Azure subscription (plan Section C.3's Open
Risk #1): plain Container Apps ingress -- even "TCP transport" -- could not carry
RuntimeConnection.for_uri's raw TCP protocol (a connect() timeout, reproduced 4x). A plain ACI
container group could, cleanly, with both a public IP and (separately) a VNET-injected private
IP. This provider targets ACI specifically because of that result, not any Container Apps
primitive.

Known gap, not resolved here: `exec_in_sandbox` uses the same `sh -c <command>` pattern as
LocalDockerProvider, but `az container exec`'s underlying command-line handling has not been
verified to preserve shell operators (`&&`, `|`, `>`) the way `docker exec` does -- ACI's
container-*create*-time `--command-line` was empirically found to naively whitespace-split
rather than invoke a shell (this cost real debugging time during the Open Risk #1 investigation;
see the architecture plan). If `exec_in_sandbox` breaks the same way when actually exercised by
workflow_persistence.py/git_ops.py, that is the next thing to fix here, not a surprise to treat
as a new investigation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field

from . import registry
from .. import config as workflow_config
from ..telemetry import traced_exec
from .provider import ExecResult, SandboxProvider, SandboxSession, runtime_auth_env, wait_for_cli_ready

logger = logging.getLogger(__name__)

_CONTAINER_NAME_PREFIX = "aidevworkflow-sandbox-"
_IP_WAIT_TIMEOUT_SECONDS = 60.0
_REAP_POLL_SECONDS = 60.0
WORKSPACE_DIR_IN_CONTAINER = "/workspace/repo"
# Matches the image's own AIDW_CACHE_DIR and LocalDockerProvider's mount point.
_CACHE_DIR_IN_CONTAINER = "/opt/aidw/cache"


def _container_group_name(session_id: str) -> str:
    # ACI container group names must be <= 63 chars, lowercase alphanumeric + hyphens -- a
    # raw thread_id (a sha256 hex digest already, per src/lib/workflow-thread.ts) is already
    # hex/lowercase, but hash again here so this doesn't silently assume that about every caller.
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:24]
    return f"{_CONTAINER_NAME_PREFIX}{digest}"


def _resolve_az_executable() -> str:
    # asyncio.create_subprocess_exec bypasses the shell entirely, so on Windows -- where `az` is
    # actually `az.cmd`, a batch wrapper -- it must be given the exact resolved filename; only a
    # real shell (which this deliberately isn't, to avoid a whole other class of quoting issues)
    # auto-tries PATHEXT extensions the way typing `az` at a prompt does.
    #
    # Resolved lazily (on first real use), not at import time: this module is imported
    # unconditionally by sandbox/factory.py regardless of SANDBOX_PROVIDER, so an eager resolution
    # would break `SANDBOX_PROVIDER=local` too on any host/image without the az CLI installed
    # (agent/Dockerfile does not install it today -- required before this provider is usable in
    # a real deployment, not just for local testing against this module).
    resolved = shutil.which("az")
    if resolved is None:
        raise RuntimeError("az CLI not found on PATH")
    return resolved


async def _run_az(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        _resolve_az_executable(), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


@dataclass
class _RunningSandbox:
    container_name: str
    ip: str
    connection_token: str
    branch: str
    last_active: float = field(default_factory=time.monotonic)


class AzureContainerInstanceProvider(SandboxProvider):
    """Environment variables (the agent's own env, not the sandbox's):

    Required:
      AZURE_RESOURCE_GROUP     -- resource group to create/delete ACI container groups in.
      AZURE_ACI_SANDBOX_IMAGE  -- e.g. myacr.azurecr.io/ai-dev-workflow-sandbox:latest.
    Optional (production should set both; omitting them falls back to a public IP, only
    appropriate for throwaway testing -- see the plan's Section C.3 note on why private+VNET is
    the production requirement):
      AZURE_ACI_VNET_NAME, AZURE_ACI_SUBNET_NAME
    Optional, for a private (non-public) registry -- prefer AZURE_ACI_IDENTITY (a pre-created
    user-assigned managed identity's resource ID, already granted AcrPull -- see infra/main.bicep)
    over registry username/password, which exists mainly for this implementation's own ad hoc
    validation against a registry with the admin account enabled:
      AZURE_ACI_IDENTITY  -- resource ID of a user-assigned identity with AcrPull; used for both
                             the container group's own identity and its ACR pull credential.
      AZURE_ACI_REGISTRY_SERVER, AZURE_ACI_REGISTRY_USERNAME, AZURE_ACI_REGISTRY_PASSWORD
                          -- used only if AZURE_ACI_IDENTITY is unset.
    Optional:
      AZURE_ACI_LOCATION -- defaults to the resource group's own location if unset.
    Optional, and off by default -- the package-cache share mounted at /opt/aidw/cache (all three
    must be set together, or none):
      AIDW_CACHE_SHARE, AIDW_CACHE_STORAGE_ACCOUNT, AIDW_CACHE_STORAGE_KEY
    """

    def __init__(self, *, idle_timeout_seconds: float = 1800.0) -> None:
        self._resource_group = os.environ["AZURE_RESOURCE_GROUP"]
        self._sandbox_image = os.environ["AZURE_ACI_SANDBOX_IMAGE"]
        self._vnet_name = os.environ.get("AZURE_ACI_VNET_NAME")
        self._subnet_name = os.environ.get("AZURE_ACI_SUBNET_NAME")
        self._identity = os.environ.get("AZURE_ACI_IDENTITY")
        self._registry_server = os.environ.get("AZURE_ACI_REGISTRY_SERVER")
        self._registry_username = os.environ.get("AZURE_ACI_REGISTRY_USERNAME")
        self._registry_password = os.environ.get("AZURE_ACI_REGISTRY_PASSWORD")
        self._location = os.environ.get("AZURE_ACI_LOCATION")
        self._cache_share = os.environ.get("AIDW_CACHE_SHARE")
        self._cache_storage_account = os.environ.get("AIDW_CACHE_STORAGE_ACCOUNT")
        self._cache_storage_key = os.environ.get("AIDW_CACHE_STORAGE_KEY")
        # Parity with LocalDockerProvider: same env var, same override semantics. The idle
        # reaper's blind spot during a long silent turn is closed by run_turn's own polling loop
        # (cli_agent_exec.py) plus exec_in_sandbox's `last_active` bump below, both of which fire
        # repeatedly over the course of a single long turn for either provider; this env var
        # remains an explicit override for a caller that wants a different idle window (e.g.
        # run_headless.py's belt-and-suspenders 86400s).
        env_timeout = os.environ.get("AIDW_SANDBOX_IDLE_TIMEOUT")
        self._idle_timeout_seconds = float(env_timeout) if env_timeout else idle_timeout_seconds
        self._sandboxes: dict[str, _RunningSandbox] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def provision(
        self,
        *,
        session_id: str,
        repo_clone_url: str,
        branch: str,
        work_branch: str,
        git_user_token: str,
        runtime_auth_token: str,
        provider: str,
        runtime_auth_kind: str | None = None,
        image: str | None = None,
        scaffold_new_repo: bool = False,
        project_name: str | None = None,
    ) -> SandboxSession:
        # Phase E audit I-3: `provider` used to be resolved HERE, live, on every call
        # ("provisioning a new session is exactly the moment a live setting change should take
        # effect"). True for a genuinely new session, wrong for a reprovision of one already
        # pinned to a run's own state["provider"] -- a container group deleted and recreated after
        # an admin flips the org setting would come back built for the NEW provider while the
        # checkpointed graph kept dispatching to the OLD one, failing auth on every turn. Fixed one
        # layer up instead: sessions_api.provision_session now resolves "this session's stored
        # provider, or live if there's no prior row" before calling here, so this method just uses
        # whatever it's told -- mirrors local_docker.py's identical fix; see SandboxProvider.
        # provision's own docstring (provider.py) for the full reasoning.
        async with self._lock:
            existing = self._sandboxes.get(session_id)
            if existing is not None:
                if existing.branch == branch:
                    existing.last_active = time.monotonic()
                    return SandboxSession(session_id, existing.ip, 0, existing.connection_token)
                # PR-target change mid-session (BLOCKER fix, mirrors LocalDockerProvider): unlike
                # the local provider, ACI has no cross-restart reattach to keep in sync with
                # reality, so the in-memory branch this session was provisioned with is already
                # the single source of truth -- no docker-inspect-style lookup needed.
                logger.info(
                    "PR target changed for session_id=%s (%s -> %s) -- deleting old container "
                    "group and reprovisioning",
                    session_id, existing.branch, branch,
                )
                await _run_az(
                    "container", "delete", "--resource-group", self._resource_group,
                    "--name", existing.container_name, "--yes",
                )
                del self._sandboxes[session_id]
                # Mirrors LocalDockerProvider: a destruction path that bypasses registry.pop
                # because the reprovision below overwrites the registry entry anyway. The
                # replacement group also gets a fresh $HOME, so every cached Claude/Copilot
                # session id for this thread is unresumable against it -- forget them here or the
                # next stage's --resume/--session-id points at a session that never existed in
                # the new container.
                #
                # forget_thread_sessions_everywhere(), not forget_thread_sessions(session_id,
                # provider=provider): `provider` above is now the caller's already-resolved choice
                # (Phase E audit I-3 -- normally this session's own stored provider, which is
                # exactly the run's pinned state["provider"]), but it can still theoretically differ
                # from whatever the OLD container's sessions actually dispatched to (e.g. a
                # pre-migration row with no stored provider, whose caller fell back to a live read
                # that has since drifted) -- evicting only `provider`'s dict here could still miss
                # the OLD container's real provider in that edge case. Mirrors local_docker.py's
                # identical fix; see chat_model.forget_thread_sessions_everywhere's own docstring
                # for why evicting both is always safe here.
                from ..chat_model import forget_thread_sessions_everywhere

                forget_thread_sessions_everywhere(session_id)

            name = _container_group_name(session_id)
            # Inert now -- nothing listens on a port for either provider (Copilot's TCP/JSON-RPC
            # session mechanism was retired by Task 3's CLI-exec rewrite), so there's no real
            # value to generate. Kept only because SandboxSession/_RunningSandbox's shape still
            # has this field; see provider.py.
            connection_token = ""

            # Best-effort cleanup of a stale container group from a previous, uncleanly-terminated
            # run under the same session_id -- `az container create --name` fails outright if a
            # group with that name still exists.
            await _run_az("container", "delete", "--resource-group", self._resource_group, "--name", name, "--yes")

            args = [
                "container", "create",
                "--resource-group", self._resource_group,
                "--name", name,
                "--image", image or self._sandbox_image,
                "--os-type", "Linux",
                "--cpu", "1",
                "--memory", "2",
                "--restart-policy", "Never",
                "--environment-variables",
                f"REPO_BRANCH={branch}",
                f"WORK_BRANCH={work_branch}",
                f"AGENT_PROVIDER={provider}",
                f"AIDW_IMAGE_REF={image or self._sandbox_image}",
            ]
            if scaffold_new_repo:
                # "+ New Project" case only (Part 3 plan, Ruling 6) -- entrypoint.sh reads these to
                # git-init-and-push instead of cloning. Appended to the SAME --environment-variables
                # group above (az's own CLI groups consecutive non-flag args under whichever `--...`
                # flag preceded them) -- must land before --secure-environment-variables starts its
                # own group below, not after. Never set on an ordinary provision, so today's clone
                # path is byte-for-byte unchanged for every other caller.
                args += ["SCAFFOLD_NEW_REPO=1", f"PROJECT_NAME={project_name}"]
            args += [
                "--secure-environment-variables",
                f"REPO_CLONE_URL={repo_clone_url}",
                f"GIT_USER_TOKEN={git_user_token}",
                # Provider-credential env pairs (runtime_auth_env, sandbox/provider.py):
                # COPILOT_GITHUB_TOKEN always real-or-empty; exactly one of ANTHROPIC_API_KEY /
                # CLAUDE_CODE_OAUTH_TOKEN when provider == "claude" (Phase E audit C-1) -- see that
                # helper's docstring for the full name-choice history (task-12 BUG B, task-12b
                # fix-round-1). Mirrors local_docker.py's identical splice.
                *(
                    f"{name}={value}"
                    for name, value in runtime_auth_env(provider, runtime_auth_token, runtime_auth_kind)
                ),
            ]
            if self._location:
                args += ["--location", self._location]
            # Package-cache share, the ACI counterpart to LocalDockerProvider's named volume.
            # OFF unless AIDW_CACHE_SHARE names a share, and deliberately so: Azure Files is SMB,
            # whose many-small-file throughput is poor enough that a package cache on it can be
            # slower than re-downloading. Turn it on after measuring, not on assumption.
            # /opt/aidw/tools is never placed here -- exec bits and symlinks do not survive
            # faithfully, and mise-installed toolchains need both.
            if self._cache_share and self._cache_storage_account and self._cache_storage_key:
                args += [
                    "--azure-file-volume-share-name", self._cache_share,
                    "--azure-file-volume-account-name", self._cache_storage_account,
                    "--azure-file-volume-account-key", self._cache_storage_key,
                    "--azure-file-volume-mount-path", _CACHE_DIR_IN_CONTAINER,
                ]
            if self._vnet_name and self._subnet_name:
                args += ["--vnet", self._vnet_name, "--subnet", self._subnet_name, "--ip-address", "Private"]
            else:
                logger.warning(
                    "AZURE_ACI_VNET_NAME/AZURE_ACI_SUBNET_NAME not set -- provisioning session_id=%s "
                    "with a PUBLIC IP. Fine for testing, wrong for production (plan Section C.3).",
                    session_id,
                )
                args += ["--ip-address", "Public"]
            if self._identity:
                # Preferred, production path: the container group pulls the image using its own
                # user-assigned identity (already granted AcrPull -- infra/main.bicep), no
                # credential of any kind passed at create time.
                args += ["--assign-identity", self._identity, "--acr-identity", self._identity]
            elif self._registry_server:
                args += [
                    "--registry-login-server", self._registry_server,
                    "--registry-username", self._registry_username or "",
                    "--registry-password", self._registry_password or "",
                ]

            # Retried as a whole (fresh container group) up to SANDBOX_PROVISION_RETRY_ATTEMPTS
            # times -- mirrors LocalDockerProvider.provision's own retry. wait_for_cli_ready
            # already polls its own exec continuously for its own 60s deadline, so re-polling the
            # SAME unresponsive group after it raises would just wait out an identical timeout
            # again; only a fresh `az container create` can distinguish "this group is just slow"
            # from "this group never came up at all."
            attempts = max(1, workflow_config.SANDBOX_PROVISION_RETRY_ATTEMPTS)
            last_exc: Exception | None = None
            ip = ""
            for attempt in range(attempts):
                if attempt > 0:
                    # This loop's own previous attempt's group, named identically -- `az container
                    # create --name` fails outright if it's still around.
                    await _run_az("container", "delete", "--resource-group", self._resource_group, "--name", name, "--yes")
                returncode, stdout, stderr = await _run_az(*args)
                if returncode != 0:
                    raise RuntimeError(f"az container create failed for session {session_id!r}: {stderr}")
                try:
                    ip = await self._resolve_ip(name, create_output=stdout)

                    async def _exec(cmd: str) -> tuple[int, str, str]:
                        return await _run_az(
                            "container", "exec",
                            "--resource-group", self._resource_group,
                            "--name", name,
                            "--exec-command", f"/bin/sh -c \"cd {WORKSPACE_DIR_IN_CONTAINER} && {cmd}\"",
                        )

                    await wait_for_cli_ready(_exec, version_command=f"{provider} --version")
                    last_exc = None
                    break
                except Exception as exc:
                    await _run_az("container", "delete", "--resource-group", self._resource_group, "--name", name, "--yes")
                    last_exc = exc
                    if attempt < attempts - 1:
                        logger.warning(
                            "ACI sandbox provision attempt %d/%d failed for session_id=%s, retrying with a fresh container group: %s",
                            attempt + 1, attempts, session_id, exc,
                        )
            if last_exc is not None:
                raise last_exc

            self._sandboxes[session_id] = _RunningSandbox(name, ip, connection_token, branch)
            self._ensure_reaper_running()
            logger.info("Provisioned ACI sandbox session_id=%s container_group=%s ip=%s", session_id, name, ip)
            return SandboxSession(session_id, ip, 0, connection_token)

    async def _resolve_ip(self, container_group_name: str, *, create_output: str) -> str:
        """`az container create`'s own JSON output usually already has the IP; fall back to
        polling `container show` for cases where allocation completes a moment after create
        returns (observed during the Open Risk #1 validation session for private/VNET IPs)."""
        try:
            parsed = json.loads(create_output)
            ip = parsed.get("ipAddress", {}).get("ip")
            if ip:
                return ip
        except (json.JSONDecodeError, AttributeError):
            pass

        deadline = time.monotonic() + _IP_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            returncode, stdout, _stderr = await _run_az(
                "container", "show",
                "--resource-group", self._resource_group,
                "--name", container_group_name,
                "--query", "ipAddress.ip",
                "--output", "tsv",
            )
            if returncode == 0 and stdout:
                return stdout
            await asyncio.sleep(1.0)
        raise RuntimeError(f"container group {container_group_name!r} never got an IP assigned")

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            sandbox = self._sandboxes.get(session_id)
            if sandbox is not None:
                sandbox.last_active = time.monotonic()

    async def terminate(self, session_id: str) -> None:
        async with self._lock:
            sandbox = self._sandboxes.pop(session_id, None)
        # The reaper routes through here too; without this pop the registry.get() guards across
        # the pipeline kept seeing a phantom session after an idle reap.
        registry.pop(session_id)
        if sandbox is None:
            return
        logger.info("Terminating ACI sandbox session_id=%s container_group=%s", session_id, sandbox.container_name)
        await _run_az(
            "container", "delete",
            "--resource-group", self._resource_group,
            "--name", sandbox.container_name,
            "--yes",
        )

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

        returncode, stdout, stderr = await _run_az(
            "container", "exec",
            "--resource-group", self._resource_group,
            "--name", sandbox.container_name,
            "--exec-command", f"/bin/sh -c \"cd {WORKSPACE_DIR_IN_CONTAINER} && {command}\"",
        )
        return ExecResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def _ensure_reaper_running(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_idle_sandboxes())

    async def _reap_idle_sandboxes(self) -> None:
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
                logger.info("Reaping idle ACI sandbox session_id=%s", session_id)
                await self.terminate(session_id)
