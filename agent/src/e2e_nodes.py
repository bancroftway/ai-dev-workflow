"""e2e -- playwright execution against the running app, screenshot harvest, and a fix loop.
Bespoke node cluster modeled on test_hardening_nodes.py, wired after test-hardening's exit check
and before metrics-report's compute node (see graph.py's `_wire_e2e`/`_wire_p13`).

Chain: e2e_gate_check (deterministic: UI-framework repo? playwright config/tests present? a
runner resolvable?) -> route("skip" -> metrics_compute | "run" -> e2e_run) -> e2e_run
(deterministic: boot the app, run the suite, harvest screenshots, parse results) ->
route("pass" -> metrics_compute | "fix" [attempt < E2E_MAX_FIX_CYCLES] -> e2e_fix -> e2e_run |
"escalate" -> e2e_escalate -> END).

Verification status, updated 2026-08-19 after a live spike against a real sandbox:

CONFIRMED. A `nohup`'d process started by one `exec_in_sandbox` survives that exec's exit and
answers on its port from a SEPARATE exec; its PID file lands correctly. This was the cluster's
headline unknown and the thing every screenshot depends on.

CONFIRMED. `playwright screenshot --full-page` works against the image's baked
chromium-headless-shell (no full chromium needed, no runtime `playwright install`), and
`screenshot: 'on'` yields images for PASSING tests, not only failures.

CONFIRMED, and it needed a fix: `@playwright/test` is NOT installed in the image (only the
`playwright` package is), so a spec importing it dies with MODULE_NOT_FOUND, and NODE_PATH does not
help. Fix: _link_global_playwright symlinks the global package and drops a tiny @playwright/test
shim, so specs keep the IDIOMATIC import. Importing `playwright/test` instead also runs, but breaks
the moment a repo ships its own @playwright/test -- the runner then loads one copy and the spec
another, and playwright rejects it ("did not expect test.beforeEach() ... two different versions").
Both the shim and the mixed-import failure were reproduced live.

CONFIRMED. `--reporter=json` writes the shape `_parse_playwright_json` expects: a live run parsed
`agent-work/e2e-report.json` into 12 expected / 1 unexpected with per-spec titles, and the failing
spec's title reached the fix node's feedback intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import Field

from . import app_discovery
from . import config as workflow_config
from . import git_ops, keyvault, model_config, repo_files, session_store
from .copilot_chat_model import get_chat_model_for_thread
from .exit_nodes import HISTORY_DIR
from .prompt_loader import load_prompt_pair, render_prompt

logger = logging.getLogger(__name__)
from . import stack_runner
from .schemas import StageReport
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
    app_candidates: list[dict[str, Any]]  # gate_check's own fresh re-scan (see its own comment)
    suite: bool  # a playwright suite exists to run; False -> boot and screenshot only
    config_dir: str  # repo-relative directory holding playwright.config.* ("" = repo root)
    routes: list[str]  # routes actually screenshotted, parallel to `screenshots`


class AppLaunchReport(StageReport):
    """What the app-launch discovery agent must report (prompts/e2e_run.md)."""

    start_command: str = ""
    port: int = 0
    routes: list[str] = Field(
        default_factory=list,
        description="Every user-facing route path the app serves, e.g. ['/', '/expenses']. Used to "
        "screenshot each screen; '/' alone is acceptable for a single-page app.",
    )


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
        "app_candidates": [],
        "suite": False,
        "config_dir": "",
        "routes": [],
    }


# The globally-installed playwright package, which provides BOTH the `playwright` CLI (with its
# `test` subcommand) and the importable test runner under its own `./test` export. Verified live in
# the sandbox image: `@playwright/test` is NOT installed there (/usr/lib/node_modules/@playwright
# holds only `mcp`), so a spec written the idiomatic way dies with MODULE_NOT_FOUND, and NODE_PATH
# does not fix it (playwright resolves config imports itself). _link_global_playwright bridges that
# gap without changing how specs are written -- see its docstring for why the shim beats telling
# specs to import 'playwright/test'.
GLOBAL_PLAYWRIGHT_PATH = "/usr/lib/node_modules/playwright"


async def _playwright_runner_available(provider: Any, thread_id: str, config_dir: str = "") -> str | None:
    """'local' (the repo's own @playwright/test resolves), 'global' (the image's pinned package is
    installed), or None (neither).

    Resolution is checked FROM THE CONFIG DIRECTORY, because that is where the suite runs and node
    walks node_modules UPWARD only. Checking from the repo root reported "global" for a repo whose
    package sat in apps/web/node_modules -- invisible from above -- and the caller then wrote its
    compatibility shim straight over a perfectly good install.
    """
    cd_prefix = f"cd {shlex.quote(config_dir)} && " if config_dir else ""
    check = await provider.exec_in_sandbox(
        thread_id,
        f"{cd_prefix}node -e \"require.resolve('@playwright/test')\" >/dev/null 2>&1 && echo LOCAL_OK; "
        f"test -d {shlex.quote(GLOBAL_PLAYWRIGHT_PATH)} && command -v playwright >/dev/null 2>&1 && echo GLOBAL_OK",
    )
    output = check.stdout or ""
    if "LOCAL_OK" in output:
        return "local"
    if "GLOBAL_OK" in output:
        return "global"
    return None


# A writable directory of aliases into the image's baked browsers. The image bakes exactly one
# chromium build (chromium_headless_shell-1237); a repo pinning its own playwright version wants a
# DIFFERENT revision -- observed live, `@playwright/test: ^1.55.0` resolved to 1.62.1, which demands
# revision 1234 and fails with "Executable doesn't exist at .../chromium_headless_shell-1234". The
# baked directory is root-owned so it cannot be aliased in place, hence redirecting
# PLAYWRIGHT_BROWSERS_PATH here. Still no runtime download: this only gives the baked binary the
# revision-numbered name the local runner is looking for.
BROWSER_ALIAS_DIR = "/tmp/aidw-pw-browsers"


async def _alias_browsers_for_local_runner(provider: Any, thread_id: str) -> None:
    """Make the baked chromium answer to whatever revision the repo's own playwright expects.

    Reads the wanted revision from the repo's playwright-core/browsers.json rather than hardcoding
    it, so this keeps working as generated apps pin different playwright versions.
    """
    await provider.exec_in_sandbox(
        thread_id,
        f"mkdir -p {shlex.quote(BROWSER_ALIAS_DIR)}; "
        f"baked=$(ls -d /opt/playwright-browsers/chromium_headless_shell-* 2>/dev/null | head -1); "
        f"[ -n \"$baked\" ] || exit 0; "
        # Mirror everything baked (chromium + ffmpeg) under the writable alias dir first.
        f"for d in /opt/playwright-browsers/*; do "
        f"  ln -sfnT \"$d\" {shlex.quote(BROWSER_ALIAS_DIR)}/$(basename \"$d\") 2>/dev/null; done; "
        # Then add the revision name each local playwright install asks for.
        f"for bj in $(find . -path '*/playwright-core/browsers.json' -not -path './.git/*' 2>/dev/null | head -5); do "
        f"  rev=$(python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); "
        f"print(next((b['revision'] for b in d['browsers'] if b['name']=='chromium-headless-shell'), ''))\" \"$bj\" 2>/dev/null); "
        f"  [ -n \"$rev\" ] || continue; "
        f"  ln -sfnT \"$baked\" {shlex.quote(BROWSER_ALIAS_DIR)}/chromium_headless_shell-$rev 2>/dev/null; "
        f"done; true",
    )


async def _link_global_playwright(provider: Any, thread_id: str, config_dir: str) -> None:
    """Make the IDIOMATIC `@playwright/test` import resolve, using the globally installed package.

    Called only when the repo has no @playwright/test of its own. Two pieces, both required:
      * node_modules/playwright        -> symlink to the global package
      * node_modules/@playwright/test  -> a 3-line shim re-exporting `playwright/test`

    Why a shim rather than telling specs to import 'playwright/test' directly (which also works):
    when the repo DOES ship @playwright/test, the runner loads that copy while a 'playwright/test'
    import resolves to a different one, and playwright rejects the mix with "did not expect
    test.beforeEach() to be called here ... two different versions of @playwright/test". Observed
    live. Keeping the import idiomatic in every case, and fixing resolution here, removes the
    dependency on which packages a given repo happens to have installed.

    Idempotent, no network, no package.json edit -- package.json is outside ac-to-tests' write scope,
    so the stage that writes specs could never add the dependency itself.
    """
    target = f"{config_dir}/node_modules" if config_dir else "node_modules"
    shim = f"{target}/@playwright/test"
    link = f"{target}/playwright"
    # Both writes are strictly ADDITIVE, guarded on the target not already existing. Without these
    # guards this function overwrote a REAL @playwright/test install (replacing its package.json and
    # index.js with the shim's), and `ln -sfn` against an existing directory silently created
    # node_modules/playwright/playwright rather than replacing it. `-T` treats the destination as a
    # name, never as a directory to descend into. Both were observed live, on the same run.
    await provider.exec_in_sandbox(
        thread_id,
        f"mkdir -p {shlex.quote(target)}; "
        f"[ -e {shlex.quote(link)} ] || ln -sfnT {shlex.quote(GLOBAL_PLAYWRIGHT_PATH)} {shlex.quote(link)}; "
        f"if [ ! -e {shlex.quote(f'{shim}/package.json')} ]; then "
        f"mkdir -p {shlex.quote(shim)} && "
        f"printf '%s\\n' '{{\"name\":\"@playwright/test\",\"version\":\"1.0.0\",\"main\":\"index.js\"}}' "
        f"> {shlex.quote(f'{shim}/package.json')} && "
        f"printf '%s\\n' 'module.exports = require(\"playwright/test\");' "
        f"> {shlex.quote(f'{shim}/index.js')}; "
        f"fi; true",
    )


async def _discover_playwright_layout(provider: Any, thread_id: str) -> tuple[str | None, bool]:
    """(config_dir, has_specs) found ANYWHERE in the repo, not just at its root.

    The previous probe ran `ls playwright.config.*` in the repo root and `find e2e tests-e2e`, so a
    generated monorepo -- which puts both under apps/web/ -- always read as "no playwright here" and
    skipped e2e entirely. config_dir matters as well as presence: playwright must be invoked from
    the directory holding its config, and the old code ran it from the repo root.
    """
    found = await provider.exec_in_sandbox(
        thread_id,
        "find . -maxdepth 4 -name 'playwright.config.*' -not -path '*/node_modules/*' "
        "-not -path './.git/*' | head -1",
    )
    config_path = (found.stdout or "").strip().splitlines()
    config_dir: str | None = None
    if config_path:
        # "./apps/web/playwright.config.ts" -> "apps/web" ("" when it sits at the repo root)
        cleaned = config_path[0].removeprefix("./")
        config_dir = cleaned.rsplit("/", 1)[0] if "/" in cleaned else ""

    specs = await provider.exec_in_sandbox(
        thread_id,
        "find . -maxdepth 6 \\( -name '*.spec.ts' -o -name '*.spec.js' -o -name '*.e2e.ts' \\) "
        "-not -path '*/node_modules/*' -not -path './.git/*' | head -5",
    )
    return config_dir, bool((specs.stdout or "").strip())


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
    config_dir, has_specs = await _discover_playwright_layout(provider, thread_id)
    runner = await _playwright_runner_available(provider, thread_id, config_dir or "")

    # A UI app NEVER skips. Screenshots are the only visual evidence a human gets that the generated
    # app actually renders, and exit's own verify blocks the merge for a UI app with none -- so
    # skipping here just guaranteed that blocker instead of avoiding it. Absent specs now mean "boot
    # and screenshot", not "do nothing": e2e_run_node still starts the app and shoots every route.
    # Only a missing RUNNER is a genuine skip, and that is an image defect, not something a repo can
    # cause or a fix cycle can repair.
    if runner is None:
        e2e.update(
            status="skipped",
            skipped_reason=(
                "no playwright runner available in this sandbox (neither the repo's own "
                "@playwright/test nor the image's global playwright package)"
            ),
        )
        return {"e2e": e2e}

    e2e["suite"] = bool(has_specs and config_dir is not None)
    e2e["config_dir"] = config_dir or ""
    e2e["status"] = "running"
    # app_check_record ran pre-scaffold, so its recorded apps list reflects a possibly-empty (or
    # since-changed) repo -- always re-derive start_command/port fresh against the now-scaffolded
    # repo for e2e_run to use (exit_nodes.py's own verify_exit_readiness established this same
    # "re-scan now that the code exists" precedent). No longer greenfield-only: a brownfield repo
    # that had zero startable apps before scaffolding has the exact same staleness problem.
    scan = await app_discovery.collect_evidence(provider, thread_id)
    e2e["app_candidates"] = scan["candidates"]

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
    # Every PID this attempt recorded, not just the primary app's: the supporting services booted
    # alongside it (an API the UI calls) would otherwise survive the attempt, hold their ports, and
    # make the NEXT fix cycle's boot fail with EADDRINUSE against a stale process.
    await provider.exec_in_sandbox(
        thread_id,
        f"for f in {E2E_APP_PID_PATH} agent-work/e2e-service-*.pid; do "
        f"  [ -f \"$f\" ] || continue; p=$(cat \"$f\" 2>/dev/null); "
        f"  [ -n \"$p\" ] && {{ kill -TERM -\"$p\" 2>/dev/null || kill -TERM \"$p\" 2>/dev/null; }}; "
        f"  rm -f \"$f\"; "
        f"done; true",
    )
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


def _route_slug(route: str) -> str:
    """'/expenses/new' -> 'expenses-new', '/' -> 'home'. Filename-safe by construction: routes come
    from a model's report, so anything outside [a-z0-9-] is dropped rather than escaped."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-")
    return cleaned or "home"


