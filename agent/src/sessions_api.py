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
from pydantic import BaseModel

from . import app_discovery, branch_naming, chat_model, git_ops, keyvault, session_store
from .sandbox import get_sandbox_provider, registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])
config_router = APIRouter(prefix="/vault-config", tags=["vault-config"])
catalog_router = APIRouter(tags=["tech-stack"])

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

    work_branch = branch_naming.work_branch_for(body.thread_id)
    provider = get_sandbox_provider()
    repo_clone_url = f"https://github.com/{body.owner}/{body.repo}.git"
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
            owner=body.owner,
            repo=body.repo,
            user_login=body.user_login,
            source_branch=body.branch,
            work_branch=work_branch,
            title="(untitled session)",
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


def _row_to_response(row: dict[str, Any]) -> "SessionResponse":
    return SessionResponse(**row, container_alive=registry.get(row["session_id"]) is not None)


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    request: Request, owner: str, repo: str, source_branch: str | None = None
) -> SessionListResponse:
    """Backs /select's session-list panel and the provision route's existence checks -- the
    frontend calls this instead of reading `.ai-dev-workflow/sessions.json` off GitHub, since
    that file no longer exists."""
    _check_shared_secret(request)
    rows = await session_store.list_sessions(owner, repo, source_branch)
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
    await registry.pop(thread_id)
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
    await registry.pop(thread_id)
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


class TechStackCatalogResponse(BaseModel):
    stacks: list[dict[str, Any]]


@catalog_router.get("/tech-stack-catalog", response_model=TechStackCatalogResponse)
async def get_tech_stack_catalog(request: Request) -> TechStackCatalogResponse:
    """The 8 canned monorepo stacks the Tech Stack tab's dropdown offers -- static, session-
    independent data (app_discovery.load_stack_catalog is @lru_cache'd), so this is its own tiny
    router rather than living under /sessions or /vault-config."""
    _check_shared_secret(request)
    return TechStackCatalogResponse(stacks=app_discovery.load_stack_catalog())
