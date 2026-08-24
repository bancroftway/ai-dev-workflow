-- Phase E audit I-3: the in-flight-run provider guarantee pinned GraphState.provider (per-thread,
-- never re-resolved -- graph.py:1516) but re-provisioning a session's CONTAINER always read the
-- LIVE org setting (sessions_api.py/local_docker.py/azure_aci.py all called chat_model.
-- get_provider() fresh). A run pinned to "claude" whose container gets idle-reaped after an admin
-- flips the org setting to "copilot" then reprovisions onto a copilot-flavored container/credential
-- while the checkpointed graph correctly keeps dispatching to claude -- every turn fails auth.
--
-- NULL means "provisioned before this migration, or a row this codebase never got around to
-- stamping" -- read as "no pinned value, fall back to the live org setting" by
-- sessions_api.provision_session, never as an implicit "copilot" default (0003_create_org_settings
-- already owns that fallback; this column's job is only to remember what a PRIOR provision actually
-- used, once one has happened).
ALTER TABLE dbo.sessions
  ADD provider NVARCHAR(16) NULL
        CONSTRAINT CK_sessions_provider
        CHECK (provider IN ('copilot','claude'));
