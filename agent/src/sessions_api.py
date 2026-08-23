"""Sandbox session provisioning/teardown endpoints (architecture plan Section C.4).

Called by a Next.js SERVER action/route -- never the browser directly -- when a user opens or
closes a repo/branch session (plan Section A). Mounted onto the main FastAPI app in main.py.

Known gaps, flagged rather than glossed over:
- plan Section C.4/Finding #4 (adversarial audit) require these endpoints to check caller
  identity, not just network placement, since an untrusted sandbox's postCreateCommand could
  otherwise reach them directly. `AIDW_AGENT_SHARED_SECRET` (optional) closes most of that gap: an
  `x-aidw-secret` header, checked below, that only the Next.js server (never the browser or a
  sandbox) is meant to hold. Unset (the default) leaves these routes exactly as unauthenticated as
  before -- appropriate only for local development.
- CORS: main.py's `CORSMiddleware(allow_origins=["*"])` is app-global (added once to the shared
  FastAPI `app`), so it also covers this router's side-effecting routes. Splitting it out cleanly
  would mean mounting this router as its own sub-application, which is more than this task's
  scope -- the shared secret above is the actual guard for an unwanted caller; CORS is a
  browser-enforced convention an arbitrary HTTP client (a sandbox's postCreateCommand included)
  never has to respect anyway.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
import httpx
import pyodbc
from pydantic import BaseModel

from . import (
    app_discovery,
    branch_naming,
    chat_model,
    git_ops,
    keyvault,
    org_credential_vault,
    org_settings,
    project_store,
    repo_scaffold,
    session_store,
)
from .sandbox import get_sandbox_provider, registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])
config_router = APIRouter(prefix="/vault-config", tags=["vault-config"])
org_settings_router = APIRouter(prefix="/org-settings", tags=["org-settings"])
catalog_router = APIRouter(tags=["tech-stack"])
projects_router = APIRouter(prefix="/projects", tags=["projects"])

_SHARED_SECRET_HEADER = "x-aidw-secret"


def _check_shared_secret(request: Request) -> None:
    """No-op when AIDW_AGENT_SHARED_SECRET is unset (documented known gap above) -- so existing
    local-dev setups keep working unchanged. Set it, and provision/delete both start rejecting any
    caller that doesn't echo it back in the `x-aidw-secret` header."""
    secret = os.environ.get("AIDW_AGENT_SHARED_SECRET")
    if not secret:
        return
    if request.headers.get(_SHARED_SECRET_HEADER) != secret:
        raise HTTPException(status_code=401, detail="missing or invalid shared secret")


class ProvisionRequest(BaseModel):
    thread_id: str
    # Which project (agent/src/project_store.py) this ticket belongs to -- every session belongs
    # to exactly one project (Ruling 1, docs/superpowers/plans/part-3-tickets-tasks.md). Optional
    # here, not required: provision_session below falls back to an EXISTING session's own stored
    # project_id (resume, or an incidental reprovision of an already-created session) so neither
    # the resume flow nor a stale/bookmarked workflow URL has to keep resupplying one. Only a
    # genuinely brand-new session (no row yet) requires the caller to have actually resolved one
    # first -- the /select and New Ticket flows do that via POST /projects/connect or /projects.
    # When the named project has no repo yet (the "+ New Project" case), owner/repo below are not
    # yet known to the frontend -- provision_session scaffolds a new GitHub repo for it instead of
    # using owner/repo as sent (see provision_session).
    project_id: str | None = None
    owner: str
    repo: str
    branch: str
    github_token: str = ""
    # Advisory only (unverifiable agent-side) -- the GitHub login the Next.js provision route's
    # own session carried, forwarded here purely so session_store.py can label the session row
    # for the /select history UI.
    user_login: str = ""
    # True on a `?resume=1` re-entry into an existing session (graph.py's intake_node consumes
    # this exactly once via sandbox.registry.pop_meta_flag). thread_id is the exact historical
    # session_id being resumed in that case -- never recomputed, unlike a new session's thread_id
    # (a fresh UUID the frontend mints), since branch-per-session means there is no deterministic
    # (owner, repo, user) -> thread_id formula to recompute from anymore.
    resume: bool = False
    # The user's Entra access token for the agent API, forwarded by the Next.js provision route.
    # Exchanged once, immediately, on-behalf-of the user for this session's Key Vault secrets
    # (keyvault.py), then discarded -- never stored, never passed into the sandbox. None in
    # E2E-bypass and headless runs, which simply skip the vault fetch.
    entra_assertion: str | None = None


class ProvisionResponse(BaseModel):
    status: str


