-- dbo.verify_check_results (agent/src/verify_check_store.py) -- durable per-check verify history:
-- one row per CheckResult (agent/src/gates/checks.py) per verify attempt. state.json's
-- last_verification only ever holds the LATEST attempt and dies with the sandbox; this table keeps
-- every attempt of every session, so the session "verify history" view and the per-repo
-- "verify insights" (fail rate per check, attempts-to-pass per stage) have data to read.
--
-- owner/repo are NOT copied here -- check_stats joins dbo.sessions for them. Check labels are not
-- stored either: they come from the live pipeline descriptor, so a renamed label never goes stale
-- here and a retired check_id still shows up (as its raw id) in old rows.
CREATE TABLE dbo.verify_check_results (
    id            BIGINT           NOT NULL IDENTITY(1,1) PRIMARY KEY,
    session_id    UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.sessions(session_id), -- no ON DELETE CASCADE: sessions_api.delete_session_full calls verify_check_store.delete_by_session first
    run_id        VARCHAR(8)       NOT NULL,               -- state["run_id"]; not a FK, remints across resumes (see 0006)
    stage         NVARCHAR(100)    NOT NULL,               -- stage key, same domain as dbo.sessions.current_stage
    attempt       INT              NOT NULL,               -- verify lap within this stage, 1-based
    timing        VARCHAR(20)      NOT NULL
                    CONSTRAINT CK_verify_check_results_timing
                    CHECK (timing IN ('before_review','after_submit')),
    code_gen_mode VARCHAR(20)      NOT NULL
                    CONSTRAINT CK_verify_check_results_mode
                    CHECK (code_gen_mode IN ('yolo','draft_verify','mission_critical')),
    policy        VARCHAR(10)      NOT NULL
                    CONSTRAINT CK_verify_check_results_policy
                    CHECK (policy IN ('off','advisory','blocking')),
    check_id      NVARCHAR(128)    NOT NULL,
    status        VARCHAR(10)      NOT NULL
                    CONSTRAINT CK_verify_check_results_status
                    CHECK (status IN ('passed','failed','infra','skipped','advisory')),
    detail        NVARCHAR(2000)   NULL,                   -- truncate_middle'd by the store to fit
    source        NVARCHAR(255)    NULL,
    stage_passed  BIT              NOT NULL,               -- the whole attempt's verdict, repeated per row
    uncatalogued  BIT              NOT NULL DEFAULT 0,     -- check emitted but not declared in the stage's Gate
    created_at    DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME()
);

-- list_attempts(session_id, stage) and delete_by_session(session_id).
CREATE INDEX IX_verify_check_results_session ON dbo.verify_check_results(session_id, stage, attempt);
-- check_stats(owner, repo, since, mode): per-check fail counts over a time window.
CREATE INDEX IX_verify_check_results_check ON dbo.verify_check_results(check_id, status, created_at);
