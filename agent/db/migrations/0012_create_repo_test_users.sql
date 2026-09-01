-- Per-repo TEST USERS for multi-role e2e (agent/src/repo_test_users.py).
-- Keyed on (owner, repo) like repo_auth_settings/repo_test_config -- who the app should be tested
-- as is a property of the codebase, shared by teammates. `users` is a hand-serialized JSON array
-- (same NVARCHAR(MAX)+json.dumps pattern), each entry:
--   {"name": "Ada Admin", "email": "ada@test.local", "roles": ["Admin"]}
-- NO PASSWORDS, ever: for custom-auth apps the seam mints a fixed test password at seed time; for
-- OIDC apps the fake IdP issues tokens. The table only declares WHO should exist and WHAT they may
-- do -- nothing sensitive is stored.
CREATE TABLE dbo.repo_test_users (
    owner       NVARCHAR(255) NOT NULL,
    repo        NVARCHAR(255) NOT NULL,
    users       NVARCHAR(MAX) NULL,                 -- JSON array; NULL/absent = no declared users
    updated_by  NVARCHAR(255) NULL,
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_repo_test_users PRIMARY KEY (owner, repo)
);
