"""Single source of truth for project metadata (SQL Server, `agent/db/migrations/0004_create_projects.sql`)
-- the grouping key Part 3 (docs/superpowers/plans/part-3-tickets-tasks.md) adds above
`dbo.sessions`. Ruling 1: a "ticket" is not a new table, it's a `dbo.sessions` row with a
`project_id` -- this module only owns the project itself (name, owner/repo, tech stack), never a
ticket/session concept, which stays entirely in session_store.py.

Same `aioodbc`-via-`db.py` pattern as session_store.py, but -- like keyvault.py and org_settings.py
-- borrows session_store's own pool (`session_store._get_pool()`) rather than opening a second
one: one process, one SQL Server, no reason for a second connection pool to this same database.

owner/repo start NULL for a "+ New Project" row (no GitHub repo yet) and get backfilled once
Task 3's scaffolding succeeds (set_project_repo) -- see Ruling 2 for why nullable-then-backfilled
beats a placeholder value, the same idiom sessions_api.py's own provision_session already uses for
dbo.sessions.title.

Self-check is offline only (no live DB in this environment, same limitation as org_settings.py's
own self-check): `cd agent && uv run python -m src.project_store`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from . import session_store

logger = logging.getLogger(__name__)

_COLUMNS = [
    "project_id", "name", "owner", "repo", "tech_stack_id", "tech_stack_text",
    "created_by", "created_at", "updated_at",
]


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Mirrors session_store._row_to_dict's own UNIQUEIDENTIFIER normalization: SQL Server hands
    back project_id as an uppercase string; every caller (frontend included) mints/compares the
    lowercase uuid4() form."""
    result = dict(zip(_COLUMNS, row))
    if result.get("project_id"):
        result["project_id"] = str(result["project_id"]).lower()
    return result


async def create_project(
    name: str, *, tech_stack_id: str | None, tech_stack_text: str | None, created_by: str
) -> str:
    """The "+ New Project" inline-fields path (Ruling 2): owner/repo start NULL, backfilled later
    by set_project_repo once Task 3's repo scaffolding actually creates the GitHub repo. The other
    creation path (Connect-Repository, Task 2) doesn't call this -- it sets owner/repo immediately
    via a plain INSERT at that call site, after find_project_by_repo confirms no row exists yet."""
    project_id = str(uuid.uuid4())
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool, not a second one
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dbo.projects (project_id, name, owner, repo, tech_stack_id, tech_stack_text, created_by)
            VALUES (?, ?, NULL, NULL, ?, ?, ?)
            """,
            project_id,
            name,
            tech_stack_id,
            tech_stack_text,
            created_by,
        )
    return project_id


async def set_project_repo(project_id: str, owner: str, repo: str) -> None:
    """The post-scaffold backfill (Ruling 2) -- called once Task 3's repo_scaffold.create_repo
    succeeds for a "+ New Project" row that started with owner/repo NULL."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.projects SET owner = ?, repo = ?, updated_at = SYSUTCDATETIME() WHERE project_id = ?",
            owner,
            repo,
            project_id,
        )


