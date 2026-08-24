"""Deploy-time drain (Phase E audit I-5, Ruling E-2: option (b) -- accept orphaning, make it
legible instead of silent).

The Part 1 Spec asked an explicit question this branch never answered anywhere: what happens to a
session already mid-run (provisioned under the old TCP-session architecture, or just mid-flight
under this one) when a deploy restarts the agent process? The honest answer, unavoidable given this
pipeline's own architecture, is that it gets orphaned: `graph.py`'s compiled graph uses
`InMemorySaver` (see `GraphState.provider`'s own comment), which holds every run's `stages`,
`run_id`, and pinned `provider` in process memory ONLY -- a restart drops all of it, for every
in-flight thread, with nothing durable left to resume into. The sandbox container itself can
survive the restart (it is reaped on its own idle clock, independent of the agent process), so
without this module a user's ticket just sits there silently: no error, no banner, the board still
shows "in progress," and the only way to discover it is stale is to poke it and watch it fail
strangely on reattach.

This module is the "make it legible" half the Spec called for: at the moment BEFORE a deploy
replaces this process, walk every sandbox this process still has registered and, for whichever of
those sessions the DB still calls "in_progress", mark it failed with a plain, user-visible reason
("interrupted by deploy -- resubmit to retry") instead of leaving it to fail confusingly later. A
drain WINDOW (Spec option (a) -- stop accepting new sessions, wait for in-flight ones to finish) was
considered and rejected as the larger option: it needs a "stop accepting new work" flag this
codebase has no mechanism for today, for a benefit (zero interrupted runs) this option (b) doesn't
need either, since every interrupted run already has a clear, actionable, resubmit-and-retry path.

CRITICAL CAVEAT, found while building this (worth recording, not glossing over): `SandboxProvider.
list_active()` (both `LocalDockerProvider` and `AzureContainerInstanceProvider`) reports session_ids
from an IN-MEMORY dict scoped to the process that provisioned them -- it does not query Docker/ACI
directly. That means `drain()` can only ever see what THIS process itself provisioned. Run as a
freshly spawned, separate `python -m src.deploy_drain --run` process AFTER the old agent process has
already exited, it sees nothing (a new interpreter's provider starts with an empty registry) and
silently drains zero sessions -- which is exactly the kind of gate that "measures nothing while
looking healthy" this codebase's own retrospectives warn about. For this module to see anything real,
`drain()` must be called from INSIDE the process that is about to be replaced -- e.g. wired into that
process's own graceful-shutdown handling (a SIGTERM/shutdown-event hook in `main.py`, not built here:
out of this task's stated scope, and main.py has no shutdown hook of any kind today to hang it off
of) -- not invoked as an independent post-mortem script. The `--run` CLI entry point below is real
and correct for that in-process case; it is not a substitute for that wiring, and running it as a
detached step after the old process is already gone will do nothing.

Usage: `cd agent && uv run python -m src.deploy_drain` runs the offline self-check (safe default,
matches every other module in this package). `cd agent && uv run python -m src.deploy_drain --run`
performs a REAL drain against the live sandbox provider and session_store -- only meaningful when
invoked from inside the process being replaced, per the caveat above.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from . import session_store
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Plain, user-visible -- this is what a user sees as this session's failure_message (session
# detail page/board card), not a log line only an operator would ever read.
INTERRUPTED_BY_DEPLOY_MESSAGE = "interrupted by deploy -- resubmit to retry"


async def drain(
    provider: SandboxProvider,
    *,
    get_session: Callable[[str], Awaitable[dict[str, Any] | None]] = session_store.get_session,
    close_session: Callable[..., Awaitable[None]] = session_store.close_session,
) -> list[str]:
    """Marks every still-`in_progress` session whose sandbox `provider` currently has registered
    as `failed`, with `INTERRUPTED_BY_DEPLOY_MESSAGE` as the reason. Returns the session_ids
    actually marked (a session `list_active()` names that is already terminal, or that
    session_store has no row for at all, is left untouched and simply not included).

    get_session/close_session default to the real session_store functions -- overridable so this
    module's own self-check (_demo below) can prove the SELECTION and MESSAGE logic without a real
    DB or a real sandbox provider; `provider` has no default for the same reason (see this
    module's own docstring for why a real deploy invocation needs the CALLER's already-running
    provider instance, not a freshly constructed one).

    Deliberately narrow: only a session still `in_progress` is touched. A live sandbox attached to
    an already-completed/failed/rejected session (its container just hasn't been reaped yet) is
    left exactly as it is -- this is a rescue for genuinely orphaned in-flight work, not a blanket
    status stamp over every row a sandbox happens to still exist for.
    """
    session_ids = await provider.list_active()
    marked: list[str] = []
    for session_id in session_ids:
        row = await get_session(session_id)
        if row is None:
            logger.warning(
                "deploy_drain: sandbox is live for session_id=%s but session_store has no row for "
                "it -- skipping (nothing to mark)", session_id,
            )
            continue
        if row.get("status") != "in_progress":
            continue
        await close_session(
            session_id,
            run_id=row.get("run_id"),
            status="failed",
            failure={
                "stage": row.get("current_stage"),
                "type": "interrupted_by_deploy",
                "feedback": INTERRUPTED_BY_DEPLOY_MESSAGE,
            },
        )
        marked.append(session_id)
        logger.info("deploy_drain: marked session_id=%s as failed (%s)", session_id, INTERRUPTED_BY_DEPLOY_MESSAGE)
    return marked


def _demo() -> None:
    """Offline self-check: a fake provider + fake session_store functions, no real DB or Docker --
    proves the SELECTION (in_progress only, tolerate a missing row) and the MESSAGE (status/type/
    feedback shape close_session actually receives), which is this module's own real logic. The
    I/O underneath (a real SandboxProvider, a real dbo.sessions row) is exactly what
    session_store.py's/local_docker.py's own self-checks already cover -- not re-proven here."""
    import asyncio

    class _FakeProvider:
        async def list_active(self) -> list[str]:
            return ["live-in-progress", "live-but-completed", "live-but-no-row"]

    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_get_session(session_id: str) -> dict[str, Any] | None:
        return {
            "live-in-progress": {"status": "in_progress", "run_id": "r1", "current_stage": "plan"},
            "live-but-completed": {"status": "completed", "run_id": "r2", "current_stage": "metrics-exit"},
            # "live-but-no-row" deliberately absent -- a sandbox the provider knows about but
            # session_store has no row for (e.g. a bare spike sandbox with no real session).
        }.get(session_id)

    async def _fake_close_session(session_id: str, **kwargs: Any) -> None:
        calls.append((session_id, kwargs))

    marked = asyncio.run(
        drain(_FakeProvider(), get_session=_fake_get_session, close_session=_fake_close_session)  # type: ignore[arg-type]
    )

    assert marked == ["live-in-progress"], (
        f"only the genuinely in_progress session must be marked -- an already-completed session "
        f"or a sandbox with no session row at all must be left alone, got {marked}"
    )
    assert len(calls) == 1, f"close_session must be called exactly once, got {calls}"
    session_id, kwargs = calls[0]
    assert session_id == "live-in-progress", session_id
    assert kwargs["run_id"] == "r1", kwargs
    assert kwargs["status"] == "failed", kwargs
    assert kwargs["failure"]["type"] == "interrupted_by_deploy", kwargs
    assert kwargs["failure"]["stage"] == "plan", kwargs
    assert kwargs["failure"]["feedback"] == INTERRUPTED_BY_DEPLOY_MESSAGE, kwargs

    print("deploy_drain self-check: all assertions passed")


async def _run_for_real() -> None:  # pragma: no cover -- exercises the real provider/DB, not offline-safe
    from .sandbox.factory import get_sandbox_provider

    marked = await drain(get_sandbox_provider())
    if marked:
        print(f"deploy_drain: marked {len(marked)} session(s) as interrupted-by-deploy: {', '.join(marked)}")
    else:
        print("deploy_drain: no in-progress sessions found on this process's live sandbox registry")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.deploy_drain [--run]
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO)
    if "--run" in sys.argv[1:]:
        # See this module's own docstring CAVEAT before wiring this into a real deploy step --
        # it only sees what THIS process provisioned, so it must run inside the process being
        # replaced, not as a separately spawned post-mortem script.
        asyncio.run(_run_for_real())
    else:
        # Default (no args), same convention as every other module in this package
        # (session_store.py, project_store.py, model_config.py, ...): run the offline self-check.
        # Re-dispatched through the PACKAGE name so this module isn't imported twice under two
        # different sys.modules identities, same reason those other modules' own __main__ blocks do.
        from src.deploy_drain import _demo as _packaged_demo

        _packaged_demo()
