"""P6's coverage gate: enforces a 95% line+branch coverage threshold purely in Python (not the
underlying tool's own pass/fail), so every stack produces identical semantics, and validates that
any coverage-exclusion config wasn't broadened to dodge the threshold (the anti-gaming check --
P6 has full write access, and coverage gates are notoriously gameable).

Two acquisition paths, one trust model:

1. CONTRACT REPLAY (preferred, polyglot-safe): the minimal-code-to-green draft -- which has the
   context to debug each stack's coverage plumbing -- records working command(s) in
   `.ai-dev-workflow/coverage-commands.json`, each naming the standard-format artifact it emits.
   This gate NEVER trusts the model's own run or any number it reports: it deletes each artifact,
   re-executes each recorded command itself, parses the regenerated artifacts (Cobertura XML or
   istanbul json-summary -- nothing else), and merges counts across entries line-weighted. The
   model owns the HOW; the number is always machine-derived from a replay.
2. Legacy single-stack fallback when no contract exists (dotnet coverlet / image-baked vitest).

Pragmatic simplification, stated honestly (same spirit as ac_coverage_gate.py): parses the
tools' own summary output rather than walking every uncovered line -- enough to enforce the
threshold and name the exclusion-gaming problem, not a full per-line coverage report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as ET
from pydantic import BaseModel, Field

from .. import repo_files, stack_runner, workflow_persistence
from ..repo_files import validate_repo_relative_path
from ..sandbox.provider import SandboxProvider
from ..schemas import StageReport

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

MIN_COVERAGE_PERCENT = float(os.environ.get("MIN_COVERAGE_PERCENT", "95.0"))

# The only strings `measure_coverage` may return as its "reason" -- this value ends up in
# repo_scan's `metrics.coverage.reason`, which IS hashed into ScanReport.content_hash (see
# repo_scan.py's determinism guarantee: "an unchanged repo hashes identically"). Raw subprocess
# stdout/stderr is NOT stable across runs (timestamps, temp paths, elapsed times) even when the
# repo itself hasn't changed -- it must never reach this variable. Verbose detail goes to the
# logger (and, for contract replay, the unhashed per-entry `entry_reports`) instead.
REASON_TIMEOUT = "timeout"
REASON_RUNNER_ERROR = "runner_error"
REASON_PARSE_ERROR = "parse_error"
REASON_CONTRACT_REPLAY_FAILED = "contract_replay_failed"
REASON_NO_TOOLING_MAPPING = "no_tooling_mapping"
STABLE_REASON_CODES = frozenset(
    {REASON_TIMEOUT, REASON_RUNNER_ERROR, REASON_PARSE_ERROR, REASON_CONTRACT_REPLAY_FAILED, REASON_NO_TOOLING_MAPPING}
)

COVERAGE_COMMANDS_PATH = ".ai-dev-workflow/coverage-commands.json"
_CONTRACT_FORMATS = ("cobertura", "istanbul-json-summary")


class CoverageEntry(BaseModel):
    """One test root that was actually run with coverage."""

    root: str = Field(default="", description='Repo-relative directory you ran in ("" for repo root).')
    command: str = Field(description="Exactly the command you ran there.")
    artifact: str = Field(description="Repo-relative path to the coverage report the run produced.")
    format: str = Field(description='Either "cobertura" or "istanbul-json-summary".')


class CoverageRunReport(StageReport):
    """What the coverage-run agent must report (prompts/coverage_run.md)."""

    entries: list[CoverageEntry] = Field(default_factory=list)


# Config/manifest files are not an application. Observed live: a run reported every stage approved
# having produced only package.json, tsconfig.json and a Directory.Build.props -- the actual task
# logic had been written INSIDE the test helper (tests/setup-task-store-stub.ts), so the suite
# passed by testing its own stub and no app existed at all. That is precisely the tautological
# outcome this pipeline exists to prevent, and a bare "did any non-test file change" check waved
# it through.
_SOURCE_EXTENSIONS = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".cs", ".fs", ".vb", ".py", ".go", ".rb", ".java", ".kt", ".rs", ".php",
    ".html", ".css", ".scss",
    # Every extension _FRONTEND_SIGNATURES below names MUST appear here, or that signature is dead
    # code: _is_application_source filters source_files BEFORE missing_declared_frontend inspects
    # it, so an unlisted extension is invisible to the very check that looks for it. `.razor` was
    # missing while ("blazor", (".razor",)) sat in the signature table -- observed live on
    # blazor-dotnet: a complete Blazor WASM frontend (CounterPage.razor, App.razor, _Imports.razor,
    # Program.cs) was on disk and the gate still reported "the repository does not actually contain
    # that frontend", redrafting a correct implementation. `.dart`/`.swift` had the same latent gap
    # for the flutter/swiftui signatures.
    ".razor", ".dart", ".swift",
)
# Filenames that are configuration/manifests even though they carry a source-ish extension.
_CONFIG_FILE_RE = re.compile(
    r"(^|/)("
    r"package(-lock)?\.json|tsconfig[^/]*\.json|angular\.json|nx\.json|jsconfig\.json|"
    r"[^/]*\.config\.([cm]?[jt]s|[jt]sx)|[^/]*\.conf\.([cm]?[jt]s)|"
    r"eslint[^/]*|prettier[^/]*|babel[^/]*"
    r")$",
    re.IGNORECASE,
)


# A declared UI framework's unmistakable source signature. Observed live (nextjs-dotnet): the run
# finished with ok=true and every stage approved, having built ONLY the .NET API -- apps/ held just
# api and api.Tests, and not one .ts/.tsx file was tracked. Coverage was 98.9% because the C# it
# did write was well tested, so no existing gate had any reason to object. Stack fidelity has to be
# checked on its own; a coverage number says nothing about whether the declared app was built.
_FRONTEND_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Checked longest-marker-first so "next"/"nuxt" win before a bare "react" also matches.
    ("blazor", (".razor",)),
    ("angular", (".component.ts",)),
    ("svelte", (".svelte",)),
    ("nuxt", (".vue",)),
    ("vue", (".vue",)),
    ("next", (".tsx", ".jsx")),
    ("react", (".tsx", ".jsx")),
    ("flutter", (".dart",)),
    ("swiftui", (".swift",)),
)


def missing_declared_frontend(frameworks: list[str], source_files: list[str]) -> str | None:
    """The declared UI framework whose source signature is absent from the tree, or None.

    Pure function of (declared frameworks, delivered files) so it is directly testable -- see this
    module's self-check.
    """
    lowered_paths = [path.lower() for path in source_files]
    for framework in frameworks:
        name = str(framework).lower()
        for marker, suffixes in _FRONTEND_SIGNATURES:
            if marker not in name:
                continue
            if not any(path.endswith(suffixes) for path in lowered_paths):
                return str(framework)
            break  # this framework is satisfied; don't let a later marker re-judge it
    return None


# The npm package a declared JS/TS framework must actually depend on. A source-file signature
# alone proved gameable: told to build a Next.js frontend, a run created apps/web/src/app/page.tsx
# plus a package.json whose every script was `node -e "console.log('web build placeholder')"` and
# no dependencies at all. That satisfies "a .tsx file exists" while installing no framework and
# building nothing, so the declared frontend still did not exist in any real sense.
_FRONTEND_NPM_PACKAGES: tuple[tuple[str, str], ...] = (
    ("angular", "@angular/core"),
    ("svelte", "svelte"),
    ("nuxt", "nuxt"),
    ("vue", "vue"),
    ("next", "next"),
    ("react", "react"),
)


# A declared backend framework and the evidence that it is a RUNNING SERVICE rather than a library.
# Observed live (nextjs-dotnet): the run declared "ASP.NET Core Web API", shipped apps/api as a plain
# `Microsoft.NET.Sdk` class library with no Program.cs and no endpoints, and passed every gate --
# 98.9% coverage on C# that the running app never called, next to a Next.js frontend keeping its
# state in localStorage. Two disconnected halves, neither of them the approved app.
_BACKEND_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("asp.net", ("Microsoft.NET.Sdk.Web", "Microsoft.AspNetCore.App", "WebApplication", "WebHost")),
    ("aspnet", ("Microsoft.NET.Sdk.Web", "Microsoft.AspNetCore.App", "WebApplication", "WebHost")),
    ("fastapi", ("FastAPI(",)),
    ("flask", ("Flask(",)),
    ("django", ("WSGI_APPLICATION", "ASGI_APPLICATION", "urlpatterns")),
    ("express", ("express(",)),
    ("nest", ("NestFactory",)),
)

# HTTP calls a frontend makes to reach its backend. localStorage-only persistence is the specific
# false-pass this exists to catch, so browser-storage APIs are deliberately NOT in this list.
_HTTP_CALL_MARKERS = ("fetch(", "axios", "httpclient", "$fetch", "xmlhttprequest", "useswr", "usequery(")

# Minimal OpenTelemetry-presence signal per backend ecosystem, checked against the SAME backend
# project+entrypoint texts missing_hosted_backend already reads -- no new I/O. Deliberately broad
# (a bare substring, not an exact package/import match): OpenTelemetry ships many co-installed
# packages per ecosystem (OpenTelemetry.Extensions.Hosting, OpenTelemetry.Instrumentation.AspNetCore
# for .NET; @opentelemetry/api, @opentelemetry/sdk-node, @opentelemetry/auto-instrumentations-node
# for Node; opentelemetry-api, opentelemetry-instrumentation-fastapi for Python) and this only needs
# to know SOME OTel signal is present, not which package. Same "verify, don't trust the model's own
# claim" discipline as the checks above -- generated code must be instrumented regardless of the
# declared stack, so an e2e failure traces to the handler/downstream call that actually broke
# instead of only a frontend symptom (agent/src/telemetry.py sets up the identical thing for the
# orchestrator itself; this is the same pattern applied to what the pipeline generates).
# Needles are ECOSYSTEM-SPECIFIC on purpose: the evidence blob is one flat string across backend
# AND frontend texts, so a generic "opentelemetry" needle let the frontend's
# `@opentelemetry/sdk-trace-web` import satisfy the .NET backend's requirement (and vice versa) --
# W6's own templates guarantee that string exists in every dual-stack repo, which silently
# no-op'ed the backend half of this blocking gate. Each row's needles can only be produced by
# instrumenting THAT ecosystem.
_DOTNET_OTEL_NEEDLES = ("opentelemetry.extensions", "addopentelemetry", "opentelemetry.instrumentation.aspnetcore")
_PYTHON_OTEL_NEEDLES = ("opentelemetry-instrumentation", "opentelemetry.instrumentation", "opentelemetry-sdk", "opentelemetry.sdk")
_NODE_BACKEND_OTEL_NEEDLES = ("@opentelemetry/sdk-node", "@opentelemetry/auto-instrumentations")
_BROWSER_OTEL_NEEDLES = ("@opentelemetry/sdk-trace-web", "@vercel/otel")
_OTEL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("asp.net", _DOTNET_OTEL_NEEDLES),
    ("aspnet", _DOTNET_OTEL_NEEDLES),
    ("blazor", _DOTNET_OTEL_NEEDLES),
    ("fastapi", _PYTHON_OTEL_NEEDLES),
    ("flask", _PYTHON_OTEL_NEEDLES),
    ("django", _PYTHON_OTEL_NEEDLES),
    ("express", _NODE_BACKEND_OTEL_NEEDLES),
    ("nest", _NODE_BACKEND_OTEL_NEEDLES),
    # Frontends (W6): the signal lives in package.json / instrumentation.ts / the app entry file,
    # all of which otel_extra_paths reads. Next.js server-side instrumentation legitimately uses
    # sdk-node (instrumentation.node.ts) or @vercel/otel.
    ("next", ("@vercel/otel", "@opentelemetry/sdk-node")),
    ("react", _BROWSER_OTEL_NEEDLES),
    ("vue", _BROWSER_OTEL_NEEDLES),
    ("angular", _BROWSER_OTEL_NEEDLES),
)


def missing_otel_instrumentation(frameworks: list[str], project_texts: dict[str, str]) -> str | None:
    """A declared, actually-hosted framework (backend OR frontend) with no OpenTelemetry signal
    anywhere in its project/entry files, or None.

    Fails OPEN (returns None) when there is nothing to read -- only positive evidence of absence
    counts, same discipline as missing_hosted_backend. Deliberately permissive (one substring match
    anywhere in the sampled files passes): this checks that instrumentation was attempted at all,
    not that it is complete or correct -- a semantic/AST-level instrumentation-quality check is a
    different, heavier gate this is not trying to be.
    """
    if not project_texts:
        return None
    blob = "\n".join(project_texts.values()).lower()
    for framework in frameworks:
        name = str(framework).lower()
        for marker, needles in _OTEL_MARKERS:
            if marker not in name:
                continue
            if not any(needle in blob for needle in needles):
                return str(framework)
            break
    return None


def missing_hosted_backend(frameworks: list[str], project_texts: dict[str, str]) -> str | None:
    """A declared backend framework with no evidence of an actual HTTP host, or None.

    `project_texts` maps a repo-relative path to its contents (project files and entry points).
    Fails OPEN when no backend framework is declared or nothing could be read: only positive
    evidence that the declared service is not hosted counts against the stage.
    """
    if not project_texts:
        return None
    blob = "\n".join(project_texts.values())
    for framework in frameworks:
        name = str(framework).lower()
        for marker, needles in _BACKEND_MARKERS:
            if marker not in name:
                continue
            if not any(needle.lower() in blob.lower() for needle in needles):
                return str(framework)
            break
    return None


def frontend_only_uses_local_storage(frontend_texts: dict[str, str]) -> bool:
    """True when the frontend persists state but never calls a backend over HTTP.

    Only meaningful when a backend was declared -- the caller checks that. A single-tier app that
    legitimately has no backend must never reach this.
    """
    if not frontend_texts:
        return False
    blob = "\n".join(frontend_texts.values()).lower()
    if any(marker in blob for marker in _HTTP_CALL_MARKERS):
        return False
    return "localstorage" in blob or "sessionstorage" in blob or "indexeddb" in blob


# An outbound call LEAVING this process: an absolute URL, or a base URL read from config/env. A
# same-origin relative path ("/api/counter") is deliberately NOT here -- that is the thing being
# detected, not evidence against it.
_OUTBOUND_CALL_MARKERS = (
    "http://",
    "https://",
    "process.env",
    "api_base",
    "apibase",
    "baseurl",
    "base_url",
    "backend",
    "upstream",
)


def frontend_reimplements_backend(route_handler_texts: dict[str, str]) -> list[str]:
    """Frontend-owned API routes that implement state themselves instead of calling the backend.

    Returns the offending paths (empty when fine). Only meaningful when a separate hosted backend
    was declared -- the caller checks that.

    This exists because `frontend_only_uses_local_storage` was evaded by a real run in a way that
    looks compliant from every angle it measured: the app declared a .NET API, actually built it
    (`apps/api/Program.cs`, `CounterStore.cs`), and the frontend really did make an HTTP call --
    `fetch('/api/counter')`. But that path is a Next.js route handler in the SAME project, backed by
    an in-process module store, so the .NET service was never contacted by anything. Every existing
    check passed: a backend framework was declared and hosted, the frontend had a real dependency,
    and an HTTP-call marker was present, so the localStorage rule short-circuited to "fine".

    The distinguishing signal is deliberately narrow, to keep a legitimate BFF/proxy layer passing:
    a route handler that forwards to the backend contains an OUTBOUND call (an absolute URL, or a
    base URL from `process.env`). One that contains no outbound call at all is not a proxy -- it is
    a second implementation of the same state.
    """
    offenders: list[str] = []
    for path, contents in (route_handler_texts or {}).items():
        lowered = contents.lower()
        if any(marker in lowered for marker in _OUTBOUND_CALL_MARKERS):
            continue  # forwards somewhere -- a proxy/BFF, which is a legitimate design
        offenders.append(path)
    return sorted(offenders)


def missing_frontend_dependency(frameworks: list[str], package_json: str | None) -> str | None:
    """A declared JS/TS framework absent from package.json's dependencies, or None.

    Fails OPEN (returns None) when there is no package.json to read: only positive evidence that
    the manifest omits the framework counts against the stage.
    """
    if package_json is None:
        return None
    try:
        manifest = json.loads(package_json)
    except json.JSONDecodeError:
        return None
    declared = {
        *(manifest.get("dependencies") or {}),
        *(manifest.get("devDependencies") or {}),
    }
    for framework in frameworks:
        name = str(framework).lower()
        for marker, package in _FRONTEND_NPM_PACKAGES:
            if marker not in name:
                continue
            if package not in declared:
                return str(framework)
            break
    return None


async def _declared_frameworks(provider: SandboxProvider, thread_id: str) -> list[str]:
    """Frameworks from the repo's own tech-stack record; empty when unreadable (fail open)."""
    for path in (workflow_persistence.TECH_STACK_APPROVED_PATH, workflow_persistence.TECH_STACK_DRAFT_PATH):
        raw = await repo_files.read_repo_file(provider, thread_id, path)
        if raw is None:
            continue
        try:
            frameworks = json.loads(raw).get("frameworks") or []
        except json.JSONDecodeError:
            continue
        if frameworks:
            return [str(f) for f in frameworks]
    return []


