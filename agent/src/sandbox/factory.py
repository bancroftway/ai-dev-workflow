"""SandboxProvider selection (architecture plan Section E: SANDBOX_PROVIDER=local|azure).

A single process-wide provider instance, not one per request/session -- LocalDockerProvider
(and the future AzureSessionsProvider) each own their own bookkeeping (running sandboxes, the
idle reaper task) that needs to persist across calls.
"""

from __future__ import annotations

import os

from .azure_aci import AzureContainerInstanceProvider
from .local_docker import LocalDockerProvider
from .provider import SandboxProvider

_provider: SandboxProvider | None = None


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
