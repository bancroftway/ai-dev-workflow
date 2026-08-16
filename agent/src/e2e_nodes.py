"""e2e -- playwright execution against the running app, screenshot harvest, and a fix loop.
Bespoke node cluster modeled on test_hardening_nodes.py, wired after test-hardening's exit check
and before metrics-report's compute node (see graph.py's `_wire_e2e`/`_wire_p13`).

Chain: e2e_gate_check (deterministic: UI-framework repo? playwright config/tests present? a
runner resolvable?) -> route("skip" -> metrics_compute | "run" -> e2e_run) -> e2e_run
(deterministic: boot the app, run the suite, harvest screenshots, parse results) ->
route("pass" -> metrics_compute | "fix" [attempt < E2E_MAX_FIX_CYCLES] -> e2e_fix -> e2e_run |
"escalate" -> e2e_escalate -> END).

Verification status: NOT exercised against a real container. In particular: whether a `nohup`'d
app process actually survives the way back out of a single `docker exec` (the mechanism every
other exec_in_sandbox call in this codebase relies on implicitly, but never for a long-lived
background process before this cluster), and whether `npx playwright test --reporter=json`'s
on-disk shape matches what `_parse_playwright_json` below expects, are both unconfirmed against a
live sandbox -- same caveat as quality-remediation/security-remediation/audit-cluster/test-hardening/metrics-report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from . import app_discovery
from . import config as workflow_config
from . import git_ops, model_config, repo_files
from .copilot_chat_model import get_chat_model_for_thread
from .exit_nodes import HISTORY_DIR
from .prompt_loader import load_prompt_pair, render_prompt
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .tech_stack_signals import tech_stack_has_ui_framework

E2E_APP_LOG_PATH = "agent-work/e2e-app.log"
E2E_APP_PID_PATH = "agent-work/e2e-app.pid"
E2E_REPORT_PATH = "agent-work/e2e-report.json"

E2E_FIX_SYSTEM_PROMPT, E2E_FIX_HUMAN_TEMPLATE = load_prompt_pair("e2e_fix")


class E2EState(TypedDict):
    status: Literal["running", "passed", "failed", "skipped"]
    attempt: int
    passed: int
    failed_tests: list[dict[str, str]]
    total: int
    cannot_verify: bool  # sandbox missing at run time -- the suite never ran, escalate not pass
    screenshots: list[str]  # repo-relative paths
    skipped_reason: str | None
    greenfield_candidates: list[dict[str, Any]]  # gate_check's own re-scan, greenfield runs only


def default_e2e_state() -> E2EState:
    return {
        "status": "skipped",
        "attempt": 0,
        "passed": 0,
        "failed_tests": [],
        "total": 0,
        "cannot_verify": False,
        "screenshots": [],
        "skipped_reason": None,
        "greenfield_candidates": [],
    }


async def _playwright_runner_available(provider: Any, thread_id: str) -> str | None:
    """'local' (the repo's own @playwright/test resolves), 'global' (the image's pinned fallback
    CLI is on PATH), or None (neither -- gate_check treats this as a skip reason; package.json is
    outside ac-to-tests' write scope, so tests can exist without the dependency ever being added,
    and that's not a fixable failure)."""
    check = await provider.exec_in_sandbox(
        thread_id,
        "node -e \"require.resolve('@playwright/test')\" >/dev/null 2>&1 && echo LOCAL_OK; "
        "command -v playwright >/dev/null 2>&1 && echo GLOBAL_OK",
    )
    output = check.stdout or ""
    if "LOCAL_OK" in output:
        return "local"
    if "GLOBAL_OK" in output:
        return "global"
    return None


async def e2e_gate_check_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    e2e = dict(state.get("e2e") or default_e2e_state())

    if not tech_stack_has_ui_framework(state):
        e2e.update(status="skipped", skipped_reason="tech-stack detection found no UI framework in this repository")
        return {"e2e": e2e}

    if sandbox_registry.get(thread_id) is None:
        # Defer to e2e_run_node's own no-sandbox handling (cannot_verify -> escalate) instead of
        # silently skipping here -- same discipline every other deterministic gate in this
        # pipeline uses for missing infra (test_hardening, rebuild, security-remediation): it
        # escalates to a human, it never quietly waves the stage through. Explicitly "running"
        # (not left as whatever stale status a prior run's checkpoint carries) so the route below
        # takes the "run" edge regardless of what e2e.status happened to be last time.
        e2e["status"] = "running"
        return {"e2e": e2e}

    provider = get_sandbox_provider()
    presence = await provider.exec_in_sandbox(
        thread_id,
        "ls playwright.config.* >/dev/null 2>&1 && echo HAS_CONFIG; "
        "find e2e tests-e2e -maxdepth 4 -name '*.spec.*' 2>/dev/null | head -1",
    )
    if not (presence.stdout or "").strip():
        e2e.update(status="skipped", skipped_reason="no playwright.config.* or e2e/tests-e2e spec files found")
        return {"e2e": e2e}

    if await _playwright_runner_available(provider, thread_id) is None:
        e2e.update(status="skipped", skipped_reason="@playwright/test is not resolvable and no global playwright runner is installed")
        return {"e2e": e2e}

    e2e["status"] = "running"
    if state.get("greenfield"):
        # app-discovery ran pre-scaffold, so its own apps list is empty -- re-derive
        # start_command/port against the now-scaffolded repo for e2e_run to use.
        scan = await app_discovery.collect_evidence(provider, thread_id)
        e2e["greenfield_candidates"] = scan["candidates"]

    return {"e2e": e2e}


def make_e2e_route_after_gate_check():
    def route(state: dict[str, Any]) -> str:
        e2e = state.get("e2e") or default_e2e_state()
        return "skip" if e2e.get("status") == "skipped" else "run"

    return route


async def _keepalive_touch(provider: Any, thread_id: str) -> None:
    """Resets the sandbox's idle-timeout clock every 60s while the suite exec is in flight -- a
    single exec's own last_active only updates at exec START, so a >30min suite would otherwise get
    reaped mid-run. Cancelled in e2e_run_node's `finally`."""
    while True:
        await asyncio.sleep(60)
        await provider.touch(thread_id)


async def _finalize_run(provider: Any, thread_id: str, e2e: E2EState) -> dict[str, Any]:
    """Best-effort app teardown, then a ledger entry + commit of whatever landed under
    .ai-dev-workflow/ this attempt (the ledger line itself, plus this run's screenshots dir, which
    already lives under HISTORY_DIR)."""
    await provider.exec_in_sandbox(thread_id, f"kill $(cat {E2E_APP_PID_PATH} 2>/dev/null) 2>/dev/null; true")
    await repo_files.append_ledger_entry(
        provider,
        thread_id,
        {
            "stage": "e2e",
            "node": "run",
            "status": e2e["status"],
            "attempt": e2e.get("attempt", 0),
            "passed": e2e.get("passed"),
            "total": e2e.get("total"),
        },
    )
    await git_ops.commit_ai_dev_workflow(provider, thread_id, f"ai-dev-workflow: e2e run ({e2e['status']})")
    return {"e2e": e2e}


async def e2e_run_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    e2e = dict(state.get("e2e") or default_e2e_state())
    run_id = state.get("run_id", "unknown")

    if sandbox_registry.get(thread_id) is None:
        # No sandbox means the suite could not run at all. Escalate rather than treat an unrun
        # suite as green (route reads cannot_verify) -- test_hardening's own convention.
        e2e["status"] = "failed"
        e2e["cannot_verify"] = True
        return {"e2e": e2e}

    provider = get_sandbox_provider()
    e2e["cannot_verify"] = False

    if state.get("greenfield"):
        candidates = e2e.get("greenfield_candidates") or []
    else:
        app_discovery_stage = (state.get("stages") or {}).get("app-discovery") or {}
        candidates = (app_discovery_stage.get("approved_content") or {}).get("apps") or []
    app = next((a for a in candidates if str(a.get("start_command") or "").strip()), None)

    await provider.exec_in_sandbox(thread_id, "mkdir -p agent-work")
    # Cleared unconditionally, every attempt: a fix cycle re-runs this node against the SAME
    # run_id, so a stale screenshot left from an earlier, more-broken attempt would otherwise
    # survive on disk (and get committed) even after the state's own "screenshots" list moves on --
    # exit_nodes.py's own report lists this directory straight off disk, not from this state.
    screens_dir = f"{HISTORY_DIR}/{run_id}-screens"
    await provider.exec_in_sandbox(thread_id, f"rm -rf {shlex.quote(screens_dir)}")

    if app is None:
        e2e.update(
            status="failed", total=0, passed=0, screenshots=[],
            failed_tests=[{"title": "app startup", "error": "no application with a start_command was found for e2e to exercise"}],
        )
        return await _finalize_run(provider, thread_id, e2e)

    port = int(app.get("port") or 3000)
    start_command = str(app["start_command"])

    # Secrets stripped from the started app's environment: docker exec inherits this container's
    # own env (including the Copilot session's fleet PAT), and an app that leaked its env on an
    # error page would otherwise get screenshotted and committed straight into git history.
    await provider.exec_in_sandbox(
        thread_id,
        f"env -u COPILOT_SDK_AUTH_TOKEN -u COPILOT_CONNECTION_TOKEN -u GITHUB_TOKEN "
        f"nohup sh -c {shlex.quote(start_command)} > {E2E_APP_LOG_PATH} 2>&1 & echo $! > {E2E_APP_PID_PATH}",
    )

    ready = False
    for _ in range(max(1, workflow_config.E2E_APP_READY_TIMEOUT_SECONDS // 3)):
        probe = await provider.exec_in_sandbox(thread_id, f"curl -sf -o /dev/null http://localhost:{port}")
        if probe.ok:
            ready = True
            break
        await asyncio.sleep(3)

    if not ready:
        log_tail = (await repo_files.read_repo_file(provider, thread_id, E2E_APP_LOG_PATH) or "")[-3000:]
        e2e.update(
            status="failed", total=0, passed=0, screenshots=[],
            failed_tests=[{
                "title": "app readiness",
                "error": f"app never answered on port {port} within {workflow_config.E2E_APP_READY_TIMEOUT_SECONDS}s -- log tail:\n{log_tail}",
            }],
        )
        return await _finalize_run(provider, thread_id, e2e)

    runner = await _playwright_runner_available(provider, thread_id)
    if runner is None:
        # Should not normally happen -- e2e_gate_check_node already confirmed one of these
        # resolves. A fix commit that removed the dependency mid-loop gets the same treatment.
        e2e.update(status="skipped", skipped_reason="@playwright/test is no longer resolvable and no global playwright runner is installed", failed_tests=[], screenshots=[])
        return await _finalize_run(provider, thread_id, e2e)

    if runner == "local":
        # Image-default writable browsers cache (PLAYWRIGHT_BROWSERS_PATH is already set in the
        # container's own env) -- install chromium once, only when a marker check shows it's
        # missing, so the per-owner cache volume absorbs the cost across sessions.
        marker = await provider.exec_in_sandbox(thread_id, 'ls "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null | grep -qi chromium && echo PRESENT')
        if "PRESENT" not in (marker.stdout or ""):
            await provider.exec_in_sandbox(thread_id, "npx --yes playwright install chromium")
        run_prefix, run_cmd = "", "npx playwright test"
    else:
        # Global pinned fallback runner: force it onto the image's baked (chromium-headless-shell
        # only) browser path rather than the writable cache, which this runner never populated.
        run_prefix, run_cmd = "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers ", "playwright test"

    command = (
        f"{run_prefix}PLAYWRIGHT_JSON_OUTPUT_NAME={E2E_REPORT_PATH} BASE_URL=http://localhost:{port} "
        f"timeout {workflow_config.E2E_SUITE_TIMEOUT_SECONDS} {run_cmd} --reporter=json 2>&1"
    )
    keepalive = asyncio.create_task(_keepalive_touch(provider, thread_id))
    try:
        suite_result = await provider.exec_in_sandbox(thread_id, command)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    combined_output = (suite_result.stdout or "") + (suite_result.stderr or "")
    if "Executable doesn't exist" in combined_output:
        # Infra gap (browser binary missing) -- not fixable by the LLM fixer, so skip with a
        # reason rather than burn a fix cycle on it.
        e2e.update(status="skipped", skipped_reason="playwright browser executable is missing in this environment", failed_tests=[], screenshots=[])
        return await _finalize_run(provider, thread_id, e2e)

    if suite_result.returncode == 124:
        # `timeout` itself killed the suite -- a fixable failure (same class as an app that never
        # got ready), not something to parse a possibly-truncated report for.
        e2e.update(
            status="failed", total=0, passed=0, screenshots=[],
            failed_tests=[{"title": "e2e suite", "error": f"suite timed out after {workflow_config.E2E_SUITE_TIMEOUT_SECONDS}s"}],
        )
        return await _finalize_run(provider, thread_id, e2e)

    # Screenshot harvest. Filenames derive from repo-controlled test titles -- a real shell
    # metacharacter injection risk -- so `find`'s raw output is NEVER re-interpolated into another
    # sh -c string. Each path is instead individually shlex-quoted from Python and used in its own
    # exec, which gives the same safety `find -print0 | xargs -0` would inside one shell pipeline.
    find_result = await provider.exec_in_sandbox(thread_id, "find test-results -name '*.png' -print0 2>/dev/null")
    found_paths = [p for p in (find_result.stdout or "").split("\x00") if p]
    screenshots: list[str] = []
    if found_paths:
        await provider.exec_in_sandbox(thread_id, f"mkdir -p {shlex.quote(screens_dir)}")
        for index, path in enumerate(found_paths, start=1):
            dest = f"{screens_dir}/{index:03d}.png"
            await provider.exec_in_sandbox(thread_id, f"cp -- {shlex.quote(path)} {shlex.quote(dest)}")
            screenshots.append(dest)
    e2e["screenshots"] = screenshots

    raw_report = await repo_files.read_repo_file(provider, thread_id, E2E_REPORT_PATH)
    if raw_report:
        parsed = _parse_playwright_json(raw_report)
    else:
        # The reporter never wrote a file at all (suite crashed before it could) -- NEVER read
        # this as "0 tests, all passed": that would let the very failures this stage exists to
        # catch skip straight past the fix/escalate loop.
        parsed = {
            "passed": 0, "total": 0,
            "failed_tests": [{"title": "e2e report", "error": f"{E2E_REPORT_PATH} was not written (suite exit code {suite_result.returncode})"}],
        }
    e2e.update(status="passed" if not parsed["failed_tests"] else "failed", **parsed)

    return await _finalize_run(provider, thread_id, e2e)


def make_e2e_route_after_run():
    def route(state: dict[str, Any]) -> str:
        e2e = state.get("e2e") or default_e2e_state()
        if e2e.get("cannot_verify"):
            return "escalate"  # no sandbox -- never loop or pass, a human must see it
        if not e2e.get("failed_tests"):
            return "pass"
        if e2e.get("attempt", 0) < workflow_config.E2E_MAX_FIX_CYCLES:
            return "fix"
        return "escalate"

    return route


async def e2e_fix_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    e2e = dict(state.get("e2e") or default_e2e_state())
    if sandbox_registry.get(thread_id) is None:
        return {"e2e": e2e}

    provider = get_sandbox_provider()
    log_tail = (await repo_files.read_repo_file(provider, thread_id, E2E_APP_LOG_PATH) or "")[-4000:]
    prompt = render_prompt(
        E2E_FIX_HUMAN_TEMPLATE,
        failed_tests_json=json.dumps(e2e.get("failed_tests") or [], indent=2),
        app_log_tail=log_tail or "(no app log captured)",
    )

    # Own session key (e2e:fix), not minimal-code-to-green's -- sharing that key would return its
    # cached session and bleed this loop's conversation across stages. Same codegen-tier model
    # fallback reasoning as rebuild.py/security_nodes.py's own fix nodes.
    model = get_chat_model_for_thread(
        thread_id,
        "e2e",
        "fix",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("e2e", "draft") or model_config.get_model_name("minimal-code-to-green", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        agent_mode="autopilot",
    )
    await model.ainvoke([SystemMessage(content=E2E_FIX_SYSTEM_PROMPT), HumanMessage(content=prompt)])

    e2e["attempt"] = e2e.get("attempt", 0) + 1
    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "e2e", "node": "fix", "attempt": e2e["attempt"], "token_usage": model._last_usage}
    )
    await git_ops.commit_all(provider, thread_id, "ai-dev-workflow: e2e fix cycle")
    return {"e2e": e2e}


async def e2e_escalate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    e2e = dict(state.get("e2e") or default_e2e_state())
    payload = {
        "stage": "e2e",
        "type": "cannot_verify" if e2e.get("cannot_verify") else "e2e_cap_exceeded",
        "report": {"failed_tests": e2e.get("failed_tests"), "total": e2e.get("total"), "passed": e2e.get("passed")},
        "feedback": "; ".join(f"{t.get('title')}: {t.get('error')}" for t in (e2e.get("failed_tests") or [])) or None,
        "run_id": state.get("run_id"),
    }
    await git_ops.record_run_failure(thread_id, payload, state.get("run_id"))
    e2e["attempt"] = 0
    e2e["cannot_verify"] = False
    e2e["status"] = "failed"
    return {"e2e": e2e, "run_failure": payload}


# --------------------------------------------------------------------------------------------
# Pure half -- no sandbox, no I/O, self-checked at the bottom of this module.
# --------------------------------------------------------------------------------------------


def _iter_specs(suite: dict[str, Any]):
    for spec in suite.get("specs") or []:
        yield spec
    for nested in suite.get("suites") or []:
        yield from _iter_specs(nested)


def _parse_playwright_json(raw_json: str) -> dict[str, Any]:
    """Playwright's `--reporter=json` output -> {passed, failed_tests: [{title, error}], total}.
    A test's outcome is judged on its LAST result only -- retries produce multiple results for the
    same test, and only the final one decides pass/fail.

    NEVER returns total==0 with an empty failed_tests: that shape is indistinguishable from "ran
    zero tests, so vacuously all passed", which would route the run straight past the fix/escalate
    loop on exactly the failures (a globalSetup throw, a config syntax error, a suite that never
    actually started) it exists to catch. Malformed JSON and a structurally-empty report each get
    their own synthetic failed_tests entry instead; playwright's own top-level `errors` (set when
    something broke before any test could run) are surfaced as real failures when present.
    """
    try:
        doc = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"passed": 0, "total": 0, "failed_tests": [{"title": "e2e report", "error": "e2e-report.json was not valid JSON"}]}

    passed = 0
    total = 0
    failed_tests: list[dict[str, str]] = []
    for suite in doc.get("suites") or []:
        for spec in _iter_specs(suite):
            title = spec.get("title", "unknown")
            for test in spec.get("tests") or []:
                total += 1
                results = test.get("results") or []
                outcome = results[-1] if results else {}
                if outcome.get("status") == "passed":
                    passed += 1
                else:
                    error = ((outcome.get("error") or {}).get("message")) or outcome.get("status") or "unknown failure"
                    failed_tests.append({"title": title, "error": str(error)})

    if total == 0:
        top_errors = doc.get("errors") or []
        if top_errors:
            for err in top_errors:
                message = err.get("message") if isinstance(err, dict) else str(err)
                failed_tests.append({"title": "e2e suite setup", "error": str(message or err)})
        else:
            failed_tests.append({"title": "e2e suite", "error": "e2e-report.json contained no tests and no top-level errors"})

    return {"passed": passed, "failed_tests": failed_tests, "total": total}


def _demo() -> None:
    """Self-check for the pure half: `uv run python -m src.e2e_nodes`."""
    sample = {
        "suites": [
            {
                "specs": [{"title": "[AC-0001.1] user can log in", "tests": [{"results": [{"status": "passed"}]}]}],
                "suites": [
                    {
                        "specs": [
                            {
                                "title": "[AC-0001.2] user sees error on bad password",
                                "tests": [{"results": [{"status": "failed", "error": {"message": "expected element to be visible"}}]}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    result = _parse_playwright_json(json.dumps(sample))
    assert result["total"] == 2, result
    assert result["passed"] == 1, result
    assert result["failed_tests"] == [
        {"title": "[AC-0001.2] user sees error on bad password", "error": "expected element to be visible"}
    ], result

    # A test that flaked-then-passed (retries) is judged on its LAST result only.
    retried = {"suites": [{"specs": [{"title": "flaky", "tests": [{"results": [{"status": "failed"}, {"status": "passed"}]}]}]}]}
    assert _parse_playwright_json(json.dumps(retried))["failed_tests"] == []

    # Malformed/missing/structurally-empty reports must NEVER read as "0 tests, all passed" --
    # that would let the very failures this stage exists to catch skip the fix/escalate loop.
    malformed = _parse_playwright_json("not json")
    assert malformed["total"] == 0 and malformed["failed_tests"], malformed
    empty_string = _parse_playwright_json("")
    assert empty_string["total"] == 0 and empty_string["failed_tests"], empty_string

    # Empty suites but a top-level setup error (globalSetup threw, config syntax error, ...) --
    # playwright's own `errors` array is the only place this ever surfaces.
    setup_failure = {"suites": [], "errors": [{"message": "Error: globalSetup failed"}]}
    result = _parse_playwright_json(json.dumps(setup_failure))
    assert result["total"] == 0, result
    assert result["failed_tests"] == [{"title": "e2e suite setup", "error": "Error: globalSetup failed"}], result

    # Structurally empty (no suites, no errors either) still classifies as a failure, not a pass.
    structurally_empty = _parse_playwright_json(json.dumps({"suites": []}))
    assert structurally_empty["total"] == 0 and structurally_empty["failed_tests"], structurally_empty

    # tech_stack_has_ui_framework: same UI-framework-marker check graph.py's stage gates use --
    # this module's own self-check is a light re-confirmation, the real one lives in
    # tech_stack_signals.py now.
    assert tech_stack_has_ui_framework({"stages": {"tech-stack": {"approved_content": {"frameworks": ["Next.js"]}}}})
    assert not tech_stack_has_ui_framework({"stages": {"tech-stack": {"approved_content": {"frameworks": ["FastAPI"]}}}})
    assert not tech_stack_has_ui_framework({})

    print("e2e_nodes self-check: ok")


if __name__ == "__main__":
    _demo()