async def _check_integration_fidelity(
    provider: SandboxProvider, thread_id: str, source_files: list[str]
) -> str | None:
    """Feedback describing a declared-but-unbuilt backend or a frontend that never calls it, else None.

    This is the "is it actually one app" check. Coverage says nothing about it: a class library with
    no host can be tested to 99% while the running product never invokes a line of it.
    """
    frameworks = await _declared_frameworks(provider, thread_id)
    if not frameworks:
        return None

    # Project/entry files that would carry the evidence of a real HTTP host.
    from ..repo_scan import is_non_application_path as _non_app

    backend_paths = [
        path for path in source_files
        if path.endswith((".csproj", "Program.cs", "Startup.cs", "main.py", "app.py", "server.js", "server.ts", "index.js", "index.ts", "main.ts"))
        and not _non_app(path)
    ]
    project_files = await provider.exec_in_sandbox(
        thread_id, "git ls-files '*.csproj' && git ls-files --others --exclude-standard '*.csproj'"
    )
    backend_paths += [line.strip() for line in (project_files.stdout or "").splitlines() if line.strip()]
    texts: dict[str, str] = {}
    for path in sorted(set(backend_paths))[:25]:
        content = await repo_files.read_repo_file(provider, thread_id, path)
        if content is not None:
            texts[path] = content

    # OTel's own Node.js docs recommend a SEPARATE bootstrap file (tracing.js/instrumentation.js)
    # required before anything else in the entrypoint -- a correctly-instrumented app following
    # that idiomatic pattern would false-positive-fail missing_otel_instrumentation if it only ever
    # saw `texts` above. Frontend entry files (main.tsx etc.) carry the WebTracerProvider evidence
    # the W6 frontend markers need. package.json comes from its OWN git listing: `source_files` is
    # pre-filtered by _is_application_source, which rejects manifests -- deriving it from there
    # (the original code) meant the "package.json is checked" claim was simply false, and a
    # correctly-instrumented React app failed this gate forever.
    otel_extra_paths = [
        path for path in source_files
        if path.endswith((
            "tracing.js", "tracing.ts", "instrumentation.js", "instrumentation.ts",
            "instrumentation.node.ts", "otel.js", "otel.ts",
            "main.ts", "main.tsx", "main.jsx", "index.tsx",
        ))
        and not _non_app(path)
    ]
    manifest_files = await provider.exec_in_sandbox(
        thread_id, "git ls-files '*package.json' && git ls-files --others --exclude-standard '*package.json'"
    )
    otel_extra_paths += [
        line.strip() for line in (manifest_files.stdout or "").splitlines()
        if line.strip() and not _non_app(line.strip())
    ]
    otel_extra_texts: dict[str, str] = {}
    for path in sorted(set(otel_extra_paths))[:14]:
        content = await repo_files.read_repo_file(provider, thread_id, path)
        if content is not None:
            otel_extra_texts[path] = content

    unhosted = missing_hosted_backend(frameworks, texts)
    if unhosted:
        return (
            f"The approved Tech Stack declares {unhosted} as this app's backend, but nothing in the "
            f"repository actually hosts it: no web-SDK project file and no entry point that builds a "
            f"running HTTP service with mapped endpoints. What exists is a library -- code nothing "
            f"invokes. Its tests can reach any coverage number and still prove nothing about the "
            f"running product.\n\nBuild the real service: for ASP.NET Core that means "
            f"Sdk=\"Microsoft.NET.Sdk.Web\" and a Program.cs that builds a WebApplication and maps "
            f"the endpoints the Specification's ACs describe, then have the frontend call it."
        )

    # Checked only once the backend is confirmed hosted -- an unhosted backend is missing_hosted_
    # backend's finding to report above, not a second one here for the same root cause.
    missing_otel = missing_otel_instrumentation(frameworks, {**texts, **otel_extra_texts})
    if missing_otel:
        return (
            f"The approved Tech Stack declares {missing_otel} for this app, but no OpenTelemetry "
            f"SDK signal is present anywhere in its project or entry files. Every generated app "
            f"(frontend AND backend) must be instrumented regardless of the declared stack, so an "
            f"e2e failure traces to the handler or downstream call that actually broke instead "
            f"of only a symptom. Follow the tech-stack doc's Observability section. Backends -- "
            f"ASP.NET Core: OpenTelemetry.Extensions.Hosting + OpenTelemetry.Instrumentation.AspNetCore "
            f"and builder.Services.AddOpenTelemetry()...AddAspNetCoreInstrumentation() in Program.cs; "
            f"Express/Nest: @opentelemetry/api + @opentelemetry/sdk-node initialized before the "
            f"app's other imports; FastAPI/Flask/Django: opentelemetry-api + the matching "
            f"opentelemetry-instrumentation-* package at startup. Frontends -- Next.js: "
            f"instrumentation.ts register() guarded by process.env.NEXT_RUNTIME === 'nodejs' with a "
            f"dynamically-imported instrumentation.node.ts (NodeSDK crashes on edge runtime), or "
            f"@vercel/otel; React/Vue/Angular: @opentelemetry/sdk-trace-web with a "
            f"ConsoleSpanExporter. Console exporter by default; respect OTEL_EXPORTER_OTLP_ENDPOINT "
            f"/ OTEL_TRACES_EXPORTER=none if set (the same convention this pipeline's own "
            f"telemetry.py uses for itself)."
        )

    # Only ask the "does the frontend call it" question when a backend was actually declared --
    # a legitimately single-tier app has nothing to call.
    if not any(marker in str(f).lower() for f in frameworks for marker, _ in _BACKEND_MARKERS):
        return None
    # Build output is excluded explicitly: `.next/` chunks are .js and would otherwise be read as
    # "frontend source", both wasting reads and (before the path validator was fixed) crashing the
    # stage on a generated filename like `[externals]__05yr04l._.js`.
    from ..repo_scan import is_non_application_path

    frontend_candidates = [
        p for p in source_files
        if p.lower().endswith((".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte"))
        and not is_non_application_path(p)
    ]
    frontend_texts: dict[str, str] = {}
    for path in frontend_candidates[:30]:
        content = await repo_files.read_repo_file(provider, thread_id, path)
        if content is not None:
            frontend_texts[path] = content
    if frontend_only_uses_local_storage(frontend_texts):
        return (
            "The frontend persists its state in browser storage and never calls the backend over "
            "HTTP -- there is no fetch/axios/HttpClient call anywhere in it. The declared API is "
            "therefore dead code, and the two halves of this app are not connected, so what would "
            "ship is not the app that was approved.\n\nWire the frontend to the API: replace the "
            "browser-storage persistence with calls to the endpoints the backend exposes."
        )

    # The same rule, evaded differently: the frontend DOES make an HTTP call, but to its own
    # framework route handler backed by an in-process store, so the declared service is still never
    # contacted. See frontend_reimplements_backend for the observed case.
    route_handlers = {
        path: text for path, text in frontend_texts.items()
        if re.search(r"(^|/)(app|src/app|pages)/api/.*/route\.[jt]sx?$", path)
        or re.search(r"(^|/)pages/api/", path)
    }
    self_implemented = frontend_reimplements_backend(route_handlers)
    if self_implemented and len(self_implemented) == len(route_handlers):
        return (
            "The frontend calls its OWN framework route handlers, not the declared backend. These "
            f"handlers implement the state themselves and make no outbound call to any other "
            f"service: {', '.join(self_implemented)}.\n\n"
            "So there are now two implementations of the same behaviour -- one in the frontend's "
            "route handlers, one in the backend the Tech Stack declares -- and the running product "
            "uses only the first. The backend is dead code, and its tests prove nothing about what "
            "would ship. A same-origin `fetch('/api/...')` does NOT make the two halves connected "
            "when the thing serving that path is the frontend itself.\n\n"
            "Fix it one of two ways: either delete the frontend's duplicate handlers and call the "
            "backend's endpoints directly (with its base URL from configuration), or keep the "
            "handlers as a thin proxy that forwards every request to the backend and returns its "
            "response -- forwarding, never re-implementing. Do not satisfy this by deleting the "
            "backend: the Tech Stack that declares it is approved."
        )
    return None