@router.post("/provision", response_model=ProvisionResponse)
async def provision_session(body: ProvisionRequest, request: Request) -> ProvisionResponse:
    _check_shared_secret(request)

    existing = await session_store.get_session(body.thread_id)
    if body.resume:
        if existing is None:
            raise HTTPException(status_code=404, detail="no session found to resume")
        if existing["status"] == "completed":
            # Server-enforced, not just a hidden Resume button: a completed session can never be
            # resumed, regardless of what the frontend sends.
            raise HTTPException(status_code=409, detail="a completed session cannot be resumed")

    # An existing session row already carries an authoritative project_id -- reused automatically
    # (not just trusted from the request) so a resume, or an incidental reprovision of a session
    # that already exists, never depends on the frontend re-supplying or re-discovering one. Only
    # a genuinely brand-new session (no row yet) requires body.project_id, which the /select and
    # New Ticket flows both resolve (via POST /projects/connect or /projects) before calling here.
    project_id = (existing.get("project_id") if existing is not None else None) or body.project_id
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required to provision a new session")

    # Vault fetch happens BEFORE the sandbox boots: a misconfigured/revoked vault fails the
    # provision in seconds with the provider's own AADSTS/403 detail, instead of surfacing hours
    # later as a confusing e2e boot failure. A configured vault with no assertion (E2E bypass,
    # headless) is only a warning -- those runs proceed secretless, exactly as before.
    vault_uri = await keyvault.get_vault_uri(body.owner, body.repo, body.user_login)
    if vault_uri and body.entra_assertion:
        try:
            app_secrets = await keyvault.fetch_app_secrets(vault_uri, body.entra_assertion)
        except keyvault.VaultAccessError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"key vault {vault_uri} is configured for this repo but not readable as you: {exc}",
            ) from None
        keyvault.set_app_secrets(body.thread_id, app_secrets)
        logger.info("fetched %d app secret(s) for thread_id=%s", len(app_secrets), body.thread_id)
    elif vault_uri:
        logger.warning(
            "key vault configured for %s/%s but provision carried no entra_assertion (thread_id=%s) -- app secrets unavailable",
            body.owner, body.repo, body.thread_id,
        )

    project = await project_store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")

    # "+ New Project" case (Ruling 6, part-3-tickets-tasks.md): no GitHub repo exists yet for this
    # project. Scaffold one now, under the signed-in user's own personal account -- POST
    # /user/repos (repo_scaffold.create_repo) always creates under whichever account
    # body.github_token authenticates as, so there is no separate "owner" to compute or pass here
    # (see task-2-report.md point 5). body.owner/body.repo are NOT used below in this branch: the
    # frontend has no repo to send yet for a project that doesn't have one. Backfilling
    # owner/repo onto the project row immediately (before this session row is even created) means
    # a retried or second provision call against the same project_id sees project["repo"] already
    # set and never re-scaffolds a second repo.
    scaffold_new_repo = project["repo"] is None
    if scaffold_new_repo:
        try:
            scaffolded = await repo_scaffold.create_repo(project["name"], body.github_token)
        except Exception as exc:  # noqa: BLE001 -- create_repo raises RuntimeError on a GitHub-side
            # failure (a 422 name collision included) but, per its own module's report, doesn't
            # itself wrap a raw httpx transport error -- catching broadly here, same as the
            # provider.provision() except block below, keeps either failure mode a clean 502
            # instead of a 500, and guarantees this request structurally cannot fall through to
            # session_store.create_session for a repo that was never actually created.
            logger.exception("repo scaffolding failed for project_id=%s", project_id)
            raise HTTPException(status_code=502, detail=f"repo scaffolding failed: {exc}") from None
        owner, repo = scaffolded["owner"], scaffolded["repo"]
        # The GitHub repo now genuinely exists -- losing this write would wedge the project
        # (owner/repo stay NULL forever, and every retry re-hits create_repo with the identical
        # slug, which GitHub now 422s as a collision). One retry covers the realistic case (a
        # transient DB blip immediately after a successful network call), not a sustained outage;
        # no GitHub-side reconciliation (searching for/re-linking an already-created repo) is
        # built here -- deliberately out of scope, a human resolves the rare double-failure below.
        try:
            await project_store.set_project_repo(project_id, owner, repo)
        except Exception:  # noqa: BLE001 -- one bounded retry, then a clean 502 with recovery detail
            logger.exception(
                "set_project_repo failed for project_id=%s (owner=%s repo=%s) -- retrying once",
                project_id, owner, repo,
            )
            try:
                await project_store.set_project_repo(project_id, owner, repo)
            except Exception as exc:
                logger.exception(
                    "set_project_repo failed again for project_id=%s (owner=%s repo=%s) -- giving up",
                    project_id, owner, repo,
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"scaffolded GitHub repo {owner}/{repo} for project {project_id} but "
                        "failed to record it after a retry -- a human needs to either delete "
                        f"https://github.com/{owner}/{repo} or run project_store.set_project_repo "
                        "manually"
                    ),
                ) from None
    else:
        owner, repo = body.owner, body.repo

    work_branch = branch_naming.work_branch_for(body.thread_id)
    provider = get_sandbox_provider()
    repo_clone_url = f"https://github.com/{owner}/{repo}.git"
    # The active provider's own secret -- an org admin's Settings-UI-saved vault credential if one
    # is configured, else the same env-var fallback this used to compute by hand (see
    # sandbox/provider.py's provision() docstring for how the sandbox uses this). Provisioning a
    # new session is exactly the moment a live setting change should take effect (Ruling 2), so
    # this reads fresh via get_runtime_auth_token() rather than a pinned state["provider"] -- there
    # is no GraphState here yet, this runs before intake_node ever does.
    runtime_auth_token = await chat_model.get_runtime_auth_token()
    try:
        session = await provider.provision(
            session_id=body.thread_id,
            repo_clone_url=repo_clone_url,
            branch=body.branch,
            work_branch=work_branch,
            git_user_token=body.github_token,
            runtime_auth_token=runtime_auth_token,
            scaffold_new_repo=scaffold_new_repo,
            project_name=project["name"] if scaffold_new_repo else None,
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced to the caller as a plain 502, not swallowed
        logger.exception("sandbox provisioning failed for thread_id=%s", body.thread_id)
        raise HTTPException(
            status_code=502, detail=f"sandbox provisioning failed: {type(exc).__name__}: {exc}"
        ) from None

    registry.set(body.thread_id, session)
    # user_login/target_branch used to live here too -- they're durable session data now
    # (session_store.py, written once below), not process-local bookkeeping. Only the one-shot
    # `resume` signal stays here, consumed exactly once by graph.py's intake_node.
    registry.set_meta(body.thread_id, resume=body.resume)
    # Retained agent-memory-only for this session's own work-branch pushes (git_ops.push_head).
    # Never passed into the container environment -- the clone credential is destroyed after
    # clone by design (entrypoint.sh), and pushes re-inject it one-shot per push.
    git_ops.set_push_token(body.thread_id, body.github_token)

    if existing is None:
        # Idempotent creation: a reattach (same session_id provisioned again, non-resume) is a
        # no-op inside create_session. The real title arrives later, once scaffold_node has
        # requirements text to generate one from (session_store.touch_run).
        await session_store.create_session(
            body.thread_id,
            owner=owner,
            repo=repo,
            user_login=body.user_login,
            source_branch=body.branch,
            work_branch=work_branch,
            title="(untitled session)",
            project_id=project_id,
        )

    return ProvisionResponse(status="ready")


class SessionResponse(BaseModel):
    """The single schema-aware representation of a session row -- the frontend never queries SQL
    directly (decision: avoid a second client/language independently knowing this shape), it only
    ever sees this model's JSON via the two GET routes below."""

    session_id: str
    owner: str
    repo: str
    user_login: str
    title: str
    source_branch: str
    work_branch: str
    run_id: str | None = None
    current_stage: str | None = None
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    merge_ready: bool | None = None
    pr_title: str | None = None
    pr_url: str | None = None
    failure_stage: str | None = None
    failure_type: str | None = None
    failure_message: str | None = None
    # Live, not persisted: whether this session's sandbox is CURRENTLY registered in this agent
    # process's memory right now (registry.py) -- independent of `status`, which only tracks the
    # workflow's own DB lifecycle. An agent restart drops this to false for every session until
    # each is reprovisioned, same as every other in-memory cache in this module.
    container_alive: bool = False
    # Part 3 Task 1 added this BIT column and the two write-side call sites that keep it current
    # (session_store.set_awaiting_gate / update_current_stage), but never declared it here -- so
    # every response silently dropped it via Pydantic v2's default `extra="ignore"` on
    # `SessionResponse(**row, ...)` below (confirmed in task-1-report.md's own "not a breaking
    # concern" note). The Board's (Task 9) pause marker reads this field directly, so it has to
    # actually leave this process now: True while `current_stage` is paused at its own human gate
    # awaiting approval, False/None otherwise.
    awaiting_gate: bool | None = None
    # Part 2 Ruling 8: the same silent-drop bug awaiting_gate had above -- `row` (session_store's
    # _COLUMNS) has carried project_id all along, Pydantic v2's `extra="ignore"` just never let it
    # through because nothing declared it here. A run-detail page needs its own project_id for
    # navigation (link back to the project/Board), which the per-session payload had no way to
    # supply until now.
    project_id: str | None = None


def _row_to_response(row: dict[str, Any]) -> "SessionResponse":
    return SessionResponse(**row, container_alive=registry.get(row["session_id"]) is not None)


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    owner: str,
    repo: str,
    source_branch: str | None = None,
    project_id: str | None = None,
) -> SessionListResponse:
    """Backs /select's session-list panel, the provision route's existence checks, and (Part 3)
    the project-scoped Board's `GET /sessions?owner=&repo=&project_id=` query -- the frontend
    calls this instead of reading `.ai-dev-workflow/sessions.json` off GitHub, since that file no
    longer exists."""
    _check_shared_secret(request)
    rows = await session_store.list_sessions(owner, repo, source_branch, project_id)
    return SessionListResponse(sessions=[_row_to_response(row) for row in rows])


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session_row(session_id: str, request: Request) -> SessionResponse:
    """Backs the workflow page's ownership check and the raw-proxy/report routes' work_branch
    lookup -- both need to resolve a session's stored facts without recomputing anything."""
    _check_shared_secret(request)
    row = await session_store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _row_to_response(row)


