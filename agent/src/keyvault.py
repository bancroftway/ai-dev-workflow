"""Per user-repo Azure Key Vault secrets, fetched on-behalf-of the signed-in user.

The agent holds NO standing vault access (enterprise constraint: nobody grants one identity
reach into every team's vault). Instead the frontend forwards the user's Entra access token for
the agent API (the "assertion"), and this module exchanges it via the OAuth2 on-behalf-of flow
(azure.identity OnBehalfOfCredential) for a vault-scoped token that carries the USER's identity
-- Azure's own RBAC on the vault is the enforcement. The mapping row (dbo.repo_vaults, migration
0002) is a pointer only; a wrong or malicious vault_uri can't expose anything the user couldn't
already read themselves.

Timing: secrets are fetched at provision time (and on the explicit "refresh-secrets" session
action) while the assertion is fresh -- never later in the pipeline, where a >1h-old assertion
would be expired. Fetched VALUES are cached in-process per session (same pattern and same
agent-restart limitation as git_ops._PUSH_TOKENS); Entra tokens are used once and discarded.

The sandbox never talks to Azure: the secrets land as a shell env file at APP_ENV_PATH (outside
the repo working tree -- never committed, never scanned, never in `docker inspect` Config.Env),
sourced only into the app process the e2e stage starts.

Offline self-check (name mapping + env rendering): `cd agent && uv run python -m src.keyvault`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex

from . import session_store
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Home of the sandbox user -- outside /workspace/repo so repo tooling (gitleaks, commits, find)
# never sees it. Constant, never interpolated from user input.
APP_ENV_PATH = "/home/vscode/.aidw-app-env"

# thread_id -> {ENV_NAME: value}. Process-local, lost on agent restart -- the "refresh-secrets"
# session action re-populates it with a fresh assertion from the frontend.
_APP_SECRETS: dict[str, dict[str, str]] = {}


class VaultAccessError(Exception):
    """OBO exchange or vault read failed -- message carries the AADSTS/HTTP detail verbatim so
    the frontend can show the user exactly what to fix (usually a missing role assignment)."""


def set_app_secrets(thread_id: str, secrets: dict[str, str]) -> None:
    _APP_SECRETS[thread_id] = dict(secrets)


def get_app_secrets(thread_id: str) -> dict[str, str] | None:
    return _APP_SECRETS.get(thread_id)


def pop_app_secrets(thread_id: str) -> None:
    _APP_SECRETS.pop(thread_id, None)


# --- mapping table (dbo.repo_vaults) ---------------------------------------------------------


async def get_vault_uri(owner: str, repo: str, user_login: str) -> str | None:
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool, not a second one
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT vault_uri FROM dbo.repo_vaults WHERE owner = ? AND repo = ? AND user_login = ?",
            owner, repo, user_login,
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_vault_uri(owner: str, repo: str, user_login: str, vault_uri: str) -> None:
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.repo_vaults AS target
            USING (SELECT ? AS owner, ? AS repo, ? AS user_login) AS src
              ON target.owner = src.owner AND target.repo = src.repo AND target.user_login = src.user_login
            WHEN MATCHED THEN UPDATE SET vault_uri = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (owner, repo, user_login, vault_uri) VALUES (?, ?, ?, ?);
            """,
            owner, repo, user_login,
            vault_uri,
            owner, repo, user_login, vault_uri,
        )


# --- OBO fetch --------------------------------------------------------------------------------


def _obo_credential(entra_assertion: str):
    from azure.identity.aio import OnBehalfOfCredential

    return OnBehalfOfCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AIDW_AGENT_APP_ID"],
        client_secret=os.environ["AIDW_AGENT_CLIENT_SECRET"],
        user_assertion=entra_assertion,
    )