async def _missing_declared_frontend(
    provider: SandboxProvider, thread_id: str, source_files: list[str]
) -> str | None:
    """Same check against the repo's own approved tech-stack record. Returns None (fails OPEN) when
    that record can't be read -- an unreadable artifact is not evidence the frontend is missing."""
    for path in (workflow_persistence.TECH_STACK_APPROVED_PATH, workflow_persistence.TECH_STACK_DRAFT_PATH):
        raw = await repo_files.read_repo_file(provider, thread_id, path)
        if raw is None:
            continue
        try:
            frameworks = json.loads(raw).get("frameworks") or []
        except json.JSONDecodeError:
            continue
        if not frameworks:
            continue
        names = [str(f) for f in frameworks]
        if (missing := missing_declared_frontend(names, source_files)) is not None:
            return missing
        # A signature file exists; now check the framework is genuinely installed. Search the
        # manifests the tree actually has rather than assuming a location -- a monorepo keeps the
        # web app's package.json under apps/web/, not at the repo root.
        listing = await provider.exec_in_sandbox(
            thread_id,
            "git ls-files && git ls-files --others --exclude-standard",
        )
        manifests = [
            stripped
            for line in (listing.stdout or "").splitlines()
            if (stripped := line.strip()).endswith("package.json")
            and "node_modules/" not in stripped
        ]
        if not manifests:
            return None  # nothing to check against; fail open
        # Satisfied if ANY manifest declares it -- which package.json owns the frontend is the
        # repo's own layout decision, not this gate's to dictate.
        unsatisfied: str | None = None
        for manifest_path in manifests[:10]:
            raw_manifest = await repo_files.read_repo_file(provider, thread_id, manifest_path)
            result = missing_frontend_dependency(names, raw_manifest)
            if result is None:
                return None
            unsatisfied = result
        return unsatisfied
    return None


def _is_application_source(path: str) -> bool:
    """A real implementation file, not a manifest, config, or project scaffold file."""
    if _CONFIG_FILE_RE.search(path):
        return False
    return path.lower().endswith(_SOURCE_EXTENSIONS)


# thread_id -> (tree-state key, last successful measurement). Process-local, same lifetime as the
# graph's own in-memory checkpointer; a fresh process just re-measures.
_COVERAGE_MEMO: dict[str, tuple[str, tuple[float | None, float | None, list["CoverageGap"], str, list[dict[str, Any]]]]] = {}

# Coverage-exclusion glob patterns considered legitimately generated/vendor code, never a
# hand-written escape hatch. Anything outside this allowlist that appears in a repo's own
# exclusion config is treated as gaming the gate, regardless of the raw coverage percentage.
_SAFE_EXCLUSION_PATTERNS = {
    "**/*.generated.*",
    "**/*.designer.cs",
    "**/migrations/**",
    "**/Migrations/**",
    "**/dist/**",
    "**/*.d.ts",
    "**/obj/**",
    "**/bin/**",
    "**/node_modules/**",
}


