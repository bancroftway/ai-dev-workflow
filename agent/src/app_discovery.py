"""Runnable-application discovery: the suitability gate for the whole workflow.

ai-dev-workflow only applies to a repository containing at least one startable application (web
app, API, or Azure Function) that the sandbox can actually run. A class library, an SDK package,
or back-end code with no entrypoint has nothing for P4's tests to exercise or P6's code to make
green, so such a repo is rejected outright -- the one hard stop in the entire pipeline.

Chain: app_discovery_pre (deterministic scan) -> app-discovery StageSpec (draft -> audit ->
auto-gate) -> app_discovery_decide -> (app_discovery_reject -> END | scaffold_finalize -> ...).

The split between deterministic and LLM work is deliberate, and matches every other gate in this
pipeline: the scan supplies the evidence, the model classifies it, and `decide_suitability` --
plain Python over the audited report, never the model's own `suitable` flag -- returns the
verdict. The model can still surface an app the scan's marker table never looked for (it gets
read-only sandbox tools), and the decision node accepts any app whose cited path really exists;
the scan is a floor, not a ceiling.

Deliberately static: nothing here launches an app. `start_command` is recorded from file evidence
and is not verified by execution -- `confidence` carries that uncertainty.

Verification status: the pure half (classify_candidates/fingerprint/decide_suitability) has an
assert-based self-check, runnable with `uv run python -m src.app_discovery`. The sandbox-I/O half
has NOT been exercised against a real container.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from . import git_ops, repo_files, session_index
from .preflight_nodes import MANIFEST_PATH, update_manifest
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

logger = logging.getLogger(__name__)

STAGE_KEY = "app-discovery"

# An app of one of these classes, with a start command and a runtime the container has, is what
# makes a repository suitable. Everything else (library/cli/unknown) is not, and `mobile` is
# rejected on purpose: the sandbox is a Linux container with no Android SDK, JDK/Gradle or Xcode.
SUITABLE_CLASSES = {"web", "api", "azure_function"}
STARTABLE_RUNTIME_PREFIXES = ("dotnet", "node", "python")

_MAX_CANDIDATE_FILES = 60
_MAX_FILE_CHARS = 4000
_MAX_EVIDENCE_CHARS = 24000

_PRUNE_DIRS = ("node_modules", ".git", "bin", "obj", "dist", "build", ".venv", "vendor", "Pods")
_CANDIDATE_NAMES = (
    "*.csproj", "host.json", "launchSettings.json", "package.json", "Program.cs", "Startup.cs",
    "Dockerfile", "docker-compose*.yml", "docker-compose*.yaml", "Procfile", "main.py", "app.py",
    "wsgi.py", "asgi.py", "manage.py", "pyproject.toml", "requirements.txt", "next.config.*",
    "vite.config.*", "app.json", "capacitor.config.*", "ionic.config.json", "AndroidManifest.xml",
    "build.gradle", "build.gradle.kts", "pom.xml", "go.mod",
)


def _find_command() -> str:
    prune = " -o ".join(f"-name {d}" for d in _PRUNE_DIRS)
    names = " -o ".join(f"-name '{n}'" for n in _CANDIDATE_NAMES)
    return (
        f"find . -maxdepth 6 \\( {prune} \\) -prune -o -type f \\( {names} \\) -print "
        f"2>/dev/null | head -200"
    )


# --------------------------------------------------------------------------------------------
# Pure half -- no sandbox, no I/O, self-checked at the bottom of this module.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SuitabilityDecision:
    suitable: bool
    reasons: list[str]


def fingerprint(files: dict[str, str]) -> str:
    """Stable digest over both the candidate paths AND their contents.

    Content matters: adding a `"dev"` script to an existing package.json renames nothing, and a
    path-only digest would leave a repo that just became runnable classified as a library forever.
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(hashlib.sha256(files[path].encode("utf-8")).digest())
    return f"sha256:{digest.hexdigest()}"


