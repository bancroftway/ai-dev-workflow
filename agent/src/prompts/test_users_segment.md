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
