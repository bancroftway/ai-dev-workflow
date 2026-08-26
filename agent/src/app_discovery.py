"""Deterministic application-discovery scan.

No suitability verdict, no LLM classification, no hard rejection: the tech-stack StageSpec
(graph.py) is the one place a human reviews and approves what this repository is, for every
repository, empty or not (see the Tech Stack tab redesign). This module now does exactly one job
-- find candidate app marker files and turn them into DiscoveredApp records, purely by regex/JSON
inspection, no model involved. That output feeds:
  - app_check_record_node, which records it in manifest.json (read later by exit's re-record and
    by the Tech Stack tab's fresh-detection path);
  - e2e_nodes.py, which re-scans fresh at e2e time to know what to boot.

Deliberately static: nothing here launches an app. `start_command` is recorded from file evidence
and is not verified by execution.

Verification status: the pure half (classify_candidates/fingerprint) has an assert-based
self-check, runnable with `uv run python -m src.app_discovery`. The sandbox-I/O half (collect_evidence)
has NOT been exercised against a real container.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from . import git_ops, repo_files
from .preflight_nodes import update_manifest
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

# The 8 canned monorepo stacks the Tech Stack tab's dropdown offers -- DATA (one markdown file per
# stack), not a prompt: see load_stack_catalog.
_TECH_STACKS_DIR = Path(__file__).parent / "templates" / "tech_stacks"

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
    # Checked BEFORE Sdk.Web: "Microsoft.NET.Sdk.BlazorWebAssembly" contains no "Microsoft.NET.Sdk.Web"
    # substring so the order is not load-bearing today, but a Blazor project is a UI and must never
    # fall through to the api/library branches below.
    #
    # Without this branch a Blazor WASM frontend was invisible to discovery entirely: its Sdk matches
    # neither `Microsoft.NET.Sdk.Web` nor the bare `Microsoft.NET.Sdk` (that regex's closing quote
    # excludes the longer name), so _csproj_signals returned None and apps/web produced no candidate
    # at all. Observed live (blazor-dotnet s01): `startable` held only apps/api, so nothing was left
    # to boot as a supporting service, the launch agent started the web app itself on the API's own
    # port, the API never ran, and all 19 e2e screenshots showed the UI stuck on "Loading current
    # count..." with every control disabled -- visual "evidence" of an app that did not work, which
    # is precisely what the supporting-service boot in e2e_nodes exists to prevent.
    #
    # Sdk.Razor is deliberately NOT here: that is a Razor Class Library (not startable). Blazor
    # SERVER projects use Sdk.Web and are already caught by the branch below.
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.BlazorWebAssembly"', text):
        return {
            "likely_class": "web",
            "runtime": "dotnet",
            "marker": 'Sdk="Microsoft.NET.Sdk.BlazorWebAssembly"',
        }
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"', text):
        return {"likely_class": "api", "runtime": "dotnet", "marker": 'Sdk="Microsoft.NET.Sdk.Web"'}
    # An ASP.NET FrameworkReference makes a plain-Sdk project a web host just as surely as Sdk.Web
    # does. Keying only on Sdk.Web classified such a project "library" with start_command=null, so
    # e2e had no API to boot and screenshotted a UI whose every request failed.
    if re.search(r'FrameworkReference\s+Include\s*=\s*"Microsoft\.AspNetCore\.App"', text):
        return {
            "likely_class": "api",
            "runtime": "dotnet",
            "marker": 'FrameworkReference Include="Microsoft.AspNetCore.App"',
        }
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

    Pure and deliberately conservative: it reports what a marker proves, nothing more. This is now
    the ONLY source of app records anywhere in the pipeline -- no LLM classification exists.
    """
    candidates: list[dict[str, Any]] = []

    def add(path: str, signals: dict[str, Any]) -> None:
        candidates.append({"path": _app_dir(path), "source": path, **signals})

    for path, text in sorted(files.items()):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".csproj"):
            signals = _csproj_signals(text)
            if signals:
                if signals["likely_class"] in ("api", "web") and "start_command" not in signals:
                    signals["start_command"] = f"dotnet run --project {_app_dir(path)}"
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
            # ponytail: no deterministic start_command for FastAPI/Flask/Procfile -- the module:app
            # target isn't determinable from marker files alone. Extend if this shows up in practice.
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