def _candidate_class(candidate: dict[str, Any]) -> str:
    """The app class of a candidate, whichever shape it arrives in.

    e2e_gate_check_node stores app_discovery's RAW candidates, which carry `likely_class`;
    `candidates_to_apps` renames that to `app_class` for the manifest. Reading only `app_class` got
    None for every raw candidate, so "pick the web app to point playwright at" never matched and fell
    through to the first entry -- apps/api -- which made the actual UI app a mere "supporting
    service" and probed it on the wrong port.
    """
    return str(candidate.get("app_class") or candidate.get("likely_class") or "").lower()


# Long-lived dev/serve processes a previous attempt (or the launch-discovery agent, which is told to
# stop what it starts and does not always comply) can leave holding a port. Killed before booting:
# observed live, a leftover `next dev` from an earlier attempt made Next.js pick port 3002 instead of
# 3000 while the readiness probe watched 3000, so a perfectly healthy app read as "never answered".
_STALE_APP_PATTERNS = ("next dev", "next start", "npm run dev", "vite", "ng serve", "dotnet run", "uvicorn", "flask run")


# Port 3000 is NOT available in this sandbox: the Copilot headless server itself listens there
# (`copilot --headless ... --port 3000`, PID 1 -- confirmed by walking /proc/net/tcp to its owner).
# app_discovery nevertheless defaults every node web app to 3000, so a dev server told to use it
# quietly picks 3001/3002 instead while the readiness probe watches 3000 and reports a perfectly
# healthy app as "never answered". Killing the holder is not an option -- it would end the session.
# So the app gets a port from this range, verified free before use.
_APP_PORT_RANGE = range(3100, 3140)


