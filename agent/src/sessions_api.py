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
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import branch_naming, git_ops, session_store
from .sandbox import get_sandbox_provider, registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

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

    work_branch = branch_naming.work_branch_for(body.thread_id)
    provider = get_sandbox_provider()
    repo_clone_url = f"https://github.com/{body.owner}/{body.repo}.git"
    try:
        session = await provider.provision(
            session_id=body.thread_id,
            repo_clone_url=repo_clone_url,
            branch=body.branch,
            work_branch=work_branch,
            git_user_token=body.github_token,
            copilot_auth_token=os.environ.get("GITHUB_TOKEN", ""),
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
    return SessionListResponse(sessions=[SessionResponse(**row) for row in rows])


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session_row(session_id: str, request: Request) -> SessionResponse:
    """Backs the workflow page's ownership check and the raw-proxy/report routes' work_branch
    lookup -- both need to resolve a session's stored facts without recomputing anything."""
    _check_shared_secret(request)
    row = await session_store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(**row)


@router.delete("/{thread_id}")
async def terminate_session(thread_id: str, request: Request) -> ProvisionResponse:
    _check_shared_secret(request)
    provider = get_sandbox_provider()
    await provider.terminate(thread_id)
    # Explicit close discards the persistent workspace too (idle reaps deliberately keep it).
    await provider.discard_workspace(thread_id)
    registry.pop(thread_id)
    return ProvisionResponse(status="terminated")
