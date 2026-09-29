-- Org-wide extra gitleaks allowlist entries (org_settings.py, repo_scan.py's
-- _build_gitleaks_command): lets an admin suppress known-false-positive secret findings (e.g.
-- e2e/smoke-test fixture values) without touching every scanned repo's own git history. Each
-- column holds raw, newline-separated text (one pattern per line), parsed only where it's
-- consumed. NULL = no override, today's exact behavior.
ALTER TABLE dbo.org_settings ADD gitleaks_extra_stopwords NVARCHAR(MAX) NULL;
ALTER TABLE dbo.org_settings ADD gitleaks_extra_allow_paths NVARCHAR(MAX) NULL;
