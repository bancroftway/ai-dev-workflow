"""Tiny SQL Server migration runner -- no ORM, no Alembic, just numbered .sql files plus a
tracking table. `agent/db/migrations/*.sql` is the single DDL source of truth.

Run: `cd agent && uv run python -m src.db_migrate`
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import db

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_CREATE_TRACKING_TABLE = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'schema_migrations' AND schema_id = SCHEMA_ID('dbo'))
CREATE TABLE dbo.schema_migrations (
    filename    NVARCHAR(255) NOT NULL PRIMARY KEY,
    applied_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
);
"""


def run() -> list[str]:
    """Applies every migration file not yet recorded, in filename order. Returns the filenames
    applied this run -- empty on a repeat run, since that's the idempotency contract."""
    conn = db.connect()
    conn.autocommit = False
    try:
        cursor = conn.cursor()
        cursor.execute(_CREATE_TRACKING_TABLE)
        conn.commit()

        cursor.execute("SELECT filename FROM dbo.schema_migrations")
        applied = {row[0] for row in cursor.fetchall()}

        newly_applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            try:
                cursor.execute(sql)
                cursor.execute("INSERT INTO dbo.schema_migrations (filename) VALUES (?)", path.name)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            newly_applied.append(path.name)
            logger.info("applied migration %s", path.name)
        return newly_applied
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.db_migrate
    logging.basicConfig(level=logging.INFO)
    applied_now = run()
    if applied_now:
        print(f"applied {len(applied_now)} migration(s): {', '.join(applied_now)}")
    else:
        print("schema up to date, nothing to apply")
