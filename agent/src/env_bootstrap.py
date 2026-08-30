"""Boot-time configuration: repo-root .env, then Azure Key Vault.

docs/superpowers/specs/2026-08-30-keyvault-config-design.md. Every enabled secret in the vault
named by AZURE_CONFIG_VAULT_URI becomes an env var (`AUTH-SECRET` -> `AUTH_SECRET`). A variable
already present in the process environment wins (platform-set values and shell overrides stay
authoritative); the vault fills the rest. Unset AZURE_CONFIG_VAULT_URI = .env only, as before.
Unreachable vault = the process refuses to start, with Azure's own error text.

Must run before any module that reads os.environ at import time -- see main.py's import order.
The frontend has the same loader in src/instrumentation.ts; keep the two in step.

Offline self-check: `cd agent && uv run python -m src.env_bootstrap`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

from dotenv import find_dotenv, load_dotenv

VAULT_URI_VAR = "AZURE_CONFIG_VAULT_URI"


def env_name(secret_name: str) -> str:
    """`AUTH-SECRET` -> `AUTH_SECRET` (Key Vault names allow only [A-Za-z0-9-])."""
    return secret_name.upper().replace("-", "_")


def apply(secrets: dict[str, str], environ: MutableMapping[str, str]) -> int:
    """Injects secrets not already set in `environ`; returns how many were added."""
    added = 0
    for secret_name, value in secrets.items():
        key = env_name(secret_name)
        if key in environ:
            continue
        environ[key] = value
        added += 1
    return added


def read_vault(vault_uri: str) -> dict[str, str]:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=vault_uri, credential=DefaultAzureCredential())
    return {
        props.name: client.get_secret(props.name).value or ""
        for props in client.list_properties_of_secrets()
        if props.enabled is not False and props.name
    }


def bootstrap_env() -> None:
    load_dotenv(find_dotenv())
    vault_uri = os.environ.get(VAULT_URI_VAR)
    if not vault_uri:
        return
    try:
        secrets = read_vault(vault_uri)
    except Exception as exc:  # noqa: BLE001 -- any SDK/auth/network failure is fatal at boot
        raise RuntimeError(
            f"config vault {vault_uri}: {exc} (locally: az login; in Azure: grant this identity "
            "'Key Vault Secrets User' on the vault)"
        ) from exc
    added = apply(secrets, os.environ)
    kept = len(secrets) - added
    # print, not logging: this runs before main.py's logging.basicConfig, where INFO is dropped.
    print(
        f"[config vault] {added} values loaded from {vault_uri}"
        + (f" ({kept} already set in env, kept)" if kept else ""),
        file=sys.stderr,
    )


def _demo() -> None:
    assert env_name("auth-secret") == "AUTH_SECRET"
    assert env_name("AIDW-AGENT-APP-ID") == "AIDW_AGENT_APP_ID"
    assert env_name("PORT") == "PORT"

    env: dict[str, str] = {"AGENT_URL": "http://platform"}
    added = apply({"agent-url": "http://vault", "auth-secret": "s3", "empty": ""}, env)
    assert added == 2, added
    assert env == {"AGENT_URL": "http://platform", "AUTH_SECRET": "s3", "EMPTY": ""}, env
    print("env_bootstrap self-check passed")


if __name__ == "__main__":
    _demo()
