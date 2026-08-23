"""Org-wide active coding-agent provider + a pointer to its credential (SQL Server,
agent/db/migrations/0003_create_org_settings.sql) -- the DB-backed store an org admin's Settings
UI (Part 4) reads from and writes to, so the active provider (Claude Code or GitHub Copilot) can
change without a redeploy.

Exactly one row, id=1 (see the migration's own header comment for why a CHECK-pinned singleton,
not app code, enforces this). credential_secret_name is a pointer only -- the Key Vault secret
name Task 2's org_credential_vault.py stores the real credential under -- this module never sees
or handles the credential value itself.

No caching here: Task 3's get_provider() owns the TTL cache in front of get_org_settings(); this
module is the plain DB access layer underneath it, the same division keyvault.py's mapping-table
functions have relative to their own callers.

Self-check is offline only (no live DB in this environment, same limitation as every other
DB-touching module's own self-check on this branch) -- it exercises the dataclass, not SQL. The
real MERGE/SELECT statements are verified by Part 4's own final verification task, against a real
DB, the same way Part 1's Task 12 verified its own work against a real container.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import session_store


@dataclass(frozen=True)
class OrgSettings:
    provider: str
    credential_secret_name: str | None
    updated_at: datetime
    updated_by: str | None


async def get_org_settings() -> OrgSettings | None:
    """None before the single row is ever written -- e.g. a fresh deployment whose admin hasn't
    visited the Settings UI yet, still running whatever AGENT_PROVIDER the container's own env
    sets (Task 3's get_provider() is where that fallback actually happens, not here)."""
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool, not a second one
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT provider, credential_secret_name, updated_at, updated_by FROM dbo.org_settings WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return OrgSettings(provider=row[0], credential_secret_name=row[1], updated_at=row[2], updated_by=row[3])


async def set_org_settings(provider: str, credential_secret_name: str | None, updated_by: str) -> None:
    """MERGE upsert against the single fixed row (id=1), mirroring keyvault.set_vault_uri's exact
    shape -- the only difference is the key being a constant rather than caller-supplied columns,
    since this table has no natural key beyond "the one row"."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.org_settings AS target
            USING (SELECT 1 AS id) AS src
              ON target.id = src.id
            WHEN MATCHED THEN UPDATE SET provider = ?, credential_secret_name = ?, updated_by = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (id, provider, credential_secret_name, updated_by) VALUES (1, ?, ?, ?);
            """,
            provider, credential_secret_name, updated_by,
            provider, credential_secret_name, updated_by,
        )


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.org_settings`. No live DB in this
    environment (see module docstring) -- exercises only the dataclass, the one piece of pure
    logic in this module. The MERGE/SELECT SQL itself is verified by Part 4's final verification
    task against a real DB (same as Part 1's Task 12 did against a real container)."""
    settings = OrgSettings(
        provider="claude",
        credential_secret_name="org-claude-api-key",
        updated_at=datetime(2026, 8, 21, 12, 0, 0),
        updated_by="octocat",
    )
    assert settings.provider == "claude", settings
    assert settings.credential_secret_name == "org-claude-api-key", settings
    assert settings.updated_at == datetime(2026, 8, 21, 12, 0, 0), settings
    assert settings.updated_by == "octocat", settings

    # frozen=True must actually block mutation, not just be decorative.
    try:
        settings.provider = "copilot"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("OrgSettings must be frozen")

    # credential_secret_name/updated_by are the two nullable columns (no credential configured
    # yet; a row predating an audit-trail caller) -- the dataclass must carry None through
    # untouched, the same round-trip a real SELECT with NULL columns would produce.
    unconfigured = OrgSettings(
        provider="copilot", credential_secret_name=None,
        updated_at=datetime(2026, 8, 21, 12, 0, 0), updated_by=None,
    )
    assert unconfigured.credential_secret_name is None, unconfigured
    assert unconfigured.updated_by is None, unconfigured

    print("org_settings self-check: ok (dataclass only, no live DB in this environment)")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.org_settings
    # Re-dispatch through the PACKAGE name on purpose -- the unconditional convention on this
    # branch (chat_model.py, model_config.py, structured_output.py, etc.): `python -m
    # src.org_settings` loads this file as "__main__", so a direct _demo() call would import this
    # module a second time under a separate sys.modules identity.
    from src.org_settings import _demo as _packaged_demo

    _packaged_demo()