@lru_cache(maxsize=None)
def load_stack_catalog() -> list[dict[str, Any]]:
    """The 8 canned monorepo stacks the Tech Stack tab's dropdown offers, one markdown file per
    stack under templates/tech_stacks/ -- DATA the user picks from and edits, not a prompt (no
    prompt_loader involved). id = filename stem, title = the file's first `# ` heading, markdown =
    the full file text verbatim (what's shown/edited in the tab and, on Submit, written verbatim
    to .ai-dev-workflow/tech-stack.md if picked as-is, or after further hand-editing)."""
    catalog: list[dict[str, Any]] = []
    for path in sorted(_TECH_STACKS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
        catalog.append({"id": path.stem, "title": title, "markdown": text})
    return catalog


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
    a handful of reads, and its fingerprint is what tells the Tech Stack tab's fresh-detection
    path whether a recorded answer is still valid."""
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        # Same tolerance as scaffold_node: no sandbox, no scan.
        return {"app_scan": {}}
    return {"app_scan": await collect_evidence(get_sandbox_provider(), thread_id)}


def candidates_to_apps(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pre-LLM scan candidates -> DiscoveredApp-shaped dicts. Candidates carry likely_class/marker,
    not name/app_class, and would fail DiscoveredApp validation raw -- this is the explicit
    mapping. One app per path (first candidate wins; later ones for the same dir are corroborating
    markers)."""
    from .schemas_app_discovery import DiscoveredApp

    valid_classes = {"web", "api", "azure_function", "mobile", "library", "cli", "unknown"}
    apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates:
        path = str(c.get("path") or ".")
        if path in seen:
            continue
        seen.add(path)
        likely = str(c.get("likely_class") or "unknown")
        apps.append(DiscoveredApp(
            path=path,
            name=path.rsplit("/", 1)[-1] if path not in (".", "") else "app",
            app_class=likely if likely in valid_classes else "unknown",
            runtime=str(c.get("runtime") or "unknown"),
            start_command=c.get("start_command"),
            port=c.get("port"),
            evidence=[f"{c.get('source', '?')}: {c.get('marker', '?')}"],
        ).model_dump())
    return apps


async def app_check_record_node(state: "GraphState", config: RunnableConfig) -> dict[str, Any]:
    """Records the deterministically-scanned apps in the manifest -- the sole source now, no LLM
    classification stage to defer to.

    Placed after the brownfield-baseline branch converges, never before it: scaffold_node treats
    the mere existence of manifest.json as "already onboarded", so creating the file earlier would
    let a run abandoned mid-brownfield-baseline skip brownfield ratification forever.
    """
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {}

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
                "apps": candidates_to_apps(scan.get("candidates") or []),
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

    webapi = {
        "src/Api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>',
        "src/Api/Program.cs": "var builder = WebApplication.CreateBuilder(args);",
        "src/Api/Properties/launchSettings.json": '{"profiles":{"http":{"applicationUrl":"http://localhost:5217"}}}',
    }
    apps = classify_candidates(webapi)
    api = next(a for a in apps if a["source"].endswith(".csproj"))
    assert api["likely_class"] == "api" and api["path"] == "src/Api", apps

    # A Blazor WASM frontend must classify as a startable "web" app, not vanish. Its Sdk matches
    # neither Sdk.Web nor the bare Sdk (whose regex ends at a closing quote), so before this was
    # handled _csproj_signals returned None and the whole app produced NO candidate -- e2e then had
    # only the API in `startable`, booted no supporting service, and every screenshot showed a UI
    # stuck loading against a backend that was never started (observed live, blazor-dotnet s01).
    blazor = {
        "apps/web/Web.csproj": '<Project Sdk="Microsoft.NET.Sdk.BlazorWebAssembly"></Project>',
        "apps/web/Properties/launchSettings.json": '{"profiles":{"http":{"applicationUrl":"http://localhost:5150"}}}',
        "apps/api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>',
        "apps/api/Properties/launchSettings.json": '{"profiles":{"http":{"applicationUrl":"http://localhost:5080"}}}',
    }
    _blazor_apps = classify_candidates(blazor)
    _web = next(a for a in _blazor_apps if a["path"] == "apps/web")
    assert _web["likely_class"] == "web", _blazor_apps
    assert _web["start_command"] == "dotnet run --project apps/web", _web
    assert _web["port"] == 5150, _web
    # Both halves startable == e2e boots the API as a supporting service before driving the UI.
    _startable = {a["path"] for a in _blazor_apps if (a.get("start_command") or "").strip()}
    assert _startable == {"apps/web", "apps/api"}, _startable

    # A plain-Sdk project with an ASP.NET FrameworkReference is a web host too. Classifying it
    # "library" left it with no start_command, so e2e booted only the frontend and screenshotted a
    # UI whose API calls all failed -- visual "evidence" of an app that did not work.
    _framework_ref = _csproj_signals(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<FrameworkReference Include="Microsoft.AspNetCore.App" /></ItemGroup></Project>'
    )
    assert _framework_ref is not None and _framework_ref["likely_class"] == "api", _framework_ref
    # A genuine library is still a library -- this must not classify everything as an API.
    _plain = _csproj_signals('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup/></Project>')
    assert _plain is not None and _plain["likely_class"] == "library", _plain
    assert api["port"] == 5217, apps

    functions = {
        "FuncApp/host.json": '{"version":"2.0"}',
        "FuncApp/FuncApp.csproj": '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Microsoft.Azure.Functions.Worker" /></ItemGroup></Project>',
    }
    assert {a["likely_class"] for a in classify_candidates(functions)} == {"azure_function"}

    expo = {"app.json": '{"expo":{"name":"demo"}}', "package.json": '{"dependencies":{"react-native":"0.74","expo":"51"}}'}
    assert {a["likely_class"] for a in classify_candidates(expo)} == {"mobile"}

    nextjs = {"package.json": '{"dependencies":{"next":"15"},"scripts":{"dev":"next dev"}}'}
    web = classify_candidates(nextjs)[0]
    assert web["likely_class"] == "web" and web["start_command"] == "npm run dev" and web["port"] == 3000, web

    # Fingerprint: order-insensitive over paths, sensitive to content.
    assert fingerprint(nextjs) == fingerprint(dict(reversed(list(nextjs.items()))))
    assert fingerprint(nextjs) != fingerprint({"package.json": '{"dependencies":{"next":"15"},"scripts":{}}'})

    # candidates_to_apps: pre-LLM candidates map to valid DiscoveredApp dicts, one per path.
    mapped = candidates_to_apps([
        {"path": "src/Api", "source": "src/Api/Api.csproj", "likely_class": "api", "runtime": "dotnet",
         "marker": "Sdk=Web", "start_command": "dotnet run --project src/Api", "port": 5001},
        {"path": "src/Api", "source": "src/Api/Program.cs", "likely_class": "api", "runtime": "dotnet", "marker": "web host"},
        {"path": ".", "likely_class": "not-a-class", "runtime": "node"},
    ])
    assert len(mapped) == 2 and mapped[0]["name"] == "Api" and mapped[0]["start_command"] == "dotnet run --project src/Api"
    assert mapped[1]["app_class"] == "unknown" and mapped[1]["name"] == "app"

    # Canned tech-stack catalog: exactly the 8 stacks the plan names, unique ids, real titles, and
    # every one carries the "Stack facts" section the greenfield tech-stack prompt reads.
    catalog = load_stack_catalog()
    ids = [entry["id"] for entry in catalog]
    assert len(catalog) == 8, ids
    assert len(ids) == len(set(ids)), ids
    assert set(ids) == {
        "angular-dotnet", "react-dotnet", "nextjs-dotnet", "nextjs-flask",
        "nextjs-fastapi", "react-express", "blazor-dotnet", "vue-dotnet",
    }, ids
    assert all(entry["title"] for entry in catalog), catalog
    assert all("## Stack facts" in entry["markdown"] for entry in catalog), [
        entry["id"] for entry in catalog if "## Stack facts" not in entry["markdown"]
    ]

    print("app_discovery self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
