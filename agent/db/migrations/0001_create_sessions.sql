-- Single source of truth for session metadata (replaces .ai-dev-workflow/sessions.json).
-- One row per session; mutated in place through in_progress -> completed|failed|rejected.
CREATE TABLE dbo.sessions (
    session_id       UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,   -- == LangGraph thread_id == sandbox session_id
    owner            NVARCHAR(255)    NOT NULL,
    repo             NVARCHAR(255)    NOT NULL,
    user_login       NVARCHAR(255)    NOT NULL,
    title            NVARCHAR(200)    NOT NULL,
    source_branch    NVARCHAR(500)    NOT NULL,               -- PR-target branch chosen at start
    work_branch      NVARCHAR(500)    NOT NULL,               -- ai-dev-workflow/<session_id>, computed once by branch_naming.py, stored, never recomputed
    run_id           VARCHAR(8)       NULL,                   -- latest attempt id, for history/ artifact correlation
    current_stage    NVARCHAR(100)    NULL,                   -- stage key, updated on every gate approval; drives the UI progress indicator
    status           VARCHAR(20)      NOT NULL
                       CONSTRAINT CK_sessions_status
                       CHECK (status IN ('in_progress','completed','failed','rejected')),
    started_at       DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME(),
    ended_at         DATETIME2(0)     NULL,
    merge_ready      BIT              NULL,
    pr_title         NVARCHAR(500)    NULL,
    pr_url           NVARCHAR(500)    NULL,
    failure_stage    NVARCHAR(100)    NULL,
    failure_type     NVARCHAR(100)    NULL,
    failure_message  NVARCHAR(1000)   NULL,
    updated_at       DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE INDEX IX_sessions_repo_recent ON dbo.sessions(owner, repo, source_branch, started_at DESC);
