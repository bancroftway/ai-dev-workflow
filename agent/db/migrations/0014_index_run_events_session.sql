-- run_event_store.list_events_by_session filters WHERE session_id = ? -- 0006_create_run_events.sql
-- only indexed (run_id, seq) (list_events(run_id)'s own query shape), so the session_id lookup has
-- been a full table scan since that table was created, just tolerated at the frontend's original
-- 15s poll cadence. sessions_api's new SSE tail endpoint (stream_session_events) calls this same
-- query every couple of seconds for the life of every open browser tab, which would otherwise
-- multiply that scan's frequency by roughly 7x.
CREATE INDEX IX_run_events_session ON dbo.run_events(session_id, seq);
