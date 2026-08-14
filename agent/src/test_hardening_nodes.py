"""test-hardening -- full test suite + flake quarantine. A deterministic run node (with retries) + one
narrow read-only LLM node for judgment (filing tickets), exactly as scoped in the plan, nothing
more.

Chain: test_hardening_run_tests (deterministic, retries) -> route(any stable_fail -> test_hardening_regression_gate
[hard interrupt, out of test-hardening's own scope to resolve] | else -> test_hardening_flake_triage) ->
test_hardening_flake_triage (read-only) -> test_hardening_mint_tickets (deterministic: allocates real US-#### ids via
spec_ledger.py, never the LLM) -> test_hardening_exit_check (deterministic) -> route(pass -> next | fail ->
test_hardening_exit_escalate [should not normally happen, since mint_tickets always links every entry --
included so this stage never silently proceeds on an unexpected gap]).

Verification status: NOT exercised against a real sandbox, same caveat as quality-remediation/security-remediation/audit-cluster. trx (.NET)
and vitest-json (JS/TS) parsing are both written from documented schema shape, not confirmed live.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypedDict

import defusedxml.ElementTree as ET
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from . import config as workflow_config
from . import git_ops, model_config, repo_files, spec_ledger
from .copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from .prompt_loader import load_prompt
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .schemas_test_hardening import FlakeTriageResponse

TEST_HARDENING_TOTAL_ATTEMPTS = int(os.environ.get("TEST_HARDENING_TOTAL_ATTEMPTS", "3"))  # 1 initial + 2 retries
FLAKE_QUARANTINE_PATH = ".ai-dev-workflow/test_hardening/flake-quarantine.json"

FLAKE_TRIAGE_SYSTEM_PROMPT = load_prompt("test_hardening_flake_triage")


class TestHardeningState(TypedDict):
    attempt: int
    test_outcomes: dict[str, list[str]]  # test_name -> ["pass"|"fail", ...] across attempts
    stable_fail: list[str]
    flaky: list[str]
    flake_quarantine: dict[str, dict[str, Any]]  # test_name -> {ticket_id, title, narrative}
    last_exit_ok: bool
    cannot_verify: bool  # sandbox missing at run time -- the suite never ran, escalate not pass


def default_test_hardening_state() -> TestHardeningState:
    return {"attempt": 0, "test_outcomes": {}, "stable_fail": [], "flaky": [], "flake_quarantine": {}, "last_exit_ok": False, "cannot_verify": False}


def _resolve_test_command(tech_stack: dict[str, Any], attempt: int) -> tuple[str, str] | None:
    """Returns (command, result_file_path) or None if no mapping."""
    if tech_stack.get("dotnet_detected"):
        path = f"agent-work/test_hardening-attempt{attempt}.trx"
        return f"dotnet test --logger 'trx;LogFileName={os.path.basename(path)}' --results-directory agent-work 2>&1", path
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if "typescript" in languages or "javascript" in languages:
        path = f"agent-work/test_hardening-attempt{attempt}.json"
        return f"npx --yes vitest run --reporter=json --outputFile={path} 2>&1", path
    return None


def _parse_trx(raw_xml: str) -> dict[str, str]:
    """testName -> 'pass'|'fail', from a Visual Studio Test Results (.trx) file."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return {}
    ns = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}
    results: dict[str, str] = {}
    for result in root.findall(".//t:UnitTestResult", ns) or root.findall(".//UnitTestResult"):
        name = result.get("testName", "unknown")
        outcome = result.get("outcome", "")
        results[name] = "pass" if outcome.lower() == "passed" else "fail"
    return results


def _parse_vitest_json(raw_json: str) -> dict[str, str]:
    try:
        doc = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    results: dict[str, str] = {}
    for test_result in doc.get("testResults", []):
        for assertion in test_result.get("assertionResults", []):
            name = assertion.get("fullName") or assertion.get("title", "unknown")
            results[name] = "pass" if assertion.get("status") == "passed" else "fail"
    return results


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
    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}

    await provider.exec_in_sandbox(thread_id, "mkdir -p agent-work")
    outcomes: dict[str, list[str]] = {}
    for attempt in range(TEST_HARDENING_TOTAL_ATTEMPTS):
        resolved = _resolve_test_command(tech_stack, attempt)
        if resolved is None:
            test_hardening["last_exit_ok"] = True
            return {"test_hardening": test_hardening}
        command, result_path = resolved
        await provider.exec_in_sandbox(thread_id, command)
        raw_result = await repo_files.read_repo_file(provider, thread_id, result_path)
        if raw_result is None:
            continue
        per_test = _parse_trx(raw_result) if result_path.endswith(".trx") else _parse_vitest_json(raw_result)
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
            return "regression"
        return "triage"

    return route


async def test_hardening_regression_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """A real regression (consistently failing across every attempt) is out of test-hardening's own scope to
    resolve -- a hard human/upstream interrupt, never auto-handled here."""
    test_hardening = state.get("test_hardening") or default_test_hardening_state()
    if test_hardening.get("cannot_verify"):
        interrupt({"stage": "test_hardening", "type": "cannot_verify", "reason": "no sandbox -- test suite did not run"})
    else:
        interrupt({"stage": "test_hardening", "type": "stable_test_regression", "stable_fail": test_hardening["stable_fail"]})
    return {}


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
        model_name=model_config.get_model_name("test-hardening-flake-triage", "draft"),
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
    """Should not normally fire -- test_hardening_mint_tickets links every entry deterministically. Present
    so an unexpected gap (e.g. the triage LLM never returned a decision for some test) surfaces
    as a real human decision rather than silently passing the gate."""
    test_hardening = state.get("test_hardening") or default_test_hardening_state()
    interrupt({"stage": "test_hardening", "type": "flake_quarantine_incomplete", "flake_quarantine": test_hardening["flake_quarantine"]})
    return {}
