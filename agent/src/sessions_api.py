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
from datetime import datetime, timedelta
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
    run_event_store,
    session_store,
)
from .run_events import RunEvent, RunEventType
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

    # Phase E audit I-3: read BEFORE the credential resolution below -- a plain SELECT, so this
    # doesn't disturb I-A's "no side effect before the credential check" ordering (that fix was
    # about repo_scaffold.create_repo/the Key Vault fetch, real side effects; a DB read has none).
    # Needed here specifically so the credential fetch below can ask for the RIGHT provider's
    # credential on a reprovision, not just so the project_id fallback further down has it.
    existing = await session_store.get_session(body.thread_id)

    # The in-flight guarantee's container/credential half (I-3): GraphState.provider is pinned
    # per-thread and never re-resolved once a run starts (graph.py:1516's own `state.get("provider")
    # or await chat_model.get_provider()`) -- but provisioning used to always read the LIVE org
    # setting, one layer down, with no memory of what a PRIOR provision for this exact session
    # actually used. A run pinned to "claude" whose container gets idle-reaped after an admin flips
    # the org setting to "copilot" would reprovision onto a copilot-flavored container/credential
    # while the checkpointed graph kept correctly dispatching to claude -- every turn then fails
    # auth. Fixed the same way graph.py:1516 fixed the same shape one layer up: prefer this
    # session's OWN stored provider (dbo.sessions.provider, written once at first provision by
    # session_store.create_session below) and only fall back to the live org setting when there is
    # genuinely no prior row -- a real brand-new session, for which "provisioning is the moment a
    # live setting change takes effect" is still exactly the right behavior.
    stored_provider = existing.get("provider") if existing is not None else None
    chat_provider = stored_provider or await chat_model.get_provider()

    # I-A fix round (Important, proved by execution): moved here, before ANY side effect --
    # get_runtime_auth_token() itself is side-effect-free, so there is no reason this has to wait
    # until immediately before provider.provision(). It used to sit after the Key Vault fetch AND
    # after repo_scaffold.create_repo/set_project_repo, so a credential-less org's "+ New Project"
    # Assign created a REAL GitHub repo and recorded it against the project row before 409ing --
    # the repo survives the 409 (nothing rolls it back), so a subsequent retry after the admin
    # actually configures a credential reuses that same real repo, not a phantom. Computed once,
    # reused unchanged all the way down to provider.provision() below -- calling
    # get_runtime_auth_token() a second time later would be pure waste (same TTL-cached provider,
    # same fresh-read credential) and could theoretically observe a different value if a save
    # landed in between, which would defeat the point of gating on the value checked here.
    #
    # provider=chat_provider (I-3): fetches the credential for the PINNED provider, not whatever
    # chat_model.get_provider() would say live -- the exact fix this finding calls for; without it,
    # a reprovision could still 200 with a real token, just the wrong provider's.
    runtime_auth_token, runtime_auth_kind = await chat_model.get_runtime_auth_token(provider=chat_provider)
    if not runtime_auth_token:
        # I-2(c), the real backstop (whole-branch review): a session with no usable credential
        # used to sail straight into provider.provision() -- minutes of container boot/clone/
        # bootstrap, then a confusing auth failure buried inside the first CLI turn. Mirrors
        # run_headless.py's own pre-existing gate (its "E2E_GITHUB_TOKEN and %s must both be set"
        # check) -- that entry point already fails fast on exactly this; the one real users hit
        # (this one) did not, until now. The Settings-UI banner (I-2 a/b) is the advisory version
        # of this same check; this 409 is what actually enforces it when the banner goes unread.
        raise HTTPException(
            status_code=409,
            detail="no coding-agent credential configured for this organization -- set one in Settings",
        )

    # Minor 3 (Phase E audit I-3 review): a session created before migration 0008 added the
    # provider column has provider=NULL forever -- create_session only ever writes it on a
    # session's first-ever provision, which already happened for this row. Left alone, that one
    # legacy row would keep resolving live on every single future reprovision, the exact gap I-3
    # exists to close, indefinitely. Backfilled here, now that chat_provider has been resolved
    # anyway for the credential fetch above -- set_session_provider's own `WHERE provider IS NULL`
    # makes this a no-op once stamped and safe to call unconditionally on every reprovision.
    if existing is not None and stored_provider is None:
        await session_store.set_session_provider(body.thread_id, chat_provider)

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
    # runtime_auth_token/runtime_auth_kind: resolved (and empty-checked, I-2c/I-A) at the very top
    # of this function now, before any side effect -- see that block's own comment for why.
    # provider=chat_provider (I-3): the same pinned-or-live choice resolved above, so the container
    # this call bakes AGENT_PROVIDER/credentials into always matches the credential that was just
    # fetched for it -- see SandboxProvider.provision's own docstring for the full reasoning.
    try:
        session = await provider.provision(
            session_id=body.thread_id,
            repo_clone_url=repo_clone_url,
            branch=body.branch,
            work_branch=work_branch,
            git_user_token=body.github_token,
            runtime_auth_token=runtime_auth_token,
            provider=chat_provider,
            runtime_auth_kind=runtime_auth_kind,
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
        #
        # provider=chat_provider (I-3): only ever written here, on a session's first real
        # provision (existing is None) -- create_session's own IF NOT EXISTS guard means this
        # write can never happen twice for the same session_id, so this is genuinely a
        # write-once-pin, not just a default that a later call could overwrite.
        await session_store.create_session(
            body.thread_id,
            owner=owner,
            repo=repo,
            user_login=body.user_login,
            source_branch=body.branch,
            work_branch=work_branch,
            title="(untitled session)",
            project_id=project_id,
            provider=chat_provider,
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


class RunEventResponse(BaseModel):
    """The single schema-aware representation of a dbo.run_events row -- mirrors SessionResponse's
    own convention above (the frontend never queries SQL directly). Field names/shape match
    run_events.RunEvent verbatim (and therefore also match run_event_stream.emit_live's live AG-UI
    CUSTOM event payload field-for-field -- both are the SAME event, just two different delivery
    paths), so the frontend can merge a live-streamed event and a fetched-from-history one by
    identical shape without a translation layer."""

    seq: int
    run_id: str
    session_id: str
    ts: datetime
    stage: str | None
    node: str | None
    type: str
    summary: str | None
    payload: dict[str, Any] | None
    token_usage: dict[str, Any] | None


class SessionEventsResponse(BaseModel):
    events: list[RunEventResponse]


@router.get("/{session_id}/events", response_model=SessionEventsResponse)
async def get_session_events(session_id: str, request: Request) -> SessionEventsResponse:
    """Backs Part 2 Task 8's EventLogView: the durable-history half of run visibility (Task 2's
    live AG-UI transport only delivers events during an ACTIVE run this browser tab is watching --
    nothing for a finished run, or a page load/reconnect that needs to see history-so-far).

    Keyed by session_id, not by first looking up the session's current run_id and calling
    run_event_store.list_events(run_id) -- deliberately: 0006_create_run_events.sql's own column
    comment says sessions.run_id "remints across resumes... not a stable target", confirmed in
    session_store.touch_run (a resume with a revised title mints a genuinely new run_id), so a
    session can accumulate more than one run_id over its lifetime and dbo.sessions only stores the
    current one. Keying on the current run_id alone would silently drop every event from a prior
    attempt on resume -- see run_event_store.list_events_by_session's own docstring for the full
    reasoning. 404 (not an empty list) for an unknown session_id, matching get_session_row just
    above, so a typo'd/foreign id can't be confused with "a real session that hasn't run yet."
    """
    _check_shared_secret(request)
    row = await session_store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    events = await run_event_store.list_events_by_session(session_id)
    # Explicit field-by-field construction, NOT dataclasses.asdict(e) -- run_event_stream.py's own
    # _json_safe_payload docstring flags exactly this gotcha: asdict() leaves a dataclass's Enum
    # field as the enum MEMBER, not its string value, so `type=e.type.value` here mirrors that
    # same already-established fix rather than relying on RunEventType's StrEnum-is-a-str behavior
    # to happen to serialize correctly.
    return SessionEventsResponse(events=[
        RunEventResponse(
            seq=e.seq, run_id=e.run_id, session_id=e.session_id, ts=e.ts,
            stage=e.stage, node=e.node, type=e.type.value, summary=e.summary,
            payload=e.payload, token_usage=e.token_usage,
        )
        for e in events
    ])


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

    # Part 2 Task 1 added dbo.run_events with a plain FK to dbo.sessions (no ON DELETE CASCADE) --
    # session_store.delete_session below 500s with a REFERENCE constraint violation for any
    # session that ever emitted a single real RunEvent (any sandboxed node run) without this,
    # found live via this exact endpoint during Task 14's end-to-end sweep. Must run before the
    # sessions row delete, same order run_event_store._demo()'s own cleanup already uses.
    await run_event_store.delete_events_by_session(thread_id)
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
    # None until a credential has actually been saved (mirrors credential_configured=False), or
    # for a provider="copilot" row, where the api_key/oauth distinction doesn't apply (C-1). Lets
    # the Settings UI (page.tsx) pre-select the right billing-mode radio on load instead of always
    # defaulting to "Subscription" the moment a saved Claude credential exists.
    credential_kind: str | None
    session_ready: bool
    updated_at: datetime | None
    updated_by: str | None


# I-1 (lazy version, whole-branch review): re-probing a saved credential on every settings-page
# load or session provision would add real latency/cost to a hot path for a check that changes
# rarely -- an hour-old "still valid" answer is plenty fresh for a banner. Not a scheduler either
# (new machinery for a once-an-hour check) -- just a staleness gate in front of the SAME probe
# _probe_provider_credential already runs at save time, folded into the one place that already
# computes session_ready.
_VALIDATION_STALENESS = timedelta(hours=1)


async def _maybe_reprobe_credential(settings: org_settings.OrgSettings) -> bool | None:
    """I-1: re-run the save-time probe against the CURRENTLY saved credential when it's never been
    checked or the check is over an hour old, and persist the result -- so a credential that was
    valid at save time but got revoked/rotated later actually flips session_ready to False instead
    of staying silently green forever (Spec Verification 10).

    Returns None when there's nothing to (re)probe: no credential saved (env-var-only deployment --
    those were never probed at save time either, see put_org_settings_endpoint), or an oauth-kind
    credential (C-1: no live probe exists for a subscription token -- see
    _probe_provider_credential's own docstring for why inventing one isn't done here either;
    session_ready for that case honestly stays on the plain non-emptiness check, same as before this
    function existed). Otherwise returns the freshly-observed (or still-fresh-enough cached) ok/not-
    ok.

    C-B fix round (whole-branch review Critical, proved by execution): a failure while re-probing
    is NOT uniformly "not ok". _probe_provider_credential distinguishes a DEFINITIVE rejection (the
    provider's own 403, or the oauth shape-check's 422 -- both mean the credential is genuinely bad)
    from a transport-class failure (502 -- a network blip, or the vault being briefly unreachable,
    which proves nothing about the credential itself). Only the former persists ok=False; the
    latter logs and returns the PREVIOUS verdict unchanged, without writing a fresh timestamp --
    the old code's blanket `except Exception: ok = False` conflated the two, so one transient
    network hiccup could persist a perfectly good credential as invalid for a full hour (this
    function's own staleness window), with no way to force an earlier recheck since the timestamp
    it also stamped looked exactly as fresh as a real rejection would have.
    """
    if settings.credential_secret_name is None:
        return None
    kind = settings.credential_kind or "api_key"
    if kind == "oauth":
        return None
    if settings.last_validated_at is not None and (
        datetime.utcnow() - settings.last_validated_at < _VALIDATION_STALENESS
    ):
        return settings.last_validation_ok

    try:
        value = await org_credential_vault.get_org_credential(settings.credential_secret_name)
        await _probe_provider_credential(settings.provider, value, kind=kind)
    except HTTPException as exc:
        if exc.status_code in (403, 422):
            # Definitive: the provider (or, for oauth, our own shape check) actively rejected it.
            logger.warning("org credential re-validation: definitively rejected (I-1): %s", exc.detail)
            await org_settings.record_validation_result(False)
            return False
        # Transport-class (502, from _probe_provider_credential's own httpx.HTTPError catch) --
        # can't tell whether the credential is actually bad. Preserve the prior verdict/timestamp
        # rather than overwrite it, so the NEXT call retries immediately instead of waiting out a
        # falsely-fresh hour.
        logger.warning(
            "org credential re-validation: could not reach the provider (I-1) -- preserving the "
            "last known verdict rather than overwriting it: %s", exc.detail,
        )
        return settings.last_validation_ok
    except Exception:
        # Anything else (e.g. the vault fetch itself failing) is the same "can't tell" case as a
        # transport failure above, not a definitive rejection -- same treatment.
        logger.warning(
            "org credential re-validation: unexpected failure (I-1) -- preserving the last known "
            "verdict rather than overwriting it",
            exc_info=True,
        )
        return settings.last_validation_ok

    await org_settings.record_validation_result(True)
    return True


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

    I-1: session_ready now also folds in _maybe_reprobe_credential's periodic re-validation, not
    just the plain non-emptiness check -- a revoked vault-stored api_key credential flips
    session_ready to False here even though get_runtime_auth_token() still happily returns its
    (stale, now-rejected) string value.
    """
    settings = await org_settings.get_org_settings()
    try:
        runtime_value, _runtime_kind = await chat_model.get_runtime_auth_token()
        session_ready = bool(runtime_value)
    except Exception:
        logger.warning("get_runtime_auth_token() failed while building the org-settings response", exc_info=True)
        session_ready = False

    if session_ready and settings is not None:
        validation_ok = await _maybe_reprobe_credential(settings)
        if validation_ok is False:
            session_ready = False

    if settings is None:
        # Fresh deployment, nobody has saved a setting yet -- the exact same env-var fallback
        # chat_model.get_provider() itself falls back to, so this page's "active provider" can
        # never disagree with what a real session would actually run under.
        return OrgSettingsResponse(
            provider=os.environ.get("AGENT_PROVIDER", "copilot"),
            credential_configured=False,
            credential_kind=None,
            session_ready=session_ready,
            updated_at=None,
            updated_by=None,
        )
    return OrgSettingsResponse(
        provider=settings.provider,
        credential_configured=settings.credential_secret_name is not None,
        credential_kind=settings.credential_kind,
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
    # C-1: which of the two Claude billing modes `credential` is. Only meaningful when
    # provider == "claude"; ignored for copilot. None means "not specified" -- defaults to
    # "api_key" below (instruction: null kind = api_key, same rule the Settings UI applies for an
    # existing saved row with no recorded kind).
    credential_kind: Literal["api_key", "oauth"] | None = None
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


# C-1: a subscription (oauth) token has no equivalent "GET something and check for 200" endpoint
# citable against real evidence. Checked the installed Claude CLI's own --help/docs surface (the
# only surface this task's brief allows checking for this) -- `claude --help`'s only auth-adjacent
# commands are `setup-token` (mints the token interactively, no validation mode) and `auth status`
# (reads the CLI's own local keychain/config, not a network call this endpoint could make on its
# behalf). Neither documents a standalone HTTP endpoint that accepts a bare `sk-ant-oat...` token.
# No network call is made with a fabricated token to "see what happens" -- that would be inventing
# evidence, not finding it. So: shape-only validation for oauth, honestly recorded as such in both
# this function's own behavior and put_org_settings_endpoint's log line below, not disguised as an
# equivalent check to the api_key probe.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat"


async def _probe_provider_credential(
    provider: str, value: str, *, kind: str | None = None, client: httpx.AsyncClient | None = None
) -> None:
    """Ruling 3's actual validation gate: one lightweight GET against the TARGET PROVIDER's own
    API -- deliberately not just a Key Vault round trip, which would only prove our own vault
    plumbing works, never that the admin didn't just paste a typo'd or already-revoked key. This
    is the smallest real check available without a full sandboxed CLI turn -- see the module
    constants just above for why these two specific endpoints were chosen. Raises HTTPException
    with the provider's own real response on any failure; the credential value itself never
    appears in the raised detail, only the provider's own response body/status.

    Raised status is a CLASSIFICATION, not just a passthrough of whatever the provider returned
    (second Minor fix round): 401/403 (and claude's 422) mean the provider itself is saying the
    credential is bad -- raised as a 403 here, which _maybe_reprobe_credential's caller treats as a
    definitive rejection worth persisting. A connection-level failure, a 429 rate limit, or any 5xx
    means the PROVIDER had a bad moment, not that the credential is bad -- all raised as a 502,
    which that same caller preserves the prior verdict for instead of overwriting it to False. An
    unrecognized status code is treated the same as the 502 case (logged, not raised as a
    rejection) -- misclassifying an unknown status as definitive is the harmful direction.

    `client` is test-only dependency injection (see this module's own _demo), same convention as
    _fetch_default_branch's/repo_scaffold.create_repo's own `client` param -- every real call site
    omits it, letting this build its own short-lived httpx.AsyncClient as before.

    kind matters only for provider == "claude": "oauth" skips the live probe entirely (see
    _OAUTH_TOKEN_PREFIX's own comment for why one doesn't exist here) and instead only checks that
    the value is non-empty, with the documented `sk-ant-oat` prefix treated as a soft hint (logged,
    not enforced -- Anthropic could change that prefix without this becoming a hard validation
    error for a token that otherwise works fine). "api_key" (the default) and copilot both keep the
    existing live-probe behavior, unchanged.
    """
    if provider == "claude" and kind == "oauth":
        if not value:
            raise HTTPException(status_code=422, detail="oauth token must not be empty")
        if not value.startswith(_OAUTH_TOKEN_PREFIX):
            logger.warning(
                "saved Claude oauth credential does not start with the documented %r prefix -- "
                "accepted anyway (shape hint only, not enforced)",
                _OAUTH_TOKEN_PREFIX,
            )
        return

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

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.get(url, headers=headers)
        else:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"could not reach {provider}'s API to validate the credential: {exc}"
        ) from None

    if resp.status_code == 200:
        return

    # Second Minor fix round (same shape as C-B, one layer up): a status code alone doesn't tell
    # you whether the CREDENTIAL is bad or the PROVIDER is having a bad day -- classify before
    # raising, since _maybe_reprobe_credential's caller-side fix only helps if what lands in its
    # hands is actually shaped right (a definitive-rejection 403 vs. a transport-class 502).
    if resp.status_code in (401, 403) or (provider == "claude" and resp.status_code == 422):
        # Definitive: the provider itself says this credential is bad.
        raise HTTPException(
            status_code=403,
            detail=f"{provider} rejected the credential: {resp.status_code} {resp.text[:300]}",
        )
    if resp.status_code == 429 or resp.status_code >= 500:
        # Transport-class: a rate limit or an upstream outage proves nothing about the credential
        # itself. Same 502 shape the connection-level failure above already raises, so
        # _maybe_reprobe_credential's existing preserve-the-prior-verdict branch handles this with
        # no changes on that side -- and, at save time, put_org_settings_endpoint surfaces this to
        # the admin as "try again", not "your brand-new credential was rejected", which is the
        # honest read of what actually happened.
        raise HTTPException(
            status_code=502,
            detail=f"could not validate the credential right now -- {provider} returned {resp.status_code} {resp.text[:300]}",
        )
    # Anything else unrecognized: lean transport-class (preserve the prior verdict on re-probe,
    # "try again" on save) rather than definitive -- misclassifying an unknown status as a
    # rejection is the harmful direction (it persists a possibly-good credential as invalid for an
    # hour, or tells an admin their working credential is bad). Logged so an actually-new
    # provider-side status code doesn't silently vanish into "try again" forever.
    logger.warning(
        "%s returned unexpected status %s while validating a credential -- treating as "
        "transport-class, not a definitive rejection", provider, resp.status_code,
    )
    raise HTTPException(
        status_code=502,
        detail=f"could not validate the credential right now -- {provider} returned an unexpected {resp.status_code} {resp.text[:300]}",
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
    rejected outright below instead. C-1 extends this same guard to a same-provider CLAUDE billing-
    mode switch with no new credential: an existing api_key-shaped secret relabeled "oauth" (or vice
    versa) with nothing having validated it AS that kind is the identical hazard one level down --
    worse for a switch TO oauth specifically, since C-1's own probe skips live validation for that
    kind, so nothing would ever catch the mismatch later either.
    """
    _check_shared_secret(request)
    existing = await org_settings.get_org_settings()
    secret_name = existing.credential_secret_name if existing is not None else None
    # Carried forward unchanged unless the `else` branch below (a genuinely new credential) sets
    # it -- a PUT that only touches provider/updated_by must not blank out an already-recorded kind.
    credential_kind = existing.credential_kind if existing is not None else None

    if body.credential is None:
        # Normalized the same way credential_kind's own dataclass comment says to: None (no
        # existing row, or a pre-0007 row) reads as "api_key", never as "unknown" -- computed as
        # its own name here specifically so the operator-precedence trap of inlining `x if y else
        # None or z` (silently parsed as `x if y else (None or z)`, NOT `(x if y else None) or z`)
        # can't quietly reintroduce a wrong default for the existing-row-with-NULL-kind case.
        existing_kind = (existing.credential_kind if existing is not None else None) or "api_key"
        kind_changing = (
            body.provider == "claude"
            and body.credential_kind is not None
            and body.credential_kind != existing_kind
        )
        if existing is not None and secret_name is not None and (body.provider != existing.provider or kind_changing):
            raise HTTPException(
                status_code=422,
                detail=(
                    "switching provider or Claude billing mode requires a new credential -- the "
                    "currently saved credential belongs to the current provider/mode and cannot "
                    "carry over"
                ),
            )
    else:
        credential = body.credential.strip()
        # Default a missing kind to "api_key" (same rule the Settings UI applies for an existing
        # row with no recorded kind: null/omitted means the mode that existed before oauth did).
        # None for copilot -- the kind distinction is meaningless there.
        credential_kind = (body.credential_kind or "api_key") if body.provider == "claude" else None
        await _probe_provider_credential(body.provider, credential, kind=credential_kind)
        try:
            secret_name = await org_credential_vault.set_org_credential(credential)
        except keyvault.VaultAccessError as exc:
            raise HTTPException(status_code=502, detail=f"org credential vault is not accessible: {exc}") from None
        # Honest about what actually happened (C-1): "validated" for api_key/copilot, since a live
        # probe ran; oauth only ever got a shape check, so it says so instead of implying parity.
        validation_note = "validated" if credential_kind != "oauth" else "shape-checked (validated on first use)"
        logger.info(
            "org credential saved and %s for provider=%s kind=%s", validation_note, body.provider, credential_kind
        )
        # C-B fix round (whole-branch review Critical, proved by execution): a credential that
        # just passed the probe above is, by definition, valid RIGHT NOW -- record that instead of
        # leaving I-1's last_validation_ok/last_validated_at untouched. Without this, a stale
        # persisted False from an earlier failed check survived a successful save and kept
        # session_ready pinned False for up to an hour, deadlocking the exact admin recovery path
        # (banner -> paste a fresh working credential -> save) I-1 exists to unblock. Harmless for
        # oauth too (its own row is never read back through this path -- _maybe_reprobe_credential
        # short-circuits before ever looking at these columns for an oauth kind -- but recording an
        # honest "shape-checked, now" is no worse than leaving stale/absent values behind).
        await org_settings.record_validation_result(True)

    await org_settings.set_org_settings(body.provider, secret_name, body.updated_by, credential_kind)
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

    # get_session_events (Part 2 Task 8): real RunEvent dataclass instances (not hand-written
    # JSON) in both of this task's real captured shapes -- Claude's correlated tool_use+result
    # (claude_chat_model._translate_intermediate_events) and a plain node-lifecycle event
    # (graph.py's draft_node) -- round-tripping through the route into RunEventResponse. Pins the
    # exact gotcha this route's own comment calls out: `type` must come back the plain string
    # "tool_call"/"node_finished", never the RunEventType member itself.
    fake_events = [
        RunEvent(
            run_id="r1", session_id="44444444-4444-4444-4444-444444444444", seq=1,
            ts=datetime(2026, 1, 1, 12, 0, 0), type=RunEventType.NODE_FINISHED,
            stage="specification", node="draft", summary="draft ready for review",
            payload={"readiness": True}, token_usage={"model": "m", "input_tokens": 1, "output_tokens": 1, "cost": 0.0},
        ),
        RunEvent(
            run_id="r2", session_id="44444444-4444-4444-4444-444444444444", seq=2,
            ts=datetime(2026, 1, 1, 12, 0, 5), type=RunEventType.TOOL_CALL,
            stage="plan", node="draft", summary="tool call: Bash",
            payload={"name": "Bash", "input": {"command": "ls"}, "result": "file.txt", "is_error": False},
            token_usage=None,
        ),
    ]

    async def fake_list_events_by_session(session_id: str) -> list[RunEvent]:
        assert session_id == "44444444-4444-4444-4444-444444444444", session_id
        return fake_events

    async def fake_get_session_found(session_id: str) -> dict[str, Any] | None:
        return {"session_id": session_id}  # get_session_events only checks this is not None

    original_list_events_by_session = run_event_store.list_events_by_session
    original_get_session = session_store.get_session
    run_event_store.list_events_by_session = fake_list_events_by_session  # type: ignore[assignment]
    session_store.get_session = fake_get_session_found  # type: ignore[assignment]
    try:
        response = asyncio.run(get_session_events("44444444-4444-4444-4444-444444444444", _FakeRequest()))  # type: ignore[arg-type]
    finally:
        run_event_store.list_events_by_session = original_list_events_by_session  # type: ignore[assignment]
        session_store.get_session = original_get_session  # type: ignore[assignment]

    assert len(response.events) == 2, response.events
    assert response.events[0].type == "node_finished", response.events[0].type  # plain str, not the enum member
    assert response.events[1].type == "tool_call", response.events[1].type
    assert response.events[1].payload == {"name": "Bash", "input": {"command": "ls"}, "result": "file.txt", "is_error": False}, response.events[1]
    assert response.events[1].token_usage is None, response.events[1]
    assert response.events[0].seq == 1 and response.events[1].seq == 2, response.events

    # 404 for an unknown session_id -- matches get_session_row's own contract just above (never an
    # empty events list for "no such session," so a typo'd id can't be confused with "exists but
    # hasn't run yet").
    async def fake_get_session_missing(session_id: str) -> dict[str, Any] | None:
        return None

    session_store.get_session = fake_get_session_missing  # type: ignore[assignment]
    try:
        asyncio.run(get_session_events("does-not-exist", _FakeRequest()))  # type: ignore[arg-type]
        raise AssertionError("get_session_events must 404 for an unknown session_id")
    except HTTPException as exc:
        assert exc.status_code == 404, exc.status_code
    finally:
        session_store.get_session = original_get_session  # type: ignore[assignment]

    # Part 2 Task 14 fix-round (coordinator review Minor): delete_session_full -- the actual code
    # path production traffic uses -- must succeed for a session that has emitted a real
    # RunEvent. 0006_create_run_events.sql's session_id FK (NOT NULL, no ON DELETE CASCADE) 500'd
    # this real endpoint for exactly that case until run_event_store.delete_events_by_session was
    # added and wired in ahead of session_store.delete_session (see both functions' own
    # docstrings; reproduced live against the real DB in task-14-report.md, not theoretical).
    # Real rows, real delete, real DB, one event loop throughout -- monkeypatching either delete
    # call would defeat the point: this must fail if delete_events_by_session is ever removed
    # from delete_session_full, or reordered to run after session_store.delete_session.
    import uuid

    async def _check_delete_survives_real_run_events() -> None:
        project_id = await project_store.create_project(
            "sessions-api-selfcheck-delete-project", tech_stack_id=None, tech_stack_text=None, created_by="octocat"
        )
        session_id = str(uuid.uuid4())
        try:
            await session_store.create_session(
                session_id, owner="octocat", repo="demo-repo-delete-selfcheck", user_login="octocat",
                source_branch="main", work_branch=f"ai-dev-workflow/{session_id}", title="t",
                project_id=project_id,
            )
            await run_event_store.append_event(RunEvent(
                run_id=uuid.uuid4().hex[:8], session_id=session_id, type=RunEventType.NODE_FINISHED,
                stage="tech-stack", node="draft", summary="draft ready for review",
            ))
            await delete_session_full(session_id, DeleteSessionRequest(github_token=""), _FakeRequest())  # type: ignore[arg-type]
            assert await session_store.get_session(session_id) is None, "session row must be gone after delete_session_full"
            assert await run_event_store.list_events_by_session(session_id) == [], "run_events rows must be gone too"
        finally:
            # Defensive only, not the normal path: delete_session_full's own success path above
            # already removes the session row and its run_events -- this only fires anything if
            # that call raised (e.g. a mutation-tested regression) and left rows behind.
            pool = await session_store._get_pool()  # noqa: SLF001 -- same package, one shared pool
            async with pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute("DELETE FROM dbo.run_events WHERE session_id = ?", session_id)
                await cur.execute("DELETE FROM dbo.sessions WHERE session_id = ?", session_id)
                await cur.execute("DELETE FROM dbo.projects WHERE project_id = ?", project_id)

    asyncio.run(_check_delete_survives_real_run_events())

    # === Phase E audit C-1/I-1/I-2: org-settings credential-kind + validation-staleness + the
    # provision 409 backstop. All against the REAL functions with only I/O boundaries (DB, vault,
    # network) monkeypatched -- same technique as connect_project_route's own check above -- since
    # dbo.org_settings is a real singleton row (id=1) on whatever DB this runs against, and this
    # self-check must not touch (let alone clobber) that one real row. ===
    global _probe_provider_credential  # reassigned further down; must precede every use in this function

    # _probe_provider_credential's oauth branch (C-1): shape-only, no network call for either
    # sub-case -- an empty value is rejected, a non-empty one is accepted without ever reaching
    # the httpx.AsyncClient block below it (if it did, this call would need real network access
    # and a real api.anthropic.com response, which this offline self-check has neither).
    asyncio.run(_probe_provider_credential("claude", "sk-ant-oat-fake-for-selfcheck", kind="oauth"))
    try:
        asyncio.run(_probe_provider_credential("claude", "", kind="oauth"))
        raise AssertionError("_probe_provider_credential must reject an empty oauth token")
    except HTTPException as exc:
        assert exc.status_code == 422, exc.status_code

    # _maybe_reprobe_credential (I-1): the staleness gate in front of the probe, and the oauth
    # no-probe-exists rule, each on their own real inputs -- no OrgSettings row ever touches a DB.
    reprobe_calls: list[str] = []

    async def _tracking_get_org_credential(secret_name: str) -> str:
        reprobe_calls.append("get_org_credential")
        return "fake-value"

    async def _tracking_record_validation_result(ok: bool) -> None:
        reprobe_calls.append(f"record_validation_result:{ok}")

    original_get_org_credential = org_credential_vault.get_org_credential
    original_record_validation_result = org_settings.record_validation_result
    original_probe = _probe_provider_credential
    org_credential_vault.get_org_credential = _tracking_get_org_credential  # type: ignore[assignment]
    org_settings.record_validation_result = _tracking_record_validation_result  # type: ignore[assignment]
    try:
        # No credential saved at all -- nothing to (re)probe.
        no_cred = org_settings.OrgSettings(
            provider="claude", credential_secret_name=None, updated_at=datetime(2026, 1, 1), updated_by="a",
        )
        assert asyncio.run(_maybe_reprobe_credential(no_cred)) is None
        assert reprobe_calls == [], f"nothing should have been probed for no_cred, got {reprobe_calls}"

        # oauth kind -- C-1's own "no live probe exists" rule means this must also skip, even
        # though a credential IS saved.
        oauth_cred = org_settings.OrgSettings(
            provider="claude", credential_secret_name="secret-1", credential_kind="oauth",
            updated_at=datetime(2026, 1, 1), updated_by="a",
        )
        assert asyncio.run(_maybe_reprobe_credential(oauth_cred)) is None
        assert reprobe_calls == [], f"oauth-kind must never probe, got {reprobe_calls}"

        # api_key kind, validated 5 minutes ago -- well inside the 1-hour staleness window, so the
        # cached last_validation_ok must come back WITHOUT a new probe.
        fresh_cred = org_settings.OrgSettings(
            provider="claude", credential_secret_name="secret-1", credential_kind="api_key",
            updated_at=datetime(2026, 1, 1), updated_by="a",
            last_validation_ok=True, last_validated_at=datetime.utcnow() - timedelta(minutes=5),
        )
        assert asyncio.run(_maybe_reprobe_credential(fresh_cred)) is True
        assert reprobe_calls == [], f"a fresh (<1h) validation must not re-probe, got {reprobe_calls}"

        # api_key kind, validated 2 hours ago -- stale, must re-probe and write the result back.
        _probe_provider_credential = lambda provider, value, kind=None: asyncio.sleep(0)  # noqa: E731 -- succeeds
        stale_cred = org_settings.OrgSettings(
            provider="claude", credential_secret_name="secret-1", credential_kind="api_key",
            updated_at=datetime(2026, 1, 1), updated_by="a",
            last_validation_ok=True, last_validated_at=datetime.utcnow() - timedelta(hours=2),
        )
        assert asyncio.run(_maybe_reprobe_credential(stale_cred)) is True
        assert reprobe_calls == ["get_org_credential", "record_validation_result:True"], reprobe_calls
        reprobe_calls.clear()

        # Same staleness, but the re-probe itself fails (revoked upstream, Spec Verification 10) --
        # must come back False and still write the (negative) result back, not raise past this
        # function or silently keep reporting the old True.
        async def _failing_probe(provider: str, value: str, *, kind: str | None = None) -> None:
            raise HTTPException(status_code=403, detail="revoked")

        _probe_provider_credential = _failing_probe
        assert asyncio.run(_maybe_reprobe_credential(stale_cred)) is False
        assert reprobe_calls == ["get_org_credential", "record_validation_result:False"], reprobe_calls
        reprobe_calls.clear()

        # C-B fix round (Critical, proved by execution): a TRANSPORT-class failure (502 -- a
        # network blip, or the vault being briefly unreachable) is NOT the same as a definitive
        # rejection. stale_cred still carries its ORIGINAL last_validation_ok=True (a frozen
        # dataclass -- unaffected by the two calls above) -- must come back True (the prior
        # verdict, preserved) and must NOT call record_validation_result at all, unlike the
        # definitive-403 case just above which explicitly records False.
        async def _transport_failing_probe(provider: str, value: str, *, kind: str | None = None) -> None:
            raise HTTPException(status_code=502, detail="could not reach claude's API")

        _probe_provider_credential = _transport_failing_probe
        assert asyncio.run(_maybe_reprobe_credential(stale_cred)) is True, (
            "a transport-class failure must preserve the prior verdict, not flip it to False"
        )
        assert reprobe_calls == ["get_org_credential"], (
            f"a transport-class failure must NOT call record_validation_result, got {reprobe_calls}"
        )
        reprobe_calls.clear()

        # Same "can't tell" treatment for a non-HTTPException failure (e.g. the vault fetch
        # itself blowing up) -- not just the transport-class HTTPException case above.
        async def _vault_failing_get_org_credential(secret_name: str) -> str:
            reprobe_calls.append("get_org_credential")
            raise RuntimeError("vault unreachable")

        org_credential_vault.get_org_credential = _vault_failing_get_org_credential  # type: ignore[assignment]
        assert asyncio.run(_maybe_reprobe_credential(stale_cred)) is True, (
            "a non-HTTPException failure must also preserve the prior verdict"
        )
        assert reprobe_calls == ["get_org_credential"], reprobe_calls
        org_credential_vault.get_org_credential = _tracking_get_org_credential  # type: ignore[assignment]
        reprobe_calls.clear()

        # Second Minor fix round: the REAL classification (not a hand-rolled fake exception shape
        # like the two cases above) -- an actual httpx.MockTransport response fed through the TRUE
        # _probe_provider_credential (original_probe, captured before any monkeypatching in this
        # block), proving the whole chain at once: the status-code classification itself, AND
        # _maybe_reprobe_credential's handling of whatever it produces.
        def _probe_returning_status(status: int):
            async def _wrapped(provider: str, value: str, *, kind: str | None = None) -> None:
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
                try:
                    await original_probe(provider, value, kind=kind, client=client)
                finally:
                    await client.aclose()
            return _wrapped

        # 401 -- the common revocation signal (re-review's own words) -- pinned explicitly:
        # definitive, must persist False.
        _probe_provider_credential = _probe_returning_status(401)
        assert asyncio.run(_maybe_reprobe_credential(stale_cred)) is False, "a 401 must persist False"
        assert reprobe_calls == ["get_org_credential", "record_validation_result:False"], reprobe_calls
        reprobe_calls.clear()

        # 429 rate-limit -- the exact gap the review found -- transport-class: must preserve the
        # prior verdict (True), not flip it to False.
        _probe_provider_credential = _probe_returning_status(429)
        assert asyncio.run(_maybe_reprobe_credential(stale_cred)) is True, (
            "a 429 rate-limit must preserve the prior verdict, not flip it to False"
        )
        assert reprobe_calls == ["get_org_credential"], (
            f"a 429 rate-limit must NOT call record_validation_result, got {reprobe_calls}"
        )
        reprobe_calls.clear()
    finally:
        org_credential_vault.get_org_credential = original_get_org_credential  # type: ignore[assignment]
        org_settings.record_validation_result = original_record_validation_result  # type: ignore[assignment]
        _probe_provider_credential = original_probe

    # put_org_settings_endpoint's credential_kind round-trip + the kind-switch-without-a-new-
    # credential 422 guard (C-1) -- the real route function, with only its I/O boundaries (DB
    # read/write, vault write, live credential probe, chat_model's cache-invalidation + runtime-
    # token read) stubbed. _probe_provider_credential's own REAL behavior (oauth shape-check vs.
    # api_key live network probe) already has dedicated coverage above -- mocked here to a
    # tracking no-op so this block can use a plain fake api_key string without it actually
    # reaching api.anthropic.com and 401ing (real behavior, observed while first writing this
    # check -- a fake credential is exactly what the real probe correctly rejects).
    put_calls: list[tuple] = []
    probe_calls: list[tuple] = []

    async def _stub_set_org_settings(provider: str, secret_name: str | None, updated_by: str, credential_kind: str | None = None) -> None:
        put_calls.append((provider, secret_name, credential_kind))

    async def _stub_set_org_credential(value: str) -> str:
        return "org-credential-secret"

    async def _stub_get_runtime_auth_token() -> tuple[str, str | None]:
        return "irrelevant-for-this-check", "oauth"

    async def _tracking_probe_ok(provider: str, value: str, *, kind: str | None = None) -> None:
        probe_calls.append((provider, kind))

    async def _stub_record_validation_result(ok: bool) -> None:
        # No-op: this block's own assertions are about set_org_settings's credential_kind
        # argument, not about record_validation_result (C-B's dedicated fake-row test below
        # exercises that properly, with real before/after state) -- mocked here purely so the
        # credential-accepted branch's new `await org_settings.record_validation_result(True)`
        # call doesn't reach the real DB from this block's otherwise fully-mocked I/O boundaries.
        pass

    def _existing_claude_api_key() -> org_settings.OrgSettings:
        # last_validated_at deliberately "just now" -- keeps this stub inside _maybe_reprobe_
        # credential's 1-hour freshness window (I-1), so put_org_settings_endpoint's own trailing
        # `return await _org_settings_response()` takes the cached-answer branch and never reaches
        # a real vault fetch/network probe/DB write. Those paths already have their own dedicated,
        # explicitly-mocked coverage just above -- this block is only testing set_org_settings's
        # credential_kind argument, not I-1's re-probe machinery a second time.
        return org_settings.OrgSettings(
            provider="claude", credential_secret_name="org-credential-secret", credential_kind="api_key",
            updated_at=datetime(2026, 1, 1), updated_by="admin",
            last_validation_ok=True, last_validated_at=datetime.utcnow(),
        )

    original_set_org_settings = org_settings.set_org_settings
    original_set_org_credential = org_credential_vault.set_org_credential
    original_get_runtime_auth_token = chat_model.get_runtime_auth_token
    original_get_org_settings2 = org_settings.get_org_settings
    original_record_validation_result2 = org_settings.record_validation_result
    original_probe2 = _probe_provider_credential
    org_settings.set_org_settings = _stub_set_org_settings  # type: ignore[assignment]
    org_settings.record_validation_result = _stub_record_validation_result  # type: ignore[assignment]
    org_credential_vault.set_org_credential = _stub_set_org_credential  # type: ignore[assignment]
    chat_model.get_runtime_auth_token = _stub_get_runtime_auth_token  # type: ignore[assignment]
    _probe_provider_credential = _tracking_probe_ok
    try:
        # A brand-new oauth credential, kind explicit -- set_org_settings must be called with
        # credential_kind="oauth" (not silently defaulted, not dropped), and the probe must have
        # been invoked with that same kind (the threading this whole finding is about).
        async def _get_existing():
            return _existing_claude_api_key()

        org_settings.get_org_settings = _get_existing  # type: ignore[assignment]
        asyncio.run(put_org_settings_endpoint(
            OrgSettingsPutRequest(provider="claude", credential="sk-ant-oat-newtoken", credential_kind="oauth", updated_by="admin"),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
        assert put_calls[-1] == ("claude", "org-credential-secret", "oauth"), put_calls[-1]
        assert probe_calls[-1] == ("claude", "oauth"), probe_calls[-1]

        # A brand-new credential with NO kind specified -- must default to "api_key" (instruction:
        # null kind = api_key), not None/"unknown".
        asyncio.run(put_org_settings_endpoint(
            OrgSettingsPutRequest(provider="claude", credential="sk-ant-new-api-key", updated_by="admin"),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
        assert put_calls[-1] == ("claude", "org-credential-secret", "api_key"), put_calls[-1]
        assert probe_calls[-1] == ("claude", "api_key"), probe_calls[-1]

        # Keep-existing PUT (credential=None, kind unspecified) against an existing api_key row --
        # must carry the existing kind forward untouched, not blank it to None.
        asyncio.run(put_org_settings_endpoint(
            OrgSettingsPutRequest(provider="claude", updated_by="admin"),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
        assert put_calls[-1] == ("claude", "org-credential-secret", "api_key"), put_calls[-1]

        # The new guard: switching billing mode (api_key -> oauth) with NO new credential must
        # 422, the same shape as the pre-existing provider-switch guard just above it in the code.
        try:
            asyncio.run(put_org_settings_endpoint(
                OrgSettingsPutRequest(provider="claude", credential_kind="oauth", updated_by="admin"),
                _FakeRequest(),  # type: ignore[arg-type]
            ))
            raise AssertionError("switching billing mode with no new credential must 422")
        except HTTPException as exc:
            assert exc.status_code == 422, exc.status_code
    finally:
        org_settings.set_org_settings = original_set_org_settings  # type: ignore[assignment]
        org_settings.record_validation_result = original_record_validation_result2  # type: ignore[assignment]
        org_credential_vault.set_org_credential = original_set_org_credential  # type: ignore[assignment]
        chat_model.get_runtime_auth_token = original_get_runtime_auth_token  # type: ignore[assignment]
        org_settings.get_org_settings = original_get_org_settings2  # type: ignore[assignment]
        _probe_provider_credential = original_probe2

    # C-B fix round (Critical, proved by execution): the actual admin recovery path I-1 exists for
    # -- a persisted last_validation_ok=False (revoked credential, banner up) + the admin pastes a
    # NEW working credential + saves -- must flip session_ready True IMMEDIATELY, not leave it
    # falsely stuck for up to an hour. The static _get_existing() stub above (fixed
    # last_validation_ok=True) could never have caught this -- proving it needs a STATEFUL fake
    # row, since the bug is specifically about what put_org_settings_endpoint's own trailing
    # _org_settings_response() call reads back AFTER the save actually ran.
    class _FakeRow:
        def __init__(self) -> None:
            self.provider = "claude"
            self.credential_secret_name = "org-credential-secret"
            self.credential_kind = "api_key"
            self.last_validation_ok = False  # the exact stuck state the review reproduced
            self.last_validated_at = datetime.utcnow() - timedelta(minutes=1)  # fresh -- inside the 1h window
            self.updated_at = datetime(2026, 1, 1)
            self.updated_by = "admin"

        def snapshot(self) -> org_settings.OrgSettings:
            return org_settings.OrgSettings(
                provider=self.provider, credential_secret_name=self.credential_secret_name,
                credential_kind=self.credential_kind, updated_at=self.updated_at, updated_by=self.updated_by,
                last_validation_ok=self.last_validation_ok, last_validated_at=self.last_validated_at,
            )

    fake_row = _FakeRow()

    async def _recovery_get_org_settings() -> org_settings.OrgSettings:
        return fake_row.snapshot()

    async def _recovery_set_org_settings(provider: str, secret_name: str | None, updated_by: str, credential_kind: str | None = None) -> None:
        fake_row.provider, fake_row.credential_secret_name, fake_row.credential_kind, fake_row.updated_by = (
            provider, secret_name, credential_kind, updated_by,
        )

    async def _recovery_record_validation_result(ok: bool) -> None:
        fake_row.last_validation_ok = ok
        fake_row.last_validated_at = datetime.utcnow()

    async def _recovery_get_org_credential(secret_name: str) -> str:
        return "irrelevant-for-this-check"

    async def _recovery_set_org_credential(value: str) -> str:
        return "org-credential-secret"

    async def _recovery_get_runtime_auth_token() -> tuple[str, str | None]:
        return "fresh-good-key", "api_key"

    recovery_original_get_org_settings = org_settings.get_org_settings
    recovery_original_set_org_settings = org_settings.set_org_settings
    recovery_original_record_validation_result = org_settings.record_validation_result
    recovery_original_get_org_credential = org_credential_vault.get_org_credential
    recovery_original_set_org_credential = org_credential_vault.set_org_credential
    recovery_original_get_runtime_auth_token = chat_model.get_runtime_auth_token
    recovery_original_probe = _probe_provider_credential
    org_settings.get_org_settings = _recovery_get_org_settings  # type: ignore[assignment]
    org_settings.set_org_settings = _recovery_set_org_settings  # type: ignore[assignment]
    org_settings.record_validation_result = _recovery_record_validation_result  # type: ignore[assignment]
    org_credential_vault.get_org_credential = _recovery_get_org_credential  # type: ignore[assignment]
    org_credential_vault.set_org_credential = _recovery_set_org_credential  # type: ignore[assignment]
    chat_model.get_runtime_auth_token = _recovery_get_runtime_auth_token  # type: ignore[assignment]
    _probe_provider_credential = _tracking_probe_ok
    try:
        # Premise check -- proves the setup genuinely reproduces the stuck state the review found
        # (not a vacuous test that would pass regardless): a plain re-read, no save, must already
        # report session_ready=False from the persisted False alone.
        stuck = asyncio.run(_org_settings_response())
        assert stuck.session_ready is False, f"premise check failed -- setup does not reproduce the stuck state: {stuck}"

        response = asyncio.run(put_org_settings_endpoint(
            OrgSettingsPutRequest(provider="claude", credential="sk-ant-fresh-good-key", updated_by="admin"),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
        assert response.session_ready is True, (
            f"a successful save must flip session_ready True immediately, not leave a stale "
            f"persisted False stuck for up to an hour: {response}"
        )
        assert fake_row.last_validation_ok is True, "record_validation_result(True) was never called on a successful save"
    finally:
        org_settings.get_org_settings = recovery_original_get_org_settings  # type: ignore[assignment]
        org_settings.set_org_settings = recovery_original_set_org_settings  # type: ignore[assignment]
        org_settings.record_validation_result = recovery_original_record_validation_result  # type: ignore[assignment]
        org_credential_vault.get_org_credential = recovery_original_get_org_credential  # type: ignore[assignment]
        org_credential_vault.set_org_credential = recovery_original_set_org_credential  # type: ignore[assignment]
        chat_model.get_runtime_auth_token = recovery_original_get_runtime_auth_token  # type: ignore[assignment]
        _probe_provider_credential = recovery_original_probe

    # provision_session's 409 backstop (I-2c) + its ordering (I-A fix round, Important, proved by
    # execution): an empty runtime_auth_token must fail fast -- before the Key Vault fetch, and
    # (the real regression the review caught by executing it) repo_scaffold.create_repo/
    # set_project_repo for a "+ New Project" Assign, which used to create a REAL GitHub repo and
    # record it against the project row before ever 409ing. Proven here by making EVERY one of
    # those calls raise if reached at all -- not just by catching the 409 itself, which a
    # differently-broken function could also raise, possibly after already doing real damage.
    #
    # session_store.get_session is deliberately NOT in that must-not-reach set any more (Phase E
    # audit I-3): it now runs before the credential check on purpose, a pure read used to prefer
    # this session's own stored provider over a live re-resolve -- stubbed below to return None
    # (a genuinely new session, same as this test's real premise) rather than asserting it's
    # unreached. chat_model.get_provider is stubbed too, so this offline check never needs a real
    # org_settings row to resolve chat_provider deterministically.
    global get_sandbox_provider

    class _ProvisionMustNotBeCalled:
        async def provision(self, **kwargs: Any) -> Any:
            raise AssertionError("provider.provision() must never be reached when the credential is empty")

    async def _stub_get_session_none(thread_id: str) -> dict[str, Any] | None:
        return None  # "no prior row" -- this test's own session_id has never been provisioned

    async def _must_not_reach_get_vault_uri(owner: str, repo: str, user_login: str) -> str | None:
        raise AssertionError("keyvault.get_vault_uri must never be reached when the credential is empty")

    async def _must_not_reach_get_project(project_id: str) -> dict[str, Any] | None:
        raise AssertionError("project_store.get_project must never be reached when the credential is empty")

    async def _must_not_reach_create_repo(name: str, github_token: str) -> dict[str, str]:
        raise AssertionError(
            "repo_scaffold.create_repo must never be reached when the credential is empty -- this "
            "is the exact regression I-A's fix closes (a real GitHub repo created before the 409)"
        )

    async def _stub_get_provider_fixed() -> str:
        return "claude"

    async def _stub_empty_runtime_auth_token(provider: str | None = None) -> tuple[str, str | None]:
        assert provider == "claude", (
            f"provision_session must resolve chat_provider (stored-or-live) BEFORE fetching the "
            f"credential, and pass that same value through -- got provider={provider!r}"
        )
        return "", None

    original_get_session3 = session_store.get_session
    original_get_vault_uri = keyvault.get_vault_uri
    original_get_project = project_store.get_project
    original_create_repo = repo_scaffold.create_repo
    original_get_sandbox_provider = get_sandbox_provider
    original_get_provider = chat_model.get_provider
    session_store.get_session = _stub_get_session_none  # type: ignore[assignment]
    keyvault.get_vault_uri = _must_not_reach_get_vault_uri  # type: ignore[assignment]
    project_store.get_project = _must_not_reach_get_project  # type: ignore[assignment]
    repo_scaffold.create_repo = _must_not_reach_create_repo  # type: ignore[assignment]
    chat_model.get_runtime_auth_token = _stub_empty_runtime_auth_token  # type: ignore[assignment]
    chat_model.get_provider = _stub_get_provider_fixed  # type: ignore[assignment]
    get_sandbox_provider = lambda: _ProvisionMustNotBeCalled()  # noqa: E731
    try:
        asyncio.run(provision_session(
            # A "+ New Project" shaped request (no owner/repo yet) -- the exact shape that used to
            # reach repo_scaffold.create_repo before this fix. If the 409 didn't actually fire
            # first, this would hit _must_not_reach_get_vault_uri immediately.
            ProvisionRequest(thread_id="t-selfcheck", project_id="p-selfcheck", owner="pending", repo="pending", branch="main"),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
        raise AssertionError("provision_session must 409 when no runtime auth credential is configured")
    except HTTPException as exc:
        assert exc.status_code == 409, exc.status_code
    finally:
        session_store.get_session = original_get_session3  # type: ignore[assignment]
        keyvault.get_vault_uri = original_get_vault_uri  # type: ignore[assignment]
        project_store.get_project = original_get_project  # type: ignore[assignment]
        repo_scaffold.create_repo = original_create_repo  # type: ignore[assignment]
        chat_model.get_runtime_auth_token = original_get_runtime_auth_token  # type: ignore[assignment]
        chat_model.get_provider = original_get_provider  # type: ignore[assignment]
        get_sandbox_provider = original_get_sandbox_provider

    # Phase E audit I-3, the actual fix (not just the fallback exercised above): a session that
    # already has a STORED provider must have that value reach both get_runtime_auth_token and
    # provider.provision -- never a live re-read, even when the live org setting has since
    # changed to something else. "copilot" below is deliberately the OPPOSITE of the stored
    # "claude" so a regression that fell back to live can't accidentally pass by coincidence, and
    # chat_model.get_provider is tracked too, proving it is never even CALLED (short-circuited by
    # `stored_provider or ...`), not merely overridden after the fact.
    from .sandbox.provider import SandboxSession

    i3_calls: list[tuple[str, str | None]] = []

    async def _i3_get_session_with_stored_provider(thread_id: str) -> dict[str, Any] | None:
        return {"project_id": "proj-i3-selfcheck", "status": "in_progress", "provider": "claude"}

    async def _i3_get_provider_live_disagrees() -> str:
        i3_calls.append(("get_provider", None))
        return "copilot"

    async def _i3_get_runtime_auth_token(provider: str | None = None) -> tuple[str, str | None]:
        i3_calls.append(("get_runtime_auth_token", provider))
        return "fake-token-i3", "api_key"

    async def _i3_get_vault_uri(owner: str, repo: str, user_login: str) -> str | None:
        return None  # no vault configured -- skip that branch entirely

    async def _i3_get_project(project_id: str) -> dict[str, Any] | None:
        assert project_id == "proj-i3-selfcheck", project_id
        return {"name": "i3-project", "repo": "already-connected-repo"}  # repo set -- no scaffolding

    class _I3FakeSandboxProvider:
        async def provision(self, **kwargs: Any) -> SandboxSession:
            i3_calls.append(("provider.provision", kwargs.get("provider")))
            return SandboxSession(kwargs["session_id"], "localhost", 0, "")

    async def _i3_set_session_provider_must_not_be_called(thread_id: str, provider: str) -> None:
        raise AssertionError(
            "set_session_provider must never be called when this session already has a stored "
            "provider -- Minor 3's backfill is for a NULL-provider legacy row only"
        )

    original_get_session_i3 = session_store.get_session
    original_get_provider_i3 = chat_model.get_provider
    original_get_runtime_auth_token_i3 = chat_model.get_runtime_auth_token
    original_get_vault_uri_i3 = keyvault.get_vault_uri
    original_get_project_i3 = project_store.get_project
    original_get_sandbox_provider_i3 = get_sandbox_provider
    original_set_session_provider_i3 = session_store.set_session_provider
    session_store.get_session = _i3_get_session_with_stored_provider  # type: ignore[assignment]
    chat_model.get_provider = _i3_get_provider_live_disagrees  # type: ignore[assignment]
    chat_model.get_runtime_auth_token = _i3_get_runtime_auth_token  # type: ignore[assignment]
    keyvault.get_vault_uri = _i3_get_vault_uri  # type: ignore[assignment]
    project_store.get_project = _i3_get_project  # type: ignore[assignment]
    session_store.set_session_provider = _i3_set_session_provider_must_not_be_called  # type: ignore[assignment]
    get_sandbox_provider = lambda: _I3FakeSandboxProvider()  # noqa: E731
    try:
        response = asyncio.run(provision_session(
            ProvisionRequest(
                thread_id="t-i3-selfcheck", owner="octocat", repo="already-connected-repo", branch="main",
            ),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
        assert response.status == "ready", response
    finally:
        session_store.get_session = original_get_session_i3  # type: ignore[assignment]
        chat_model.get_provider = original_get_provider_i3  # type: ignore[assignment]
        chat_model.get_runtime_auth_token = original_get_runtime_auth_token_i3  # type: ignore[assignment]
        keyvault.get_vault_uri = original_get_vault_uri_i3  # type: ignore[assignment]
        project_store.get_project = original_get_project_i3  # type: ignore[assignment]
        session_store.set_session_provider = original_set_session_provider_i3  # type: ignore[assignment]
        get_sandbox_provider = original_get_sandbox_provider_i3
        registry.pop("t-i3-selfcheck")

    assert i3_calls == [("get_runtime_auth_token", "claude"), ("provider.provision", "claude")], (
        f"the session's STORED provider ('claude') must reach both calls, and live get_provider() "
        f"must never even be invoked (it would have returned the disagreeing 'copilot') -- got {i3_calls}"
    )

    # Minor 3 (Phase E audit I-3 review): a legacy pre-0008 row (existing, but provider=None) must
    # get backfilled via set_session_provider with whatever chat_provider resolved to (the live
    # setting, since there's no stored value to prefer) -- so it stops resolving live on every
    # future reprovision instead of just this one.
    m3_calls: list[tuple[str, str]] = []

    async def _m3_get_session_null_provider(thread_id: str) -> dict[str, Any] | None:
        return {"project_id": "proj-m3-selfcheck", "status": "in_progress", "provider": None}

    async def _m3_get_provider_live() -> str:
        return "claude"

    async def _m3_get_runtime_auth_token(provider: str | None = None) -> tuple[str, str | None]:
        return "fake-token-m3", "api_key"

    async def _m3_get_vault_uri(owner: str, repo: str, user_login: str) -> str | None:
        return None

    async def _m3_get_project(project_id: str) -> dict[str, Any] | None:
        return {"name": "m3-project", "repo": "already-connected-repo"}

    async def _m3_set_session_provider(thread_id: str, provider: str) -> None:
        m3_calls.append((thread_id, provider))

    class _M3FakeSandboxProvider:
        async def provision(self, **kwargs: Any) -> SandboxSession:
            return SandboxSession(kwargs["session_id"], "localhost", 0, "")

    original_get_session_m3 = session_store.get_session
    original_get_provider_m3 = chat_model.get_provider
    original_get_runtime_auth_token_m3 = chat_model.get_runtime_auth_token
    original_get_vault_uri_m3 = keyvault.get_vault_uri
    original_get_project_m3 = project_store.get_project
    original_set_session_provider_m3 = session_store.set_session_provider
    original_get_sandbox_provider_m3 = get_sandbox_provider
    session_store.get_session = _m3_get_session_null_provider  # type: ignore[assignment]
    chat_model.get_provider = _m3_get_provider_live  # type: ignore[assignment]
    chat_model.get_runtime_auth_token = _m3_get_runtime_auth_token  # type: ignore[assignment]
    keyvault.get_vault_uri = _m3_get_vault_uri  # type: ignore[assignment]
    project_store.get_project = _m3_get_project  # type: ignore[assignment]
    session_store.set_session_provider = _m3_set_session_provider  # type: ignore[assignment]
    get_sandbox_provider = lambda: _M3FakeSandboxProvider()  # noqa: E731
    try:
        asyncio.run(provision_session(
            ProvisionRequest(
                thread_id="t-m3-selfcheck", owner="octocat", repo="already-connected-repo", branch="main",
            ),
            _FakeRequest(),  # type: ignore[arg-type]
        ))
    finally:
        session_store.get_session = original_get_session_m3  # type: ignore[assignment]
        chat_model.get_provider = original_get_provider_m3  # type: ignore[assignment]
        chat_model.get_runtime_auth_token = original_get_runtime_auth_token_m3  # type: ignore[assignment]
        keyvault.get_vault_uri = original_get_vault_uri_m3  # type: ignore[assignment]
        project_store.get_project = original_get_project_m3  # type: ignore[assignment]
        session_store.set_session_provider = original_set_session_provider_m3  # type: ignore[assignment]
        get_sandbox_provider = original_get_sandbox_provider_m3
        registry.pop("t-m3-selfcheck")

    assert m3_calls == [("t-m3-selfcheck", "claude")], (
        f"a legacy NULL-provider row must be stamped with the live-resolved chat_provider on "
        f"reprovision, got {m3_calls}"
    )

    print("sessions_api self-check: all assertions passed")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.sessions_api
    from src.sessions_api import _demo as _packaged_demo  # re-dispatch via package name, see project_store.py

    _packaged_demo()