async def get_project(project_id: str) -> dict[str, Any] | None:
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM dbo.projects WHERE project_id = ?", project_id)
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_projects() -> list[dict[str, Any]]:
    """Backs the New Ticket form's project picker (GET /projects, Task 2) -- no owner/repo scoping,
    every project regardless of connected/scaffolded state, most recently created first."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM dbo.projects ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [_row_to_dict(row) for row in rows]


async def find_project_by_repo(owner: str, repo: str) -> dict[str, Any] | None:
    """Backs Connect-Repository's idempotent "already connected" check (Task 2, POST
    /projects/connect) against UX_projects_owner_repo -- that unique filtered index guarantees at
    most one project row can ever match a given (owner, repo) pair."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM dbo.projects WHERE owner = ? AND repo = ?", owner, repo
        )
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.project_store`. No live DB in this
    environment (see module docstring) -- monkeypatches session_store._get_pool with a tiny
    in-memory fake standing in for aioodbc's pool/connection/cursor protocol (the same three
    `async with`-able layers session_store.py's own real-DB self-check exercises against SQL
    Server), so every function above runs its real code path, just against a fake row store
    instead of a live connection. The real SQL text itself is verified by this Part's own final
    verification task against a real DB, same as org_settings.py's MERGE/SELECT."""
    import asyncio
    from datetime import datetime, timezone

    class _FakeCursor:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows  # shared list -- mutated in place, same object every acquire()
            self._result: list[tuple[Any, ...]] = []

        @staticmethod
        def _as_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
            return tuple(row[c] for c in _COLUMNS)

        async def execute(self, sql: str, *params: Any) -> None:
            flat = " ".join(sql.split())
            if flat.startswith("INSERT INTO dbo.projects"):
                project_id, name, tech_stack_id, tech_stack_text, created_by = params
                now = datetime.now(timezone.utc)
                self._rows.append(
                    {
                        "project_id": project_id.upper(),  # SQL Server hands UNIQUEIDENTIFIER back uppercase
                        "name": name,
                        "owner": None,
                        "repo": None,
                        "tech_stack_id": tech_stack_id,
                        "tech_stack_text": tech_stack_text,
                        "created_by": created_by,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            elif flat.startswith("UPDATE dbo.projects SET owner"):
                owner, repo, project_id = params
                for row in self._rows:
                    if row["project_id"].lower() == project_id.lower():
                        row["owner"], row["repo"] = owner, repo
            elif "FROM dbo.projects WHERE project_id = ?" in flat:
                (project_id,) = params
                self._result = [
                    self._as_tuple(r) for r in self._rows if r["project_id"].lower() == project_id.lower()
                ]
            elif "WHERE owner = ? AND repo = ?" in flat:
                owner, repo = params
                self._result = [self._as_tuple(r) for r in self._rows if r["owner"] == owner and r["repo"] == repo]
            elif "FROM dbo.projects ORDER BY" in flat:
                ordered = sorted(self._rows, key=lambda r: r["created_at"], reverse=True)
                self._result = [self._as_tuple(r) for r in ordered]
            else:
                raise AssertionError(f"fake cursor got an unrecognized statement: {flat!r}")

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self._result[0] if self._result else None

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return list(self._result)

        async def __aenter__(self) -> "_FakeCursor":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _FakeConn:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def cursor(self) -> "_FakeCursor":
            return _FakeCursor(self._rows)

        async def __aenter__(self) -> "_FakeConn":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _FakePool:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        def acquire(self) -> "_FakeConn":
            return _FakeConn(self._rows)

    async def _run() -> None:
        fake_pool = _FakePool()

        async def _fake_get_pool() -> _FakePool:
            return fake_pool

        original_get_pool = session_store._get_pool
        session_store._get_pool = _fake_get_pool  # type: ignore[assignment]
        try:
            project_id = await create_project(
                "Demo Project", tech_stack_id="nextjs-fastapi", tech_stack_text=None, created_by="octocat"
            )
            # Minted lowercase (uuid4()); the fake cursor stores it uppercase, exactly like a real
            # UNIQUEIDENTIFIER column would -- this round-trip is only correct if _row_to_dict
            # actually re-lowers it, same normalization session_store._row_to_dict does.
            assert project_id == project_id.lower(), project_id

            row = await get_project(project_id)
            assert row is not None, "just-created project should be readable"
            assert row["project_id"] == project_id, row
            assert row["name"] == "Demo Project", row
            assert row["owner"] is None and row["repo"] is None, row
            assert row["tech_stack_id"] == "nextjs-fastapi", row
            assert row["tech_stack_text"] is None, row

            assert await find_project_by_repo("octocat", "demo-repo") is None

            await set_project_repo(project_id, "octocat", "demo-repo")
            row = await get_project(project_id)
            assert row["owner"] == "octocat" and row["repo"] == "demo-repo", row

            found = await find_project_by_repo("octocat", "demo-repo")
            assert found is not None and found["project_id"] == project_id, found

            projects = await list_projects()
            assert any(p["project_id"] == project_id for p in projects), projects

            other_id = await create_project(
                "Second Project", tech_stack_id=None, tech_stack_text="Rails + Postgres", created_by="hubot"
            )
            projects = await list_projects()
            ids = {p["project_id"] for p in projects}
            assert {project_id, other_id} <= ids, projects
            assert await find_project_by_repo("someone-else", "unrelated-repo") is None
        finally:
            session_store._get_pool = original_get_pool  # type: ignore[assignment]

    asyncio.run(_run())
    print("project_store self-check: ok (offline, fake pool -- no live DB in this environment)")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.project_store
    # Re-dispatch through the PACKAGE name on purpose -- the unconditional convention on this
    # branch (chat_model.py, model_config.py, org_settings.py, etc.): `python -m src.project_store`
    # loads this file as "__main__", so a direct _demo() call would import this module a second
    # time under a separate sys.modules identity.
    from src.project_store import _demo as _packaged_demo

    _packaged_demo()