async def _listening_ports(provider: Any, thread_id: str) -> set[int]:
    """Ports currently in LISTEN state, read from /proc (no ss/netstat/lsof in this image)."""
    result = await provider.exec_in_sandbox(
        thread_id,
        "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk '$4==\"0A\" {split($2,a,\":\"); print a[2]}'",
    )
    ports: set[int] = set()
    for token in (result.stdout or "").split():
        try:
            ports.add(int(token, 16))
        except ValueError:
            continue
    return ports


async def _pick_free_port(provider: Any, thread_id: str, preferred: int = 0) -> int:
    """A port nothing is listening on: `preferred` when it is genuinely free and not 3000, else the
    first free port in _APP_PORT_RANGE. Falls back to the range's start when everything looks busy
    (better to try and get a real error than to silently reuse a known-occupied port)."""
    busy = await _listening_ports(provider, thread_id)
    if preferred and preferred != 3000 and preferred not in busy:
        return preferred
    for candidate in _APP_PORT_RANGE:
        if candidate not in busy:
            return candidate
    return _APP_PORT_RANGE.start


# Environment variables an app might use to locate its backend. Discovered from the repo rather
# than assumed, because every stack spells it differently (API_BASE_URL, NEXT_PUBLIC_API_URL,
# VITE_API_URL, REACT_APP_API_BASE, ...).
# Captures ANY env name, then Python filters for the API-ish ones. Doing the API/BACKEND match
# inside the regex looked neater and was wrong: the leading [A-Z] consumed the "A" of API_BASE_URL,
# so the very name this exists to find never matched.
_ENV_NAME_RE = re.compile(r"(?:process\.env|import\.meta\.env)\.([A-Z][A-Z0-9_]*)")
_API_ENV_HINTS = ("API", "BACKEND", "SERVER")


