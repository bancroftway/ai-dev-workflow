"""On-demand "Generate Code Health Report" endpoints -- sibling to sessions_api.py, same
`_check_shared_secret` guard (called by a Next.js server route, never the browser directly).

Fire-and-forget background job, not a synchronous request: a health-report scan runs for
several minutes (`agent/src/repo_scan.py`'s `"health_report"` profile), far too long to hold an
HTTP request open. POST creates the job and returns its id immediately; GET is polled for
status/result. Appropriate because this app runs `minReplicas=maxReplicas=1` -- the same
in-process `asyncio.create_task` idiom `repo_scan.py`'s own background-refresh scans already use.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import chat_model, health_report_store
from .health_report import run_health_report
from .sessions_api import _check_shared_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health-reports", tags=["health-reports"])


class HealthReportRequest(BaseModel):
    owner: str
    repo: str
    branch: str
    github_token: str = ""
    user_login: str = ""
    entra_assertion: str | None = None


class HealthReportCreateResponse(BaseModel):
    job_id: str


@router.post("", response_model=HealthReportCreateResponse, status_code=202)
async def create_health_report(body: HealthReportRequest, request: Request) -> HealthReportCreateResponse:
    _check_shared_secret(request)

    # One report job in flight per (owner, repo) at a time -- adapted from sessions_api.py's
    # _reject_if_another_ticket_open. Deliberately NOT shared with the per-repo *session*
    # container cap (a report job gets its own independent sandbox slot) -- this guards against
    # a different problem: an unbounded number of tabs/clicks each provisioning a real
    # clone-and-bootstrap container for the same repo.
    if await health_report_store.has_active_job(body.owner, body.repo):
        raise HTTPException(
            status_code=409,
            detail=f"a health report is already generating for {body.owner}/{body.repo} -- wait for it to finish",
        )

    # Same pinned-or-live provider choice and empty-credential guard as sessions_api.py's
    # provision_session -- a report job's sandbox boots the same CLI-bearing image, so it needs
    # the same credential even though this job never actually drives an agent turn with it.
    chat_provider = await chat_model.get_provider()
    runtime_auth_token, runtime_auth_kind = await chat_model.get_runtime_auth_token(provider=chat_provider)
    if not runtime_auth_token:
        raise HTTPException(
            status_code=409,
            detail="no coding-agent credential configured for this organization -- set one in Settings",
        )

    job_id = str(uuid.uuid4())
    await health_report_store.create_job(
        job_id, owner=body.owner, repo=body.repo, branch=body.branch, user_login=body.user_login,
    )
    asyncio.create_task(
        run_health_report(
            job_id,
            owner=body.owner,
            repo=body.repo,
            branch=body.branch,
            git_user_token=body.github_token,
            runtime_auth_token=runtime_auth_token,
            runtime_auth_kind=runtime_auth_kind,
            chat_provider=chat_provider,
        )
    )
    return HealthReportCreateResponse(job_id=job_id)


@router.get("/{job_id}")
async def get_health_report(job_id: str, request: Request) -> dict:
    _check_shared_secret(request)
    job = await health_report_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such health report job")
    return {
        "job_id": job["job_id"],
        "owner": job["owner"],
        "repo": job["repo"],
        "branch": job["branch"],
        "status": job["status"],
        "report": job["report_json"],
        "error": job["error_message"],
    }
