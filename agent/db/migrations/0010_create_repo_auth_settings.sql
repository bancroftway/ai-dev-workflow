-- Per-repo application-authentication posture (agent/src/repo_auth_settings.py). Keyed on
-- (owner, repo) -- NOT per-user like repo_vaults: the auth posture of the generated application is
-- a property of the codebase, and teammates who share a repo's sessions (see src/lib/
-- session-access.ts) must get the same generated app. `updated_by` is advisory attribution only.
--
-- auth_mode:
--   'required'       every route and API endpoint of the generated app must demand Entra sign-in
--                    (the enterprise default -- also the default when NO row exists; enforcement
--                    additionally requires Key Vault auth secrets to actually be present, see
--                    graph.py's app_auth seeding)
--   'anonymous_list' locked down except the fnmatch patterns in anonymous_routes (JSON array)
--   'none'           no auth requirement is injected and no auth gate runs
CREATE TABLE dbo.repo_auth_settings (
    owner            NVARCHAR(255) NOT NULL,
    repo             NVARCHAR(255) NOT NULL,
    auth_mode        NVARCHAR(20)  NOT NULL DEFAULT 'required'
        CONSTRAINT CK_repo_auth_settings_mode CHECK (auth_mode IN ('required', 'anonymous_list', 'none')),
    anonymous_routes NVARCHAR(MAX) NULL,                -- JSON array of route patterns, e.g. ["/", "/health*"]
    updated_by       NVARCHAR(255) NULL,
    updated_at       DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_repo_auth_settings PRIMARY KEY (owner, repo)
);