async def _api_env_names(provider: Any, thread_id: str) -> list[str]:
    """Env var names this repo reads to find its backend.

    The frontend and the API are started on ports chosen HERE, so an app whose base URL is baked in
    would call the wrong place: observed live, a Next.js rewrite proxying /api to
    `process.env.API_BASE_URL ?? "http://127.0.0.1:5080"` while the API had been given a free port,
    so every request 404'd, the page rendered its error state, and the e2e specs failed looking for
    an element that never mounted -- a green app reported as broken.
    """
    result = await provider.exec_in_sandbox(
        thread_id,
        "grep -rhoE '(process\\.env|import\\.meta\\.env)\\.[A-Z][A-Z0-9_]*' "
        "--include='*.ts' --include='*.tsx' --include='*.js' --include='*.mjs' --include='*.vue' "
        "--include='*.svelte' . 2>/dev/null "
        "| grep -vE '/(node_modules|\\.next|dist|build)/' | sort -u | head -40",
    )
    names: list[str] = []
    for token in (result.stdout or "").split():
        match = _ENV_NAME_RE.search(token)
        if not match:
            continue
        name = match.group(1)
        if any(hint in name for hint in _API_ENV_HINTS) and name not in names:
            names.append(name)
    return names


def _with_port_env(command: str, port: int, runtime: str) -> str:
    """Force an app onto `port` without knowing its framework's flag syntax.

    Env vars, not CLI flags: PORT is honoured by Next/Nuxt/Vite/Express/react-scripts, and
    ASPNETCORE_URLS by any ASP.NET Core host -- whereas the flag spelling differs per framework and
    guessing it wrong is the brittleness this pipeline keeps removing.
    """
    if runtime == "dotnet":
        return f"ASPNETCORE_URLS=http://127.0.0.1:{port} {command}"
    return f"PORT={port} {command}"


async def _kill_stale_app_processes(provider: Any, thread_id: str) -> None:
    """Kill leftover app servers in this sandbox. Safe by isolation: one container per session, so
    nothing here belongs to another run. No pkill/fuser/lsof in the image -- /proc is what there is.
    """
    patterns = "|".join(_STALE_APP_PATTERNS)
    # Excluding this shell AND its parent is not paranoia: the sweep's own command line contains
    # these patterns (they are the grep argument), so without the guard it matches itself and the
    # exec dies of its own SIGTERM -- observed immediately, exit code 143, before any real leftover
    # was reached. $PPID is excluded too because docker exec's `sh -c` wrapper carries the same text.
    await provider.exec_in_sandbox(
        thread_id,
        'me=$$; parent=$(awk "{print \\$4}" /proc/$$/stat 2>/dev/null); '
        "for d in /proc/[0-9]*; do "
        "  pid=${d#/proc/}; "
        '  [ "$pid" = "$me" ] && continue; '
        '  [ "$pid" = "$parent" ] && continue; '
        f"  if tr '\\0' ' ' < $d/cmdline 2>/dev/null | grep -qE {shlex.quote(patterns)}; then "
        "    kill -TERM $pid 2>/dev/null; victims=\"$victims $pid\"; "
        "  fi; "
        "done; "
        # WAIT for them to actually exit before returning. A `dotnet run` killed mid-build tears
        # down its own bin/ output as it dies, so booting immediately afterwards raced that cleanup
        # and the app launched against a half-deleted tree: "Could not load file or assembly
        # '.../Api.dll'" for a project that builds perfectly by hand. Polling beats a fixed sleep --
        # it is usually instant, and still bounded when something ignores SIGTERM.
        # A ZOMBIE still has a /proc entry, so `[ -d /proc/$p ]` would wait the full timeout for a
        # process that is already dead and merely unreaped -- measured: 10s of pure delay per call,
        # versus 0s once state is checked. Field 3 of /proc/<pid>/stat is the state letter.
        "for i in 1 2 3 4 5 6 7 8 9 10; do "
        '  alive=""; '
        "  for p in $victims; do "
        '    st=$(awk "{print \\$3}" /proc/$p/stat 2>/dev/null); '
        '    [ -n "$st" ] && [ "$st" != "Z" ] && alive="$alive $p"; '
        "  done; "
        '  [ -z "$alive" ] && break; sleep 1; '
        "done; "
        '  for p in $alive; do kill -KILL $p 2>/dev/null; done; '
        "sleep 1; true",
    )


