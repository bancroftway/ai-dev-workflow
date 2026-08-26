-- Which vault secrets the user chose to expose to the sandbox, and under which env names
-- (agent/src/keyvault.py). JSON array of {"name": "<vault secret name>", "env_name": null|"OVERRIDE"}.
-- NULL means "no selection saved" = expose every enabled secret (the pre-selection behavior, kept
-- for every existing row). An EMPTY array means "expose nothing" -- callers must branch on
-- NULL-ness, never truthiness (see keyvault.fetch_app_secrets).
ALTER TABLE dbo.repo_vaults ADD secret_selection NVARCHAR(MAX) NULL;
