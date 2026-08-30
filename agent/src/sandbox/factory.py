"""SandboxProvider selection (architecture plan Section E: SANDBOX_PROVIDER=local|azure).

A single process-wide provider instance, not one per request/session -- LocalDockerProvider
(and the future AzureSessionsProvider) each own their own bookkeeping (running sandboxes, the
idle reaper task) that needs to persist across calls.
"""

from __future__ import annotations

import logging
import os

from . import registry
from .azure_aci import AzureContainerInstanceProvider
from .local_docker import LocalDockerProvider
from .provider import SandboxProvider

logger = logging.getLogger(__name__)

_provider: SandboxProvider | None = None


async def end_session_container(thread_id: str) -> None:
    """Best-effort immediate teardown of a session's container, if this process holds one.

    Called (fire-and-forget) from session_store.close_session -- the single choke point every
    terminal transition (failed OR completed) passes through -- so an errored/escalated/finished
    run frees its per-repo cap slot in seconds instead of waiting on the 30-minute idle reaper.
    No-ops harmlessly when the registry has no entry: off-process callers of close_session (the
    CI runner's deploy_drain) and already-torn-down sessions land here with nothing to do.
    terminate() itself routes through registry.pop, so no extra eviction is needed here."""
    if registry.get(thread_id) is None:
        return
    try:
        await get_sandbox_provider().terminate(thread_id)
        logger.info("terminated container for ended session thread_id=%s", thread_id)
    except Exception:  # noqa: BLE001 -- teardown is a cleanup courtesy; never fail the caller
        logger.warning("container teardown failed for thread_id=%s", thread_id, exc_info=True)


def get_sandbox_provider() -> SandboxProvider:
    global _provider
    if _provider is not None:
        return _provider

    kind = os.environ.get("SANDBOX_PROVIDER", "local")
    if kind == "local":
        _provider = LocalDockerProvider()
    elif kind == "azure":
        # Azure Container Instances, not any Container Apps primitive -- Open Risk #1 (plan
        # Section C.3) was resolved against a real subscription: plain Container Apps ingress,
        # even "TCP transport", could not carry RuntimeConnection.for_uri's raw TCP protocol
        # (reproducible connection timeout), while ACI carried it cleanly with both a public and
        # a VNET-injected private IP. See AzureContainerInstanceProvider's own required env vars.
        _provider = AzureContainerInstanceProvider()
    else:
        raise ValueError(f"Unknown SANDBOX_PROVIDER={kind!r}, expected 'local' or 'azure'")
    return _provider
