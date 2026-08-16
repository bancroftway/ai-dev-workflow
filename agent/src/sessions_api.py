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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import git_ops
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
    # own session carried, forwarded here purely so session_index.py can label sessions.json rows
    # for the /select history UI.
    user_login: str = ""
    # True on a `?resume=1` re-entry into a live thread (graph.py's intake_node consumes this
    # exactly once via sandbox.registry.pop_meta_flag).
    resume: bool = False


class ProvisionResponse(BaseModel):
    status: str


@router.post("/provision", response_model=ProvisionResponse)
async def provision_session(body: ProvisionRequest, request: Request) -> ProvisionResponse:
    _check_shared_secret(request)
    provider = get_sandbox_provider()
    repo_clone_url = f"https://github.com/{body.owner}/{body.repo}.git"
    try:
        session = await provider.provision(
            session_id=body.thread_id,
            repo_clone_url=repo_clone_url,
            branch=body.branch,
            git_user_token=body.github_token,
            copilot_auth_token=os.environ.get("GITHUB_TOKEN", ""),
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced to the caller as a plain 502, not swallowed
        logger.exception("sandbox provisioning failed for thread_id=%s", body.thread_id)
        raise HTTPException(
            status_code=502, detail=f"sandbox provisioning failed: {type(exc).__name__}: {exc}"
        ) from None

    registry.set(body.thread_id, session)
    registry.set_meta(body.thread_id, user_login=body.user_login, target_branch=body.branch, resume=body.resume)
    # Retained agent-memory-only for stage-end pushes to the ai-dev-workflow/<branch> work branch
    # (git_ops.push_head). Never passed into the container environment -- the clone credential is
    # destroyed after clone by design (entrypoint.sh), and pushes re-inject it one-shot per push.
    git_ops.set_push_token(body.thread_id, body.github_token)
    return ProvisionResponse(status="ready")


@router.delete("/{thread_id}")
async def terminate_session(thread_id: str, request: Request) -> ProvisionResponse:
    _check_shared_secret(request)
    provider = get_sandbox_provider()
    await provider.terminate(thread_id)
    # Explicit close discards the persistent workspace too (idle reaps deliberately keep it).
    await provider.discard_workspace(thread_id)
    registry.pop(thread_id)
    return ProvisionResponse(status="terminated")
