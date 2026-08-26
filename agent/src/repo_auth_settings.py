"""Per-repo application-authentication posture (dbo.repo_auth_settings, migration 0010).

Keyed on (owner, repo) -- NOT per-user like repo_vaults: the generated app's auth posture is a
property of the codebase, and teammates who share a repo's sessions (src/lib/session-access.ts)
must get the same generated app whoever clicked Start.

DEFAULT_SETTINGS is what a repo with no row gets: locked ('required'). Enforcement is still
conditional on Key Vault auth secrets actually being present at provision -- an app cannot demand
Entra sign-in with no ClientId to sign in against -- so the locked default is inert until the
repo's vault is configured (see graph.py's app_auth seeding and e2e's auth gate).

Per-thread store follows keyvault._APP_SECRETS: process-local, lost on agent restart, re-seeded
at (re)provision.

Offline self-check: `cd agent && uv run python -m src.repo_auth_settings`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import session_store

logger = logging.getLogger(__name__)

AUTH_MODES = ("required", "anonymous_list", "none")
DEFAULT_SETTINGS: dict[str, Any] = {"auth_mode": "required", "anonymous_routes": []}

# thread_id -> settings dict. Same restart caveat as keyvault._APP_SECRETS.
_THREAD_SETTINGS: dict[str, dict[str, Any]] = {}


def set_for_thread(thread_id: str, settings: dict[str, Any]) -> None:
    _THREAD_SETTINGS[thread_id] = normalize(settings)


def get_for_thread(thread_id: str) -> dict[str, Any]:
    return _THREAD_SETTINGS.get(thread_id) or dict(DEFAULT_SETTINGS)


def pop_for_thread(thread_id: str) -> None:
    _THREAD_SETTINGS.pop(thread_id, None)


def normalize(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Clamps any stored/user shape to {auth_mode: one of AUTH_MODES, anonymous_routes: [str]}.
    Unknown modes fall back to the locked default rather than an open one. Bare '*'/'/*' patterns
    are dropped here too (belt to sessions_api's braces): a wildcard that matches everything is
    not an allowlist, it is the 'none' mode spelled confusingly."""
    settings = settings or {}
    mode = settings.get("auth_mode")
    if mode not in AUTH_MODES:
        mode = DEFAULT_SETTINGS["auth_mode"]
    routes = [
        r.strip() for r in (settings.get("anonymous_routes") or [])
        if isinstance(r, str) and r.strip() and r.strip() not in ("*", "/*")
    ]
    return {"auth_mode": mode, "anonymous_routes": routes}


async def get_settings(owner: str, repo: str) -> dict[str, Any]:
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT auth_mode, anonymous_routes FROM dbo.repo_auth_settings WHERE owner = ? AND repo = ?",
            owner, repo,
        )
        row = await cur.fetchone()
    if not row:
        return dict(DEFAULT_SETTINGS)
    routes: list[str] = []
    if row[1]:
        try:
            parsed = json.loads(row[1])
            if isinstance(parsed, list):
                routes = [str(r) for r in parsed]
        except json.JSONDecodeError:
            logger.warning("repo_auth_settings.anonymous_routes for %s/%s is not JSON; ignoring", owner, repo)
    return normalize({"auth_mode": row[0], "anonymous_routes": routes})


async def set_settings(owner: str, repo: str, settings: dict[str, Any], updated_by: str | None) -> None:
    clean = normalize(settings)
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.repo_auth_settings AS target
            USING (SELECT ? AS owner, ? AS repo) AS src
              ON target.owner = src.owner AND target.repo = src.repo
            WHEN MATCHED THEN UPDATE SET auth_mode = ?, anonymous_routes = ?, updated_by = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (owner, repo, auth_mode, anonymous_routes, updated_by)
              VALUES (?, ?, ?, ?, ?);
            """,
            owner, repo,
            clean["auth_mode"], json.dumps(clean["anonymous_routes"]), updated_by,
            owner, repo, clean["auth_mode"], json.dumps(clean["anonymous_routes"]), updated_by,
        )


def _demo() -> None:
    """Offline self-check of the pure half: `cd agent && uv run python -m src.repo_auth_settings`."""
    assert normalize(None) == {"auth_mode": "required", "anonymous_routes": []}
    assert normalize({"auth_mode": "wide_open"})["auth_mode"] == "required", "unknown modes must fall back LOCKED"
    assert normalize({"auth_mode": "none"})["auth_mode"] == "none"
    cleaned = normalize({"auth_mode": "anonymous_list", "anonymous_routes": ["/", "  /health*  ", "*", "/*", "", 3]})
    assert cleaned["anonymous_routes"] == ["/", "/health*"], cleaned
    assert get_for_thread("no-such-thread") == DEFAULT_SETTINGS
    set_for_thread("t1", {"auth_mode": "none"})
    assert get_for_thread("t1")["auth_mode"] == "none"
    pop_for_thread("t1")
    assert get_for_thread("t1") == DEFAULT_SETTINGS
    print("repo_auth_settings self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.repo_auth_settings
    _demo()
