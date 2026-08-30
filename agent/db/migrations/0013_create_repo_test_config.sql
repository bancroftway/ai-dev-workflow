-- Per-repo application CONFIG VALUES for test boots (agent/src/repo_test_config.py).
-- Keyed on (owner, repo) like repo_auth_settings (0010) -- config is a property of the codebase,
-- not the user. `entries` is a hand-serialized JSON array (same NVARCHAR(MAX)+json.dumps pattern
-- as repo_auth_settings.anonymous_routes / repo_vaults.secret_selection): each entry is
--   {"key": "Section:Key", "value": "...", "secret": false, "source": "detected"|"user"|"boot-error"}
-- Non-secret values live here in plain text and are injected as env vars at e2e boot (key -> env
-- via key.replace(":","__")). A `secret:true` entry carries NO value -- it only records that the
-- key belongs in Key Vault, and the vault secret-picker supplies its value; nothing sensitive is
-- ever stored in this table.
CREATE TABLE dbo.repo_test_config (
    owner       NVARCHAR(255) NOT NULL,
    repo        NVARCHAR(255) NOT NULL,
    entries     NVARCHAR(MAX) NULL,                 -- JSON array; NULL/absent = no config
    updated_by  NVARCHAR(255) NULL,                 -- audit only
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_repo_test_config PRIMARY KEY (owner, repo)
);
