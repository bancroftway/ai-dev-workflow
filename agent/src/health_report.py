"""On-demand "Generate Code Health Report" job orchestration (health_report_api.py).

A plain async function, deliberately NOT a LangGraph StageSpec/node: this never touches
graph.py's checkpointer, ledger or GraphState -- it only needs a sandbox to provision, a scan to
run, and a container to tear down again. repo_scan.run_repo_scan/ScanReport.to_dashboard_dict()
are pure in-process once tool output is parsed, so nothing is read back from the sandbox after
`_teardown` below -- safe to kill the container immediately.
"""

from __future__ import annotations

import json
import logging

from . import health_report_store, repo_files, repo_scan
from .sandbox import get_sandbox_provider

logger = logging.getLogger(__name__)


async def _teardown(job_id: str) -> None:
    """Full cleanup (container AND its named workspace volume) -- not `provider.terminate`,
    which only stops the container and deliberately leaves the workspace volume for a future
    resume (see LocalDockerProvider.terminate's own docstring). A report job never resumes, so
    leaving that volume behind would be a pure leak.

    Force-remove by name rather than depending on the provider's in-memory registry: this same
    call is also what reap_orphaned_jobs uses post-restart, when that registry is guaranteed
    empty.

    # ponytail: LocalDockerProvider is the only SandboxProvider that overrides
    # discard_workspace -- AzureContainerInstanceProvider falls back to the base no-op, so a
    # report job's ACI container is NOT actually torn down under SANDBOX_PROVIDER=azure today.
    # Add AzureContainerInstanceProvider.discard_workspace (name/label-based delete, matching its
    # own `terminate`) before enabling this feature there.
    """
    try:
        await get_sandbox_provider().discard_workspace(job_id)
    except Exception:  # noqa: BLE001 -- teardown is a cleanup courtesy; never mask the real outcome
        logger.warning("health report job %s: sandbox teardown failed", job_id, exc_info=True)


async def run_health_report(
    job_id: str,
    *,
    owner: str,
    repo: str,
    branch: str,
    git_user_token: str,
    runtime_auth_token: str,
    runtime_auth_kind: str | None,
    chat_provider: str,
) -> None:
    provider = get_sandbox_provider()
    await health_report_store.mark_running(job_id)
    try:
        # branch/work_branch both the selected branch: this job never pushes, so there is no
        # separate work branch to check out (unlike a real session -- see branch_naming.py).
        await provider.provision(
            session_id=job_id,
            repo_clone_url=f"https://github.com/{owner}/{repo}.git",
            branch=branch,
            work_branch=branch,
            git_user_token=git_user_token,
            runtime_auth_token=runtime_auth_token,
            runtime_auth_kind=runtime_auth_kind,
            provider=chat_provider,
        )
        gitleaks_stopwords, gitleaks_allow_paths = await repo_scan.org_gitleaks_allowlist()
        report = await repo_scan.run_repo_scan(
            provider, job_id, tools=repo_scan.PROFILES["health_report"],
            gitleaks_extra_stopwords=gitleaks_stopwords, gitleaks_extra_allow_paths=gitleaks_allow_paths,
        )
        dashboard = report.to_dashboard_dict()

        # Full SBOM capture: run_repo_scan already wrote syft's raw CycloneDX output to
        # agent-work/sbom.json inside the sandbox (repo_scan.py's run_repo_scan, syft branch),
        # but only via write_repo_file into the git checkout -- meant for a session's own PR
        # branch. This job pushes nothing and its container disappears right after, so read the
        # full component list directly, before teardown, or it's lost; to_dashboard_dict()'s own
        # `metrics` blob only carries syft's small summary, not the full per-component table.
        sbom_raw = await repo_files.read_repo_file(provider, job_id, "agent-work/sbom.json")
        if sbom_raw:
            try:
                dashboard["sbom"] = json.loads(sbom_raw)
            except json.JSONDecodeError:
                logger.warning("health report job %s: agent-work/sbom.json did not parse", job_id)

        await health_report_store.mark_completed(job_id, dashboard)
    except Exception as exc:  # noqa: BLE001 -- record the failure; never leave the job stuck
        logger.exception("health report job %s failed", job_id)
        await health_report_store.mark_failed(job_id, str(exc))
    finally:
        await _teardown(job_id)


async def reap_orphaned_jobs() -> None:
    """Called once at process startup (main.py's lifespan), before any request is served.

    A report job's `try/finally` in run_health_report only tears down its container if the
    *process itself* survives to run it -- LocalDockerProvider's in-memory sandbox registry is
    wiped on every restart, and unlike a session, a report job has no resume/reopen UI that would
    ever call `provider.provision()` again for its job_id (the one path that would otherwise
    reattach and eventually let a future close tear it down). A mid-scan restart (an ordinary
    deploy) would otherwise orphan a `--rm` container forever and leave its `dbo.health_reports`
    row stuck `running`.

    Every row `list_active_jobs` returns predates this process (nothing has had a chance to
    create one yet), so each is orphaned by definition -- force-remove its container (and
    workspace volume) by job_id and mark it failed, regardless of whether that container is
    still actually running (idempotent: a no-op if it already exited on its own).
    """
    jobs = await health_report_store.list_active_jobs()
    for job in jobs:
        job_id = job["job_id"]
        logger.warning("health report job %s orphaned by a process restart -- reaping", job_id)
        await _teardown(job_id)
        await health_report_store.mark_failed(job_id, "orphaned: agent process restarted before job completed")
