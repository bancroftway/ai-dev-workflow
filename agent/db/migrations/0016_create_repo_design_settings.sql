-- Per-repo DESIGN.md override (agent/src/repo_design_settings.py).
-- Keyed on (owner, repo), same reasoning as repo_test_config (0013): the design system is a
-- property of the codebase/product, not the user. A NULL/absent row means "no repo-specific
-- override" -- repo_design_settings.get_effective_design_md() then falls back to
-- dbo.org_settings.design_md (0017), the deployment-wide default. design_md is the full DESIGN.md
-- markdown document (YAML frontmatter + canonical sections per the DESIGN.md format spec impeccable
-- follows), stored verbatim -- never parsed or reshaped on write, only on read by the deterministic
-- token gate and by the settings API's tokens_detected hint.
CREATE TABLE dbo.repo_design_settings (
    owner       NVARCHAR(255) NOT NULL,
    repo        NVARCHAR(255) NOT NULL,
    design_md   NVARCHAR(MAX) NULL,
    updated_by  NVARCHAR(255) NULL,                 -- audit only
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_repo_design_settings PRIMARY KEY (owner, repo)
);