def _app_dir(path: str) -> str:
    parent = path.rsplit("/", 1)[0] if "/" in path else "."
    # A .NET project's launchSettings.json lives in <app>/Properties/, not <app>/.
    return parent[: -len("/Properties")] if parent.endswith("/Properties") else parent


def _csproj_signals(text: str) -> dict[str, Any] | None:
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"', text):
        return {"likely_class": "api", "runtime": "dotnet", "marker": 'Sdk="Microsoft.NET.Sdk.Web"'}
    if re.search(r"<AzureFunctionsVersion>|Microsoft\.NET\.Sdk\.Functions|Microsoft\.Azure\.Functions\.Worker", text):
        return {"likely_class": "azure_function", "runtime": "dotnet", "marker": "Azure Functions SDK reference"}
    if re.search(r"<OutputType>\s*Exe\s*</OutputType>", text):
        return {"likely_class": "cli", "runtime": "dotnet", "marker": "<OutputType>Exe</OutputType>"}
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk"', text):
        return {"likely_class": "library", "runtime": "dotnet", "marker": 'Sdk="Microsoft.NET.Sdk" with no OutputType'}
    return None


_MOBILE_DEPS = ("react-native", "expo", "@capacitor/core", "@ionic/")
_WEB_DEPS = ("next", "express", "fastify", "koa", "@nestjs/core", "vite", "nuxt", "@remix-run/")


def _package_json_signals(text: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    deps = {**(doc.get("dependencies") or {}), **(doc.get("devDependencies") or {})}
    scripts = doc.get("scripts") or {}
    start_script = next((s for s in ("dev", "start", "serve") if scripts.get(s)), None)

    if any(any(d.startswith(m) for m in _MOBILE_DEPS) for d in deps):
        return {"likely_class": "mobile", "runtime": "node", "marker": "react-native/expo/capacitor/ionic dependency"}
    if "@azure/functions" in deps:
        return {"likely_class": "azure_function", "runtime": "node", "marker": "@azure/functions dependency"}
    if any(any(d.startswith(m) for m in _WEB_DEPS) for d in deps) and start_script:
        matched = next(d for d in deps if any(d.startswith(m) for m in _WEB_DEPS))
        return {
            "likely_class": "web",
            "runtime": "node",
            "marker": f"{matched} dependency with a '{start_script}' script",
            "start_command": f"npm run {start_script}",
        }
    if start_script:
        return {"likely_class": "unknown", "runtime": "node", "marker": f"'{start_script}' script, no known web framework"}
    if doc.get("main") or doc.get("exports"):
        return {"likely_class": "library", "runtime": "node", "marker": "main/exports with no start, dev or serve script"}
    return None


_PY_WEB_RE = re.compile(r"fastapi|flask|django|uvicorn|gunicorn", re.IGNORECASE)


def classify_candidates(files: dict[str, str]) -> list[dict[str, Any]]:
    """Candidate marker files (path -> truncated content) -> candidate app records.

    Pure and deliberately conservative: it reports what a marker proves, nothing more. The LLM
    stage turns these into DiscoveredApp records with names and start commands; this function's
    output is the grounding evidence and the allowlist of paths the model may cite.
    """
    candidates: list[dict[str, Any]] = []

    def add(path: str, signals: dict[str, Any]) -> None:
        candidates.append({"path": _app_dir(path), "source": path, **signals})

    for path, text in sorted(files.items()):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".csproj"):
            signals = _csproj_signals(text)
            if signals:
                add(path, signals)
        elif name == "host.json":
            add(path, {"likely_class": "azure_function", "runtime": "unknown", "marker": "host.json present"})
        elif name == "package.json":
            signals = _package_json_signals(text)
            if signals:
                add(path, signals)
        elif name == "app.json" and '"expo"' in text:
            add(path, {"likely_class": "mobile", "runtime": "node", "marker": 'app.json declares "expo"'})
        elif name in ("capacitor.config.json", "capacitor.config.ts", "ionic.config.json", "AndroidManifest.xml"):
            add(path, {"likely_class": "mobile", "runtime": "unknown", "marker": f"{name} present"})
        elif name in ("Program.cs", "Startup.cs"):
            if re.search(r"WebApplication\.CreateBuilder|CreateHostBuilder|MapGet|MapControllers", text):
                add(path, {"likely_class": "api", "runtime": "dotnet", "marker": f"{name} builds a web host"})
            elif "ConfigureFunctionsWorkerDefaults" in text or "FunctionsApplication.CreateBuilder" in text:
                add(path, {"likely_class": "azure_function", "runtime": "dotnet", "marker": f"{name} builds a Functions host"})
        elif name == "manage.py":
            add(path, {"likely_class": "web", "runtime": "python", "marker": "Django manage.py", "start_command": "python manage.py runserver"})
        elif name in ("main.py", "app.py", "asgi.py", "wsgi.py"):
            if re.search(r"FastAPI\(|Flask\(", text):
                add(path, {"likely_class": "api", "runtime": "python", "marker": f"{name} instantiates FastAPI/Flask"})
        elif name in ("pyproject.toml", "requirements.txt"):
            if _PY_WEB_RE.search(text):
                add(path, {"likely_class": "api", "runtime": "python", "marker": f"{name} declares a Python web framework"})
        elif name == "Procfile":
            if re.search(r"^web:", text, re.MULTILINE):
                add(path, {"likely_class": "web", "runtime": "unknown", "marker": "Procfile web: process"})

    # Ports are corroborating evidence, attached to whatever app owns the directory.
    for path, text in sorted(files.items()):
        port = _extract_port(path, text)
        if port is None:
            continue
        owner = _app_dir(path)
        for candidate in candidates:
            if candidate["path"] == owner and candidate.get("port") is None:
                candidate["port"] = port
    return candidates


