# ai-dev-workflow

## GitHub Copilot authentication

The Python agent (`agent/`) authenticates to GitHub Copilot via a single, shared
server-side `GITHUB_TOKEN` — a fine-grained personal access token (owned by a
personal GitHub account) with the "Copilot Requests" permission. This is
separate from the `AUTH_GITHUB_ID`/`AUTH_GITHUB_SECRET` GitHub OAuth App used
for user sign-in to this app.

We investigated replacing the shared PAT with each signed-in user's own
Copilot access (so requests would run against the requesting user's own
Copilot license instead of a shared token). This is **not currently possible**:
GitHub's "Copilot Requests" permission can only be granted to user-owned
fine-grained personal access tokens — it is not available to OAuth App or
GitHub App tokens, so there is no OAuth-based way to obtain per-user Copilot
access today. See the open, unresolved feature request:
[github/gh-aw#18379 — Feature request to support GitHub app-based
authentication for copilot requests](https://github.com/github/gh-aw/issues/18379).

If GitHub adds App-based Copilot auth in the future, this would let each user
authenticate with their own Copilot seat via the existing GitHub OAuth sign-in
flow, removing the need for the shared `GITHUB_TOKEN`.