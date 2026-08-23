-- dbo.run_events (agent/src/run_event_store.py, agent/src/run_events.py) -- durable per-run event
-- log for Part 2 (run-visibility UI redesign). Additive alongside repo_files.append_ledger_entry's
-- existing JSON-lines ledger, which lives only inside the sandbox's own workspace and is gone once
-- that sandbox is torn down: a run-detail page needs history that outlives the sandbox that
-- produced it. graph.py's draft/audit/verify nodes call run_event_store.append_event right next to
-- their existing append_ledger_entry call, same data -- the ledger write itself is untouched.
--
-- seq is the table's own IDENTITY, not a per-run counter reset to 1 for each run_id -- ordering a
-- given run_id's events by seq is exactly as correct as a real per-run 1,2,3..., and IDENTITY needs
-- no read-modify-write to assign (no races, no locking), unlike a hand-rolled per-run counter would.
CREATE TABLE dbo.run_events (
    seq          BIGINT           NOT NULL IDENTITY(1,1) PRIMARY KEY,
    run_id       VARCHAR(8)       NOT NULL,               -- state["run_id"] (graph.py, uuid4().hex[:8]) -- not a FK, sessions.run_id remints across resumes so it's not a stable target
    session_id   UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.sessions(session_id),
    ts           DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME(),
    stage        NVARCHAR(100)    NULL,                   -- stage key, same domain as dbo.sessions.current_stage
    node         NVARCHAR(100)    NULL,                   -- sub-step within the stage, e.g. draft/audit/verify
    type         VARCHAR(20)      NOT NULL
                   CONSTRAINT CK_run_events_type
                   CHECK (type IN ('node_started','node_finished','tool_call','reasoning','gate_paused','gate_resolved')),
    summary      NVARCHAR(500)    NULL,
    payload      NVARCHAR(MAX)    NULL,                   -- JSON, arbitrary per-type/node detail
    token_usage  NVARCHAR(MAX)    NULL                    -- JSON ({model, input_tokens, output_tokens, cost}), NULL for non-LLM events
);

-- list_events(run_id)'s only query shape: every event for one run, oldest first.
CREATE INDEX IX_run_events_run ON dbo.run_events(run_id, seq);
