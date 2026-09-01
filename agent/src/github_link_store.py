"""Per-user GitHub link tokens, stored in the org Key Vault under the agent's OWN standing identity.

Lets a user link GitHub ONCE: the access+refresh token pair lives in the org vault keyed by the
user's Entra object id (oid), so a later Entra sign-in (fresh cookie, new browser, after sign-out)
recovers the link instead of forcing a re-link. The token value never touches SQL and is never
returned to the browser -- it rides the server-side JWT exactly like the Entra pair does today.

Standing identity, NOT OBO -- same reasoning and same DefaultAzureCredential path as
org_credential_vault.py (which this clones): the vault client is the org vault's, and "as which
user" is answered by the secret NAME (github-link-<oid>), not by an on-behalf-of exchange. The
client itself is reused from org_credential_vault so the process holds one org-vault SecretClient.

SECURITY -- who may read a link: the oid is org-public (it appears in every JWT and in logs), so an
endpoint that accepted a bare oid behind only the fleet shared secret would let any secret-holder
read any user's GitHub token. This module therefore validates the oid's GUID shape before it ever
enters a secret name (a trust boundary, like keyvault.ENV_NAME_RE), but the REAL caller-identity
check lives one layer up: sessions_api verifies the caller's Entra assertion via tenant JWKS and
derives the oid from the verified token, never trusting a client-supplied oid. This module is the
dumb storage layer under that check.

Offline self-check: `cd agent && uv run python -m src.github_link_store`.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from . import org_credential_vault
from .keyvault import VaultAccessError

# Entra object ids are GUIDs. Validated before building a secret name because the value is
# interpolated into `github-link-<oid>` -- a trust boundary (Key Vault secret names allow
# 0-9a-zA-Z- only, so a non-GUID would either be rejected by the vault or, worse, alias another
# secret). fullmatch, not match: a trailing-newline oid must never pass (the ENV_NAME_RE lesson).
_OID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_LINK_SECRET_PREFIX = "github-link-"

# Reuse org_credential_vault's timeout so both org-vault surfaces degrade identically.
_VAULT_TIMEOUT_SECONDS = org_credential_vault._VAULT_TIMEOUT_SECONDS  # noqa: SLF001 -- same package, one org vault


def _secret_name(oid: str) -> str:
    if not _OID_RE.fullmatch(oid or ""):
        raise ValueError(f"invalid Entra oid: {oid!r}")
    return f"{_LINK_SECRET_PREFIX}{oid.lower()}"


async def get_github_link(oid: str) -> dict[str, Any] | None:
    """The stored link payload for `oid`, or None if none is stored. Raises VaultAccessError on a
    real auth/network failure (callers degrade to today's cookie-only behavior on that), but a
    genuinely absent secret returns None, not an error."""
    from azure.core.exceptions import AzureError, ResourceNotFoundError

    name = _secret_name(oid)

    async def _fetch() -> dict[str, Any] | None:
        try:
            secret = await org_credential_vault._get_client().get_secret(name)  # noqa: SLF001
        except ResourceNotFoundError:
            return None
        raw = secret.value or ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    try:
        return await asyncio.wait_for(_fetch(), timeout=_VAULT_TIMEOUT_SECONDS)
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise VaultAccessError(f"timed out after {_VAULT_TIMEOUT_SECONDS}s contacting the org vault") from exc


async def set_github_link(oid: str, payload: dict[str, Any]) -> None:
    """Stores `payload` (access_token, refresh_token, expires_at, refresh_token_expires_at,
    github_id, login) as a new version of github-link-<oid>. Standing identity, VaultAccessError,
    timeout-bounded -- same contract as org_credential_vault.set_org_credential."""
    from azure.core.exceptions import AzureError

    name = _secret_name(oid)
    value = json.dumps(payload)

    async def _store() -> None:
        await org_credential_vault._get_client().set_secret(name, value)  # noqa: SLF001

    try:
        await asyncio.wait_for(_store(), timeout=_VAULT_TIMEOUT_SECONDS)
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise VaultAccessError(f"timed out after {_VAULT_TIMEOUT_SECONDS}s contacting the org vault") from exc


async def delete_github_link(oid: str) -> None:
    """Removes the stored link (Disconnect). Idempotent: an already-absent secret is not an error.
    NEVER called on a failed token refresh -- refresh rotation makes a stale stored copy normal,
    so deletion is the user's explicit Disconnect action only (see the plan's Phase 1 amendment)."""
    from azure.core.exceptions import AzureError, ResourceNotFoundError

    name = _secret_name(oid)

    async def _delete() -> None:
        try:
            # begin_delete_secret returns a poller; awaiting .wait() completes the soft-delete.
            poller = await org_credential_vault._get_client().begin_delete_secret(name)  # noqa: SLF001
            await poller.wait()
        except ResourceNotFoundError:
            return

    try:
        await asyncio.wait_for(_delete(), timeout=_VAULT_TIMEOUT_SECONDS)
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise VaultAccessError(f"timed out after {_VAULT_TIMEOUT_SECONDS}s contacting the org vault") from exc


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.github_link_store`. Pure logic only
    (oid validation + secret-name shape); the real vault round trip needs a live org vault, same
    limitation as org_credential_vault's own self-check."""
    assert _secret_name("0ff17323-170A-4b95-863a-e4062e43542b") == "github-link-0ff17323-170a-4b95-863a-e4062e43542b"
    for bad in ("", "not-a-guid", "0ff17323-170a-4b95-863a-e4062e43542b\n", "'; DROP", "0ff17323170a4b95863ae4062e43542b"):
        try:
            _secret_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"invalid oid was accepted: {bad!r}")
    assert VaultAccessError.__module__.endswith("keyvault"), "must reuse keyvault.VaultAccessError"
    print("github_link_store self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _demo()