def _scanned_launch_command(candidate: dict[str, Any]) -> str:
    """A scanned start_command made runnable from the repo root.

    app_discovery records what a marker file PROVES, not a runnable invocation: a package.json with a
    dev script yields the bare `npm run dev`, with no idea of its own directory. Executed from the
    repo root that dies with `ENOENT ... open '/workspace/repo/package.json'` -- observed live on a
    generated monorepo, where the app lives in apps/web. Only the PRIMARY app gets a GHCP-proven
    command (which includes its own cd); supporting services use this.

    Left alone when the command already changes directory or names its project explicitly (e.g.
    `dotnet run --project apps/api`), which is already root-relative.
    """
    command = str(candidate.get("start_command") or "").strip()
    path = str(candidate.get("path") or ".").strip()
    if not command or path in ("", "."):
        return command
    if command.startswith("cd ") or "--project" in command or "--prefix" in command:
        return command
    return f"cd {shlex.quote(path)} && {command}"


def _report_path_for(config_dir: str) -> str:
    """The playwright JSON report path, expressed relative to the directory the suite RUNS in.

    E2E_REPORT_PATH is repo-relative ('agent-work/e2e-report.json'), but the suite is invoked with
    `cd <config_dir>` -- so passing it unchanged would write the report inside apps/web/agent-work
    and the repo-relative read afterwards would find nothing.
    """
    if not config_dir:
        return E2E_REPORT_PATH
    return "/".join([".."] * len(config_dir.split("/"))) + f"/{E2E_REPORT_PATH}"


async def _boot_process(
    provider: Any, thread_id: str, launch_command: str, log_path: str, pid_path: str, *, env_file: bool
) -> None:
    """Start one detached process, recording its PID so teardown can kill it.

    Verified live (spike, 2026-08-19): a `nohup`'d process started by one `exec_in_sandbox` DOES
    survive that exec's exit and answers on its port from a SEPARATE exec, and the PID file lands
    correctly (every exec runs with -w /workspace/repo, so these relative paths resolve). That was
    this module's headline unknown; it is now confirmed rather than assumed.

    Fleet secrets are stripped from the started app's environment: docker exec inherits this
    container's own env (including the Copilot session's fleet PAT), and an app that leaked its env
    on an error page would otherwise get screenshotted and committed straight into git history. The
    vault env file is the OPPOSITE case -- secrets the user intends the app to have.
    """
    command = launch_command
    if env_file:
        command = f"set -a; . {keyvault.APP_ENV_PATH} 2>/dev/null; set +a; {launch_command}"
    await provider.exec_in_sandbox(
        thread_id,
        # setsid: the app becomes its own process-group leader, so teardown can kill the WHOLE
        # group. Killing the recorded pid alone left the real server (a child of the sh wrapper)
        # running -- which is how a previous attempt's dev server survived to hold port 3000.
        f"env -u COPILOT_SDK_AUTH_TOKEN -u COPILOT_CONNECTION_TOKEN -u GITHUB_TOKEN "
        f"setsid nohup sh -c {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 & "
        f"echo $! > {shlex.quote(pid_path)}",
    )


