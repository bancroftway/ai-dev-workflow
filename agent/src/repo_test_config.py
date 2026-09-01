"""Per-repo application config values for test boots (dbo.repo_test_config, migration 0013).

Keyed on (owner, repo), same reasoning as repo_auth_settings: config is a property of the codebase.
Each entry is {"key": "Section:Key", "value": str, "secret": bool, "source": "detected"|"user"|
"boot-error"}. Non-secret entries carry a value and are injected as env vars at e2e boot
(key -> env via keyvault.config_key_to_env); `secret:true` entries carry NO value -- they point at
the Key Vault picker instead, so nothing sensitive is stored here.

No per-thread cache (unlike repo_auth_settings): e2e reads config straight from SQL by owner/repo
via the session row it already fetches, so a resumed run in a fresh process still gets it.

Offline self-check: `cd agent && uv run python -m src.repo_test_config`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import keyvault, session_store

logger = logging.getLogger(__name__)

_SOURCES = ("detected", "user", "boot-error")


def normalize_entries(entries: Any) -> list[dict[str, Any]]:
    """Clamp any stored/user shape to a clean list, dropping entries whose key can't map to a legal
    env var (the env file is shell-sourced -- keyvault.ENV_NAME_RE is the boundary), de-duping by
    resolved env name (first wins), and blanking the value on secret entries."""
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        env_name = keyvault.config_key_to_env(key)
        if not keyvault.ENV_NAME_RE.fullmatch(env_name) or env_name in seen:
            continue
        seen.add(env_name)
        is_secret = bool(entry.get("secret"))
        source = entry.get("source") if entry.get("source") in _SOURCES else "user"
        clean.append({
            "key": key,
            "value": "" if is_secret else str(entry.get("value") or ""),
            "secret": is_secret,
            "source": source,
        })
    return clean


def to_env(entries: list[dict[str, Any]]) -> dict[str, str]:
    """The env-var map injected at boot: non-secret entries with a non-empty value only. Empty
    values are dropped (KEY='' binds as present-but-empty in .NET -- worse than missing)."""
    out: dict[str, str] = {}
    for entry in entries:
        if entry.get("secret"):
            continue
        value = str(entry.get("value") or "")
        if not value:
            continue
        out[keyvault.config_key_to_env(entry["key"])] = value
    return out


async def get_config(owner: str, repo: str) -> list[dict[str, Any]]:
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT entries FROM dbo.repo_test_config WHERE owner = ? AND repo = ?", owner, repo,
        )
        row = await cur.fetchone()
    if not row or not row[0]:
        return []
    try:
        parsed = json.loads(row[0])
    except json.JSONDecodeError:
        logger.warning("repo_test_config.entries for %s/%s is not JSON; ignoring", owner, repo)
        return []
    return normalize_entries(parsed)


async def set_config(owner: str, repo: str, entries: list[dict[str, Any]], updated_by: str | None) -> None:
    clean = normalize_entries(entries)
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.repo_test_config AS target
            USING (SELECT ? AS owner, ? AS repo) AS src
              ON target.owner = src.owner AND target.repo = src.repo
            WHEN MATCHED THEN UPDATE SET entries = ?, updated_by = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (owner, repo, entries, updated_by) VALUES (?, ?, ?, ?);
            """,
            owner, repo,
            json.dumps(clean), updated_by,
            owner, repo, json.dumps(clean), updated_by,
        )


async def merge_detected(owner: str, repo: str, keys: list[str], source: str = "detected") -> None:
    """Additive-only upsert of newly-detected config KEYS (value-empty rows) — never clobbers a
    user-supplied value or an existing row's secret flag. Called from the tech-stack post-approve
    hook and the e2e boot-error mop-up; both run every run, so it must be idempotent."""
    existing = await get_config(owner, repo)
    have = {keyvault.config_key_to_env(e["key"]) for e in existing}
    added = False
    for key in keys:
        key = str(key or "").strip()
        if not key:
            continue
        env_name = keyvault.config_key_to_env(key)
        if not keyvault.ENV_NAME_RE.fullmatch(env_name) or env_name in have:
            continue
        have.add(env_name)
        existing.append({"key": key, "value": "", "secret": False, "source": source})
        added = True
    if added:
        await set_config(owner, repo, existing, updated_by=source)


def _demo() -> None:
    """`cd agent && uv run python -m src.repo_test_config`."""
    n = normalize_entries([
        {"key": "AzureAd:TenantId", "value": "abc", "source": "user"},
        {"key": "  ", "value": "x"},                                  # empty key dropped
        {"key": "Bad Key With Spaces", "value": "y"},                 # invalid env name dropped
        {"key": "Stripe:Secret", "value": "sk_live", "secret": True}, # secret value blanked
        {"key": "AzureAd__TenantId", "value": "dup"},                 # env-name dup of entry 1 dropped
    ])
    assert [e["key"] for e in n] == ["AzureAd:TenantId", "Stripe:Secret"], n
    assert n[1]["value"] == "" and n[1]["secret"] is True, n
    env = to_env(n)
    assert env == {"AzureAd__TenantId": "abc"}, env  # secret + empty excluded
    assert to_env(normalize_entries([{"key": "A:B", "value": ""}])) == {}, "empty value excluded"
    print("repo_test_config self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _demo()