def _extract_port(path: str, text: str) -> int | None:
    name = path.rsplit("/", 1)[-1]
    if name == "launchSettings.json":
        match = re.search(r"https?://[^\"',\s]*:(\d{2,5})", text)
        return int(match.group(1)) if match else None
    if name == "Dockerfile":
        match = re.search(r"^EXPOSE\s+(\d{2,5})", text, re.MULTILINE)
        return int(match.group(1)) if match else None
    if name == "package.json" and '"next"' in text:
        return 3000
    return None


def decide_suitability(apps: list[dict[str, Any]]) -> SuitabilityDecision:
    """The verdict. Plain Python over the audited report -- never the model's own `suitable`."""
    startable = [
        app
        for app in apps
        if app.get("app_class") in SUITABLE_CLASSES
        and str(app.get("runtime") or "").startswith(STARTABLE_RUNTIME_PREFIXES)
        and (app.get("start_command") or "").strip()
    ]
    if startable:
        return SuitabilityDecision(True, [])

    reasons: list[str] = []
    by_class: dict[str, list[str]] = {}
    for app in apps:
        by_class.setdefault(str(app.get("app_class") or "unknown"), []).append(str(app.get("path") or "?"))

    if not apps:
        reasons.append(
            "No application manifests or entrypoints were found. ai-dev-workflow needs at least "
            "one startable web app, API, or Azure Function."
        )
    if by_class.get("mobile"):
        reasons.append(
            f"Only a mobile application was found ({', '.join(by_class['mobile'])}). "
            "ai-dev-workflow runs in a Linux container, which cannot build or run an iOS/Android app."
        )
    library_like = [p for cls in ("library", "cli", "unknown") for p in by_class.get(cls, [])]
    if library_like:
        reasons.append(
            f"The projects found are libraries or non-startable code ({', '.join(library_like)}). "
            "ai-dev-workflow needs at least one startable web app, API, or Azure Function."
        )
    unstartable = [
        str(app.get("path"))
        for app in apps
        if app.get("app_class") in SUITABLE_CLASSES and not (app.get("start_command") or "").strip()
    ]
    if unstartable:
        reasons.append(
            f"An application was identified at {', '.join(unstartable)} but no start command could "
            "be determined from the repository, so the sandbox cannot run it."
        )
    if not reasons:
        reasons.append("No startable application could be confirmed in this repository.")
    return SuitabilityDecision(False, reasons)


