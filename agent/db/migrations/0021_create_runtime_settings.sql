-- Generic key/value store for operator-tunable runtime settings migrated off
-- agent/src/config.py's hardcoded env-var constants (see agent/src/runtime_settings.py). One row
-- per setting. setting_value holds the SAME string shape the setting's own env-var parser already
-- expects (a CSV-style setting stores "a,b,c", a JSON-typed setting stores real JSON text) --
-- deliberately not a blanket json.dumps() of an arbitrary Python value, which would conflate two
-- different string encodings under one parser (see runtime_settings.py's own module docstring for
-- the bug this caused in an earlier draft). Deliberately NOT folded into dbo.org_settings (0003),
-- whose columns are individually hand-validated (CHECK constraints, _reject_literal_quote) and
-- don't scale to the ~100+ generic tunables this table holds. Still no org_id column: this
-- deployment has no multi-tenant concept (see 0003's own header comment) and this table inherits
-- that same one-deployment-one-org shape.
--
-- No seed step: an absent row (or setting_value IS NULL) means "no override, fall back to
-- config.py's env-var/default" -- the same None-on-fresh-deploy contract org_settings.
-- get_org_settings() already has. Leaving this table empty on deploy makes day-1 behavior
-- byte-identical to today's hardcoded config.py, without duplicating every default in SQL AND
-- Python.
--
-- Pinned once per session, not live mid-session: runtime_settings.pin_for_session() reads this
-- table once per session/run (agent/main.py's long-lived server pins once per session via a
-- contextvars.ContextVar propagated through that session's own asyncio Task; run_headless.py's
-- short-lived CLI process pins once at its own startup, since its whole process is one session).
-- An edit here reaches the next session started, never an in-flight one -- deliberately matching
-- state["provider"]'s own "pin once per run, never drift mid-run" rule (chat_model.py) rather than
-- a live-refreshed cache; see the migration plan's "Read/refresh frequency" section.
CREATE TABLE dbo.runtime_settings (
    setting_key   NVARCHAR(200) NOT NULL PRIMARY KEY,
    setting_value NVARCHAR(MAX) NULL,       -- env-var-shaped string; NULL = no override, use config.py's env/default
    updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_by    NVARCHAR(255) NULL        -- admin's GitHub/Entra login, audit trail only
);
