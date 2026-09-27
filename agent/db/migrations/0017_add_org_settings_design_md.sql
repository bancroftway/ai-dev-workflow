-- Deployment-wide default DESIGN.md (agent/src/org_settings.py), same shape as support_repo (0011):
-- an unrelated pointer bolted onto the org_settings singleton rather than a new one-row table.
-- A repo with no dbo.repo_design_settings override (0016) falls back to this value; NULL means no
-- default has been set, and the platform falls back further to its existing per-repo
-- reverse-engineered DESIGN.md behavior (impeccable's `document` command).
ALTER TABLE dbo.org_settings ADD design_md NVARCHAR(MAX) NULL;