# Files no model authored and no test can meaningfully cover: compiler/source-generator output
# (observed live: System.Text.RegularExpressions.Generator's RegexGenerator.g.cs held the branch
# aggregate at 88.9% while every authored file stood higher -- 12 verify laps burned on a
# denominator the model could not touch). Same rule the scanners already apply to generated paths.
_GENERATED_FILE_RE = re.compile(
    r"(^|[/\\])(obj|bin|node_modules|\.next|dist|build)([/\\])"
    r"|\.(g|g\.i|generated|designer)\.(cs|ts|js)$"
    r"|\.d\.ts$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CoverageGap:
    file: str
    line_rate: float
    branch_rate: float


@dataclass(frozen=True)
class _Counts:
    """Covered/total counts, mergeable across stacks (rates are not -- a 10-line worker and a
    10k-line app would weigh equally)."""

    lines_covered: int
    lines_total: int
    branches_covered: int
    branches_total: int
    gaps: list[CoverageGap]


def _parse_cobertura_counts(raw_xml: str) -> tuple[_Counts | None, str]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None, "artifact failed to parse as Cobertura XML"
    try:
        lc, lt = int(root.get("lines-covered", "0")), int(root.get("lines-valid", "0"))
        bc, bt = int(root.get("branches-covered", "0")), int(root.get("branches-valid", "0"))
    except ValueError:
        return None, "Cobertura root counters are not integers"
    if lt == 0:
        return None, "Cobertura artifact reports zero valid lines -- nothing was instrumented"
    gaps: list[CoverageGap] = []
    for cls in root.iter("class"):
        cls_line_rate = float(cls.get("line-rate", "1")) * 100
        # Branch numbers are recomputed from the class's own lines rather than trusting its
        # branch-rate attribute. Two reasons, both seen live: a class with no branch points reports
        # branch-rate="0" (vacuous, not a real 0% gap), and a class WITH partially-covered branches
        # can still report a healthy branch-rate, so the aggregate came out at 86% while every
        # per-file gap looked fine and the model was handed an empty "here's where to look" list.
        # .lower(): coverlet emits branch="True" (capital T), coverage.py emits "true". Comparing
        # case-sensitively made branch_lines ALWAYS empty on .NET, so every class scored a vacuous
        # 100% and gaps came back empty while the aggregate sat at 88% -- the model was told it
        # failed and handed nowhere to look, for six verify cycles.
        covered_branches = total_branches = 0
        cls_lines_covered = cls_lines_total = 0
        uncovered_lines: list[str] = []
        # Coverlet emits each <line> TWICE -- once under its <method> and once in the class-level
        # <lines> block -- so counts and the reported line list are de-duplicated by line number.
        seen_lines: set[str] = set()
        for line in cls.iter("line"):
            if (num := line.get("number")) is not None:
                if num in seen_lines:
                    continue
                seen_lines.add(num)
            cls_lines_total += 1
            try:
                if int(line.get("hits", "0") or "0") > 0:
                    cls_lines_covered += 1
            except ValueError:
                pass
            if (line.get("branch") or "").lower() != "true":
                continue
            # condition-coverage looks like: "50% (1/2)"
            match = re.search(r"\((\d+)/(\d+)\)", line.get("condition-coverage", ""))
            if not match:
                continue
            hit, total = int(match.group(1)), int(match.group(2))
            covered_branches += hit
            total_branches += total
            if hit < total and (number := line.get("number")) and number not in uncovered_lines:
                uncovered_lines.append(number)
        name = cls.get("filename", cls.get("name", "?"))
        if _GENERATED_FILE_RE.search(name):
            # Generated code is nobody's to cover: pull its contribution back OUT of the root
            # aggregate (its per-line counts, deduped above) and never report it as a gap.
            lc -= cls_lines_covered
            lt -= cls_lines_total
            bc -= covered_branches
            bt -= total_branches
            continue
        cls_branch_rate = (100.0 * covered_branches / total_branches) if total_branches else 100.0
        if cls_line_rate < MIN_COVERAGE_PERCENT or cls_branch_rate < MIN_COVERAGE_PERCENT:
            # Name the exact lines whose branches are only half-taken -- that is the actionable
            # part; a bare percentage tells the model nothing about which case it forgot to test.
            if uncovered_lines:
                name = f"{name} (partially-covered branch lines: {', '.join(uncovered_lines[:20])})"
            gaps.append(CoverageGap(file=name, line_rate=cls_line_rate, branch_rate=cls_branch_rate))
    if lt <= 0:
        return None, "every instrumented line is in generated code -- nothing authored was measured"
    return _Counts(lc, lt, max(bc, 0), max(bt, 0), gaps), ""


def _parse_istanbul_counts(raw: str) -> tuple[_Counts | None, str]:
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError:
        return None, "artifact failed to parse as istanbul json-summary"

    def _count(entry: dict, key: str, field: str) -> int:
        try:
            return int(entry.get(key, {}).get(field, 0))
        except (TypeError, ValueError):
            return 0

    total = summary.get("total", {})
    lt, lc = _count(total, "lines", "total"), _count(total, "lines", "covered")
    bt, bc = _count(total, "branches", "total"), _count(total, "branches", "covered")
    if lt == 0:
        return None, "istanbul summary reports zero total lines -- nothing was instrumented"

    def _pct(entry: dict, key: str, default: float) -> float:
        try:
            return float(entry.get(key, {}).get("pct", default))
        except (TypeError, ValueError):
            return default

    gaps: list[CoverageGap] = []
    for file_path, entry in summary.items():
        if file_path == "total" or not isinstance(entry, dict):
            continue
        file_line_rate = _pct(entry, "lines", 100)
        # 0-of-0 branches is vacuous, same rule as the Cobertura parsers.
        file_branch_rate = 100.0 if _count(entry, "branches", "total") == 0 else _pct(entry, "branches", 100)
        if file_line_rate < MIN_COVERAGE_PERCENT or file_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=file_path, line_rate=file_line_rate, branch_rate=file_branch_rate))
    return _Counts(lc, lt, bc, bt, gaps), ""


# Per-command ceiling for a deterministic contract replay (same knob repo_scan's own coverage
# leg honours; a hung `dotnet test` must not stall the gate forever).
_REPLAY_TIMEOUT_SECONDS = int(os.environ.get("REPO_SCAN_COVERAGE_TIMEOUT_SECONDS", "600"))