@router.delete("/{thread_id}")
async def terminate_session(thread_id: str, request: Request) -> ProvisionResponse:
    """Container-only teardown (WorkspaceHeader's "Stop container" button): the sandbox and its
    workspace volume are discarded, but the session's history row and its GitHub work branch are
    untouched -- resuming later just provisions a fresh sandbox onto the same branch. For "delete
    this session entirely," see delete_session_full below."""
    _check_shared_secret(request)
    provider = get_sandbox_provider()
    await provider.terminate(thread_id)
    # Explicit close discards the persistent workspace too (idle reaps deliberately keep it).
    await provider.discard_workspace(thread_id)
    registry.pop(thread_id)
    keyvault.pop_app_secrets(thread_id)
    return ProvisionResponse(status="terminated")


class DeleteSessionRequest(BaseModel):
    # The CURRENT caller's live GitHub token, forwarded fresh per request -- git_ops._PUSH_TOKENS
    # is agent-restart-fragile by design and would silently no-op on exactly the old sessions a
    # user is most likely to be purging. Optional: an empty/absent token just skips the remote
    # branch delete (the session row is still removed -- this app's own bookkeeping shouldn't be
    # held hostage by a GitHub-side failure).
    github_token: str = ""


class DeleteSessionResponse(BaseModel):
    status: str
    branch_deleted: bool


