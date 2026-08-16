"""Headless full-graph runner: executes the entire pipeline for (owner, repo, branch) with no
frontend. Spec/plan gates are auto-approved; a greenfield repo's tech-stack picker (the graph's
one other interrupt) auto-selects via --greenfield-stack/AIDW_GREENFIELD_STACK instead of pausing,
or the repo is rejected if neither is set. Clarifying questions are disallowed via the
AIDW_HEADLESS draft-prompt injection (graph.py make_draft_node); an exhausted deterministic gate
ENDs the run with `run_failure` set, which lands in the report.

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("run_headless")

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from src import copilot_chat_model, git_ops  # noqa: E402
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
        help="auto-select this canned stack (agent/src/templates/tech_stacks/*.md filename stem, "
        "e.g. nextjs-fastapi) when a blank repository is offered the greenfield picker, instead of "
        "rejecting it -- sets AIDW_GREENFIELD_STACK, which app_discovery.py's decide/select nodes "
        "read. Headless has no interrupt to answer, so a blank repo without this flag is rejected "
        "exactly as before.",
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

    # uuid chars FIRST: entrypoint.sh suffixes the remote work branch with the first 8 chars of
    # the session id -- a constant prefix would collapse every headless run onto one shared,
    # force-pushed branch that also rehydrates the previous run's state.
    thread_id = args.thread or uuid.uuid4().hex[:12]
    if args.thread:
        os.environ["AIDW_RESUME"] = "1"
        logger.info("resuming thread %s -- approved stages will be skipped", thread_id)
    if args.greenfield_stack:
        os.environ["AIDW_GREENFIELD_STACK"] = args.greenfield_stack
        logger.info("greenfield auto-select armed: stack_id=%s", args.greenfield_stack)
    cfg = {"configurable": {"thread_id": thread_id}}
    started = time.monotonic()

    provider = get_sandbox_provider()
    logger.info("provisioning sandbox for %s/%s@%s (thread %s)", args.owner, args.repo, args.branch, thread_id)
    session = await provider.provision(
        session_id=thread_id,
        repo_clone_url=f"https://github.com/{args.owner}/{args.repo}.git",
        branch=args.branch,
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
                    app_rejection=values.get("app_rejection"),
                    run_failure=values.get("run_failure"),
                    needs_clarification=stuck,
                    ok=statuses.get("exit") == "approved",
                )
                break

            interrupts = snap.interrupts or tuple(i for task in snap.tasks for i in task.interrupts)
            if not interrupts:
                logger.error("graph paused with no interrupt -- aborting")
                outcome.update(stage_statuses=_stage_statuses(snap.values), error="paused_without_interrupt")
                break
            payload = interrupts[0].value if isinstance(interrupts[0].value, dict) else {}
            # Spec/plan approval gates are the only interrupts this loop ever actually resumes.
            # The greenfield stack picker's own interrupt() never fires here: AIDW_GREENFIELD_STACK
            # set auto-selects a stack without calling interrupt() at all; unset, a blank repo is
            # rejected outright instead of ever being offered the picker (app_discovery.py's
            # headless_blocked guard). Failures END the run with run_failure instead of pausing.
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