async def fetch_app_secrets(vault_uri: str, entra_assertion: str) -> dict[str, str]:
    """All enabled secrets in the vault, as an env-var dict. Raises VaultAccessError with the
    provider's own detail on any auth/permission/network failure -- callers surface it verbatim."""
    from azure.core.exceptions import AzureError
    from azure.keyvault.secrets.aio import SecretClient

    secrets: dict[str, str] = {}
    try:
        async with _obo_credential(entra_assertion) as credential:
            async with SecretClient(vault_url=vault_uri, credential=credential) as client:
                # List first, then fetch the values concurrently -- N sequential get_secret round
                # trips were the provision path's slowest serial loop. gather preserves order, so
                # the resulting dict (collision warning included) is the same as the old loop's.
                names = [
                    prop.name
                    async for prop in client.list_properties_of_secrets()
                    if prop.enabled is not False and prop.name
                ]
                fetched = await asyncio.gather(*(client.get_secret(name) for name in names))
                for name, secret in zip(names, fetched):
                    env_name = secret_name_to_env(name)
                    if env_name in secrets:
                        logger.warning("vault %s: secret %r collides with an earlier name after env mapping", vault_uri, name)
                    secrets[env_name] = secret.value or ""
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    return secrets


# --- env-file rendering + sandbox injection ---------------------------------------------------


def secret_name_to_env(name: str) -> str:
    """Key Vault names are [A-Za-z0-9-]; env names want [A-Z0-9_]: `connection-string-main` ->
    `CONNECTION_STRING_MAIN`. A digit-leading result gets a `_` prefix (sh requires it)."""
    env = name.upper().replace("-", "_")
    return f"_{env}" if env[:1].isdigit() else env


def render_env_file(secrets: dict[str, str]) -> str:
    """`KEY=<quoted>` lines consumable by `set -a; . file; set +a` -- values are shlex-quoted so
    spaces/quotes/$ in secret values survive the shell parse literally."""
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in sorted(secrets.items()))


async def write_env_file(provider: SandboxProvider, thread_id: str, secrets: dict[str, str]) -> None:
    """Writes APP_ENV_PATH inside the sandbox via the same base64-through-exec channel git_ops
    uses for push credentials (repo_files.write_repo_file's trick, but for a fixed absolute path
    outside the repo tree, which its repo-relative validator rightly refuses)."""
    import base64

    encoded = base64.b64encode(render_env_file(secrets).encode("utf-8")).decode("ascii")
    result = await provider.exec_in_sandbox(
        thread_id,
        f"umask 077 && echo {encoded} | base64 -d > {shlex.quote(APP_ENV_PATH)}",
    )
    if not result.ok:
        raise RuntimeError(f"failed to write {APP_ENV_PATH}: {result.stderr}")


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.keyvault`."""
    assert secret_name_to_env("connection-string-main") == "CONNECTION_STRING_MAIN"
    assert secret_name_to_env("ApiKey") == "APIKEY"
    assert secret_name_to_env("0-leading") == "_0_LEADING"

    rendered = render_env_file({"B_KEY": "with space", "A_KEY": "it's $HOME `x`"})
    assert rendered.splitlines()[0] == "A_KEY='it'\"'\"'s $HOME `x`'", rendered
    assert rendered.splitlines()[1] == "B_KEY='with space'", rendered
    # The rendered file must round-trip through a POSIX shell parse to the original values.
    # (Skipped where no `sh` exists, e.g. a bare Windows dev box -- the sandbox always has one.)
    import subprocess

    try:
        script = f"set -a\n{rendered}set +a\nprintf '%s' \"$A_KEY\""
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
        assert out == "it's $HOME `x`", out
    except FileNotFoundError:
        print("keyvault self-check: no sh on PATH, shell round-trip skipped")

    set_app_secrets("t1", {"K": "v"})
    assert get_app_secrets("t1") == {"K": "v"}
    pop_app_secrets("t1")
    assert get_app_secrets("t1") is None
    print("keyvault self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.keyvault
    _demo()
