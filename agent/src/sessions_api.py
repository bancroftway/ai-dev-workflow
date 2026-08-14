"""Sandbox session provisioning/teardown endpoints (architecture plan Section C.4).

Called by a Next.js SERVER action/route -- never the browser directly -- when a user opens or
closes a repo/branch session (plan Section A). Mounted onto the main FastAPI app in main.py.

Known gap, flagged rather than glossed over: plan Section C.4/Finding #4 (adversarial audit)
require these endpoints to check caller identity, not just network placement, since an untrusted
sandbox's postCreateCommand could otherwise reach them directly. That hardening depends on
Section A's auth plumbing (the caller needs a real identity to check), which hasn't been built
yet -- these routes are unauthenticated for now, appropriate only for local development.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import git_ops
from .sandbox import get_sandbox_provider, registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


class ProvisionRequest(BaseModel):
    thread_id: str
    owner: str
    repo: str
    branch: str
    github_token: str = ""


class ProvisionResponse(BaseModel):
    status: str


@router.post("/provision", response_model=ProvisionResponse)
async def provision_session(body: ProvisionRequest) -> ProvisionResponse:
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
    # Retained agent-memory-only for stage-end pushes to the ai-dev-workflow/<branch> work branch
    # (git_ops.push_head). Never passed into the container environment -- the clone credential is
    # destroyed after clone by design (entrypoint.sh), and pushes re-inject it one-shot per push.
    git_ops.set_push_token(body.thread_id, body.github_token)
    return ProvisionResponse(status="ready")


@router.delete("/{thread_id}")
async def terminate_session(thread_id: str) -> ProvisionResponse:
    provider = get_sandbox_provider()
    await provider.terminate(thread_id)
    registry.pop(thread_id)
    return ProvisionResponse(status="terminated")
