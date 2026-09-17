-- Overview tab redraft history (Workstream 3): agent/src/run_events.py's RunEvent gained four
-- fields for a draft/audit/fix NODE_FINISHED event's full input/output -- nothing durable captured
-- this before (the assembled prompt and the model's response both lived only in-process during the
-- call and were discarded; Claude's own CLI transcript exists but only inside the ephemeral
-- per-session sandbox, gone at teardown).
--
-- VARBINARY (UTF-8 encoded by the Python side), not NVARCHAR (UTF-16) -- halves stored bytes,
-- deliberate given dbo.run_events already shares this DB's 2GB Basic-tier cap with everything else.
-- input_size/output_size are the byte lengths, stored alongside so the Overview tab can render
-- sizes from the existing events-list query without ever fetching the (potentially large) text --
-- see sessions_api.py's new GET /sessions/{session_id}/events/{seq}/io for the on-demand fetch of
-- the text itself.
ALTER TABLE dbo.run_events
  ADD input_text  VARBINARY(MAX) NULL,
      output_text VARBINARY(MAX) NULL,
      input_size  INT NULL,
      output_size INT NULL;
