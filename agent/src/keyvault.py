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
import json
import logging
import os
import re
import shlex

from . import session_store
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# The one legal shape for an env var name that ends up in APP_ENV_PATH. SECURITY BOUNDARY, not a
# style rule: the env file is `. `-sourced by a shell inside the sandbox (e2e_nodes._boot_process),
# so a free-form name like `X; rm -rf / #` would EXECUTE. Enforced at selection-save time in
# sessions_api AND defensively in render_env_file below; the UI's client-side check is advisory.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

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


# selection: list of {"name": str, "env_name": str | None}. None (no row / NULL column) means
# "expose all enabled secrets" -- the pre-selection behavior. An EMPTY list means "expose
# nothing"; every consumer must branch on `is None`, never truthiness (migration 0009).
Selection = list[dict[str, str | None]]


async def get_secret_selection(owner: str, repo: str, user_login: str) -> Selection | None:
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT secret_selection FROM dbo.repo_vaults WHERE owner = ? AND repo = ? AND user_login = ?",
            owner, repo, user_login,
        )
        row = await cur.fetchone()
    if not row or row[0] is None:
        return None
    try:
        parsed = json.loads(row[0])
    except json.JSONDecodeError:
        logger.warning("repo_vaults.secret_selection for %s/%s is not JSON; treating as no selection", owner, repo)
        return None
    if not isinstance(parsed, list):
        return None
    return [
        {"name": str(entry.get("name")), "env_name": entry.get("env_name") or None}
        for entry in parsed
        if isinstance(entry, dict) and entry.get("name")
    ]


async def set_secret_selection(owner: str, repo: str, user_login: str, selection: Selection) -> bool:
    """False when no vault row exists to attach the selection to (a plain UPDATE matching zero
    rows would otherwise 200 silently -- the API turns False into a 404)."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.repo_vaults SET secret_selection = ?, updated_at = SYSUTCDATETIME() "
            "WHERE owner = ? AND repo = ? AND user_login = ?",
            json.dumps(selection), owner, repo, user_login,
        )
        return (cur.rowcount or 0) > 0


def resolve_env_names(selection: Selection) -> dict[str, str]:
    """secret name -> final env name (override wins, else the automap). Raises ValueError naming
    the offender on an invalid env name or a duplicate resolved name -- callers surface it as 422."""
    resolved: dict[str, str] = {}
    by_env: dict[str, str] = {}
    for entry in selection:
        name = entry["name"] or ""
        env_name = entry.get("env_name") or secret_name_to_env(name)
        if not ENV_NAME_RE.match(env_name):
            raise ValueError(f"invalid env name {env_name!r} for secret {name!r} (must match {ENV_NAME_RE.pattern})")
        if env_name in by_env:
            raise ValueError(f"secrets {by_env[env_name]!r} and {name!r} both resolve to env name {env_name!r}")
        by_env[env_name] = name
        resolved[name] = env_name
    return resolved


# --- OBO fetch --------------------------------------------------------------------------------


def _obo_credential(entra_assertion: str):
    from azure.identity.aio import OnBehalfOfCredential

    return OnBehalfOfCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AIDW_AGENT_APP_ID"],
        client_secret=os.environ["AIDW_AGENT_CLIENT_SECRET"],
        user_assertion=entra_assertion,
    )


async def fetch_app_secrets(
    vault_uri: str, entra_assertion: str, selection: Selection | None = None,
) -> dict[str, str]:
    """Enabled secrets in the vault as an env-var dict. Raises VaultAccessError with the
    provider's own detail on any auth/permission/network failure -- callers surface it verbatim.

    `selection=None` (the default, and every pre-selection caller) fetches ALL enabled secrets.
    A selection fetches only the named secrets under their resolved env names -- and an EMPTY
    selection fetches nothing (the `is None` branch is load-bearing: a user who unchecked every
    box must not get the whole vault). A selected secret that has since been disabled or deleted
    degrades to a logged warning, never a failed provision -- the vault-save test-read already
    proved access, and one stale name must not brick the session."""
    from azure.core.exceptions import AzureError
    from azure.keyvault.secrets.aio import SecretClient

    secrets: dict[str, str] = {}
    try:
        async with _obo_credential(entra_assertion) as credential:
            async with SecretClient(vault_url=vault_uri, credential=credential) as client:
                if selection is None:
                    # List first, then fetch the values concurrently -- N sequential get_secret
                    # round trips were the provision path's slowest serial loop.
                    names = [
                        prop.name
                        async for prop in client.list_properties_of_secrets()
                        if prop.enabled is not False and prop.name
                    ]
                    env_by_name = {name: secret_name_to_env(name) for name in names}
                else:
                    env_by_name = resolve_env_names(selection)
                    names = list(env_by_name)
                fetched = await asyncio.gather(
                    *(client.get_secret(name) for name in names), return_exceptions=True
                )
                for name, secret in zip(names, fetched):
                    if isinstance(secret, BaseException):
                        if selection is None or not isinstance(secret, AzureError):
                            raise secret
                        logger.warning(
                            "vault %s: selected secret %r unavailable (%s) -- skipped",
                            vault_uri, name, type(secret).__name__,
                        )
                        continue
                    env_name = env_by_name[name]
                    if env_name in secrets:
                        logger.warning("vault %s: secret %r collides with an earlier name after env mapping", vault_uri, name)
                    secrets[env_name] = secret.value or ""
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc
    return secrets


async def list_secret_names(vault_uri: str, entra_assertion: str) -> list[str]:
    """Names (properties only, never values) of the vault's enabled secrets, for the settings
    page's picker. Same OBO path and error contract as fetch_app_secrets."""
    from azure.core.exceptions import AzureError
    from azure.keyvault.secrets.aio import SecretClient

    try:
        async with _obo_credential(entra_assertion) as credential:
            async with SecretClient(vault_url=vault_uri, credential=credential) as client:
                return sorted([
                    prop.name
                    async for prop in client.list_properties_of_secrets()
                    if prop.enabled is not False and prop.name
                ])
    except AzureError as exc:
        raise VaultAccessError(str(exc)) from exc