@router.post("/{thread_id}/delete", response_model=DeleteSessionResponse)
async def delete_session_full(thread_id: str, body: DeleteSessionRequest, request: Request) -> DeleteSessionResponse:
    """Full purge (SessionHistory's "Delete" button): stop the container if one is running,
    discard its workspace, delete the session's own GitHub work branch, and remove the history
    row so it no longer shows up in the list at all. Session-scoped teardown only -- never called
    for a session still in progress from the UI, but idempotent either way (every step here
    already tolerates an already-gone container/branch/row)."""
    _check_shared_secret(request)
    row = await session_store.get_session(thread_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")

    provider = get_sandbox_provider()
    await provider.terminate(thread_id)
    await provider.discard_workspace(thread_id)
    registry.pop(thread_id)
    keyvault.pop_app_secrets(thread_id)

    branch_deleted = False
    if body.github_token:
        branch_deleted = await git_ops.delete_remote_branch(
            owner=row["owner"], repo=row["repo"], branch=row["work_branch"], token=body.github_token
        )

    await session_store.delete_session(thread_id)
    return DeleteSessionResponse(status="deleted", branch_deleted=branch_deleted)


class SessionActionRequest(BaseModel):
    """Named actions only -- the frontend never sends shell. Adding an action = a new Literal
    member plus a handler branch below; anything else is rejected by validation before it runs."""

    action: Literal["refresh-secrets"]
    entra_assertion: str = ""


class SessionActionResponse(BaseModel):
    ok: bool = True
    secret_count: int


@router.post("/{thread_id}/actions", response_model=SessionActionResponse)
async def run_session_action(thread_id: str, body: SessionActionRequest, request: Request) -> SessionActionResponse:
    """On-demand, frontend-initiated work against a live session. v1: "refresh-secrets" -- the
    user added/rotated a vault secret mid-session and wants it picked up without starting over.
    Re-fetches on-behalf-of the user with the fresh assertion the frontend just minted, updates
    the in-process cache (also the recovery path when an agent restart dropped it), and re-writes
    the env file inside the sandbox if one is running."""
    _check_shared_secret(request)
    row = await session_store.get_session(thread_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Only "refresh-secrets" exists; a second action turns this into a match on body.action.
    vault_uri = await keyvault.get_vault_uri(row["owner"], row["repo"], row["user_login"])
    if not vault_uri:
        raise HTTPException(status_code=404, detail="no key vault is configured for this repo")
    if not body.entra_assertion:
        raise HTTPException(status_code=401, detail="no Entra assertion -- sign in again")
    try:
        app_secrets = await keyvault.fetch_app_secrets(vault_uri, body.entra_assertion)
    except keyvault.VaultAccessError as exc:
        raise HTTPException(status_code=403, detail=f"key vault {vault_uri} is not readable as you: {exc}") from None

    keyvault.set_app_secrets(thread_id, app_secrets)
    if registry.get(thread_id) is not None:
        await keyvault.write_env_file(get_sandbox_provider(), thread_id, app_secrets)
    logger.info("refreshed %d app secret(s) for thread_id=%s", len(app_secrets), thread_id)
    return SessionActionResponse(secret_count=len(app_secrets))


# --- per user-repo vault configuration (settings page) ----------------------------------------

_VAULT_URI_RE = re.compile(r"^https://[a-z0-9][a-z0-9-]{1,22}[a-z0-9]\.vault\.azure\.net/?$")


class VaultConfigResponse(BaseModel):
    vault_uri: str


@config_router.get("", response_model=VaultConfigResponse)
async def get_vault_config(request: Request, owner: str, repo: str, user_login: str) -> VaultConfigResponse:
    _check_shared_secret(request)
    vault_uri = await keyvault.get_vault_uri(owner, repo, user_login)
    if not vault_uri:
        raise HTTPException(status_code=404, detail="no key vault configured")
    return VaultConfigResponse(vault_uri=vault_uri)


class VaultConfigPutRequest(BaseModel):
    owner: str
    repo: str
    user_login: str
    vault_uri: str
    entra_assertion: str


@config_router.put("", response_model=SessionActionResponse)
async def put_vault_config(body: VaultConfigPutRequest, request: Request) -> SessionActionResponse:
    """Save the mapping -- but only after proving it works: a test-read on-behalf-of the caller.
    The 403 detail (AADSTS code and all) goes back verbatim so the settings page can show the
    user exactly which grant is missing. Nothing is saved on a failed test."""
    _check_shared_secret(request)
    vault_uri = body.vault_uri.strip()
    if not _VAULT_URI_RE.match(vault_uri):
        raise HTTPException(status_code=422, detail="vault_uri must look like https://<name>.vault.azure.net/")
    try:
        app_secrets = await keyvault.fetch_app_secrets(vault_uri, body.entra_assertion)
    except keyvault.VaultAccessError as exc:
        raise HTTPException(status_code=403, detail=f"key vault {vault_uri} is not readable as you: {exc}") from None
    await keyvault.set_vault_uri(body.owner, body.repo, body.user_login, vault_uri)
    return SessionActionResponse(secret_count=len(app_secrets))


# --- org-wide coding-agent provider settings (settings page, Part 4) --------------------------


class OrgSettingsResponse(BaseModel):
    """Never carries the credential value itself -- write-only once saved (Part 4 Spec's own
    explicit gap resolution), matching VaultConfigResponse's own convention above. The only signal
    the Settings UI gets about the credential is `credential_configured`."""

    provider: str
    credential_configured: bool
    session_ready: bool
    updated_at: datetime | None
    updated_by: str | None


async def _org_settings_response() -> OrgSettingsResponse:
    """Shared GET/PUT response builder -- always a fresh, uncached DB read. The TTL cache lives
    one layer up (chat_model.get_provider(), _PROVIDER_CACHE_TTL_SECONDS) for in-flight session
    dispatch, where up to 30s of staleness is fine; this settings-management surface must show a
    just-saved change back immediately, so it reads org_settings directly rather than going
    through that cache.

    session_ready (whole-branch review Important finding): whether a session provisioned right
    now would actually get a usable credential, computed by calling the SAME function real
    provisioning calls (chat_model.get_runtime_auth_token()) rather than re-deriving that logic
    here a second time. This is deliberately a DIFFERENT signal from credential_configured -- a
    fresh deployment with no vault credential saved but a valid ANTHROPIC_API_KEY/GITHUB_TOKEN env
    var is credential_configured=False but session_ready=True, and the settings-checks.ts banner
    (Task 8) should key off THIS field, not credential_configured, to avoid a permanent false
    alarm on every env-var-only deployment. A raised exception here (e.g. a configured vault
    credential whose vault is currently unreachable) means sessions genuinely cannot run right
    now -- fails closed to False, the same posture as this module's other credential checks.
    """
    settings = await org_settings.get_org_settings()
    try:
        session_ready = bool(await chat_model.get_runtime_auth_token())
    except Exception:
        logger.warning("get_runtime_auth_token() failed while building the org-settings response", exc_info=True)
        session_ready = False

    if settings is None:
        # Fresh deployment, nobody has saved a setting yet -- the exact same env-var fallback
        # chat_model.get_provider() itself falls back to, so this page's "active provider" can
        # never disagree with what a real session would actually run under.
        return OrgSettingsResponse(
            provider=os.environ.get("AGENT_PROVIDER", "copilot"),
            credential_configured=False,
            session_ready=session_ready,
            updated_at=None,
            updated_by=None,
        )
    return OrgSettingsResponse(
        provider=settings.provider,
        credential_configured=settings.credential_secret_name is not None,
        session_ready=session_ready,
        updated_at=settings.updated_at,
        updated_by=settings.updated_by,
    )


@org_settings_router.get("", response_model=OrgSettingsResponse)
async def get_org_settings_endpoint(request: Request) -> OrgSettingsResponse:
    _check_shared_secret(request)
    return await _org_settings_response()


class OrgSettingsPutRequest(BaseModel):
    provider: Literal["copilot", "claude"]
    # None/omitted means "keep whatever's already saved" (the masked-dots-plus-Update-button UI
    # pattern) -- only a non-None value here triggers the save-and-test-fetch below.
    credential: str | None = None
    # Mirrors VaultConfigPutRequest's own user_login above: who to attribute this save to. Not
    # derivable from anything this agent process itself knows -- there is no end-user session at
    # this layer, only the shared-secret check that authenticates "the Next.js server", not "which
    # admin clicked save" -- so the frontend BFF route (Part 4 Task 7) must supply it, the same way
    # it already supplies user_login for the per-repo vault-config PUT above.
    updated_by: str


# The two real, lightweight, already-authenticated checks this task found: each is the SAME class
# of "does the identity service accept this credential at all" probe, not a full completion/CLI
# turn. Anthropic's Models list needs a valid x-api-key and cannot run for a session anyway;
# GitHub's authenticated-user lookup is this codebase's own established way to prove a PAT is live
# (git_ops.py already calls api.github.com with an identical Bearer header for PR/branch
# operations). Neither proves the deeper, provider-specific entitlement (a syntactically valid
# Anthropic key this org still can't call a model with; a GitHub PAT valid but lacking an actual
# Copilot seat) -- proving that needs a real sandboxed CLI turn, which is exactly the latency this
# endpoint must not wait on (see _probe_provider_credential's own docstring).
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_API_VERSION = "2023-06-01"
_GITHUB_USER_URL = "https://api.github.com/user"


async def _probe_provider_credential(provider: str, value: str) -> None:
    """Ruling 3's actual validation gate: one lightweight GET against the TARGET PROVIDER's own
    API -- deliberately not just a Key Vault round trip, which would only prove our own vault
    plumbing works, never that the admin didn't just paste a typo'd or already-revoked key. This
    is the smallest real check available without a full sandboxed CLI turn -- see the module
    constants just above for why these two specific endpoints were chosen. Raises HTTPException
    with the provider's own real response on any failure; the credential value itself never
    appears in the raised detail, only the provider's own response body/status.
    """
    if provider == "claude":
        url = _ANTHROPIC_MODELS_URL
        headers = {"x-api-key": value, "anthropic-version": _ANTHROPIC_API_VERSION}
    else:
        url = _GITHUB_USER_URL
        headers = {
            "Authorization": f"Bearer {value}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"could not reach {provider}'s API to validate the credential: {exc}"
            ) from None

    if resp.status_code != 200:
        raise HTTPException(
            status_code=403,
            detail=f"{provider} rejected the credential: {resp.status_code} {resp.text[:300]}",
        )


@org_settings_router.put("", response_model=OrgSettingsResponse)
async def put_org_settings_endpoint(body: OrgSettingsPutRequest, request: Request) -> OrgSettingsResponse:
    """Save the org-wide active provider + credential -- but, per Ruling 3, only after proving a
    NEWLY provided credential actually works. Fix round 1 correctness note: the probe MUST run
    against the raw candidate BEFORE it ever touches the vault, never after writing it.
    org_credential_vault.py's secret name is one fixed, shared slot -- get_runtime_auth_token()
    (every real session's own credential fetch) always reads whatever is CURRENTLY in that slot,
    with no version pinning. Writing the candidate first and validating second would mean a
    REJECTED save still overwrites the live, previously-working credential for the window between
    this request and the next successful save -- wrong at the credential-value layer even though
    the org_settings DB row (checked at the previous line) stays untouched and correctly implies
    nothing changed. So: probe first; only a passing candidate ever reaches
    org_credential_vault.set_org_credential. No separate read-back-from-vault check afterward --
    once the raw value has already passed the real provider's own probe, a subsequent write
    succeeding or not is a vault-plumbing question (set_org_credential's own VaultAccessError
    already covers that), not a credential-validity one.

    A `credential` of None carries the existing credential_secret_name forward untouched and
    unrevalidated -- but only when `provider` is not actually changing. The vault slot is
    provider-agnostic (one fixed name for either provider's credential), so carrying last time's
    secret into a genuinely DIFFERENT provider would silently mark the new, wrong-typed credential
    as `credential_configured: true` with nothing having ever proven it works for that provider --
    rejected outright below instead.
    """
    _check_shared_secret(request)
    existing = await org_settings.get_org_settings()
    secret_name = existing.credential_secret_name if existing is not None else None

    if body.credential is None:
        if existing is not None and secret_name is not None and body.provider != existing.provider:
            raise HTTPException(
                status_code=422,
                detail=(
                    "switching provider requires a new credential -- the currently saved "
                    "credential belongs to the current provider and cannot carry over"
                ),
            )
    else:
        credential = body.credential.strip()
        await _probe_provider_credential(body.provider, credential)
        try:
            secret_name = await org_credential_vault.set_org_credential(credential)
        except keyvault.VaultAccessError as exc:
            raise HTTPException(status_code=502, detail=f"org credential vault is not accessible: {exc}") from None
        logger.info("org credential saved and validated for provider=%s", body.provider)

    await org_settings.set_org_settings(body.provider, secret_name, body.updated_by)
    chat_model.invalidate_provider_cache()
    return await _org_settings_response()


class TechStackCatalogResponse(BaseModel):
    stacks: list[dict[str, Any]]


@catalog_router.get("/tech-stack-catalog", response_model=TechStackCatalogResponse)
async def get_tech_stack_catalog(request: Request) -> TechStackCatalogResponse:
    """The 8 canned monorepo stacks the Tech Stack tab's dropdown offers -- static, session-
    independent data (app_discovery.load_stack_catalog is @lru_cache'd), so this is its own tiny
    router rather than living under /sessions or /vault-config."""
    _check_shared_secret(request)
    return TechStackCatalogResponse(stacks=app_discovery.load_stack_catalog())


# --- projects (Part 3: tickets/board -- docs/superpowers/plans/part-3-tickets-tasks.md) --------


class ProjectResponse(BaseModel):
    """The single schema-aware representation of a project row -- mirrors SessionResponse's own
    convention above (the frontend never queries SQL directly). owner/repo/tech_stack_id/
    tech_stack_text are all nullable: a "+ New Project" row starts with owner/repo NULL (Ruling 2)
    until scaffolding backfills them; a Connect-Repository row starts with tech_stack_id/
    tech_stack_text NULL until brownfield detection or a later Tech Stack confirmation runs.
    default_branch (Task 5) is NULL for the same "not connected/scaffolded yet" reason, and also
    stays NULL forever for a scaffolded (never connected) repo -- callers fall back to "main"."""

    project_id: str
    name: str
    owner: str | None
    repo: str | None
    tech_stack_id: str | None
    tech_stack_text: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    default_branch: str | None


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]


@projects_router.get("", response_model=ProjectListResponse)
async def list_projects_route(request: Request) -> ProjectListResponse:
    """Backs the New Ticket form's project picker (GET /projects)."""
    _check_shared_secret(request)
    rows = await project_store.list_projects()
    return ProjectListResponse(projects=[ProjectResponse(**row) for row in rows])


class CreateProjectRequest(BaseModel):
    name: str
    tech_stack_id: str | None = None
    tech_stack_text: str | None = None
    created_by: str


@projects_router.post("", response_model=ProjectResponse)
async def create_project_route(body: CreateProjectRequest, request: Request) -> ProjectResponse:
    """The "+ New Project" inline-fields case (New Ticket form): creates a project row with
    owner/repo still NULL -- provision_session scaffolds the actual GitHub repo later, the first
    time a ticket is filed against this project (see provision_session above)."""
    _check_shared_secret(request)
    project_id = await project_store.create_project(
        body.name,
        tech_stack_id=body.tech_stack_id,
        tech_stack_text=body.tech_stack_text,
        created_by=body.created_by,
    )
    row = await project_store.get_project(project_id)
    return ProjectResponse(**row)


class ConnectProjectRequest(BaseModel):
    owner: str
    repo: str
    created_by: str
    # The user's live GitHub token (BFF-forwarded from the session, same as ProvisionRequest's own
    # github_token) -- needed below to look up the repo's real default branch before writing/
    # refreshing this project row.
    github_token: str


async def _fetch_default_branch(
    owner: str, repo: str, token: str, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """GitHub's own source of truth for a repo's default branch (GET /repos/{owner}/{repo} ->
    response body's `default_branch` field) -- same Bearer/Accept/API-Version headers git_ops.py's
    open_pull_request/delete_remote_branch already send. Unlike those best-effort calls (side
    effects after a session already succeeded), this one runs before any DB write, so a lookup
    that fails is a hard stop (HTTPException), not a silently-swallowed None.

    `client` is test-only dependency injection (see this module's own _demo), same convention as
    repo_scaffold.create_repo's own `client` param -- every real call site omits it.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.get(url, headers=headers)
        else:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"could not reach GitHub to look up {owner}/{repo}: {exc}"
        ) from None

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub rejected the repo lookup for {owner}/{repo}: {resp.status_code} {resp.text[:300]}",
        )
    return resp.json().get("default_branch")


def _needs_default_branch_fetch(existing: dict[str, Any] | None) -> bool:
    """Whether connect_project_route has a real reason to hit GitHub for this repo's default
    branch: a genuinely new connect (no existing row), or an existing row whose default_branch is
    still NULL (a pre-migration row, or a previously-failed fetch). False for an already-connected
    row that already has one -- the actual idempotent path (review fix): GitHub must never be hit
    on that path, since /select's "start new session"/"resume" actions now call this route on
    every session start and don't even use the returned value themselves. Pulled out as its own
    pure predicate so this exact rule -- the one the review's Important finding was about -- has a
    direct, DB-free assertion in this module's own self-check, not just an inline `if`."""
    return existing is None or not existing.get("default_branch")


@projects_router.post("/connect", response_model=ProjectResponse)
async def connect_project_route(body: ConnectProjectRequest, request: Request) -> ProjectResponse:
    """The Connect-Repository action. Idempotent: connecting an already-connected (owner, repo)
    returns the existing project rather than erroring or duplicating (matches provision_session's
    own idempotent-creation convention for dbo.sessions) -- UX_projects_owner_repo guarantees at
    most one row can ever match a given pair, so find_project_by_repo is authoritative. Otherwise
    creates a new row with owner/repo already set; tech_stack_id/tech_stack_text stay NULL (not
    this route's job to guess brownfield tech stack). No separate "project name" field exists for
    this flow (the wireframe has none) -- `repo` doubles as the project's display name, same as
    this codebase's own pre-Part-3 assumption that repo == project.

    default_branch (Task 5) is only ever fetched from GitHub when there's a real reason to: a
    genuinely new connect, or an existing row whose default_branch is still NULL (a pre-migration
    row, or a previously-failed fetch) -- self-healing, never on an already-connected row that
    already has one. That matters because /select's "start new session"/"resume" actions now call
    this route on every session start (Task 5), not just an explicit Connect click, and don't even
    use the returned default_branch themselves (they already have the user's own explicitly-picked
    branch) -- unconditionally re-hitting GitHub on that hot path would mean a transient GitHub
    hiccup (rate limit, brief outage, timeout) could newly block starting a session on an
    already-connected, previously-working repo, a capability that worked unconditionally before
    this feature existed.

    The fetch also runs BEFORE create_project, not after (review fix round 2): for a genuinely new
    connect, a DB row must never exist before its GitHub lookup has actually succeeded. Creating
    the row first and fetching second would leave an orphaned, unreachable project (owner/repo
    still NULL) on a fetch failure -- find_project_by_repo filters on the real (owner, repo), which
    an orphan doesn't have, so it could never be found or completed again, every retry would create
    ANOTHER orphan, and it would still surface, selectable, in GET /projects (the New Ticket form's
    picker) -- picking it would scaffold a brand-new GitHub repo instead of the one actually being
    connected. The already-connected short-circuit above has no such risk either way (no new row
    is ever created on that path), so this ordering only matters for the branch below."""
    _check_shared_secret(request)
    existing = await project_store.find_project_by_repo(body.owner, body.repo)
    if existing is not None and not _needs_default_branch_fetch(existing):
        return ProjectResponse(**existing)

    default_branch = await _fetch_default_branch(body.owner, body.repo, body.github_token)
    project_id = (
        existing["project_id"]
        if existing is not None
        else await project_store.create_project(
            body.repo, tech_stack_id=None, tech_stack_text=None, created_by=body.created_by
        )
    )
    try:
        await project_store.set_project_repo(project_id, body.owner, body.repo, default_branch)
    except pyodbc.IntegrityError:
        # Task 10 sweep item #12 -- a pre-existing, narrow TOCTOU race (confirmed to actually
        # occur, not just theoretical: task-10-report.md reproduces it against the real DB). Two
        # concurrent FIRST-TIME connects to the exact same never-before-seen (owner, repo) can both
        # pass the `existing is None` check above and both reach this UPDATE -- the loser collides
        # on UX_projects_owner_repo's own unique index. The DB-level safety property this index
        # exists for always held (never two rows survive), but without this catch the loser's own
        # request surfaced as a raw, unhandled 500 instead of resolving to the same project the
        # winner got. A fresh find_project_by_repo lookup now finds the winner's just-committed
        # row; if it somehow still can't (a different integrity error entirely), re-raise rather
        # than fabricate a false idempotency win.
        winner = await project_store.find_project_by_repo(body.owner, body.repo)
        if winner is None:
            raise
        return ProjectResponse(**winner)
    row = await project_store.get_project(project_id)
    return ProjectResponse(**row)


def _demo() -> None:
    """`cd agent && uv run python -m src.sessions_api`. No live network call -- httpx.MockTransport
    stands in for api.github.com (same technique repo_scaffold.py's own self-check uses), covering
    the two genuinely new bits of pure logic this file adds: _needs_default_branch_fetch's
    skip-when-already-connected rule (the review's own Important finding), and
    _fetch_default_branch's success/failure parsing. connect_project_route's own DB-touching logic
    is exercised by project_store.py's own self-check instead (its default_branch round-trip
    assertions through set_project_repo/get_project/find_project_by_repo) -- no second fake DB
    pool is wired up here."""
    import asyncio

    # Task 9 (Board pause marker): a plain dict-in, model-out check that awaiting_gate survives
    # SessionResponse(**row, ...) -- pinned here because it silently did NOT before this task
    # (Pydantic v2's default extra="ignore" dropped it, per task-1-report.md's own "not a breaking
    # concern... yet" note). Part 2 Ruling 8 found project_id had the exact same silent-drop bug
    # (see SessionResponse's own comment above) -- a run-detail page needs its own project_id for
    # navigation, so this now pins the opposite of what it used to: the field IS declared and DOES
    # survive, not that it's correctly absent.
    fake_row = {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "owner": "octocat", "repo": "demo", "user_login": "octocat", "title": "t",
        "source_branch": "main", "work_branch": "wb", "run_id": None,
        "current_stage": "plan", "status": "in_progress",
        "started_at": datetime(2026, 1, 1), "ended_at": None,
        "merge_ready": None, "pr_title": None, "pr_url": None,
        "failure_stage": None, "failure_type": None, "failure_message": None,
        "project_id": "22222222-2222-2222-2222-222222222222", "awaiting_gate": True,
    }
    resp = _row_to_response(fake_row)
    assert resp.awaiting_gate is True, resp
    assert resp.project_id == "22222222-2222-2222-2222-222222222222", resp

    global _fetch_default_branch  # reassigned further down; must precede every use in this function

    # A brand-new connect (no row yet) and a pre-migration/previously-failed row (NULL or empty
    # default_branch) both need the GitHub call; an already-connected row that already has a real
    # default_branch must not -- that's the exact case a transient GitHub hiccup must never block.
    assert _needs_default_branch_fetch(None) is True
    assert _needs_default_branch_fetch({"default_branch": None}) is True
    assert _needs_default_branch_fetch({"default_branch": ""}) is True
    assert _needs_default_branch_fetch({"default_branch": "develop"}) is False

    def handle_ok(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/repos/octocat/hello-world", str(request.url)
        assert request.headers.get("authorization") == "Bearer tok123", dict(request.headers)
        assert request.headers.get("x-github-api-version") == "2022-11-28", dict(request.headers)
        return httpx.Response(200, json={"default_branch": "develop", "name": "hello-world"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_ok))
    branch = asyncio.run(_fetch_default_branch("octocat", "hello-world", "tok123", client=mock_client))
    asyncio.run(mock_client.aclose())
    assert branch == "develop", branch

    def handle_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    mock_client_404 = httpx.AsyncClient(transport=httpx.MockTransport(handle_404))
    try:
        asyncio.run(_fetch_default_branch("octocat", "missing-repo", "tok123", client=mock_client_404))
        raise AssertionError("_fetch_default_branch must raise on a non-200 response")
    except HTTPException as exc:
        assert exc.status_code == 502 and "404" in exc.detail, exc.detail
    finally:
        asyncio.run(mock_client_404.aclose())

    # Review fix round 2: for a genuinely new connect (existing is None), create_project must
    # never run before a successful fetch -- creating the row first would orphan it forever on a
    # fetch failure (owner/repo stay NULL, so find_project_by_repo can never find it again to
    # finish the job; every retry would create ANOTHER orphan, and the orphan would still surface,
    # selectable, in GET /projects). Monkeypatches project_store.find_project_by_repo/create_project
    # (module attributes, same technique project_store.py's own self-check uses for
    # session_store._get_pool) and this module's own _fetch_default_branch (a plain global
    # reassignment -- connect_project_route resolves that name from this module's globals at call
    # time, so this affects it exactly like a real patch would) to run the REAL route function
    # against a simulated GitHub failure, and asserts create_project was never even called.
    calls: list[str] = []

    async def fake_find_project_by_repo(owner: str, repo: str) -> dict[str, Any] | None:
        return None  # "existing is None" -- a genuinely new connect, nothing to short-circuit on

    async def fake_create_project(*args: Any, **kwargs: Any) -> str:
        calls.append("create_project")
        return "should-never-be-created"

    async def fake_fetch_that_fails(*args: Any, **kwargs: Any) -> str | None:
        calls.append("fetch_default_branch")
        raise HTTPException(status_code=502, detail="simulated GitHub outage")

    class _FakeRequest:
        headers: dict[str, str] = {}

    original_find = project_store.find_project_by_repo
    original_create = project_store.create_project
    original_fetch = _fetch_default_branch
    project_store.find_project_by_repo = fake_find_project_by_repo  # type: ignore[assignment]
    project_store.create_project = fake_create_project  # type: ignore[assignment]
    _fetch_default_branch = fake_fetch_that_fails
    try:
        body = ConnectProjectRequest(owner="octocat", repo="new-repo", created_by="octocat", github_token="tok123")
        try:
            asyncio.run(connect_project_route(body, _FakeRequest()))  # type: ignore[arg-type]
            raise AssertionError("connect_project_route must propagate a fetch failure, not swallow it")
        except HTTPException:
            pass
    finally:
        project_store.find_project_by_repo = original_find  # type: ignore[assignment]
        project_store.create_project = original_create  # type: ignore[assignment]
        _fetch_default_branch = original_fetch

    assert calls == ["fetch_default_branch"], (
        f"create_project must never run before a successful fetch -- orphan-row bug reintroduced, calls={calls}"
    )

    # Task 10 sweep item #12: the TOCTOU race between the `existing is None` check and this same
    # route's own set_project_repo UPDATE, reproduced empirically against the real DB in
    # task-10-report.md (two concurrent first-time connects to the same never-seen (owner, repo)
    # both passed `existing is None`, and the loser's set_project_repo hit
    # UX_projects_owner_repo's real unique-index violation as a raw, unhandled pyodbc.
    # IntegrityError). This pins the fix offline: set_project_repo raises that same exception type
    # once (simulating "the other concurrent caller already won"), and a second
    # find_project_by_repo call (the retry lookup) now finds the winner's row -- the route must
    # return THAT project gracefully, not propagate the exception.
    winner_row = {"project_id": "33333333-3333-3333-3333-333333333333", "name": "racey-repo",
                  "owner": "octocat", "repo": "racey-repo", "tech_stack_id": None, "tech_stack_text": None,
                  "created_by": "someone-else", "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1),
                  "default_branch": "main"}
    find_calls = 0

    async def fake_find_first_none_then_winner(owner: str, repo: str) -> dict[str, Any] | None:
        nonlocal find_calls
        find_calls += 1
        return None if find_calls == 1 else winner_row

    async def fake_create_project_ok(*args: Any, **kwargs: Any) -> str:
        return "loser-project-id-never-returned"

    async def fake_fetch_ok(*args: Any, **kwargs: Any) -> str | None:
        return "main"

    async def fake_set_project_repo_loses_race(*args: Any, **kwargs: Any) -> None:
        raise pyodbc.IntegrityError(
            "23000", "Cannot insert duplicate key row ... 'UX_projects_owner_repo' ... (2601)"
        )

    original_find2 = project_store.find_project_by_repo
    original_create2 = project_store.create_project
    original_set_repo = project_store.set_project_repo
    original_fetch2 = _fetch_default_branch
    project_store.find_project_by_repo = fake_find_first_none_then_winner  # type: ignore[assignment]
    project_store.create_project = fake_create_project_ok  # type: ignore[assignment]
    project_store.set_project_repo = fake_set_project_repo_loses_race  # type: ignore[assignment]
    _fetch_default_branch = fake_fetch_ok
    try:
        body = ConnectProjectRequest(owner="octocat", repo="racey-repo", created_by="octocat", github_token="tok123")
        result = asyncio.run(connect_project_route(body, _FakeRequest()))  # type: ignore[arg-type]
        assert result.project_id == winner_row["project_id"], (
            f"a lost race must resolve to the WINNER's project_id, got {result.project_id!r}"
        )
    finally:
        project_store.find_project_by_repo = original_find2  # type: ignore[assignment]
        project_store.create_project = original_create2  # type: ignore[assignment]
        project_store.set_project_repo = original_set_repo  # type: ignore[assignment]
        _fetch_default_branch = original_fetch2
    assert find_calls == 2, f"expected exactly one retry lookup after losing the race, find_calls={find_calls}"

    print("sessions_api self-check: all assertions passed")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.sessions_api
    from src.sessions_api import _demo as _packaged_demo  # re-dispatch via package name, see project_store.py

    _packaged_demo()
