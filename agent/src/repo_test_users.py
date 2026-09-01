"""Per-repo declared test users for multi-role e2e (dbo.repo_test_users, migration 0012).

Keyed on (owner, repo), same reasoning as repo_auth_settings. Each user is
{"name": str, "email": str, "roles": [str]} -- NO passwords (the seam mints a fixed test password
for custom-auth apps; the fake IdP issues tokens for OIDC apps). The prompt stages need the list in
graph state, so this keeps a per-thread store like repo_auth_settings; the fake IdP and e2e read
from SQL (or checkpointed state) rather than the cache, so an agent restart mid-run doesn't blank
the users.

Offline self-check: `cd agent && uv run python -m src.repo_test_users`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import session_store

logger = logging.getLogger(__name__)

# thread_id -> [user dict]. Same restart caveat as keyvault._APP_SECRETS / repo_auth_settings.
_THREAD_USERS: dict[str, list[dict[str, Any]]] = {}

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")


def normalize_users(users: Any) -> list[dict[str, Any]]:
    """Clamp to [{name, email, roles:[str]}], dropping entries without a name, de-duping by name
    (first wins). roles are trimmed non-empty strings; email kept only if plausible (advisory)."""
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in users or []:
        if not isinstance(u, dict):
            continue
        name = str(u.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        email = str(u.get("email") or "").strip()
        roles = [str(r).strip() for r in (u.get("roles") or []) if isinstance(r, str) and str(r).strip()]
        clean.append({"name": name, "email": email if _EMAIL_RE.match(email) else "", "roles": roles})
    return clean


def set_for_thread(thread_id: str, users: list[dict[str, Any]]) -> None:
    _THREAD_USERS[thread_id] = normalize_users(users)


def get_for_thread(thread_id: str) -> list[dict[str, Any]]:
    return _THREAD_USERS.get(thread_id) or []


def pop_for_thread(thread_id: str) -> None:
    _THREAD_USERS.pop(thread_id, None)


async def get_users(owner: str, repo: str) -> list[dict[str, Any]]:
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT users FROM dbo.repo_test_users WHERE owner = ? AND repo = ?", owner, repo)
        row = await cur.fetchone()
    if not row or not row[0]:
        return []
    try:
        parsed = json.loads(row[0])
    except json.JSONDecodeError:
        logger.warning("repo_test_users.users for %s/%s is not JSON; ignoring", owner, repo)
        return []
    return normalize_users(parsed)


async def set_users(owner: str, repo: str, users: list[dict[str, Any]], updated_by: str | None) -> None:
    clean = normalize_users(users)
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.repo_test_users AS target
            USING (SELECT ? AS owner, ? AS repo) AS src
              ON target.owner = src.owner AND target.repo = src.repo
            WHEN MATCHED THEN UPDATE SET users = ?, updated_by = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (owner, repo, users, updated_by) VALUES (?, ?, ?, ?);
            """,
            owner, repo,
            json.dumps(clean), updated_by,
            owner, repo, json.dumps(clean), updated_by,
        )


def render_table(users: list[dict[str, Any]]) -> str:
    """Markdown table of declared users for the prompt segment's `<<test_users>>` placeholder."""
    if not users:
        return "(none declared)"
    lines = ["| Name | Email | Roles |", "|---|---|---|"]
    for u in users:
        roles = ", ".join(u.get("roles") or []) or "(none)"
        lines.append(f"| {u['name']} | {u.get('email') or '(none)'} | {roles} |")
    return "\n".join(lines)


def _demo() -> None:
    """`cd agent && uv run python -m src.repo_test_users`."""
    n = normalize_users([
        {"name": "Ada Admin", "email": "ada@test.local", "roles": ["Admin", " ", "Owner"]},
        {"name": "  ", "email": "x@y.z"},                 # no name dropped
        {"name": "Ada Admin", "roles": ["dup"]},          # dup name dropped
        {"name": "Bob", "email": "not-an-email", "roles": []},
    ])
    assert [u["name"] for u in n] == ["Ada Admin", "Bob"], n
    assert n[0]["roles"] == ["Admin", "Owner"], n
    assert n[1]["email"] == "", "implausible email blanked"
    assert get_for_thread("none") == []
    set_for_thread("t1", n)
    assert len(get_for_thread("t1")) == 2
    pop_for_thread("t1")
    assert get_for_thread("t1") == []
    assert "Ada Admin" in render_table(n) and render_table([]) == "(none declared)"
    print("repo_test_users self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _demo()