async def _wait_ready(provider: Any, thread_id: str, port: int) -> bool:
    """True once the port SPEAKS HTTP -- any status code, not necessarily a successful one.

    Deliberately not `curl -sf`: -f makes curl exit non-zero on 4xx/5xx, so an API whose routes are
    all under /api and which correctly 404s on `/` read as "never answered" for the full 120s
    timeout while its own log said `Now listening on: http://localhost:5033`. Readiness here means
    the server is accepting connections; whether `/` is a route is the app's business.
    `%{http_code}` is 000 when the connection itself failed, which is the real not-up signal.
    """
    for _ in range(max(1, workflow_config.E2E_APP_READY_TIMEOUT_SECONDS // 3)):
        probe = await provider.exec_in_sandbox(
            thread_id,
            f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' http://localhost:{port} 2>/dev/null || true",
        )
        if (probe.stdout or "").strip() not in ("", "000"):
            return True
        await asyncio.sleep(3)
    return False


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

    candidates = e2e.get("app_candidates") or []
    # Deduplicate by directory FIRST. app_discovery emits one candidate per marker file, so a single
    # Next.js app yields two records for apps/web (its package.json and its next.config.ts). Without
    # this, one copy became the primary app and its own duplicate was booted a second time as a
    # "supporting service", which then failed its readiness probe against the port the primary
    # already held -- reported as "the supporting app at apps/web never answered".
    startable: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        if not str(candidate.get("start_command") or "").strip():
            continue
        path = str(candidate.get("path") or ".")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        startable.append(candidate)
    # BACKEND FIRST, then the UI app. A full-stack repo has two processes, and the previous code
    # booted only the first startable candidate -- for a generated monorepo that is the web app,
    # leaving the API down. Every screenshot then showed a UI whose fetch calls failed, which still
    # satisfied "screenshots exist" and shipped visual "evidence" of a working app that wasn't.
    # A dependency ordering, not a preference: the UI's first render may call the API.
    startable.sort(key=lambda a: 0 if _candidate_class(a) in ("api", "service", "azure_function") else 1)
    # The app to point playwright at is the UI one when there is one (that's what has routes to
    # screenshot); otherwise whatever single process exists.
    app = next((a for a in startable if _candidate_class(a) == "web"), None) or (
        startable[0] if startable else None
    )

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

    # GHCP discovers AND proves the launch values (it actually starts the app, curls it, then
    # stops it) instead of Python trusting app_discovery's static guess -- the same wrong-root /
    # wrong-command failure family that broke every other stack-specific invocation. Python still
    # owns the launch itself: this app must outlive the GHCP turn, and a detached process started
    # inside a finished tool call has no defined lifetime.
    # The port is chosen HERE, before the agent runs, and handed to it -- rather than letting each
    # side guess. 3000 is taken by the sandbox's own Copilot server (see _APP_PORT_RANGE), and a dev
    # server that finds its port busy silently moves to another one, which reads as a dead app.
    requested_port = await _pick_free_port(provider, thread_id, int(app.get("port") or 0))
    launch = await stack_runner.run_and_report(
        thread_id,
        stage_key="e2e-run",
        prompt_name="e2e_run",
        schema=AppLaunchReport,
        requested_port=str(requested_port),  # render_prompt substitutes strings only
    )
    if launch.success and launch.start_command:
        port = int(launch.port or requested_port)
        start_command = launch.start_command
    else:
        # Fall back to the scan's own candidate rather than failing outright: a proven command is
        # better, but an unproven one is still better than not trying to boot at all.
        logger.warning("e2e: launch discovery failed (%s) -- falling back to scanned candidate", launch.error)
        port = requested_port
        # Same cd-injection the supporting services need: a scanned command is not root-runnable,
        # plus an explicit port so it cannot drift onto one the probe is not watching.
        start_command = _with_port_env(
            _scanned_launch_command(app), port, str(app.get("runtime") or "")
        )

    # App secrets (keyvault.py): fetched on-behalf-of the user at provision time, injected here
    # as an env file sourced only into the app's own shell. Cache empty but a vault IS configured
    # means the agent restarted since provision -- an infra/user-action gap the e2e fix LLM can't
    # patch, so escalate (cannot_verify) instead of burning fix cycles booting a secretless app.
    app_secrets = keyvault.get_app_secrets(thread_id)
    if app_secrets is None:
        sess_row = await session_store.get_session(thread_id)
        if sess_row is not None and await keyvault.get_vault_uri(
            sess_row["owner"], sess_row["repo"], sess_row["user_login"]
        ):
            e2e["status"] = "failed"
            e2e["cannot_verify"] = True
            e2e["failed_tests"] = [{
                "title": "app secrets",
                "error": "a key vault is configured for this repo but no secrets are cached "
                         "(agent restarted since provision?) -- click 'Refresh Key Vault secrets' "
                         "in the workspace header, then retry",
            }]
            return {"e2e": e2e}
    use_env_file = bool(app_secrets)
    if app_secrets:
        await keyvault.write_env_file(provider, thread_id, app_secrets)

    # Clear the field before booting. A fix cycle re-enters this node, and the launch-discovery agent
    # is asked to stop whatever it starts but does not reliably do so -- a survivor holding the port
    # makes a dev server silently pick a DIFFERENT port ("Port 3000 is in use ... using 3002") while
    # the readiness probe watches the one we asked for, reporting a healthy app as never answering.
    await _kill_stale_app_processes(provider, thread_id)

    # Boot the SUPPORTING processes (an API/service the UI calls) before the app playwright drives.
    # Their readiness is required, not best-effort: a UI screenshotted against a dead API renders
    # error states or empty lists, and that image would still satisfy exit's "screenshots exist"
    # check -- shipping visual evidence of an app that does not work. Failing here with the API's own
    # log tail is strictly more useful than a green run with misleading pictures.
    service_urls: list[str] = []
    for index, other in enumerate([a for a in startable if a is not app], start=1):
        other_log = f"agent-work/e2e-service-{index}.log"
        # An explicit, verified-free port for every service too -- otherwise a .NET API takes the
        # framework default (5000/5080) which may already be held, and drifts exactly as the web app
        # did. Probed unconditionally now: a supporting service we booted has an HTTP surface by
        # definition, since app_discovery only gives a start_command to something it can serve.
        other_port = await _pick_free_port(provider, thread_id, int(other.get("port") or 0))
        service_urls.append(f"http://127.0.0.1:{other_port}")
        await _boot_process(
            provider, thread_id,
            _with_port_env(_scanned_launch_command(other), other_port, str(other.get("runtime") or "")),
            other_log, f"agent-work/e2e-service-{index}.pid", env_file=use_env_file,
        )
        if not await _wait_ready(provider, thread_id, other_port):
            log_tail = (await repo_files.read_repo_file(provider, thread_id, other_log) or "")[-3000:]
            e2e.update(
                status="failed", total=0, passed=0, screenshots=[],
                failed_tests=[{
                    "title": f"{other.get('name') or other.get('path')} readiness",
                    "error": (
                        f"the {_candidate_class(other) or 'supporting'} app at {other.get('path')} never "
                        f"answered on port {other_port} within "
                        f"{workflow_config.E2E_APP_READY_TIMEOUT_SECONDS}s, so the UI would be "
                        f"exercised against a dead backend -- log tail:\n{log_tail}"
                    ),
                }],
            )
            return await _finalize_run(provider, thread_id, e2e)

    # Point the UI at the API we actually started. Without this the app keeps its baked-in default
    # (a port nothing is listening on, since Python chose the API's port), every request fails, and
    # the suite reports a healthy app as broken.
    if service_urls:
        api_env = " ".join(f"{name}={shlex.quote(service_urls[0])}" for name in await _api_env_names(provider, thread_id))
        if api_env:
            logger.info("e2e: pointing app env at booted service: %s", api_env)
            start_command = f"{api_env} {start_command}"
    await _boot_process(
        provider, thread_id, start_command, E2E_APP_LOG_PATH, E2E_APP_PID_PATH, env_file=use_env_file
    )
    ready = await _wait_ready(provider, thread_id, port)

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

    runner = await _playwright_runner_available(provider, thread_id, str(e2e.get("config_dir") or ""))
    if runner is None:
        # Should not normally happen -- e2e_gate_check_node already confirmed one of these
        # resolves. A fix commit that removed the dependency mid-loop gets the same treatment.
        e2e.update(status="skipped", skipped_reason="@playwright/test is no longer resolvable and no global playwright runner is installed", failed_tests=[], screenshots=[])
        return await _finalize_run(provider, thread_id, e2e)

    # No runtime `playwright install` -- PLAYWRIGHT_BROWSERS_PATH is baked into the image at
    # /opt/playwright-browsers (chromium-headless-shell, Dockerfile), never a per-owner cache
    # volume: concurrent sessions of the same owner previously raced to extract browser binaries
    # into that shared, non-content-addressed path, and on-the-fly installs are explicitly not
    # wanted regardless. Only the invocation binary differs between "local" (repo's own
    # @playwright/test via npx) and "global" (the image's pinned fallback CLI) runners.
    config_dir = str(e2e.get("config_dir") or "")
    # Always populated: the standalone `playwright screenshot` calls below use it too.
    await _alias_browsers_for_local_runner(provider, thread_id)
    if runner == "global":
        # Makes the idiomatic `@playwright/test` import resolve against the image's global package.
        await _link_global_playwright(provider, thread_id, config_dir)
    else:
        # The repo brought its own playwright, which pins a browser revision the image does not bake.
        await _alias_browsers_for_local_runner(provider, thread_id)

    # Paths that the suite's own working directory owns. Playwright must run from the directory
    # holding its config (previously it always ran from the repo root, which simply found no config
    # in a monorepo), so the report path and the harvest below are relative to THAT directory.
    cd_prefix = f"cd {shlex.quote(config_dir)} && " if config_dir else ""
    results_root = f"{config_dir}/test-results" if config_dir else "test-results"

    suite_result = None
    if e2e.get("suite"):
        run_cmd = "npx playwright test" if runner == "local" else "playwright test"
        command = (
            f"{cd_prefix}PLAYWRIGHT_BROWSERS_PATH={shlex.quote(BROWSER_ALIAS_DIR)} "
            f"PLAYWRIGHT_JSON_OUTPUT_NAME={shlex.quote(_report_path_for(config_dir))} "
            f"BASE_URL=http://localhost:{port} "
            f"timeout {workflow_config.E2E_SUITE_TIMEOUT_SECONDS} {run_cmd} --reporter=json 2>&1"
        )
        keepalive = asyncio.create_task(_keepalive_touch(provider, thread_id))
        try:
            suite_result = await provider.exec_in_sandbox(thread_id, command)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
    else:
        # No suite in the repo -- the app is up and this stage's other job (visual evidence) is
        # still owed. Falls through to the screenshot pass below with a zero-test result.
        logger.info("e2e: no playwright suite found for thread_id=%s -- capturing screenshots only", thread_id)

    combined_output = ((suite_result.stdout or "") + (suite_result.stderr or "")) if suite_result else ""
    if "Executable doesn't exist" in combined_output:
        # Infra gap (browser binary missing) -- not fixable by the LLM fixer, so skip with a
        # reason rather than burn a fix cycle on it.
        e2e.update(status="skipped", skipped_reason="playwright browser executable is missing in this environment", failed_tests=[], screenshots=[])
        return await _finalize_run(provider, thread_id, e2e)

    if suite_result and suite_result.returncode == 124:
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
    find_result = await provider.exec_in_sandbox(
        thread_id, f"find {shlex.quote(results_root)} -name '*.png' -print0 2>/dev/null"
    )
    found_paths = [p for p in (find_result.stdout or "").split("\x00") if p]
    screenshots: list[str] = []
    await provider.exec_in_sandbox(thread_id, f"mkdir -p {shlex.quote(screens_dir)}")
    for index, path in enumerate(found_paths, start=1):
        dest = f"{screens_dir}/{index:03d}-suite.png"
        await provider.exec_in_sandbox(thread_id, f"cp -- {shlex.quote(path)} {shlex.quote(dest)}")
        screenshots.append(dest)

    # Per-route screenshots, ALWAYS taken (not just as a fallback). Two reasons: playwright's
    # default screenshot config is only-on-failure, so a green suite harvests nothing; and a suite's
    # own images show whatever state its assertions left behind, whereas the human reviewing exit.md
    # wants a plain picture of each screen the app serves. Named after the route so the report can
    # label them, which is what makes "list of screens created" possible at all.
    shot_cmd = "npx playwright screenshot" if runner == "local" else "playwright screenshot"
    routes = [r for r in (launch.routes or []) if str(r).startswith("/")] or ["/"]
    for index, route in enumerate(routes[:12], start=1):
        dest = f"{screens_dir}/{index:03d}-{_route_slug(route)}.png"
        shot = await provider.exec_in_sandbox(
            thread_id,
            f"PLAYWRIGHT_BROWSERS_PATH={shlex.quote(BROWSER_ALIAS_DIR)} "
            f"{shot_cmd} --full-page {shlex.quote(f'http://localhost:{port}{route}')} "
            f"{shlex.quote(dest)} 2>&1",
        )
        landed = await provider.exec_in_sandbox(thread_id, f"ls {shlex.quote(dest)} 2>/dev/null")
        if (landed.stdout or "").strip():
            screenshots.append(dest)
        else:
            # Best-effort per route: a browser/infra gap is not fixable by the e2e fix LLM, and
            # exit's own verify is what blocks a UI merge with zero screenshots. Same
            # "Executable doesn't exist" surface the suite path checks -- surfaced here too, since
            # only chromium-headless-shell is baked and a full-chromium code path fails on it.
            logger.warning(
                "e2e route screenshot failed for thread_id=%s route=%s: %s",
                thread_id, route, (shot.stdout or "")[-500:],
            )
    e2e["screenshots"] = screenshots
    e2e["routes"] = routes

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

    # Route -> filename slug. These names are what let exit.md label each screenshot with the
    # screen it shows, and routes arrive from a model report, so anything unsafe is dropped.
    assert _route_slug("/") == "home"
    assert _route_slug("/expenses") == "expenses"
    assert _route_slug("/expenses/new") == "expenses-new"
    assert _route_slug("/a b;rm -rf/") == "a-b-rm-rf"  # no shell metacharacters survive

    # The playwright JSON report path has to be rewritten relative to the directory the suite runs
    # in. Passing the repo-relative path unchanged wrote the report to apps/web/agent-work/... and
    # the repo-relative read afterwards found nothing -- a silently testless "0 tests" result.
    assert _report_path_for("") == E2E_REPORT_PATH
    assert _report_path_for("apps/web") == f"../../{E2E_REPORT_PATH}"
    assert _report_path_for("packages/a/b") == f"../../../{E2E_REPORT_PATH}"

    # A scanned start_command must be runnable FROM THE REPO ROOT. app_discovery records `npm run
    # dev` with no directory, which died with ENOENT on /workspace/repo/package.json when the app
    # actually lived in apps/web.
    assert _scanned_launch_command({"path": "apps/web", "start_command": "npm run dev"}) == "cd apps/web && npm run dev"
    assert _scanned_launch_command({"path": ".", "start_command": "npm run dev"}) == "npm run dev"
    # Commands that already resolve their own location are left untouched -- no double cd.
    assert _scanned_launch_command(
        {"path": "apps/api", "start_command": "dotnet run --project apps/api"}
    ) == "dotnet run --project apps/api"
    assert _scanned_launch_command(
        {"path": "apps/web", "start_command": "cd apps/web && npm start"}
    ) == "cd apps/web && npm start"
    assert _scanned_launch_command({"path": "apps/web", "start_command": ""}) == ""

    # The app class must be read from EITHER shape. e2e stores app_discovery's raw candidates
    # (`likely_class`); only the manifest carries `app_class`. Reading one key alone made every raw
    # candidate classless, so playwright was pointed at apps/api and the UI app was demoted to a
    # "supporting service" probed on the wrong port.
    assert _candidate_class({"likely_class": "web"}) == "web"
    assert _candidate_class({"app_class": "web"}) == "web"
    assert _candidate_class({"likely_class": "API"}) == "api"
    assert _candidate_class({}) == ""

    # Port forcing is by ENV VAR, not a framework-specific flag -- PORT works across
    # Next/Nuxt/Vite/Express, ASPNETCORE_URLS across any ASP.NET Core host.
    assert _with_port_env("npm run dev", 3100, "node") == "PORT=3100 npm run dev"
    assert _with_port_env("dotnet run --project apps/api", 3101, "dotnet") == (
        "ASPNETCORE_URLS=http://127.0.0.1:3101 dotnet run --project apps/api"
    )
    # 3000 must never be handed out: it belongs to the sandbox's own Copilot server.
    assert 3000 not in _APP_PORT_RANGE

    # Primary selection over realistic raw candidates: the UI app is chosen, APIs boot first.
    _raw = [
        {"path": "apps/api", "likely_class": "api", "start_command": "dotnet run --project apps/api"},
        {"path": "apps/web", "likely_class": "web", "start_command": "npm run dev", "port": 3000},
    ]
    _sorted = sorted(_raw, key=lambda a: 0 if _candidate_class(a) in ("api", "service", "azure_function") else 1)
    assert _sorted[0]["path"] == "apps/api", _sorted
    _primary = next((a for a in _sorted if _candidate_class(a) == "web"), None)
    assert _primary is not None and _primary["path"] == "apps/web", _primary

    print("e2e_nodes self-check: ok")


if __name__ == "__main__":
    _demo()