def _load_coverage_contract(raw: str | None) -> list[CoverageEntry]:
    """Validated entries from a committed coverage-commands.json, or [] when absent/unusable.
    Pure. Every entry must carry a non-empty command, a repo-relative artifact path and a known
    format -- anything else means the contract is not replayable and discovery must run."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    entries: list[CoverageEntry] = []
    for item in (parsed or {}).get("entries") or []:
        try:
            entry = CoverageEntry(**item)
            validate_repo_relative_path(entry.artifact)
        except Exception:  # noqa: BLE001 -- one bad entry invalidates the whole contract
            return []
        if not entry.command.strip() or entry.format not in _CONTRACT_FORMATS:
            return []
        entries.append(entry)
    return entries


async def _replay_coverage_contract(
    provider: SandboxProvider, thread_id: str, entries: list[CoverageEntry]
) -> list[dict[str, Any]]:
    """Deterministic acquisition: delete each entry's prior artifact, then run its command from its
    root. No model in the loop. Returns per-entry run summaries for the gate's feedback; the
    freshness/parse loop in _run_coverage_via_ghcp judges the artifacts exactly as it would after a
    model-driven run. Observed live (run d16959d3, mctg lap 3): the coverage-run session executed
    the commands on laps 1-2, then answered lap 3 from conversation memory with zero tool calls --
    "Replayed the contract exactly" over artifacts 25 minutes old. The gate's staleness check
    caught it, but the lap was already burned."""
    runs: list[dict[str, Any]] = []
    for entry in entries:
        root = entry.root.strip() or "."
        command = (
            f"rm -f {shlex.quote(entry.artifact)}; cd {shlex.quote(root)} && "
            + _with_timeout(entry.command, _REPLAY_TIMEOUT_SECONDS)
        )
        result = await provider.exec_in_sandbox(thread_id, command)
        runs.append({
            "root": root, "command": entry.command, "exit_code": result.returncode,
            "stdout_tail": (result.stdout or "")[-1500:], "stderr_tail": (result.stderr or "")[-1500:],
        })
    return runs


def _with_timeout(command: str, timeout_seconds: int | None) -> str:
    """`sh -c` wrap keeps a single `timeout` bound to the WHOLE command even when it chains
    multiple statements (&&, if/then/else) -- a bare `timeout N cmd1 && cmd2` would only bound
    cmd1. None (every gate caller today) is a no-op, so behavior is unchanged unless a caller
    opts in."""
    return f"timeout {timeout_seconds} sh -c {shlex.quote(command)}" if timeout_seconds else command


async def _run_coverage_via_ghcp(
    provider: SandboxProvider, thread_id: str, *, chat_provider: str, run_id: str = "unknown"
) -> tuple[float | None, float | None, list[CoverageGap], str, list[dict[str, Any]]]:
    """Acquisition half: one GHCP session finds every test root, runs it with coverage, and
    reports where the report files landed; THIS function parses those files and does the math.

    `chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is required,
    keyword-only, no default -- threaded straight through to stack_runner.run_and_report below,
    which now requires it itself; not resolved in here. `run_id` (Phase E known-bugs fix) is
    threaded the same way, defaulting to "unknown".

    Replaces three deleted code paths that all guessed commands in Python (a canned `dotnet test`
    at the repo root -> MSB1003 on every greenfield monorepo; a canned image-baked vitest
    invocation -> swallowed Playwright specs and instrumented nothing; and a replay of a contract
    the drafting model was supposed to write but reliably didn't). The model decides HOW to
    produce coverage; the NUMBER is still machine-derived here from artifacts on disk, and the
    artifacts must be fresh (mtime >= the epoch captured before the session ran), so a
    pre-fabricated or stale report cannot pass.
    """
    epoch_result = await provider.exec_in_sandbox(thread_id, "date +%s")
    try:
        epoch = int((epoch_result.stdout or "0").strip())
    except ValueError:
        epoch = 0

    # Contract replay first: once a validated coverage-commands.json exists (written below from a
    # model run whose artifacts parsed), every later measurement re-runs THOSE commands here in
    # Python. The model only runs discovery -- no contract yet, or a replay whose artifacts all
    # fail (a stale contract after the tree changed shape), in which case it gets the replay's
    # own errors as failure_detail and gets to re-discover.
    contract = _load_coverage_contract(await repo_files.read_repo_file(provider, thread_id, COVERAGE_COMMANDS_PATH))
    replay_runs: list[dict[str, Any]] = []
    entries: list[CoverageEntry] = []
    if contract:
        replay_runs = await _replay_coverage_contract(provider, thread_id, contract)
        entries = contract
        failure_detail = "; ".join(
            f"[{r['root']}] `{r['command']}` exited {r['exit_code']}: {(r['stderr_tail'] or r['stdout_tail'])[-300:]}"
            for r in replay_runs
        )
    else:
        failure_detail = (
            "No prior automatic attempt: determine how to run this repository's tests with "
            "coverage from scratch."
        )

    async def _discover() -> list[CoverageEntry]:
        report = await stack_runner.run_and_report(
            thread_id,
            stage_key="coverage-run",
            prompt_name="coverage_run",
            schema=CoverageRunReport,
            provider=chat_provider,
            run_id=run_id,
            failure_detail=failure_detail,
        )
        return list(report.entries)

    if not entries:
        entries = await _discover()
    if not entries:
        return None, None, [], REASON_RUNNER_ERROR, [{"error": "no coverage entries reported"}]

    merged: list[_Counts] = []
    entry_reports: list[dict[str, Any]] = []
    if replay_runs:
        entry_reports.append({"replay": replay_runs})
    for entry in entries[:10]:  # bounded: dozens of entries is itself suspect
        detail: dict[str, Any] = {"entry": entry.model_dump()}
        entry_reports.append(detail)
        try:
            validate_repo_relative_path(entry.artifact)
        except ValueError as exc:
            detail["error"] = f"invalid artifact path: {exc}"
            continue

        # Freshness: the file must have been written by the run we just asked for.
        stat = await provider.exec_in_sandbox(thread_id, f"stat -c %Y {shlex.quote(entry.artifact)} 2>/dev/null")
        try:
            mtime = int((stat.stdout or "0").strip())
        except ValueError:
            mtime = 0
        if mtime == 0:
            detail["error"] = f"no artifact at {entry.artifact}"
            continue
        if mtime < epoch:
            detail["error"] = f"stale artifact at {entry.artifact} (written before this run)"
            continue

        artifact_raw = await repo_files.read_repo_file(provider, thread_id, entry.artifact)
        if artifact_raw is None:
            detail["error"] = f"artifact at {entry.artifact} could not be read"
            continue
        counts, parse_error = (
            _parse_cobertura_counts(artifact_raw)
            if entry.format == "cobertura"
            else _parse_istanbul_counts(artifact_raw)
        )
        if counts is None:
            detail["error"] = parse_error
            continue
        detail["lines"] = f"{counts.lines_covered}/{counts.lines_total}"
        detail["branches"] = f"{counts.branches_covered}/{counts.branches_total}"
        merged.append(counts)

    if not merged and replay_runs:
        # The replayed contract produced nothing usable -- the tree may have changed shape since it
        # was written. One model-driven re-discovery, handed the replay's own errors.
        logger.warning("repo_scan coverage: contract replay produced no usable artifact -- re-discovering")
        replay_errors = "; ".join(str(d.get("error")) for d in entry_reports if d.get("error"))
        failure_detail = f"Contract replay failed: {failure_detail}. Artifact errors: {replay_errors}"
        entries = await _discover()
        replay_runs = []
        merged, entry_reports = [], []
        for entry in entries[:10]:
            detail = {"entry": entry.model_dump()}
            entry_reports.append(detail)
            try:
                validate_repo_relative_path(entry.artifact)
            except ValueError as exc:
                detail["error"] = f"invalid artifact path: {exc}"
                continue
            stat = await provider.exec_in_sandbox(thread_id, f"stat -c %Y {shlex.quote(entry.artifact)} 2>/dev/null")
            try:
                mtime = int((stat.stdout or "0").strip())
            except ValueError:
                mtime = 0
            if mtime < epoch or mtime == 0:
                detail["error"] = f"stale or missing artifact at {entry.artifact}"
                continue
            artifact_raw = await repo_files.read_repo_file(provider, thread_id, entry.artifact)
            counts, parse_error = (
                (_parse_cobertura_counts(artifact_raw) if entry.format == "cobertura" else _parse_istanbul_counts(artifact_raw))
                if artifact_raw is not None else (None, "artifact could not be read")
            )
            if counts is None:
                detail["error"] = parse_error
                continue
            detail["lines"] = f"{counts.lines_covered}/{counts.lines_total}"
            detail["branches"] = f"{counts.branches_covered}/{counts.branches_total}"
            merged.append(counts)

    if not merged:
        errors = "; ".join(str(d.get("error")) for d in entry_reports if d.get("error"))
        logger.warning("repo_scan coverage: no usable coverage artifact from the run: %s", errors)
        return None, None, [], REASON_RUNNER_ERROR, entry_reports

    # Persist the contract in its historical shape: exit_nodes.py reads it for the run manifest.
    # Written by US from a validated report, never begged out of the drafting model.
    await repo_files.write_repo_file(
        provider,
        thread_id,
        COVERAGE_COMMANDS_PATH,
        json.dumps({"entries": [e.model_dump() for e in entries]}, indent=2) + "\n",
    )

    lines_total = sum(c.lines_total for c in merged)
    lines_covered = sum(c.lines_covered for c in merged)
    branches_total = sum(c.branches_total for c in merged)
    branches_covered = sum(c.branches_covered for c in merged)
    line_rate = 100.0 * lines_covered / lines_total if lines_total else 0.0
    # No branch points anywhere is vacuously satisfied, not a 0% failure.
    branch_rate = 100.0 * branches_covered / branches_total if branches_total else 100.0
    gaps = [gap for c in merged for gap in c.gaps]
    return line_rate, branch_rate, gaps, "", entry_reports



async def _check_exclusion_gaming(provider: SandboxProvider, thread_id: str) -> list[str]:
    """Reads whichever exclusion config exists and flags any pattern outside the known-safe
    allowlist -- catches the model broadening an exclude glob (e.g. to `src/**`) to dodge the
    threshold instead of actually writing tests."""
    violations: list[str] = []

    runsettings = await repo_files.read_repo_file(provider, thread_id, "coverage.runsettings")
    if runsettings is not None:
        for pattern in re.findall(r"<Exclude>\s*\[?([^<\]]+)\]?\s*</Exclude>", runsettings):
            if pattern.strip() not in _SAFE_EXCLUSION_PATTERNS:
                violations.append(f"coverage.runsettings excludes {pattern.strip()!r}, not on the known-safe allowlist")

    c8rc = await repo_files.read_repo_file(provider, thread_id, ".c8rc.json")
    if c8rc is not None:
        try:
            config = json.loads(c8rc)
            for pattern in config.get("exclude", []):
                if pattern not in _SAFE_EXCLUSION_PATTERNS:
                    violations.append(f".c8rc.json excludes {pattern!r}, not on the known-safe allowlist")
        except json.JSONDecodeError:
            pass

    return violations


async def measure_coverage(
    provider: SandboxProvider, thread_id: str, *, chat_provider: str, timeout_seconds: int | None = None,
    run_id: str = "unknown",
) -> tuple[float | None, float | None, list[CoverageGap], str, list[dict[str, Any]]]:
    """Acquisition half of `verify_coverage`: a GHCP session runs the tests with coverage and
    reports its artifacts, which THIS module parses (see `_run_coverage_via_ghcp`).

    `chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is required,
    keyword-only, no default -- threaded straight through to `_run_coverage_via_ghcp`; not
    resolved in here. `run_id` (Phase E known-bugs fix) is threaded the same way, defaulting to
    "unknown" -- reached from both `verify_coverage` (a real run_id already in scope there) and
    repo_scan.py's `_scan_with_coverage` (same).

    Returns (line_rate, branch_rate, gaps, reason, entry_reports) with line_rate=None on any
    failure -- NEVER a fabricated 0, the same rule the gate's callers already depend on. `reason`
    is only meaningful when line_rate is None, and is always one of `STABLE_REASON_CODES` -- this
    value flows into repo_scan's hashed `metrics.coverage.reason` (via the baseline path), so it
    can never carry volatile subprocess output. Verbose detail is logged instead.
    """
    # Cheap guard, kept from the old tech-stack dispatch: on a repo with no detected languages
    # (the blank-repo baseline scan, which runs before anything is scaffolded) there is nothing to
    # measure, so don't spend a GHCP session discovering that.
    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.TECH_STACK_APPROVED_PATH)
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if not languages and not tech_stack.get("dotnet_detected"):
        logger.info("repo_scan coverage: no tooling mapping for detected languages %s", languages)
        return None, None, [], REASON_NO_TOOLING_MAPPING, []

    # Memoized on the exact tree state (HEAD + dirty-file digest): this gate re-fires on every
    # verify cycle and again from metrics-report, and re-running a full coverage suite for a tree
    # that has not changed is pure spend. Dirty-state is part of the key because during verify
    # cycles HEAD often stands still while the working tree moves.
    head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD 2>/dev/null")
    dirty = await provider.exec_in_sandbox(thread_id, "git status --porcelain 2>/dev/null")
    memo_key = f"{(head.stdout or '').strip()}:{hashlib.sha256((dirty.stdout or '').encode()).hexdigest()}"
    cached = _COVERAGE_MEMO.get(thread_id)
    if cached and cached[0] == memo_key:
        logger.info("repo_scan coverage: reusing measurement for unchanged tree")
        return cached[1]

    result = await _run_coverage_via_ghcp(provider, thread_id, chat_provider=chat_provider, run_id=run_id)
    if result[0] is not None:
        _COVERAGE_MEMO[thread_id] = (memo_key, result)
    return result


# Human-readable expansion of each stable reason code, for the GATE's own `feedback` (read by the
# drafting LLM on retry) -- unlike `reason` itself, this text is never hashed into scan metrics, so
# it's free to be as actionable as it likes.
_REASON_FEEDBACK: dict[str, str] = {
    REASON_TIMEOUT: "the coverage run did not finish within its timeout",
    REASON_RUNNER_ERROR: "the coverage runner produced no parseable artifact (see server logs for the raw output)",
    REASON_PARSE_ERROR: "the coverage artifact could not be parsed",
    REASON_CONTRACT_REPLAY_FAILED: f"every entry in {COVERAGE_COMMANDS_PATH} failed on replay (see server logs for details)",
    REASON_NO_TOOLING_MAPPING: (
        "no coverage tooling mapping for this stack. Record working coverage command(s) in "
        f"{COVERAGE_COMMANDS_PATH} -- a JSON `entries` list where each entry has `command` (runs "
        "the suite with coverage), `artifact` (the file that command writes), `format` "
        "('cobertura' or 'istanbul-json-summary'), and `root` (the directory the command runs "
        "from; '' = repo root) -- one entry per app/test root, so the gate can replay them. The "
        "tech-stack doc's Testing section shows this stack's exact entries."
    ),
}


async def check_ac_depth(provider: SandboxProvider, thread_id: str) -> tuple[str, dict[str, Any]] | None:
    """GREEN-phase per-AC depth: `(feedback, report)` when a criterion is under-tested, else None.

    Reuses ac_coverage_gate's counters so RED and GREEN measure identically -- only the threshold
    differs. `ui_relevant` is NOT consulted here: that flag lives in ac-to-tests' own coverage_plan,
    which this stage does not produce, so only the level-agnostic "tests below the browser layer"
    requirement is enforced at this phase.
    """
    from .ac_coverage_gate import (
        MIN_NON_E2E_TESTS_PER_AC,
        count_tests_per_ac,
        depth_shortfalls,
    )
    from ..spec_ledger import load_ledger

    entries = await load_ledger(provider, thread_id)
    # Live criteria only: a RETIRED AC's tests are deliberately deleted (deletion propagation), so
    # counting it here would demand >=MIN tests for a criterion whose tests must not exist --
    # a deterministic deadlock on every ticket that removes a feature.
    ac_ids = sorted(
        {
            str(e.get("id"))
            for e in entries
            if "." in str(e.get("id") or "") and e.get("status") in ("active", "revised")
        }
    )
    if not ac_ids:
        return None  # no ledger, nothing to attribute -- never a fabricated failure

    listing = await provider.exec_in_sandbox(
        thread_id,
        "git ls-files -co --exclude-standard | grep -iE '(test|spec)' "
        r"| grep -vE '(^|/)(node_modules|\.playwright-browsers|bin|obj|dist|build|\.next|\.venv|vendor|TestResults|coverage|\.ai-dev-workflow|agent-work)/' "
        "| head -60 || true",
    )
    test_files: dict[str, str] = {}
    for line in (listing.stdout or "").splitlines():
        path = line.strip()
        if not path:
            continue
        contents = await repo_files.read_repo_file(provider, thread_id, path)
        if contents is not None:
            test_files[path] = contents
    if not test_files:
        return None  # the coverage gate above already blocks a repo with no tests

    counts = count_tests_per_ac(ac_ids, test_files)
    shortfalls = depth_shortfalls(
        counts, ui_relevant=set(), min_non_e2e=MIN_NON_E2E_TESTS_PER_AC, test_files=test_files
    )
    if not shortfalls:
        return None
    detail = "; ".join(f"{ac}: {' and '.join(problems)}" for ac, problems in sorted(shortfalls.items()))
    return (
        "Coverage meets the threshold, but these acceptance criteria are not tested deeply enough "
        f"-- each needs at least {MIN_NON_E2E_TESTS_PER_AC} test(s) BELOW the browser layer (unit "
        f"and/or integration), named with the criterion id so they can be attributed: {detail}\n\n"
        "A high coverage percentage does not mean each criterion is proven: one integration test "
        "through a small app can colour in every line while most criteria are only ever exercised "
        "through Playwright. Add the missing unit/integration tests -- do not weaken existing "
        "tests, and do not add browser tests to satisfy this.",
        {"ac_depth_shortfalls": {ac: problems for ac, problems in sorted(shortfalls.items())}},
    )


async def verify_coverage(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: SandboxProvider,
    chat_provider: str,
) -> "VerificationResult":
    """`chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is threaded straight
    through to measure_coverage below, which needs it for its own stack_runner.run_and_report
    call -- named distinctly from `provider` (the pre-existing SandboxProvider connection object)
    to avoid colliding with it. `run_id` (Phase E known-bugs fix) was previously accepted-and-
    ignored (`_run_id`, the same leading-underscore convention `_baseline_commit` below still
    uses) -- graph.py's deterministic_verify call site already passes a real one
    (`state.get("run_id", "unknown")`) positionally, it just wasn't threaded any further than this
    function's own signature. Now threaded through to measure_coverage the same way chat_provider
    is."""
    from ..graph import VerificationResult
    from .write_scope_gate import _is_pipeline_owned, _is_test_path

    # Did this stage write any APPLICATION code at all? Checked first, deterministically, because
    # the alternative is a confusing infra error several minutes later: with no app in the tree
    # there is no runnable project, so the coverage run correctly reports "nothing to measure" and
    # the drafting model gets feedback about coverage tooling when its actual mistake was writing
    # no code. Observed live: minimal-code-to-green "completed" in 18s having only answered in
    # JSON prose -- the same failure ac-to-tests already guards against with its own no-test-files
    # check, which is what makes that stage self-correct instead of looping blind.
    # Untracked files MUST be included, for the same reason write_scope_gate spells out: code this
    # stage just wrote is untracked until a later node commits it. A bare `git ls-files` sees none
    # of it -- observed live on nextjs-fastapi, where apps/api/main.py, apps/api/routers/ and
    # apps/web/ all existed on disk and this gate still reported "you wrote NO application code"
    # and killed the run.
    tracked = await provider.exec_in_sandbox(
        thread_id, "git ls-files && git ls-files --others --exclude-standard"
    )
    source_files = [
        stripped for line in (tracked.stdout or "").splitlines()
        if (stripped := line.strip())
        and not _is_pipeline_owned(stripped)
        and not _is_test_path(stripped)
        and _is_application_source(stripped)
    ]
    if not source_files:
        return VerificationResult(
            passed=False,
            feedback=(
                "You wrote NO application code -- the repository contains only test files and "
                "pipeline artifacts, with no project manifest (package.json/angular.json/.csproj/"
                "etc.) and no source files at all. Your structured response is metadata ABOUT work "
                "that must already exist on disk. Scaffold the application described in the "
                "approved Plan and implement it with your file tools NOW, before responding."
            ),
            report={"infra_error": "no_application_code", "source_files": 0},
        )

    missing_ui = await _missing_declared_frontend(provider, thread_id, source_files)
    if missing_ui:
        return VerificationResult(
            passed=False,
            feedback=(
                f"The approved Tech Stack declares {missing_ui} as this app's frontend, but the "
                "repository does not actually contain that frontend -- either it has no source "
                f"file for {missing_ui}, or its package.json never declares {missing_ui} as a "
                "dependency. Only the backend was really built, so the delivered app cannot be "
                "the app that was approved.\n\n"
                "Build the frontend described in the approved Plan: a package.json that really "
                "depends on the framework, real components/pages, and real build/test scripts, "
                "wired to the API you already wrote. A file that merely has the right extension, "
                "or a script that echoes a placeholder string, does not count -- that leaves the "
                "app non-functional while looking complete, which is the exact outcome every gate "
                "here exists to prevent."
            ),
            report={"infra_error": "declared_frontend_missing", "framework": missing_ui},
        )

    integration_problem = await _check_integration_fidelity(provider, thread_id, source_files)
    if integration_problem:
        return VerificationResult(
            passed=False,
            feedback=integration_problem,
            report={"infra_error": "integration_fidelity"},
        )

    line_rate, branch_rate, gaps, reason, entry_reports = await measure_coverage(provider, thread_id, chat_provider=chat_provider, run_id=run_id)

    if line_rate is None:
        # Infra failure, not a coverage gap: the coverage run itself never produced a readable
        # artifact. The per-entry detail carries what the run agent actually reported/attempted.
        # The per-entry errors are the ONLY place the concrete blocker is named, and the model
        # reads `feedback`, not `report` -- leaving them out of the feedback meant handing over a
        # generic "see server logs" while the actual, fixable diagnosis sat one field away.
        # Observed live (nextjs-dotnet): the run agent reported exactly "apps/api.Tests targets
        # net8.0 ... the sandbox has Microsoft.NETCore.App 10.0.10 but not 8.0.0, so testhost.dll
        # could not start", and the model was told none of it and burned the stage's whole budget.
        entry_errors = [str(detail["error"]) for detail in entry_reports if detail.get("error")]
        detail_text = ""
        if entry_errors:
            detail_text = "\n\nWhat the coverage run actually reported:\n" + "\n".join(
                f"- {err}" for err in entry_errors
            ) + (
                "\n\nFix the cause it names. If it is a toolchain mismatch (a project targeting a "
                "runtime this sandbox does not have installed), retarget the project to the "
                "version that IS installed -- check `dotnet --list-runtimes` / the equivalent for "
                "this stack rather than assuming a version."
            )
        return VerificationResult(
            passed=False,
            feedback=(
                "The coverage run produced no parseable report -- treat this as an infra failure, "
                f"not a coverage gap: {_REASON_FEEDBACK.get(reason, reason)}{detail_text}"
            ),
            report={"infra_error": reason, "contract_replay": entry_reports},
        )

    gaming_violations = await _check_exclusion_gaming(provider, thread_id)
    if gaming_violations:
        return VerificationResult(
            passed=False,
            feedback=(
                "Coverage-exclusion config was broadened outside known-safe generated-code patterns "
                f"-- this looks like gaming the coverage gate, not legitimate exclusion: {gaming_violations}"
            ),
            report={"gaming_violations": gaming_violations},
        )

    passed = line_rate >= MIN_COVERAGE_PERCENT and branch_rate >= MIN_COVERAGE_PERCENT
    report = {
        "line_rate": line_rate,
        "branch_rate": branch_rate,
        "threshold": MIN_COVERAGE_PERCENT,
        "gaps": [{"file": g.file, "line_rate": g.line_rate, "branch_rate": g.branch_rate} for g in gaps],
    }
    if entry_reports:
        report["contract_replay"] = entry_reports
    if passed:
        # Per-AC depth, enforced HERE rather than at ac-to-tests. This is the GREEN phase: the
        # implementation exists, so "2 tests below the browser layer per criterion" is a thing that
        # can actually be written. At the RED phase there is no module to unit-test yet and the same
        # requirement escalated three consecutive runs (see MIN_NON_E2E_TESTS_PER_AC_RED). High
        # coverage does NOT imply per-criterion depth: one integration test through a small app can
        # colour in every line while leaving most criteria proven only in the browser.
        depth = await check_ac_depth(provider, thread_id)
        if depth is not None:
            return VerificationResult(passed=False, feedback=depth[0], report={**report, **depth[1]})
        return VerificationResult(passed=True, feedback=f"Coverage {line_rate:.1f}%/{branch_rate:.1f}% (line/branch) meets the {MIN_COVERAGE_PERCENT}% threshold.", report=report)

    # Absolute deficit, not just rates: "88.0% vs 95%" reads as far away, "+7 branch sides" reads
    # as one lap of work. Summed from the replay entries' own covered/total counts.
    def _deficit(key: str) -> int | None:
        covered = total = 0
        for detail in entry_reports or []:
            match = re.fullmatch(r"(\d+)/(\d+)", str(detail.get(key, "")))
            if not match:
                return None
            covered += int(match.group(1))
            total += int(match.group(2))
        return max(0, math.ceil(MIN_COVERAGE_PERCENT / 100 * total) - covered) if total else None

    line_deficit, branch_deficit = _deficit("lines"), _deficit("branches")

    # Quote the SOURCE of every half-covered branch line. A bare line number sent the model
    # re-reading coverage reports for laps; the line's own text shows immediately whether the miss
    # is a testable other-side (write the test) or a defensive half no input can reach (delete it
    # -- observed live: every branch stuck across 8 flat laps was a `??`/ternary fallback the
    # code could never produce).
    gap_snippets: list[str] = []
    for gap in gaps[:6]:
        anno = re.match(r"(.+?) \(partially-covered branch lines: ([0-9, ]+)\)$", gap.file)
        if not anno:
            continue
        rel_name, nums = anno.group(1), [n.strip() for n in anno.group(2).split(",") if n.strip()][:6]
        awk = (
            f"awk -v ns={shlex.quote(','.join(nums))} "
            "'BEGIN{split(ns,a,\",\"); for(i in a) want[a[i]]=1} (NR in want){printf \"    line %d: %s\\n\", NR, $0}'"
        )
        found = await provider.exec_in_sandbox(
            thread_id,
            "cd /workspace/repo && f=$(git ls-files -co --exclude-standard | grep -F "
            + shlex.quote(rel_name)
            + " | head -1) && [ -n \"$f\" ] && " + awk + " \"$f\"",
        )
        if (found.stdout or "").strip():
            gap_snippets.append(f"  {rel_name}:\n{found.stdout.rstrip()}")
    snippet_text = (
        "\n\nThe exact half-covered branch lines:\n" + "\n".join(gap_snippets) + "\n\n"
        "For each line above: write the test that makes the condition take its OTHER side -- or, "
        "if no input can (a `??`/ternary fallback for a state your own code never produces), the "
        "condition is dead: delete it."
        if gap_snippets
        else ""
    )
    deficit_text = (
        f" To reach {MIN_COVERAGE_PERCENT}%: {line_deficit} more covered line(s) and "
        f"{branch_deficit} more covered branch side(s)."
        if line_deficit is not None and branch_deficit is not None
        else ""
    )
    return VerificationResult(
        passed=False,
        feedback=(
            f"Coverage {line_rate:.1f}%/{branch_rate:.1f}% (line/branch) is below the "
            f"{MIN_COVERAGE_PERCENT}% threshold.{deficit_text} "
            f"Files below threshold: {report['gaps']}{snippet_text}\n\n"
            "These per-file rates say WHERE the gap is, not WHICH paths are missed -- do not guess "
            "at it. Run the coverage command yourself and read the detailed report (an lcov/HTML "
            "report, or the per-line hit counts in the cobertura XML / istanbul json), find the "
            "exact lines and branches with zero hits in the files above, and write tests that "
            "drive those specific paths. Branch coverage is usually the binding constraint: for "
            "each uncovered branch, ask which input makes the condition take its other side -- "
            "typically an error/rejection path, an empty or single-element collection, an exact "
            "boundary value, or a rounding remainder case. Never weaken a test or broaden an "
            "exclusion to close the gap.\n\n"
            # Observed live (vue-dotnet, 6 straight verify cycles): every remaining branch was
            # either a guard clause unreachable through the public API or Program.cs wiring. The
            # model kept inventing reflection/private-constructor tests to colour those lines in,
            # the audit rejected them as gaming, and it reverted -- churning without ever gaining
            # a branch. Naming the two legitimate exits is what breaks that loop.
            "If a branch cannot be reached through the code's real public surface, do NOT reach "
            "for reflection, internal-visibility hacks, or a test that invokes a private "
            "constructor purely to colour in a line -- that is gaming the gate and the audit will "
            "reject it. Exactly two resolutions are legitimate:\n"
            "1. DELETE the unreachable code. A defensive check no caller can trigger is dead code; "
            "removing it closes the gap AND shrinks the codebase. This is usually right for guard "
            "clauses duplicating validation already enforced upstream.\n"
            "2. TEST IT AT THE RIGHT LEVEL. An app's composition root and endpoint wiring (e.g. "
            "Program.cs) is not unit-testable by construction; it is covered by in-process "
            "integration tests that boot the real app and drive it over HTTP -- ASP.NET Core's "
            "WebApplicationFactory<T>/TestServer, or this stack's equivalent. If the gap is in "
            "wiring, add those tests rather than deleting it. The factory hook is "
            "`public partial class Program { }` with an EMPTY body -- a private constructor added "
            "to Program is itself permanently-uncoverable lines (observed live: a 3-line private "
            "ctor was the whole remaining Program.cs gap); if one exists, delete it.\n"
            "Pick per branch, say which you chose and why, then actually make the edit. Reporting "
            "that coverage is stuck without changing any source or test file is not a valid "
            "outcome for this stage."
        ),
        report=report,
    )


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.gates.test_coverage_gate`
    """Self-check for the pure parts. `measure_coverage` itself is all sandbox I/O (replay a
    command, read the artifact back); its two artifact parsers and the timeout-wrap helper are
    the pure logic worth pinning here."""

    counts, err = _parse_cobertura_counts(
        '<coverage lines-covered="80" lines-valid="100" branches-covered="18" branches-valid="20">'
        "<packages><package><classes>"
        '<class filename="a.py" line-rate="0.5" branch-rate="1.0"/>'
        "</classes></package></packages></coverage>"
    )
    assert counts is not None and err == ""
    assert counts.lines_covered == 80 and counts.lines_total == 100
    assert counts.gaps and counts.gaps[0].file == "a.py", "under-threshold class must surface as a gap"
    assert _parse_cobertura_counts("not xml")[0] is None
    assert _parse_cobertura_counts('<coverage lines-covered="0" lines-valid="0"/>')[0] is None, (
        "zero instrumented lines must not report a hollow rate"
    )

    summary = {
        "total": {"lines": {"total": 100, "covered": 96, "pct": 96}, "branches": {"total": 10, "covered": 10, "pct": 100}},
        "src/x.ts": {"lines": {"pct": 50}, "branches": {"pct": 100}},
    }
    counts, err = _parse_istanbul_counts(json.dumps(summary))
    assert counts is not None and counts.lines_covered == 96, err
    assert counts.gaps and counts.gaps[0].file == "src/x.ts"
    assert _parse_istanbul_counts(json.dumps({"total": {"lines": {"total": 0}}}))[0] is None

    # timeout wraps the WHOLE command via `sh -c`, so a chained/if-else command is bounded as one
    # unit -- a bare `timeout N cmd1 && cmd2` would only bound cmd1.
    assert _with_timeout("echo hi", None) == "echo hi", "no timeout_seconds must be a no-op"
    wrapped = _with_timeout("echo hi && echo bye", 30)
    assert wrapped.startswith("timeout 30 sh -c "), wrapped
    assert "echo hi && echo bye" in wrapped

    # Every reason `measure_coverage` can return is a short stable code (never raw subprocess
    # output) -- this is what keeps repo_scan's metrics.coverage.reason, and therefore
    # content_hash, deterministic across two runs of an unchanged repo. `_REASON_FEEDBACK` must
    # cover every one of them so the gate's own feedback stays actionable despite the terse code.
    for code in STABLE_REASON_CODES:
        assert " " not in code, f"{code!r} looks like prose, not a stable code"
        assert code in _REASON_FEEDBACK, f"{code!r} has no human-readable gate feedback"

    # MIN_COVERAGE_PERCENT is a module-level env read -- pin that it parsed to a float and still
    # defaults to 95.0 when unset. Its MIN_COVERAGE_PERCENT_DEFAULT alias went with
    # gates/audit_gates.py, which was its only importer.
    assert isinstance(MIN_COVERAGE_PERCENT, float)
    assert MIN_COVERAGE_PERCENT == 95.0, "default must stay 95.0 when env var MIN_COVERAGE_PERCENT is unset"

    # The exact false-green tree observed live: manifests + a task store hidden in the test
    # helper, and no application at all -- must NOT count as application source.
    assert not _is_application_source("package.json")
    assert not _is_application_source("tsconfig.json")
    assert not _is_application_source("apps/Directory.Build.props")
    assert not _is_application_source("apps/web/vitest.config.ts")
    assert not _is_application_source("apps/web/karma.conf.cjs")
    # Real implementation files do count.
    assert _is_application_source("apps/web/src/app/task.service.ts")
    assert _is_application_source("apps/api/Program.cs")
    assert _is_application_source("apps/web/src/app/app.component.html")
    # Regression: coverlet's capital-T branch="True" must be recognised, and the gap must name
    # the specific half-covered line rather than an empty list.
    _dotnet = """<coverage lines-covered="257" lines-valid="261" branches-covered="74" branches-valid="84">
      <packages><package><classes>
      <class name="Api.Program" filename="Program.cs" line-rate="0.9555" branch-rate="0.8888"><lines>
      <line number="16" hits="30" branch="True" condition-coverage="50% (1/2)"/>
      <line number="25" hits="22" branch="True" condition-coverage="100% (2/2)"/>
      </lines></class></classes></package></packages></coverage>"""
    _counts, _err = _parse_cobertura_counts(_dotnet)
    assert _err == "" and _counts is not None, _err
    assert _counts.gaps, 'capital-T branch="True" was not recognised -- gaps came back empty'
    assert "16" in _counts.gaps[0].file and "25" not in _counts.gaps[0].file, _counts.gaps[0].file
    assert 74.0 < _counts.gaps[0].branch_rate < 76.0, _counts.gaps[0].branch_rate

    # Generated code is excluded from the aggregate AND the gap list: a source-generator's .g.cs
    # under obj/ (observed live holding branches at 88.9% while authored code stood higher) has its
    # per-line contribution subtracted from the root counters.
    _gen = """<coverage lines-covered="90" lines-valid="110" branches-covered="8" branches-valid="12">
      <packages><package><classes>
      <class name="Api.Isbn" filename="Services/IsbnValidator.cs" line-rate="1.0"><lines>
      <line number="5" hits="1" branch="True" condition-coverage="100% (2/2)"/>
      </lines></class>
      <class name="Regex_g" filename="obj/Debug/net10.0/System.Text.RegularExpressions.Generator/RegexGenerator.g.cs" line-rate="0.5"><lines>
      <line number="73" hits="1" branch="True" condition-coverage="50% (2/4)"/>
      <line number="74" hits="0"/>
      <line number="75" hits="1"/>
      </lines></class>
      </classes></package></packages></coverage>"""
    _gc, _gerr = _parse_cobertura_counts(_gen)
    assert _gerr == "" and _gc is not None, _gerr
    assert (_gc.lines_covered, _gc.lines_total) == (88, 107), (_gc.lines_covered, _gc.lines_total)
    assert (_gc.branches_covered, _gc.branches_total) == (6, 8), (_gc.branches_covered, _gc.branches_total)
    assert all("RegexGenerator" not in g.file for g in _gc.gaps), _gc.gaps

    # Stack fidelity. The live nextjs-dotnet miss: Next.js declared, only C# delivered.
    assert missing_declared_frontend(
        ["Next.js", "ASP.NET Core"], ["apps/api/Program.cs", "apps/api/AppState.cs"]
    ) == "Next.js"
    # Satisfied once real frontend sources exist.
    assert missing_declared_frontend(
        ["Next.js", "ASP.NET Core"], ["apps/api/Program.cs", "apps/web/app/page.tsx"]
    ) is None
    # Each framework family recognised by its own signature -- a Vue app is not satisfied by .tsx.
    assert missing_declared_frontend(["Vue"], ["apps/web/src/App.vue"]) is None
    assert missing_declared_frontend(["Vue"], ["apps/web/src/App.tsx"]) == "Vue"
    assert missing_declared_frontend(["Blazor"], ["apps/web/Pages/Index.razor"]) is None
    assert missing_declared_frontend(["Blazor"], ["apps/api/Program.cs"]) == "Blazor"
    assert missing_declared_frontend(["Angular"], ["apps/web/src/app/app.component.ts"]) is None
    # Cross-table invariant, not another fixture: verify_coverage filters source_files through
    # _is_application_source BEFORE missing_declared_frontend sees them, so a signature suffix the
    # extension allowlist rejects is dead code and the framework reads as "never built". The
    # assertions above pass a path list in directly, so they proved the pure function correct while
    # production was rejecting every .razor file -- which is exactly how the blazor-dotnet false
    # negative survived. Assert the two tables agree instead of trusting another hand-written path.
    for _marker, _suffixes in _FRONTEND_SIGNATURES:
        for _suffix in _suffixes:
            assert _is_application_source(f"apps/web/src/thing{_suffix}"), (
                f"_FRONTEND_SIGNATURES declares {_suffix!r} for {_marker!r} but "
                "_is_application_source rejects it -- that signature can never match"
            )
    # Backend-only stacks must never be flagged -- no UI framework is declared at all.
    assert missing_declared_frontend(["FastAPI", "ASP.NET Core"], ["apps/api/main.py"]) is None

    # Dependency half. The live placeholder manifest: scripts that echo, no dependencies at all.
    _placeholder = json.dumps({"name": "web", "scripts": {"build": "node -e \"console.log('x')\""}})
    assert missing_frontend_dependency(["Next.js"], _placeholder) == "Next.js"
    assert missing_frontend_dependency(["Next.js"], json.dumps({"dependencies": {"next": "15.0.0"}})) is None
    # devDependencies count -- test-only frameworks legitimately live there.
    assert missing_frontend_dependency(["Vue"], json.dumps({"devDependencies": {"vue": "3.4.0"}})) is None
    assert missing_frontend_dependency(["Angular"], json.dumps({"dependencies": {"@angular/core": "18.0.0"}})) is None
    assert missing_frontend_dependency(["Angular"], json.dumps({"dependencies": {"angular-cli": "1.0.0"}})) == "Angular"
    # Fails OPEN on no/unreadable manifest -- absence of evidence is not evidence of absence.
    assert missing_frontend_dependency(["Next.js"], None) is None
    assert missing_frontend_dependency(["Next.js"], "{not json") is None
    # Non-npm frontends are not judged by package.json at all.
    assert missing_frontend_dependency(["Blazor"], _placeholder) is None

    # Integration fidelity. The live nextjs-dotnet failure: "ASP.NET Core Web API" declared, but
    # apps/api was a plain class library with no Program.cs, beside a localStorage-only frontend.
    _lib_only = {"apps/api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup/></Project>'}
    assert missing_hosted_backend(["ASP.NET Core Web API"], _lib_only) == "ASP.NET Core Web API"
    _hosted = {
        "apps/api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>',
        "apps/api/Program.cs": "var builder = WebApplication.CreateBuilder(args);",
    }
    assert missing_hosted_backend(["ASP.NET Core Web API"], _hosted) is None
    # A FrameworkReference-style host counts too -- plain Sdk is not by itself proof of a library.
    assert missing_hosted_backend(
        ["ASP.NET Core"], {"a/Program.cs": "WebApplication.CreateBuilder(args)"}
    ) is None
    assert missing_hosted_backend(["FastAPI"], {"api/main.py": "app = FastAPI()"}) is None
    assert missing_hosted_backend(["FastAPI"], {"api/main.py": "def add(a, b): return a + b"}) == "FastAPI"
    # Fails OPEN: nothing declared, or nothing readable.
    assert missing_hosted_backend(["Next.js", "xUnit"], _lib_only) is None
    assert missing_hosted_backend(["ASP.NET Core"], {}) is None

    # OTel presence: any signal anywhere in the sampled backend files passes (deliberately
    # permissive -- see missing_otel_instrumentation's docstring); its total absence for a declared,
    # hosted backend does not.
    assert missing_otel_instrumentation(["ASP.NET Core Web API"], _hosted) == "ASP.NET Core Web API"
    _hosted_with_otel = {**_hosted, "apps/api/Api.csproj": _hosted["apps/api/Api.csproj"] + "<!-- OpenTelemetry.Extensions.Hosting -->"}
    assert missing_otel_instrumentation(["ASP.NET Core Web API"], _hosted_with_otel) is None
    assert missing_otel_instrumentation(["FastAPI"], {"api/main.py": "app = FastAPI()"}) == "FastAPI"
    assert missing_otel_instrumentation(
        ["FastAPI"], {"api/main.py": "from opentelemetry.sdk.trace import TracerProvider\napp = FastAPI()"}
    ) is None
    # The needles are ecosystem-specific: the frontend's @opentelemetry/sdk-trace-web import must
    # NOT satisfy the .NET backend's requirement (one flat evidence blob made exactly that exploit
    # possible -- W6's own templates guarantee the frontend string exists in every dual-stack repo).
    assert missing_otel_instrumentation(
        ["ASP.NET Core Web API", "Vue"],
        {**_hosted, "apps/web/src/main.ts": 'import { WebTracerProvider } from "@opentelemetry/sdk-trace-web"'},
    ) == "ASP.NET Core Web API"
    # ...and the backend's sdk-node must not satisfy an uninstrumented React frontend.
    assert missing_otel_instrumentation(
        ["React", "Express"],
        {"apps/api/src/tracing.ts": 'import { NodeSDK } from "@opentelemetry/sdk-node"'},
    ) == "React"
    # Frontends are checked too (W6): a Next.js app with readable files and no OTel signal fails;
    # either the scoped packages or Next's own @vercel/otel wrapper passes. A bare "hotel" string
    # must NOT pass (the needle is the scoped-package prefix, not "otel").
    assert missing_otel_instrumentation(["Next.js", "xUnit"], _lib_only) == "Next.js"
    assert missing_otel_instrumentation(["Next.js"], {"package.json": '"@vercel/otel": "^1.0"'}) is None
    assert missing_otel_instrumentation(["Next.js"], {"package.json": '"@opentelemetry/sdk-node": "^0.52"'}) is None
    assert missing_otel_instrumentation(["Next.js"], {"web/app.tsx": "const hotel = bookHotel()"}) == "Next.js"
    # Fails OPEN: nothing declared, or nothing readable -- same discipline as missing_hosted_backend.
    assert missing_otel_instrumentation(["ASP.NET Core"], {}) is None

    # Frontend that never calls the API is the other half of the same defect.
    assert frontend_only_uses_local_storage({"web/storage.ts": "localStorage.setItem('x', v)"})
    assert not frontend_only_uses_local_storage({"web/api.ts": "await fetch(`${base}/expenses`)"})
    # A cache in front of real HTTP calls is fine -- only storage with NO http call is the failure.
    assert not frontend_only_uses_local_storage(
        {"web/api.ts": "await fetch(u)", "web/cache.ts": "localStorage.setItem('c', v)"}
    )
    assert not frontend_only_uses_local_storage({})

    # The evasion that passed every check above, taken verbatim from a live run: a declared .NET API
    # that was really built, a frontend that really calls fetch -- at its OWN Next.js route handler,
    # backed by an in-process module store. The .NET service was never contacted by anything.
    self_implemented = {
        "apps/web/src/app/api/counter/route.ts": (
            "import { NextResponse } from 'next/server';\n"
            "import { getCounterStore } from '../../../lib/counter-store';\n"
            "export function GET() {\n"
            "  const store = getCounterStore();\n"
            "  return NextResponse.json({ value: store.get() });\n"
            "}\n"
        )
    }
    assert frontend_reimplements_backend(self_implemented) == [
        "apps/web/src/app/api/counter/route.ts"
    ], frontend_reimplements_backend(self_implemented)

    # A thin proxy must keep PASSING -- forwarding to the backend is a legitimate BFF design, and
    # this check must not push anyone away from it.
    proxy = {
        "apps/web/src/app/api/counter/route.ts": (
            "export async function GET() {\n"
            "  const res = await fetch(`${process.env.API_BASE_URL}/counter`);\n"
            "  return Response.json(await res.json());\n"
            "}\n"
        )
    }
    assert frontend_reimplements_backend(proxy) == [], frontend_reimplements_backend(proxy)
    absolute = {"r.ts": "await fetch('http://127.0.0.1:5080/counter')"}
    assert frontend_reimplements_backend(absolute) == []
    assert frontend_reimplements_backend({}) == []

    # Contract replay: a validated coverage-commands.json is re-run by Python (delete artifact,
    # cd root, command under a timeout); a malformed contract yields [] so discovery runs instead.
    import asyncio

    good = json.dumps({"entries": [
        {"root": "", "command": "dotnet test /p:CollectCoverage=true", "artifact": "apps/api.Tests/TestResults/coverage.cobertura.xml", "format": "cobertura"},
        {"root": "apps/web", "command": "npx vitest run --coverage", "artifact": "apps/web/coverage/coverage-summary.json", "format": "istanbul-json-summary"},
    ]})
    loaded = _load_coverage_contract(good)
    assert len(loaded) == 2 and loaded[1].root == "apps/web", loaded
    assert _load_coverage_contract(None) == [] and _load_coverage_contract("not json") == []
    assert _load_coverage_contract(json.dumps({"entries": [{"root": "", "command": "", "artifact": "x.xml", "format": "cobertura"}]})) == []
    assert _load_coverage_contract(json.dumps({"entries": [{"root": "", "command": "x", "artifact": "../evil", "format": "cobertura"}]})) == []

    class _Res:
        def __init__(self, rc: int) -> None:
            self.returncode, self.stdout, self.stderr = rc, "ran", ""

        @property
        def ok(self) -> bool:
            return self.returncode == 0

    class _Prov:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def exec_in_sandbox(self, _t: str, command: str) -> _Res:
            self.commands.append(command)
            return _Res(0)

    prov = _Prov()
    runs = asyncio.run(_replay_coverage_contract(prov, "t", loaded))
    assert len(runs) == 2 and runs[0]["exit_code"] == 0, runs
    assert prov.commands[0].startswith("rm -f apps/api.Tests/TestResults/coverage.cobertura.xml; cd . && timeout"), prov.commands[0]
    assert "cd apps/web && timeout" in prov.commands[1] and "npx vitest run --coverage" in prov.commands[1], prov.commands[1]
    print("test_coverage_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
