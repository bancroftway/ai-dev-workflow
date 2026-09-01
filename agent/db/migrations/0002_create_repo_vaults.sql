-- Per user-repo Azure Key Vault pointer (agent/src/keyvault.py). A row here grants NOTHING by
-- itself: secrets are fetched on-behalf-of the signed-in user (Entra OBO), so Azure's own RBAC on
-- the vault is the enforcement -- a wrong or malicious vault_uri can't expose anything the user
-- couldn't already read themselves.
CREATE TABLE dbo.repo_vaults (
    owner       NVARCHAR(255) NOT NULL,
    repo        NVARCHAR(255) NOT NULL,
    user_login  NVARCHAR(255) NOT NULL,               -- same advisory GitHub login sessions rows carry
    vault_uri   NVARCHAR(256) NOT NULL,               -- https://<name>.vault.azure.net/
    created_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_repo_vaults PRIMARY KEY (owner, repo, user_login)
);
