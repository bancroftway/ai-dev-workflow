-- Support repo for failed-run issues: the GitHub "owner/repo" of THIS TOOL's own support/ops
-- repository, where the frontend's user-initiated "Open support issue" action files issues about
-- failed runs -- never the customer repo the run worked on. NULL = not configured; the action
-- refuses with a pointer to the organization settings page. A pointer only (same discipline as
-- credential_secret_name above it): format is validated app-side in sessions_api.py, no CHECK,
-- matching 0003's keep-the-DB-constraint-simple choice for cross-column/app-owned rules.
ALTER TABLE dbo.org_settings
  ADD support_repo NVARCHAR(255) NULL;
