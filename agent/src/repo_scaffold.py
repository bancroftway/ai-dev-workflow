"""GitHub repo scaffolding for the "+ New Project" path (Part 3 plan, Ruling 6).

Same plain-REST-over-Bearer-token pattern git_ops.py's open_pull_request/delete_remote_branch
already establish for api.github.com -- no SDK dependency needed for one POST. Unlike those two
(both best-effort, log-and-continue), a failed repo creation must stop the "+ New Project" flow
cold: there is no reasonable "continue anyway" when the repo a ticket needs never got created. So
this raises with GitHub's own response body instead of swallowing it, rather than inventing a new
error-reporting convention.

Personal account only (POST /user/repos, never an org endpoint -- the wireframe has no owner
picker), repo name == the project's name slugified (no separate repo-name field), private by
default (no UI toggle exists yet). No client-side name-collision pre-check -- GitHub's own 422 is
the real validator, same "fail fast with the provider's own error" precedent org_settings_router's
credential probe and the per-repo vault PUT already use.
"""

from __future__ import annotations

import re

import httpx

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_repo_name(name: str) -> str:
    """'My Cool App!' -> 'my-cool-app'. Same collapse-runs-to-one-hyphen idiom as
    e2e_nodes._route_slug (lowercase, any run of chars outside [a-z0-9] becomes one hyphen, trim
    the ends) -- GitHub's own 422 is the real validator for anything this doesn't catch (length,
    reserved names, an actual collision), matching this module's own no-pre-check stance."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "repo"  # GitHub rejects an empty name outright; give it something to reject/accept instead


async def create_repo(
    name: str, github_token: str, *, client: httpx.AsyncClient | None = None
) -> dict:
    """POST /user/repos under the token's own personal account. Returns {owner, repo, clone_url}
    straight from GitHub's response on success. Raises RuntimeError carrying GitHub's real
    response body (truncated, same 300-char convention git_ops.py logs with) on any non-201 --
    a name collision's 422 included.

    `client` is test-only dependency injection (see this module's own _demo): omitted in every
    real call site, which gets a fresh short-lived client exactly like open_pull_request/
    delete_remote_branch already do.
    """
    slug = slugify_repo_name(name)
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"name": slug, "private": True}
    if client is None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://api.github.com/user/repos", headers=headers, json=payload)
    else:
        resp = await client.post("https://api.github.com/user/repos", headers=headers, json=payload)

    if resp.status_code != 201:
        raise RuntimeError(f"create_repo failed for {slug!r}: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    return {"owner": body["owner"]["login"], "repo": body["name"], "clone_url": body["clone_url"]}


def _demo() -> None:
    """`cd agent && uv run python -m src.repo_scaffold`.

    No live network call -- httpx.MockTransport (already a transitive part of the installed httpx,
    not a new dependency) stands in for api.github.com so this exercises the real
    request-building/response-parsing/error-raising code paths without ever touching the network.

    Why not a real live call: two currently-valid GitHub tokens exist in this dev environment (gh
    CLI's own stored session, and .env's E2E_GITHUB_TOKEN) -- confirmed live via a read-only GET
    /user against both. But GET /user's own X-OAuth-Scopes response header shows neither carries
    `delete_repo` (gh CLI: gist, read:org, repo, workflow; E2E_GITHUB_TOKEN: project, repo) -- so a
    real POST /user/repos here could not be cleaned up afterward via DELETE as required, and would
    leave a stray private repo on a real personal GitHub account. See task-3-report.md.
    """
    import asyncio

    # --- slugification -------------------------------------------------------------------------
    assert slugify_repo_name("My Cool App!") == "my-cool-app"
    assert slugify_repo_name("  spaces   everywhere  ") == "spaces-everywhere"
    assert slugify_repo_name("Already-Slug-42") == "already-slug-42"
    assert slugify_repo_name("!!!") == "repo", "an all-rejected name must still produce something POST-able"
    assert slugify_repo_name("") == "repo"

    # --- request building + success-response parsing ------------------------------------------
    seen: dict = {}

    def handle_created(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(
            201,
            json={
                "name": "my-cool-app",
                "owner": {"login": "octocat"},
                "clone_url": "https://github.com/octocat/my-cool-app.git",
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_created))
    result = asyncio.run(create_repo("My Cool App!", "tok123", client=mock_client))
    asyncio.run(mock_client.aclose())

    assert result == {
        "owner": "octocat",
        "repo": "my-cool-app",
        "clone_url": "https://github.com/octocat/my-cool-app.git",
    }, result
    assert seen["url"] == "https://api.github.com/user/repos", seen["url"]
    assert seen["auth"] == "Bearer tok123", seen["auth"]
    assert b'"name":"my-cool-app"' in seen["body"], seen["body"]
    assert b'"private":true' in seen["body"], seen["body"]

    # --- failure surfaces GitHub's real body, not a generic message ----------------------------
    def handle_422(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "name already exists on this account"})

    mock_client_422 = httpx.AsyncClient(transport=httpx.MockTransport(handle_422))
    try:
        asyncio.run(create_repo("taken-name", "tok123", client=mock_client_422))
        raise AssertionError("create_repo must raise on a non-201 response")
    except RuntimeError as exc:
        assert "422" in str(exc) and "name already exists" in str(exc), str(exc)
    finally:
        asyncio.run(mock_client_422.aclose())

    print("repo_scaffold self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
