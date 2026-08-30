TEST USERS -- the app must be testable as each of these declared users, so end-to-end tests can
prove role-based authorization, not just that a page renders:

<<test_users>>

Requirements this places on your work:

- **Seed these users at startup**, but ONLY under the test seam (when `AIDW_TEST_AUTH=1`): create
  each declared user with its roles. For a custom-auth app (the app checks credentials itself), give
  every seeded user the SAME fixed test password `AidwTest!1` -- this password exists only under the
  seam and never in production configuration. For an OIDC app, the users are served by the test
  identity provider; you do not store passwords.
- **Write role-boundary tests, not just happy-path.** For each declared role, prove both what it MAY
  do and what it MAY NOT: an admin-only screen or endpoint must render/200 for the admin persona and
  403/redirect/hide for a lesser persona. Name each test with its acceptance-criterion id as usual.
- **When several roles share the SAME expected outcome on one surface** (two elevated roles that can
  both see a page), write ONE test that iterates those personas inside its body, and pair it with the
  excluded role's negative case. Two separate near-identical per-role tests that assert the same
  thing collapse to one under the duplicate-test check and waste a redraft -- the paired negative is
  what gives the criterion two genuinely distinct assertion targets.
- Log in as each persona through the app's real sign-in path (the login form for custom auth; the
  redirect flow for OIDC). Do not fabricate a session the app's own middleware would never issue.

For an OIDC app, at test time the environment variable `AIDW_IDP_URL` points at a local identity
provider preloaded with these users:

- Point the app's OIDC authority at it. The authority is `http://` (local), so in the test
  environment only, allow HTTP metadata — for .NET set `RequireHttpsMetadata = false` guarded by
  `env.IsDevelopment()` (never in production config). The provider issues Entra-shaped claims
  (`oid`, `preferred_username`, `roles`), so read roles from those claims exactly as you would from
  real Entra.
- Playwright drives real logins: write a `global-setup` (placed UNDER `tests/e2e/` so the test
  write-scope allows it) that, for each persona, opens the app, follows the redirect to the IdP, and
  clicks that persona's `data-testid="login-<email>"` button, then saves the session with
  `storageState` to `test-results/.auth/<persona>.json` (that directory is git-ignored and wiped
  between runs — never write auth state under `playwright/.auth/`, which would be committed). Define
  one Playwright project per persona that loads its `storageState`; tag each role test with the
  project/persona it must run as.
- If the app's OIDC library rejects the local http authority outright, fall back to the
  `AIDW_TEST_AUTH=1` seam sign-in for these tests rather than failing.
