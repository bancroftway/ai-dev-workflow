"""test-hardening -- full test suite + flake quarantine. A deterministic run node (with retries) + one
narrow read-only LLM node for judgment (filing tickets), exactly as scoped in the plan, nothing
more.

Chain: test_hardening_run_tests (deterministic, retries) -> route(any stable_fail -> test_hardening_regression_gate
[terminal run_failure, out of test-hardening's own scope to resolve] | else -> test_hardening_flake_triage) ->
test_hardening_flake_triage (read-only) -> test_hardening_mint_tickets (deterministic: allocates real US-#### ids via
spec_ledger.py, never the LLM) -> test_hardening_exit_check (deterministic) -> route(pass -> next | fail ->
test_hardening_exit_escalate [should not normally happen, since mint_tickets always links every entry --
included so this stage never silently proceeds on an unexpected gap]).

Verification status: NOT exercised against a real sandbox, same caveat as quality-remediation/security-remediation/audit-cluster. trx (.NET)
and vitest-json (JS/TS) parsing are both written from documented schema shape, not confirmed live.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from . import config as workflow_config
from . import git_ops, model_config, repo_files, run_failure, spec_ledger, stack_runner, test_results
from .chat_model import ainvoke_structured, get_chat_model_for_thread
from .prompt_loader import load_prompt, load_prompt_pair, render_prompt
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .schemas import StageReport
from .schemas_test_hardening import FlakeTriageResponse

logger = logging.getLogger(__name__)

TEST_HARDENING_TOTAL_ATTEMPTS = int(os.environ.get("TEST_HARDENING_TOTAL_ATTEMPTS", "3"))  # 1 initial + 2 retries
FLAKE_QUARANTINE_PATH = ".ai-dev-workflow/test_hardening/flake-quarantine.json"

# Placeholder the discovery agent leaves in its command/result path; Python substitutes the
# attempt number so repeated runs don't overwrite each other's result files.
_ATTEMPT_TOKEN = "__ATTEMPT__"


class TestCommandReport(StageReport):
    """What the test-command discovery agent must report (prompts/test_hardening_run.md)."""

    command: str = ""
    result_path: str = ""
    format: str = ""


FLAKE_TRIAGE_SYSTEM_PROMPT = load_prompt("test_hardening_flake_triage")


class TestHardeningState(TypedDict):
    attempt: int
    test_outcomes: dict[str, list[str]]  # test_name -> ["pass"|"fail", ...] across attempts
    stable_fail: list[str]
    flaky: list[str]
    flake_quarantine: dict[str, dict[str, Any]]  # test_name -> {ticket_id, title, narrative}
    last_exit_ok: bool
    cannot_verify: bool  # sandbox missing at run time -- the suite never ran, escalate not pass
    fix_attempt: int  # stable-regression fix laps used (test_hardening_fix_node)


def default_test_hardening_state() -> TestHardeningState:
    return {"attempt": 0, "fix_attempt": 0, "test_outcomes": {}, "stable_fail": [], "flaky": [], "flake_quarantine": {}, "last_exit_ok": False, "cannot_verify": False}


# Moved to test_results.py: repo_scan's eval layer needs the same parsers, and three copies of
# "what did the suite actually do" is how the three drift apart. Aliased rather than
# call-site-renamed so the diff stays reviewable.
_parse_trx = test_results.parse_trx
_parse_vitest_json = test_results.parse_vitest_json
_repo_relative = test_results.repo_relative


async def test_hardening_run_tests_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    test_hardening = dict(state.get("test_hardening") or default_test_hardening_state())
    if sandbox_registry.get(thread_id) is None:
        # No sandbox means the test suite could not run. Escalate to a human rather than treat an
        # unrun suite as green (route reads cannot_verify).
        test_hardening["last_exit_ok"] = False
        test_hardening["cannot_verify"] = True
        return {"test_hardening": test_hardening}

    provider = get_sandbox_provider()
    # Clear the sticky no-sandbox flag: it survives END-terminated runs in the checkpoint, and
    # the router checks it FIRST -- without this a healthy resubmit insta-fails.
    test_hardening["cannot_verify"] = False

    await provider.exec_in_sandbox(thread_id, "mkdir -p agent-work")

    # GHCP discovers and PROVES the test command once (canned per-stack commands guessed the wrong
    # root on generated monorepos); Python then repeats that exact command N times. The repetition
    # deliberately stays here: flake detection compares runs of an IDENTICAL command, and a model
    # re-deciding the invocation each round would make the comparison meaningless.
    discovery = await stack_runner.run_and_report(
        thread_id,
        stage_key="test-hardening-run",
        prompt_name="test_hardening_run",
        schema=TestCommandReport,
        attempt_token=_ATTEMPT_TOKEN,
    )
    if not discovery.success or not discovery.command or _ATTEMPT_TOKEN not in discovery.result_path:
        # No usable command: treat as "nothing to harden" exactly as an unmapped stack did before,
        # rather than blocking the pipeline on a flake check that cannot run.
        logger.warning("test-hardening: no usable test command discovered (%s)", discovery.error)
        test_hardening["last_exit_ok"] = True
        return {"test_hardening": test_hardening}

    outcomes: dict[str, list[str]] = {}
    for attempt in range(TEST_HARDENING_TOTAL_ATTEMPTS):
        command = discovery.command.replace(_ATTEMPT_TOKEN, str(attempt))
        result_path = _repo_relative(discovery.result_path.replace(_ATTEMPT_TOKEN, str(attempt)))
        if result_path is None:
            # A path outside the repo is a bad report, not a reason to kill the run. Previously the
            # reported '/workspace/repo/test-results-0/test-results-0.trx' reached read_repo_file
            # verbatim and its ValueError propagated out of the node, ending an otherwise healthy
            # run with a stack trace instead of a stage result.
            logger.warning(
                "test-hardening: reported result_path %r is not inside the repo -- skipping flake check",
                discovery.result_path,
            )
            test_hardening["last_exit_ok"] = True
            return {"test_hardening": test_hardening}
        await provider.exec_in_sandbox(thread_id, command)
        raw_result = await repo_files.read_repo_file(provider, thread_id, result_path)
        if raw_result is None:
            continue
        per_test = (
            _parse_trx(raw_result) if discovery.format == "trx" or result_path.endswith(".trx")
            else _parse_vitest_json(raw_result)
        )
        for test_name, outcome in per_test.items():
            outcomes.setdefault(test_name, []).append(outcome)

    stable_fail = [name for name, results in outcomes.items() if results and all(r == "fail" for r in results)]
    flaky = [name for name, results in outcomes.items() if len(set(results)) > 1]

    test_hardening["test_outcomes"] = outcomes
    test_hardening["stable_fail"] = stable_fail
    test_hardening["flaky"] = flaky
    for name in flaky:
        test_hardening["flake_quarantine"].setdefault(name, {"linked_id": None, "ticket_title": "", "ticket_narrative": ""})

    await repo_files.write_repo_file(provider, thread_id, FLAKE_QUARANTINE_PATH, json.dumps(test_hardening["flake_quarantine"], indent=2) + "\n")
    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "test_hardening", "node": "run_tests", "stable_fail_count": len(stable_fail), "flaky_count": len(flaky)}
    )
    return {"test_hardening": test_hardening}


def make_test_hardening_route_after_run() -> Any:
    def route(state: dict[str, Any]) -> str:
        test_hardening = state.get("test_hardening") or default_test_hardening_state()
        if test_hardening.get("cannot_verify"):
            return "regression"  # no sandbox -- the suite never ran; a human must see it
        if test_hardening.get("last_exit_ok") and not test_hardening.get("test_outcomes"):
            return "triage"  # no test-command mapping for this stack -- nothing to gate on
        if test_hardening["stable_fail"]:
            # The loop's job is to FIX the regression, not to end the run (user directive
            # 2026-08-21) -- observed live: an e2e fix lap left a frontend api migration
            # half-finished, 5 unit tests went stably red, and the run ended with nothing in the
            # pipeline empowered to repair it. The cap keeps a truly-stuck loop bounded.
            if test_hardening.get("fix_attempt", 0) < workflow_config.TEST_HARDENING_MAX_FIX_CYCLES:
                return "fix"
            return "regression"
        return "triage"

    return route


async def test_hardening_fix_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """One bounded repair lap for a STABLE test regression, mirroring e2e_fix_node: fresh Copilot
    session per lap (a reused session's history eventually answers "already addressed" and edits
    nothing -- measured on e2e's loop), autopilot write access, then back to run_tests for the
    deterministic re-check."""
    thread_id = config["configurable"]["thread_id"]
    test_hardening = dict(state.get("test_hardening") or default_test_hardening_state())
    if sandbox_registry.get(thread_id) is None:
        return {"test_hardening": test_hardening}

    provider = get_sandbox_provider()
    system, template = load_prompt_pair("test_hardening_fix")
    prompt = render_prompt(template, stable_fail_json=json.dumps(test_hardening.get("stable_fail") or [], indent=2))
    model = get_chat_model_for_thread(
        thread_id,
        "test-hardening",
        f"fix-{state.get('run_id', 'run')}-{test_hardening.get('fix_attempt', 0) + 1}",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("e2e", "draft", state["provider"]) or model_config.get_model_name("minimal-code-to-green", "draft", state["provider"]),
        sandbox=sandbox_registry.get(thread_id),
        agent_mode="autopilot",
    )
    timed_out = ""
    try:
        await model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])
    except (TimeoutError, RuntimeError) as exc:  # a failed session is a failed lap, not a dead run -- e2e_fix's rule
        timed_out = str(exc)
        logger.warning("test-hardening fix lap lost to a Copilot session failure, counting the lap: %s", exc)

    test_hardening["fix_attempt"] = test_hardening.get("fix_attempt", 0) + 1
    entry: dict[str, Any] = {"stage": "test_hardening", "node": "fix", "attempt": test_hardening["fix_attempt"]}
    if timed_out:
        entry["timeout"] = timed_out
    await repo_files.append_ledger_entry(provider, thread_id, entry)
    await git_ops.commit_all(provider, thread_id, "ai-dev-workflow: test regression fix cycle")
    return {"test_hardening": test_hardening}


async def test_hardening_regression_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """A real regression (consistently failing across every attempt) is out of test-hardening's own
    scope to resolve -- the run ENDs with run_failure set (no human pause; fix out-of-band and
    resubmit)."""
    thread_id = config["configurable"]["thread_id"]
    test_hardening = state.get("test_hardening") or default_test_hardening_state()
    if test_hardening.get("cannot_verify"):
        payload = {"stage": "test_hardening", "type": "cannot_verify", "reason": "no sandbox -- test suite did not run"}
        detail = payload["reason"]
    else:
        payload = {"stage": "test_hardening", "type": "stable_test_regression", "stable_fail": test_hardening["stable_fail"]}
        detail = json.dumps(payload["stable_fail"])
    payload = await run_failure.record_run_failure_and_reset(
        thread_id, state.get("run_id"), payload=payload, detail_for_classification=detail,
    )
    reset = dict(test_hardening)
    reset["cannot_verify"] = False
    return {"test_hardening": reset, "run_failure": payload}


async def test_hardening_flake_triage_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    test_hardening = dict(state.get("test_hardening") or default_test_hardening_state())
    untriaged = [name for name, entry in test_hardening["flake_quarantine"].items() if entry.get("linked_id") is None and not entry.get("ticket_title")]
    if not untriaged or sandbox_registry.get(thread_id) is None:
        return {"test_hardening": test_hardening}

    model = get_chat_model_for_thread(
        thread_id,
        "test-hardening-flake-triage",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("test-hardening-flake-triage", "draft", state["provider"]),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=workflow_config.READ_ONLY_AVAILABLE_TOOLS,
    )
    prompt = f"Flaky tests needing triage: {json.dumps(untriaged)}"
    response = await ainvoke_structured(
        model, [SystemMessage(content=FLAKE_TRIAGE_SYSTEM_PROMPT), HumanMessage(content=prompt)], FlakeTriageResponse
    )
    quarantine = dict(test_hardening["flake_quarantine"])
    for decision in response.decisions:
        entry = dict(quarantine.get(decision.test_name, {"linked_id": None, "ticket_title": "", "ticket_narrative": ""}))
        if decision.likely_duplicate_of:
            entry["linked_id"] = decision.likely_duplicate_of
        else:
            entry["ticket_title"] = decision.new_ticket_title
            entry["ticket_narrative"] = decision.new_ticket_narrative
        quarantine[decision.test_name] = entry
    test_hardening["flake_quarantine"] = quarantine
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "test_hardening", "node": "flake_triage", "token_usage": model._last_usage})
    return {"test_hardening": test_hardening}


async def test_hardening_mint_tickets_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Deterministic: allocates the actual US-#### id for every net-new ticket the triage node
    proposed -- never LLM-authored, per spec_ledger.py's own allocation contract."""
    thread_id = config["configurable"]["thread_id"]
    test_hardening = dict(state.get("test_hardening") or default_test_hardening_state())
    if sandbox_registry.get(thread_id) is None:
        return {"test_hardening": test_hardening}

    provider = get_sandbox_provider()
    ledger_entries = await spec_ledger.load_ledger(provider, thread_id)
    quarantine = dict(test_hardening["flake_quarantine"])
    run_id = state.get("run_id", "unknown")

    for test_name, entry in quarantine.items():
        if entry.get("linked_id") or not entry.get("ticket_title"):
            continue
        new_id = spec_ledger.allocate_next_id(ledger_entries, "user_story")
        ledger_entries.append(
            {
                "id": new_id,
                "kind": "user_story",
                "status": "active",
                "title": f"[Flaky test] {entry['ticket_title']}",
                "first_seen_run_id": run_id,
                "last_revised_run_id": run_id,
            }
        )
        quarantine[test_name] = {**entry, "linked_id": new_id}

    test_hardening["flake_quarantine"] = quarantine
    await spec_ledger.save_ledger(provider, thread_id, ledger_entries)
    await repo_files.write_repo_file(provider, thread_id, FLAKE_QUARANTINE_PATH, json.dumps(quarantine, indent=2) + "\n")
    await git_ops.commit_paths(provider, thread_id, [spec_ledger.LEDGER_PATH, FLAKE_QUARANTINE_PATH], "ai-dev-workflow: test_hardening flake tickets")
    return {"test_hardening": test_hardening}


async def test_hardening_exit_check_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    test_hardening = dict(state.get("test_hardening") or default_test_hardening_state())
    all_linked = all(entry.get("linked_id") for entry in test_hardening["flake_quarantine"].values())
    test_hardening["last_exit_ok"] = not test_hardening["stable_fail"] and all_linked
    return {"test_hardening": test_hardening}


def make_test_hardening_route_after_exit() -> Any:
    def route(state: dict[str, Any]) -> str:
        test_hardening = state.get("test_hardening") or default_test_hardening_state()
        return "next" if test_hardening.get("last_exit_ok") else "escalate"

    return route


async def test_hardening_exit_escalate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Should not normally fire -- test_hardening_mint_tickets links every entry deterministically.
    Present so an unexpected gap (e.g. the triage LLM never returned a decision for some test)
    ENDs the run with run_failure set rather than silently passing the gate."""
    thread_id = config["configurable"]["thread_id"]
    test_hardening = state.get("test_hardening") or default_test_hardening_state()
    payload = {"stage": "test_hardening", "type": "flake_quarantine_incomplete", "flake_quarantine": test_hardening["flake_quarantine"]}
    payload = await run_failure.record_run_failure_and_reset(
        thread_id, state.get("run_id"),
        payload=payload,
        detail_for_classification=json.dumps(test_hardening["flake_quarantine"]),
    )
    return {"run_failure": payload}