# --------------------------------------------------------------------------------------------
# Sandbox-I/O half and the graph nodes.
# --------------------------------------------------------------------------------------------


async def collect_evidence(provider: SandboxProvider, thread_id: str) -> dict[str, Any]:
    """Bounded scan -> {"candidates": [...], "evidence": "...", "fingerprint": "..."}.

    Bounds mirror preflight_nodes.brownfield_baseline_context_node: a capped `find`, then a capped number
    of capped reads, then a capped blob -- a prompt-grounding artifact, not a repo dump.
    """
    listing = await provider.exec_in_sandbox(thread_id, _find_command())
    paths = [p.strip().lstrip("./") for p in (listing.stdout or "").splitlines() if p.strip()]

    files: dict[str, str] = {}
    for path in paths[:_MAX_CANDIDATE_FILES]:
        try:
            content = await repo_files.read_repo_file(provider, thread_id, path)
        except ValueError:
            # validate_repo_relative_path rejects spaces/unicode -- skip the file, never fail the run.
            continue
        if content is not None:
            files[path] = content[:_MAX_FILE_CHARS]

    sections = [f"--- {path} ---\n{text}" for path, text in sorted(files.items())]
    return {
        "candidates": classify_candidates(files),
        "evidence": "\n\n".join(sections)[:_MAX_EVIDENCE_CHARS],
        "fingerprint": fingerprint(files),
        "scanned_file_count": len(files),
    }


async def app_discovery_pre_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Deterministic grounding scan. Always runs, even on the hydrated path -- it is a `find` plus
    a handful of reads, and its fingerprint is what tells the draft node whether the recorded
    answer is still valid."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        # Same tolerance as scaffold_node: no sandbox, no scan. The decide node treats an
        # unscanned repo as "not assessed" and lets the run through rather than rejecting it.
        return {"app_scan": {}}
    return {"app_scan": await collect_evidence(get_sandbox_provider(), thread_id)}


