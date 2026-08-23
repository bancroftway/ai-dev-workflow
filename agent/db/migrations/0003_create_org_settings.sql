-- Org-wide active coding-agent provider + a pointer to its credential (agent/src/org_settings.py).
-- This pipeline has no multi-tenant/org-id concept today -- one deployment is one org, matching
-- infra/main.bicep's own one-Container-App-per-deployment shape -- so the table holds exactly one
-- conceptual row. Singleton is pinned the same way SQL Server conventionally fixes a genuinely
-- one-row table: PRIMARY KEY on a column CHECKed to a single fixed value, so a second INSERT fails
-- the constraint instead of silently creating a second, ambiguous "active" row -- no app-level
-- guard could enforce this as cheaply or as unconditionally as the engine itself.
-- credential_secret_name is a POINTER ONLY -- the Key Vault secret name Task 2's vault stores the
-- real credential under -- never the credential value itself, same discipline as
-- dbo.repo_vaults.vault_uri (0002) being a pointer rather than a copy of what it names.
CREATE TABLE dbo.org_settings (
    id                      INT           NOT NULL PRIMARY KEY
                              CONSTRAINT CK_org_settings_singleton
                              CHECK (id = 1),
    provider                NVARCHAR(16)  NOT NULL              -- 'copilot' | 'claude', mirrors AGENT_PROVIDER's own values
                              CONSTRAINT CK_org_settings_provider
                              CHECK (provider IN ('copilot','claude')),
    credential_secret_name  NVARCHAR(255) NULL,                 -- Key Vault secret name; never the secret value
    updated_at              DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_by              NVARCHAR(255) NULL                  -- admin's GitHub or Entra login, audit trail only; same width as dbo.sessions.user_login
);
