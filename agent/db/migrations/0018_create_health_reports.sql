-- On-demand "Generate Code Health Report" jobs (health_report_api.py): a standalone, ephemeral
-- scan (agent/src/repo_scan.py's "health_report" profile) run outside the normal session
-- lifecycle, so it does not fit dbo.sessions (0001) -- no project, no work branch, no PR, and
-- its container is torn down the instant the scan finishes rather than staying open for a human
-- gate. One row per job, mutated in place through queued -> running -> completed|failed, never
-- resumed/reopened (a failed job's fix is to click "Generate Code Health Report" again, which
-- mints a new job_id).
CREATE TABLE dbo.health_reports (
    job_id        UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    owner         NVARCHAR(255) NOT NULL,
    repo          NVARCHAR(255) NOT NULL,
    branch        NVARCHAR(500) NOT NULL,
    user_login    NVARCHAR(255) NOT NULL,
    status        VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    -- repo_scan.py's ScanReport.to_dashboard_dict() output, once completed. This job's only
    -- durable artifact store: unlike a session's report.json, there is no PR branch to commit
    -- this into, and the sandbox that produced it is gone within seconds of the scan finishing.
    report_json   NVARCHAR(MAX) NULL,
    error_message NVARCHAR(1000) NULL,
    created_at    DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    completed_at  DATETIME2(0) NULL
);

-- health_report_api.py's per-repo "one job in flight at a time" 409 guard (mirrors
-- sessions_api.py's _reject_if_another_ticket_open) filters on exactly (owner, repo, status).
CREATE INDEX IX_health_reports_owner_repo_status ON dbo.health_reports (owner, repo, status);
