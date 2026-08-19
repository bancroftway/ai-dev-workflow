"""Headless full-graph runner: executes the entire pipeline for (owner, repo, branch) with no
frontend. Every gate auto-approves, including the Tech Stack tab: --greenfield-stack picks one of
the canned catalog stacks for it (its interrupt is otherwise resumed with the detect-pass's own
draft, same as every other gate). There is no more hard-rejection path -- an empty repo without
--greenfield-stack simply proceeds with whatever the detect pass honestly found (a sparse draft).
Clarifying questions are disallowed via the AIDW_HEADLESS draft-prompt injection (graph.py
make_draft_node); an exhausted deterministic gate ENDs the run with `run_failure` set, which lands
in the report.

Runs IN-PROCESS on purpose: the sandbox registry, push-token map, and the graph's InMemorySaver
checkpointer are all process-local, so provisioning and the graph must share one process.

    cd agent && uv run python run_headless.py <owner> <repo> <branch> \
        --requirements-file req.md [--discard-sandbox]

Expectation: a full run is 25-40+ LLM calls -- hours of wall time and real Copilot spend.
Docker Desktop and both tokens must survive the whole window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())  # tokens live in the repo-root .env, one level above agent/
os.environ.setdefault("AIDW_HEADLESS", "1")
os.environ.setdefault("AIDW_SANDBOX_IDLE_TIMEOUT", "86400")  # belt-and-suspenders: _touch_sandbox() already keeps a silent turn's sandbox alive; this just widens the margin

logging.basicConfig(level=os.environ.get("AIDW_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("run_headless")

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from src import app_discovery, branch_naming, copilot_chat_model, git_ops  # noqa: E402
from src.graph import graph  # noqa: E402
from src.sandbox import get_sandbox_provider, registry  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner")
    parser.add_argument("repo")
    parser.add_argument("branch")
    parser.add_argument("--requirements-file", required=True, help="markdown file with the requirements text")
    parser.add_argument("--discard-sandbox", action="store_true", help="terminate container + delete workspace volume at the end")
    parser.add_argument(
        "--thread",
        default=None,
        help="resume an earlier run's thread id: reattaches its sandbox/volume and skips every "
        "stage already APPROVED there (sets AIDW_RESUME=1). Stages mid-flight redraft.",
    )
    parser.add_argument(
        "--greenfield-stack",
        default=None,
        metavar="STACK_ID",
        help="when the run reaches the Tech Stack tab's gate, resume it with this canned stack's "
        "markdown (agent/src/templates/tech_stacks/*.md filename stem, e.g. nextjs-fastapi) "
        "instead of whatever the detect pass drafted -- for repeatable canned-scenario testing "
        "against a blank repository. Without this flag, the gate just auto-approves the detect "
        "pass's own draft, same as every other gate.",
    )
    return parser.parse_args()


def _stage_statuses(values: dict) -> dict[str, str]:
    return {key: (stage or {}).get("status") for key, stage in (values.get("stages") or {}).items()}


async def run(args: argparse.Namespace) -> int:
    requirements = Path(args.requirements_file).read_text(encoding="utf-8")
    if not requirements.strip():
        logger.error("requirements file is empty")
        return 2

    git_token = os.environ.get("E2E_GITHUB_TOKEN", "")
    copilot_token = os.environ.get("GITHUB_TOKEN", "")
    if not git_token or not copilot_token:
        logger.error("E2E_GITHUB_TOKEN and GITHUB_TOKEN must both be set (root .env)")
        return 2
    if git_token.startswith("github_pat_"):
        # Fine-grained PATs are often read-only; a pushless run silently loses all output.
        logger.warning("E2E_GITHUB_TOKEN looks fine-grained -- confirm it has PUSH (contents: write). A read-only token voids every stage-end push.")

    # full canonical uuid4 string, not a truncated .hex -- session_store's `sessions.session_id`
    # column is SQL Server `uniqueidentifier`; a short hex-only string fails that conversion
    # (matches the format session_store.create_session mints for API-driven sessions).
    thread_id = args.thread or str(uuid.uuid4())
    if args.thread:
        os.environ["AIDW_RESUME"] = "1"
        logger.info("resuming thread %s -- approved stages will be skipped", thread_id)
    greenfield_stack_markdown: str | None = None
    if args.greenfield_stack:
        catalog = app_discovery.load_stack_catalog()
        match = next((entry for entry in catalog if entry["id"] == args.greenfield_stack), None)
        if match is None:
            valid = ", ".join(entry["id"] for entry in catalog)
            logger.error("--greenfield-stack %r is not a known stack id. Valid: %s", args.greenfield_stack, valid)
            return 2
        greenfield_stack_markdown = match["markdown"]
        logger.info("greenfield auto-select armed: stack_id=%s", args.greenfield_stack)
    cfg = {"configurable": {"thread_id": thread_id}}
    started = time.monotonic()

    provider = get_sandbox_provider()
    logger.info("provisioning sandbox for %s/%s@%s (thread %s)", args.owner, args.repo, args.branch, thread_id)
    session = await provider.provision(
        session_id=thread_id,
        repo_clone_url=f"https://github.com/{args.owner}/{args.repo}.git",
        branch=args.branch,
        work_branch=branch_naming.work_branch_for(thread_id),
        git_user_token=git_token,
        copilot_auth_token=copilot_token,
    )
    registry.set(thread_id, session)
    git_ops.set_push_token(thread_id, git_token)

    outcome: dict = {"thread_id": thread_id, "ok": False}
    try:
        stream_input: object = {"messages": [HumanMessage(content=requirements)]}
        while True:
            async for chunk in graph.astream(stream_input, config=cfg, stream_mode="updates"):
                for node_name in chunk:
                    if node_name != "__interrupt__":
                        logger.info("node done: %s", node_name)

            snap = await graph.aget_state(cfg)
            if not snap.next:
                values = snap.values
                statuses = _stage_statuses(values)
                stuck = [k for k, s in statuses.items() if s == "needs_clarification"]
                outcome.update(
                    stage_statuses=statuses,
                    run_failure=values.get("run_failure"),
                    needs_clarification=stuck,
                    # "metrics-exit", not "exit": the pipeline consolidated its final stage and
                    # this success check kept reading the old key, so a fully-approved run with
                    # no run_failure still reported ok=false. Guard on run_failure too -- a stage
                    # can be approved while the run recorded a terminal problem elsewhere.
                    ok=statuses.get("metrics-exit") == "approved" and values.get("run_failure") is None,
                )
                # A stage status can be APPROVED purely from hydration -- restored from a previous
                # run's metrics-exit.approved.json in the repo. When that happens the stage
                # short-circuits, exit_finalize never runs, and the exit report left on the branch
                # belongs to an earlier run: observed live, a run that had just re-run e2e and
                # metrics reported ok=true while the committed report still read "no e2e
                # screenshots were captured" from 40 minutes earlier. Proof of THIS run finishing is
                # its own exit report, which only exit_finalize writes.
                run_id = values.get("run_id")
                if outcome["ok"] and run_id:
                    probe = await provider.exec_in_sandbox(
                        thread_id, f"ls .ai-dev-workflow/history/{run_id}-exit.md 2>/dev/null"
                    )
                    if not (probe.stdout or "").strip():
                        outcome["ok"] = False
                        outcome["error"] = (
                            f"exit_finalize did not run this run (no history/{run_id}-exit.md): the "
                            "final stage was approved from hydrated state, so the exit report, "
                            "manifest and screenshots on the branch are a previous run's."
                        )
                break

            interrupts = snap.interrupts or tuple(i for task in snap.tasks for i in task.interrupts)
            if not interrupts:
                logger.error("graph paused with no interrupt -- aborting")
                outcome.update(stage_statuses=_stage_statuses(snap.values), error="paused_without_interrupt")
                break
            payload = interrupts[0].value if isinstance(interrupts[0].value, dict) else {}
            # Every gate auto-approves. The Tech Stack tab's gate is the one exception: with
            # --greenfield-stack set, resume it with that canned stack's markdown (matching
            # preflight_nodes.resolve_tech_stack_submission's expected {"markdown": ...} shape) --
            # without it, a bare resume=True falls through to the detect pass's own draft, same as
            # every other gate. There is no more hard-rejection path to guard against here.
            if payload.get("stage") == "tech-stack" and greenfield_stack_markdown is not None:
                logger.info("resuming tech-stack gate with canned stack %s", args.greenfield_stack)
                stream_input = Command(resume={"markdown": greenfield_stack_markdown})
            else:
                logger.info("auto-approving gate: %s", payload.get("stage"))
                stream_input = Command(resume=True)
    finally:
        try:
            await copilot_chat_model.close_thread_session(thread_id)
        except Exception:  # noqa: BLE001 -- teardown must not mask the run outcome
            logger.warning("close_thread_session failed", exc_info=True)
        if args.discard_sandbox:
            await provider.terminate(thread_id)
            await provider.discard_workspace(thread_id)

    outcome["wall_seconds"] = round(time.monotonic() - started, 1)
    report_dir = Path(__file__).parent / "agent-work"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"headless-{thread_id}.json"
    report_path.write_text(json.dumps(outcome, indent=2, default=str), encoding="utf-8")
    print(json.dumps(outcome, indent=2, default=str))
    logger.info("report written to %s", report_path)
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run(_parse_args())))
