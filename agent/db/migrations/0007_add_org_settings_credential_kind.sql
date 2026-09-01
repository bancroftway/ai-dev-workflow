-- Phase E audit fixes C-1/I-1: dbo.org_settings gained two independent needs since 0003.
--
-- credential_kind (C-1): the Spec's second Claude billing mode -- a `claude setup-token`
-- CLAUDE_CODE_OAUTH_TOKEN (subscription billing) alongside the existing ANTHROPIC_API_KEY (metered
-- billing). NULL means "saved before this column existed" -- every credential saved before this
-- migration was necessarily an API key (oauth mode didn't exist yet), so NULL is read as 'api_key'
-- by org_settings.py/sessions_api.py, never as "unknown". NULL is also the only valid value when
-- provider = 'copilot' (the kind distinction is meaningless there) -- not enforced by a CHECK tying
-- it to provider, same "keep the DB constraint simple, let the app layer own the cross-column rule"
-- choice 0003's own provider CHECK makes for credential_secret_name.
--
-- last_validation_ok / last_validated_at (I-1, lazy version): sessions_api.py's
-- _org_settings_response() re-runs the existing save-time probe at most once an hour and writes the
-- result back here, so a credential that was valid in March and got revoked in June actually flips
-- session_ready to False instead of staying silently green forever (Spec Verification 10). Both
-- NULL until the first probe ever runs -- read as "needs a probe now", not as "known good".
ALTER TABLE dbo.org_settings
  ADD credential_kind NVARCHAR(16) NULL
        CONSTRAINT CK_org_settings_credential_kind
        CHECK (credential_kind IN ('api_key','oauth')),
      last_validation_ok BIT NULL,
      last_validated_at DATETIME2(0) NULL;
