"""Org-wide coding-agent credential (Anthropic API key or Copilot PAT) -- ONE secret, in its own
dedicated Key Vault (infra/main.bicep's `orgVault`), read/written with the agent's OWN STANDING
identity.

Deliberately NOT agent/src/keyvault.py's pattern. That module's whole point is "the agent holds no
standing vault access, only an OBO-exchanged token scoped to whichever user is signed in" --
exactly right for a *per-user, per-repo* vault, where "as which user" is a meaningful question
with a real answer. It has no answer here: this is the ONE credential the whole fleet's sessions
use, configured by whichever admin last saved it, read by whatever session happens to provision
next -- there is no natural per-user owner to exchange an OBO assertion on behalf of (plan Part 4
Ruling 1). So this module uses `DefaultAzureCredential`, not `OnBehalfOfCredential`: no
`entra_assertion` parameter anywhere below, by design.

Credential class precedent: agent/src/db.py's `_azure_access_token_struct()` already uses the same
sync `DefaultAzureCredential` for its own standing AAD SQL auth -- this codebase's existing,
established pattern for "standing, non-OBO" Azure access (independently corroborated by
sandbox/azure_aci.py's module docstring: "az login" interactively for local dev, a Container App's
system-assigned managed identity in production, no separate wiring for either). We use the
`azure.identity.aio` async variant here to match keyvault.py's own async SecretClient usage, not
because the credential class differs -- same DefaultAzureCredential resolution order either way.

Vault target: AZURE_ORG_VAULT_URI (infra/main.bicep wires this to orgVault's vaultUri). The agent's
managed identity holds "Key Vault Secrets Officer" on that vault ONLY -- see the bicep comments
next to `kvSecretsOfficerRoleAgent` for exactly why "Key Vault Secrets User" (the role Ruling 1's
own text names) is NOT what's actually granted: it cannot set a secret, and set_org_credential
below needs to.

Secret name: fixed and well-known (ORG_CREDENTIAL_SECRET_NAME below), not generated. Key Vault's
own secret versioning already gives every set_org_credential call a new, individually retrievable
version under the same name -- this codebase doesn't need to invent a naming/versioning scheme on
top of a mechanism the platform already provides.

Offline self-check (name constant + exception identity only -- no live vault I/O in this
environment, matching keyvault.py's own self-check limitation on this branch):
`cd agent && uv run python -m src.org_credential_vault`.
"""

from __future__ import annotations

import asyncio
import os

from .keyvault import VaultAccessError

# Fixed, well-known secret name. Every set_org_credential call writes a new VERSION under this
# same name (Key Vault's own versioning) rather than this codebase generating one. Documented here
# as the single owner of this string -- org_settings.credential_secret_name (Task 1) is populated
# with exactly this value, never anything else.
ORG_CREDENTIAL_SECRET_NAME = "org-provider-credential"

# Bounds a real Key Vault round trip so a slow/unreachable vault degrades to a clear, fast
# VaultAccessError instead of hanging the caller indefinitely -- found by the whole-branch
# re-review: get_org_credential() is now called from _org_settings_response() (sessions_api.py),
# which is polled by the frontend's settings-check on every page mount/repo switch, for every
# signed-in user. Shorter than _probe_provider_credential's 30s (sessions_api.py) deliberately --
# this sits on a page-load path a human is actively waiting on, not a one-shot admin save.
_VAULT_TIMEOUT_SECONDS = 10.0


def _vault_uri() -> str:
    uri = os.environ.get("AZURE_ORG_VAULT_URI")
    if not uri:
        raise VaultAccessError("AZURE_ORG_VAULT_URI is not configured")
    return uri


async def get_org_credential(secret_name: str) -> str:
    """Fetches `secret_name`'s current value from the org vault under the agent's OWN standing
    identity -- no entra_assertion, no per-user exchange. Raises VaultAccessError (keyvault.py's,
    reused rather than duplicated) with the real Azure error detail on any auth/permission/network
    failure, matching this codebase's existing fail-fast-with-the-provider's-own-error convention.
    Bounded to _VAULT_TIMEOUT_SECONDS -- a hang here is otherwise unbounded (see that constant's
    own comment for why this matters more here than it looks).
    """
    from azure.core.exceptions import AzureError
    from azure.identity.aio import DefaultAzureCredential
    from azure.keyvault.secrets.aio import SecretClient

    async def _fetch() -> str:
        async with DefaultAzureCredential() as credential:
            async with SecretClient(vault_url=_vault_uri(), credential=credential) as client:
                secret = await client.get_secret(secret_name)
                return secret.value or ""

    try:
        return await asyncio.wait_for(_fetch(), timeout=_VAULT_TIMEOUT_SECONDS)
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise VaultAccessError(
            f"timed out after {_VAULT_TIMEOUT_SECONDS}s contacting the org vault"
        ) from exc


async def set_org_credential(value: str) -> str:
    """Writes `value` as a new version of ORG_CREDENTIAL_SECRET_NAME under the agent's OWN
    standing identity, and returns that name so the caller (org_settings.set_org_settings) can
    store it without needing to know the constant itself. Same standing-identity, VaultAccessError,
    and _VAULT_TIMEOUT_SECONDS-bounded contract as get_org_credential."""
    from azure.core.exceptions import AzureError
    from azure.identity.aio import DefaultAzureCredential
    from azure.keyvault.secrets.aio import SecretClient

    async def _store() -> None:
        async with DefaultAzureCredential() as credential:
            async with SecretClient(vault_url=_vault_uri(), credential=credential) as client:
                await client.set_secret(ORG_CREDENTIAL_SECRET_NAME, value)

    try:
        await asyncio.wait_for(_store(), timeout=_VAULT_TIMEOUT_SECONDS)
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise VaultAccessError(
            f"timed out after {_VAULT_TIMEOUT_SECONDS}s contacting the org vault"
        ) from exc
    return ORG_CREDENTIAL_SECRET_NAME


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.org_credential_vault`. Pure-logic
    only, per this task's own brief -- no live vault I/O in this environment (matching
    keyvault.py's own self-check limitation on this branch); the real get/set round trip against a
    real vault is Part 4's own final verification task (Task 9), not this module's self-check."""
    assert ORG_CREDENTIAL_SECRET_NAME == "org-provider-credential"
    # Guards against a future edit accidentally redefining VaultAccessError locally instead of
    # importing keyvault's -- that would silently break `except VaultAccessError` for any caller
    # that imports the other definition.
    assert VaultAccessError.__module__.endswith("keyvault"), "must reuse keyvault.VaultAccessError, not redefine it"
    print("org_credential_vault self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.org_credential_vault
    _demo()