# --- env-file rendering + sandbox injection ---------------------------------------------------


def secret_name_to_env(name: str) -> str:
    """Key Vault names are [A-Za-z0-9-]; env names want [A-Z0-9_]: `connection-string-main` ->
    `CONNECTION_STRING_MAIN`. A digit-leading result gets a `_` prefix (sh requires it)."""
    env = name.upper().replace("-", "_")
    return f"_{env}" if env[:1].isdigit() else env


def render_env_file(secrets: dict[str, str]) -> str:
    """`KEY=<quoted>` lines consumable by `set -a; . file; set +a` -- values are shlex-quoted so
    spaces/quotes/$ in secret values survive the shell parse literally. Keys are validated against
    ENV_NAME_RE here DEFENSIVELY (sessions_api already rejects them at save): this file is
    executed by a shell, so a malformed key is dropped with a warning, never emitted."""
    lines = []
    for key, value in sorted(secrets.items()):
        if not ENV_NAME_RE.match(key):
            logger.warning("refusing to render env entry with invalid name %r", key)
            continue
        lines.append(f"{key}={shlex.quote(value)}\n")
    return "".join(lines)


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

    # --- selection env-name resolution: the security boundary ---------------------------------
    resolved = resolve_env_names([
        {"name": "client-id", "env_name": None},
        {"name": "client-secret", "env_name": "AzureAd__ClientSecret"},
    ])
    assert resolved == {"client-id": "CLIENT_ID", "client-secret": "AzureAd__ClientSecret"}, resolved
    # An empty override string means "use the automap", same as null -- the UI's placeholder.
    assert resolve_env_names([{"name": "api-key", "env_name": ""}]) == {"api-key": "API_KEY"}
    for bad in ("X; touch /tmp/pwned #", "A=B", "has space", "1LEADING-"):
        try:
            resolve_env_names([{"name": "s", "env_name": bad}])
            raise AssertionError(f"env name {bad!r} must be rejected")
        except ValueError:
            pass
    try:
        resolve_env_names([{"name": "client-id", "env_name": None}, {"name": "legacy", "env_name": "CLIENT_ID"}])
        raise AssertionError("duplicate resolved env names must be rejected")
    except ValueError:
        pass
    # render_env_file's defensive half: a malformed key is DROPPED, never emitted into a file a
    # shell will source.
    assert render_env_file({"OK_KEY": "v", "X; touch /tmp/pwned #": "v"}) == "OK_KEY=v\n"
    print("keyvault self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.keyvault
    _demo()