async def hydrate_from_manifest(
    thread_id: str, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.hydrate_from_repo_file: skip the LLM entirely when this exact repo state was
    already assessed and accepted. Invalidated by the evidence fingerprint, so a repo that gained
    an app since the last run is re-assessed rather than trusted."""
    raw = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH)
    if raw is None:
        return None
    try:
        recorded = (json.loads(raw) or {}).get("app_check") or {}
    except json.JSONDecodeError:
        logger.warning("manifest.json is not valid JSON; re-running app discovery")
        return None
    if not recorded.get("suitable"):
        return None
    if recorded.get("evidence_fingerprint") != (state.get("app_scan") or {}).get("fingerprint"):
        return None
    return {
        "apps": recorded.get("apps") or [],
        "suitable": True,
        "rejection_reasons": [],
        "notes": f"Hydrated from manifest.json (assessed in run {recorded.get('run_id')}).",
    }


def _surviving_apps(report: dict[str, Any], scan: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop any app whose path the model invented. A scan candidate is proof enough; so is a path
    that really exists (the model gets read-only tools and may legitimately find a stack the
    marker table never looked for)."""
    candidate_paths = {c["path"] for c in (scan.get("candidates") or [])}
    surviving: list[dict[str, Any]] = []
    for app in report.get("apps") or []:
        path = str(app.get("path") or "").strip()
        if not path:
            continue
        if path in candidate_paths or path == ".":
            surviving.append(app)
            continue
        try:
            repo_files.validate_repo_relative_path(path)
        except ValueError:
            logger.info("Dropping app with unusable path %r", path)
            continue
        surviving.append(app)
    return surviving


async def app_discovery_decide_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """The suitability verdict -- deterministic, and the only hard stop in the pipeline.

    Fails closed: no report at all is a rejection, with a reason that says so honestly rather than
    blaming the repository."""
    thread_id = config["configurable"]["thread_id"]
    scan = state.get("app_scan") or {}

    if sandbox_registry.get(thread_id) is None:
        # Nothing was scanned, so nothing can be concluded. Never reject on absent evidence.
        return {"app_rejection": None}

    report = (state.get("stages") or {}).get(STAGE_KEY, {}).get("approved_content")
    if not report:
        decision = SuitabilityDecision(
            False,
            [
                "App discovery produced no report, so this repository could not be assessed. "
                "This is a failure of the discovery step itself, not a finding about the repository."
            ],
        )
        surviving: list[dict[str, Any]] = []
    else:
        surviving = _surviving_apps(report, scan)
        decision = decide_suitability(surviving)
        decision = SuitabilityDecision(
            decision.suitable, [*decision.reasons, *(report.get("rejection_reasons") or [])]
        )

    await repo_files.append_ledger_entry(
        get_sandbox_provider(),
        thread_id,
        {"stage": STAGE_KEY, "node": "decide", "suitable": decision.suitable, "app_count": len(surviving)},
    )

    if decision.suitable:
        return {"app_rejection": None}
    return {
        "app_rejection": {
            "reasons": decision.reasons,
            "found": [{"path": a.get("path"), "app_class": a.get("app_class")} for a in surviving],
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


async def app_discovery_reject_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Hard stop. Surfaces the reasons two ways (shared state for the banner, a chat message for
    the sidebar) and returns the repository to exactly the state it arrived in."""
    thread_id = config["configurable"]["thread_id"]
    rejection = state.get("app_rejection") or {}
    reasons = rejection.get("reasons") or ["This repository is not suitable for ai-dev-workflow."]

    lines = ["**ai-dev-workflow cannot run on this repository.**", ""]
    lines += [f"- {reason}" for reason in reasons]
    lines += ["", "No changes were made to the repository."]

    if sandbox_registry.get(thread_id) is not None:
        provider = get_sandbox_provider()
        cleanup_note = await _clean_up_repo(provider, thread_id, state.get("run_baseline_commit"))
        if cleanup_note:
            lines += ["", cleanup_note]

        # The cleanup reset above resets to run_baseline_commit and never touches sessions.json
        # (git clean is scoped to .ai-dev-workflow, but sessions.json's own commit already
        # happened before that baseline was captured -- see scaffold_node -- so it survives the
        # reset). This is the one writer that closes the session's row AFTER that reset, so it
        # needs its own commit+push rather than relying on some other node's commit to pick it up.
        await session_index.end_session(provider, thread_id, run_id=state.get("run_id"), status="rejected")
        await git_ops.commit_paths(
            provider, thread_id, [session_index.SESSIONS_PATH], "ai-dev-workflow: session rejected"
        )

    return {"messages": [AIMessage(content="\n".join(lines))]}


async def _clean_up_repo(provider: SandboxProvider, thread_id: str, baseline: str | None) -> str | None:
    """Undo the workflow's own commits and remove its own directory.

    Guarded: if anything in baseline..HEAD was not committed by this workflow, the reset is
    skipped entirely and reported. Discarding someone else's work to tidy up would be a far worse
    outcome than leaving a few files behind.
    """
    if not baseline:
        return None

    log = await provider.exec_in_sandbox(thread_id, f"git log --format=%s {baseline}..HEAD")
    subjects = [s for s in (log.stdout or "").splitlines() if s.strip()]
    foreign = [s for s in subjects if not s.startswith("ai-dev-workflow:")]
    if foreign:
        logger.info("Skipping cleanup reset for thread_id=%s: foreign commits present", thread_id)
        return (
            "Note: the sandbox contains commits this workflow did not make, so its own commits "
            "were left in place rather than risk discarding your work."
        )

    await provider.exec_in_sandbox(
        thread_id, f"git reset --hard {baseline} && git clean -fd -- .ai-dev-workflow"
    )
    return None


async def app_check_record_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Records the accepted apps in the manifest.

    Placed after the brownfield-baseline branch converges, never before it: scaffold_node treats the mere existence
    of manifest.json as "already onboarded", so creating the file earlier would let a run abandoned
    mid-brownfield-baseline skip brownfield ratification forever.
    """
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {}

    report = (state.get("stages") or {}).get(STAGE_KEY, {}).get("approved_content") or {}
    scan = state.get("app_scan") or {}
    provider = get_sandbox_provider()

    await update_manifest(
        provider,
        thread_id,
        {
            "app_check": {
                "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "run_id": state.get("run_id", "unknown"),
                "suitable": True,
                "evidence_fingerprint": scan.get("fingerprint"),
                "apps": _surviving_apps(report, scan),
            }
        },
    )
    await git_ops.commit_ai_dev_workflow(provider, thread_id, "ai-dev-workflow: record runnable apps")
    return {}


def _demo() -> None:
    """Self-check for the pure half: `uv run python -m src.app_discovery`."""
    library = {
        "src/Foo/Foo.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>',
        "src/Foo/Calculator.cs": "public class Calculator {}",
    }
    apps = classify_candidates(library)
    assert [a["likely_class"] for a in apps] == ["library"], apps
    assert not decide_suitability(
        [{"path": "src/Foo", "app_class": "library", "runtime": "dotnet10", "start_command": ""}]
    ).suitable

    webapi = {
        "src/Api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>',
        "src/Api/Program.cs": "var builder = WebApplication.CreateBuilder(args);",
        "src/Api/Properties/launchSettings.json": '{"profiles":{"http":{"applicationUrl":"http://localhost:5217"}}}',
    }
    apps = classify_candidates(webapi)
    api = next(a for a in apps if a["source"].endswith(".csproj"))
    assert api["likely_class"] == "api" and api["path"] == "src/Api", apps
    assert api["port"] == 5217, apps
    assert decide_suitability(
        [{"path": "src/Api", "app_class": "api", "runtime": "dotnet10", "start_command": "dotnet run --project src/Api"}]
    ).suitable

    functions = {
        "FuncApp/host.json": '{"version":"2.0"}',
        "FuncApp/FuncApp.csproj": '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Microsoft.Azure.Functions.Worker" /></ItemGroup></Project>',
    }
    assert {a["likely_class"] for a in classify_candidates(functions)} == {"azure_function"}

    expo = {"app.json": '{"expo":{"name":"demo"}}', "package.json": '{"dependencies":{"react-native":"0.74","expo":"51"}}'}
    assert {a["likely_class"] for a in classify_candidates(expo)} == {"mobile"}
    mobile_only = decide_suitability([{"path": ".", "app_class": "mobile", "runtime": "node22", "start_command": "npx expo start"}])
    assert not mobile_only.suitable and "Linux container" in mobile_only.reasons[0], mobile_only

    nextjs = {"package.json": '{"dependencies":{"next":"15"},"scripts":{"dev":"next dev"}}'}
    web = classify_candidates(nextjs)[0]
    assert web["likely_class"] == "web" and web["start_command"] == "npm run dev" and web["port"] == 3000, web

    # An app with no start command is not startable, however confidently it was classified.
    assert not decide_suitability(
        [{"path": "src/Api", "app_class": "api", "runtime": "dotnet10", "start_command": ""}]
    ).suitable

    # Path filtering: a scan candidate survives, a plausible-but-unscanned path survives (the
    # model may find a stack the marker table has no rule for), an unusable path is dropped.
    scan = {"candidates": [{"path": "src/Api"}]}
    report = {
        "apps": [
            {"path": "src/Api", "app_class": "api"},
            {"path": "services/go-gateway", "app_class": "api"},
            {"path": "a path with spaces", "app_class": "api"},
            {"path": "", "app_class": "api"},
        ]
    }
    assert [a["path"] for a in _surviving_apps(report, scan)] == ["src/Api", "services/go-gateway"]

    # Fingerprint: order-insensitive over paths, sensitive to content.
    assert fingerprint(nextjs) == fingerprint(dict(reversed(list(nextjs.items()))))
    assert fingerprint(nextjs) != fingerprint({"package.json": '{"dependencies":{"next":"15"},"scripts":{}}'})

    print("app_discovery self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
