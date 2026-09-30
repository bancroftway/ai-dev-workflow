"""metrics-exit's pure verdict logic, extracted from exit_nodes.verify_exit_readiness (2026-09-30,
Task 14) so the sandbox's own same-turn Stop hook (sandbox-image/hooks/check-exit-readiness-stop.mjs)
can run the REAL checks by shelling out to `python3` on this ONE file, instead of a hand-ported
JavaScript reimplementation drifting from it -- same rationale as `adversarial_audit_checks.py`'s
own extraction from `adversarial_gate.py`; see that module's docstring for the full argument.

`exit_nodes.py` itself cannot be the staged copy: it imports `..chat_model`/`..git_ops`/
`..preflight_nodes`/`..session_store`/`..spec_ledger`/`..workflow_persistence` (pydantic-backed,
sandbox-provider-backed, none installed in the sandbox). This module needs only the stdlib.

Two families of functions live here:

1. Ported UNCHANGED from `verify_exit_readiness`'s own body (manifest presence checks, screenshot
   check, metrics/regression-gate/readme check, auth check, targeted-fix-unresolved fold-in, the
   stale-carried-over-blocker filter, and the final merge_ready decision) -- `exit_nodes.py` imports
   every one of these back and calls them exactly where the inline logic used to live; this is a
   pure code-move, not a fork. The self-check below is the proof this extraction changed no
   behavior (values ported straight from `exit_nodes.py`'s own prior assertions).

2. A hand-port of `app_discovery.classify_candidates`'s classification rules (`_app_dir`,
   `_csproj_signals`, `_package_json_signals`, `classify_candidates`, `candidates_to_app_dicts`).
   `app_discovery.py` itself cannot be imported here (its module level pulls in
   `langchain_core.runnables` and the sandbox provider stack) or byte-copied (same reason), so this
   is a deliberate, bounded, KEEP-IN-SYNC-BY-HAND duplication -- same convention as
   `check-coverage-stop.mjs`'s own `TEST_FILE_LISTING` comment. Scope is deliberately narrower than
   the source: only the marker names `classify_candidates` actually branches on (no port
   corroboration, no Dockerfile/docker-compose/go.mod/build.gradle/pom.xml/next.config/vite.config
   reads -- none of those affect whether ANY app is found, only cosmetic port/evidence detail this
   hook's boolean "is app_check.apps empty" check never needs). Re-sync by re-reading
   `app_discovery.py`'s own `_csproj_signals`/`_package_json_signals`/`classify_candidates` if their
   classification RULES (not the port/evidence extras) ever change.

WHY THIS MATTERS FOR THE SAME-TURN HOOK SPECIFICALLY: `app_check.apps` is genuinely empty in
manifest.json at the START of every greenfield run's metrics-exit turn (app_check_record_node runs
pre-scaffold, before any code exists -- see `app_discovery.py`'s own docstring). `verify_exit_readiness`
re-scans and backfills it before checking presence; a same-turn hook that checked presence on the
RAW, not-yet-completed manifest would false-nudge "no runnable app" on every single greenfield run.
Porting the classification rules (not skipping this check) is what keeps this hook honest on the
single most common case it will actually see.

CLI mode (`python3 exit_readiness_checks.py --check-hook`, stdin: JSON payload built by the hook,
stdout: JSON `{"passed": bool, "reasons": [str, ...]}`) is what the Stop hook actually invokes.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------------------------
# Presence-list tolerant unwrap/build -- private duplicates of schemas.presence_values /
# exit_nodes._presence_from_values (both pydantic-adjacent modules this file must not import; see
# this module's own docstring). Same 4-6 line duplication precedent as adversarial_audit_checks.py's
# own `_findings_from`. exit_nodes.py keeps its OWN copies for the call sites outside
# verify_exit_readiness (exit_finalize_node) that this extraction deliberately leaves untouched.
# ---------------------------------------------------------------------------------------------


def _presence_values(entry: Any) -> list[Any]:
    """The `values` list of a PresenceList-shaped dict, tolerating a legacy bare list or a
    missing/None field."""
    if isinstance(entry, dict):
        return list(entry.get("values") or [])
    if isinstance(entry, list):
        return list(entry)
    return []


def _presence_from_values(values: list[str], *, empty_reason: str) -> dict[str, Any]:
    """Build a PresenceList-shaped dict from a plain list: `status='present'` for a non-empty
    list, `status='absent'` with `empty_reason` otherwise."""
    if values:
        return {"status": "present", "values": values, "reason": ""}
    return {"status": "absent", "values": [], "reason": empty_reason}


# ---------------------------------------------------------------------------------------------
# Gate-owned reason vocabulary + manifest-completeness topics -- moved verbatim from exit_nodes.py.
# ---------------------------------------------------------------------------------------------

# Phrases the metrics regression gate OWNS -- every one of these comes from
# metrics_nodes.regression_reasons and from nowhere else. A blocking reason containing one of them
# is a claim about a deterministic measurement, so this run's gate output is the only authority on
# whether it is true. Anything outside this vocabulary is the drafting model's own reasoning and is
# never second-guessed here. Kept as substrings, not exact strings, because the gate interpolates
# live numbers ("duplication 10.5% exceeds...") that will not match a previous run's text.
GATE_OWNED_REASON_MARKERS = (
    "gating finding(s) open",
    "coverage unmeasured",
    "coverage below threshold",
    "exceeds the",          # duplication threshold
    "regressed",            # coverage/health regression deltas
    # Any NEW deterministic blocker vocabulary must be added here too, or a blocker fixed on run
    # N re-blocks every later run: the drafting model reads the committed EXIT-REPORT.md and
    # copies old blockers forward verbatim (see the stale-reason filter below).
    "README.md is missing or empty",           # readme_gate hard problems (verbatim prefixes)
    "standard-readme requires it",
    "has no H1 title",
    "must be the LAST section",
    "authentication enforcement was required for this run",  # this module's own blocker
    # exit_finalize_node's run_failure injection. Deliberately NOT "run failed at" -- that exact
    # phrase appears in git_ops's failure commit message and in ordinary model prose, so it would
    # both get copied forward and falsely filter legitimate reasons.
    "terminal pipeline failure recorded at",
    # metrics_problems' own "metrics.get('run_id') == run_id" check -- this exact marker's own
    # absence was a real bug once (income-investor thread f0fef8ba, 2026-09-26): this phrase got
    # baked into metrics-exit's approved_content on a run whose metrics genuinely hadn't landed yet,
    # then survived every later resume forever because it was never gate-owned by any existing
    # marker.
    "the regression gate never passed",
)

# Manifest-completeness topics manifest_presence_problems computes fresh on every call -- these
# need topic-based (not exact-substring) matching, unlike GATE_OWNED_REASON_MARKERS above. Each
# keyword must appear in BOTH this module's own short deterministic phrase ("manifest.json has no
# test_command for this stack", "...has no coverage_commands -- coverage is not replayable",
# "...records no runnable app...") and plausibly in the model's own freely-worded paraphrase of the
# same topic.
MANIFEST_COMPLETENESS_TOPIC_KEYWORDS = ("test_command", "coverage_command", "runnable app")


def manifest_completeness_topic(reason: str) -> str | None:
    """Which manifest-completeness topic (if any) `reason` is about -- "manifest.json" together
    with one of MANIFEST_COMPLETENESS_TOPIC_KEYWORDS, requiring both so a reason that merely
    mentions one of the keywords in an unrelated context is never falsely claimed."""
    if "manifest.json" not in reason:
        return None
    return next((kw for kw in MANIFEST_COMPLETENESS_TOPIC_KEYWORDS if kw in reason), None)


# ---------------------------------------------------------------------------------------------
# Manifest completion -- moved verbatim from exit_nodes.py.
# ---------------------------------------------------------------------------------------------


def combined_test_command_from_apps(apps: list[dict[str, Any]]) -> str | None:
    """One combined `cd <path> && <command>` per app, joined with ` && `, from manifest.json's
    own app_check.apps -- the fallback the manifest-completion step reaches for when
    `resolve_test_command(tech_stack)` returns None (tech-stack's own languages/package_managers
    are empty) AND a fixing agent has already added a per-app `test_command` directly to one or
    more app objects.

    A bare per-app command ("npm test") is not yet runnable from the repo root without first
    `cd`-ing into that app's own path, which this function supplies; a `runtime`-based guess
    (python -> pytest, node -> npm test) covers an app whose own test_command is still missing, so
    ONE app carrying it is enough to unblock the whole manifest rather than requiring every app to.

    Purely documentary output (nothing in this codebase executes manifest.json's test_command --
    unlike coverage_commands, which IS replayed and validated), so no path validation is applied
    here."""
    segments: list[str] = []
    for app in apps:
        path = app.get("path")
        if not path:
            continue
        command = app.get("test_command")
        if not command:
            runtime = app.get("runtime")
            command = {"python": "python3 -m pytest", "node": "npm test"}.get(str(runtime))
        if command:
            segments.append(f"cd {path} && {command}")
    return " && ".join(segments) or None


def resolve_manifest_updates(
    manifest: dict[str, Any],
    *,
    resolved_apps: list[dict[str, Any]] | None,
    scan_fingerprint: str | None,
    resolved_test_command: str | None,
    coverage_entries: list[Any] | None,
) -> dict[str, Any]:
    """The `updates` dict verify_exit_readiness (or the same-turn hook) would persist into
    manifest.json, given already-computed candidate values -- callers only compute
    `resolved_apps`/`resolved_test_command`/`coverage_entries` when the corresponding manifest
    field is ALREADY missing (this function re-checks that too, so passing a value the manifest
    doesn't need is harmless), preserving the real gate's own conditional-computation cost profile
    (it only calls the expensive `app_discovery.collect_evidence` sandbox scan when apps are
    genuinely missing)."""
    updates: dict[str, Any] = {}
    app_check = manifest.get("app_check") or {}
    if not (app_check.get("apps") or []) and resolved_apps:
        updates["app_check"] = {"apps": resolved_apps, "evidence_fingerprint": scan_fingerprint}
    if not manifest.get("test_command") and resolved_test_command:
        updates["test_command"] = resolved_test_command
    if not manifest.get("coverage_commands") and coverage_entries:
        updates["coverage_commands"] = coverage_entries
    return updates


def apply_manifest_updates(manifest: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """A read-only PREVIEW of what `preflight_nodes.update_manifest` would persist, without writing
    to disk -- for the same-turn hook, which must never mutate repo state itself (only
    verify_exit_readiness's own real call actually persists via preflight_nodes.update_manifest).
    Mirrors that function's `app_check` deep-merge exactly (its own comment: "app_check is itself
    co-owned ... deep-merge that one key so a partial update can't clobber the fingerprint")."""
    if isinstance(updates.get("app_check"), dict) and isinstance(manifest.get("app_check"), dict):
        updates = {**updates, "app_check": {**manifest["app_check"], **updates["app_check"]}}
    merged = dict(manifest)
    merged.update(updates)
    return merged


# ---------------------------------------------------------------------------------------------
# The 3 presence checks + screenshot check -- moved verbatim from exit_nodes.py.
# ---------------------------------------------------------------------------------------------


def manifest_presence_problems(
    manifest: dict[str, Any], *, test_command_resolvable: bool = False
) -> list[str]:
    """What a merge actually needs recorded: a runnable app, a test_command, coverage_commands.
    Operates on the manifest AFTER completion (resolve_manifest_updates/apply_manifest_updates) --
    calling this on a raw, not-yet-completed manifest false-positives on every greenfield run.

    `test_command_resolvable` (Task 14 review fix): True suppresses the "no test_command" problem
    even though `manifest.get("test_command")` is still falsy -- for the same-turn HOOK only, which
    can determine (via `tech_stack_resolves_test_command`) that
    `ac_coverage_gate.resolve_test_command` WOULD resolve one at verify time, without being able to
    compute (or wanting to persist) the exact resolved STRING itself. Writing a placeholder/guessed
    string into the hook's own local preview risked the model copying a WRONG guessed value into
    manifest.json by hand after seeing it in a nudge -- the "sticky wrong value" risk this parameter
    exists to avoid; see `tech_stack_resolves_test_command`'s own docstring for the full reasoning.
    The real gate never needs this (stays False, the default): by the time it calls this function,
    `resolve_test_command` has already run for real and `manifest.get("test_command")` is already
    set whenever this would have mattered, so the two branches can never disagree in practice."""
    problems: list[str] = []
    app_check = manifest.get("app_check") or {}
    if app_check.get("suitable") is not False and not (app_check.get("apps") or []):
        problems.append("manifest.json records no runnable app (app_check.apps is empty even after re-scan)")
    if not manifest.get("test_command") and not test_command_resolvable:
        problems.append("manifest.json has no test_command for this stack")
    if not manifest.get("coverage_commands"):
        problems.append("manifest.json has no coverage_commands -- coverage is not replayable")
    return problems


def tech_stack_resolves_test_command(tech_stack: dict[str, Any] | None) -> bool:
    """Best-effort BOOLEAN mirror of `ac_coverage_gate.resolve_test_command`'s own branch
    structure -- NOT a byte-faithful command-string port. This hook never needs the resolved
    STRING (see `manifest_presence_problems`'s own docstring for why persisting a guess would be
    unsafe); it only needs to know whether `resolve_test_command` would find SOMETHING at verify
    time, to decide whether "no test_command" is a real gap the model must act on now, or something
    the real gate's own resolution will silently fill in moments later.

    Root-caused during task-14 review: the hook's own `combined_test_command_from_apps` fallback
    only ever helps when `app_check.apps` is ALREADY populated with per-app `test_command` fields
    (a narrow rescue-mechanism case, not this function's job) -- it never covers
    `resolve_test_command`'s PRIMARY, tech-stack-driven resolution, whose FIRST branch is dotnet,
    matched unconditionally whenever `dotnet_detected` is true. Without this function, EVERY
    dotnet-stack repo's first metrics-exit draft turn (a stack this same module's own
    `csproj_signals`/`Program.cs` branches explicitly support) hit a deterministic false "no
    test_command" block, since manifest.json's `test_command` field is written nowhere else in this
    codebase except this exact completion step (confirmed by review: grepped all of `agent/src` for
    writers) -- so it is unconditionally unset on that very first turn.

    `resolve_test_command`'s own branches (dotnet / typescript-or-javascript / python) each return
    SOME command unconditionally once their language/stack test fires -- `testing_frameworks`
    (vitest vs. jest vs. mocha) only picks WHICH exact command text, never whether one exists at
    all -- so mirroring the OUTER branch structure alone (which this function does) is sufficient
    to answer "would resolve_test_command return non-None", with no need to also replicate the
    inner framework-specific command text or the `dotnet_root_prefix`/`ecosystem_root_prefix`
    monorepo-root logic (`tech_stack_signals.py`), neither of which changes this boolean.

    Reads the tech-stack JSON directly (both the CURRENT `dotnet: {status: ...}` shape and the
    legacy pre-consolidation `dotnet_detected: bool` shape an older on-disk sidecar may still carry
    -- see `tech_stack_signals.dotnet_detected`'s own docstring for that legacy shape's history),
    tolerating a missing/malformed tech-stack file as "nothing detected" -- same pydantic-avoidance
    simplification the hook's own `is_ui` read already uses, for the identical reason
    (`ac_coverage_gate.resolve_test_command` needs `tech_stack_signals.dotnet_detected`/
    `presence_values`, both pydantic-backed via `load_tech_stack`)."""
    tech_stack = tech_stack or {}
    dotnet = tech_stack.get("dotnet")
    if isinstance(dotnet, dict) and dotnet.get("status") == "detected":
        return True
    if tech_stack.get("dotnet_detected") is True:  # legacy pre-consolidation shape
        return True
    languages = {str(lang).lower() for lang in _presence_values(tech_stack.get("languages"))}
    return bool(languages & {"typescript", "javascript", "python"})


def screenshot_problems(is_ui: bool, screenshot_count: int) -> list[str]:
    """Mandatory visual evidence for UI apps, whatever path e2e took."""
    if is_ui and screenshot_count == 0:
        return ["UI application but no e2e screenshots were captured"]
    return []


# ---------------------------------------------------------------------------------------------
# Metrics regression gate / README / auth checks -- moved verbatim from exit_nodes.py.
# ---------------------------------------------------------------------------------------------


def metrics_problems(metrics: dict[str, Any], run_id: str) -> tuple[list[str], bool]:
    """(problems, metrics_matched). metrics_matched=False means metrics.run_id != run_id (a stale
    or absent metrics-latest.json) -- callers must skip the auth check too in that case, mirroring
    verify_exit_readiness's own two separate `if metrics.get("run_id") == run_id:` blocks."""
    if metrics.get("run_id") != run_id:
        return ["metrics were not recorded for this run -- the regression gate never passed"], False
    problems = list((metrics.get("regression_gate") or {}).get("reasons") or [])
    # README leg (W7): hard standard-readme problems still open after the leg's own retry laps
    # block the merge -- but only when the leg OWNS the README (a human-authored brownfield README
    # is advisory-only by design, readme_write_node's rule).
    readme = metrics.get("readme") or {}
    if readme.get("owned"):
        problems.extend(readme.get("problems") or [])
    return problems, True


def auth_problems(
    metrics: dict[str, Any], auth_gate_enabled: bool
) -> tuple[list[str], str | None]:
    """(problems, auth_note). Only meaningful when metrics_problems already confirmed
    metrics.run_id == run_id -- caller must gate this the same way verify_exit_readiness nests it."""
    app_auth = metrics.get("app_auth") or {}
    e2e_snapshot = metrics.get("e2e") or {}
    auth_required = (
        auth_gate_enabled
        and app_auth.get("auth_mode") in ("required", "anonymous_list")
        and bool(app_auth.get("secrets_present"))
    )
    if not auth_required:
        return [], None
    if not (e2e_snapshot.get("auth_check") or {}).get("passed"):
        return [
            "authentication enforcement was required for this run but was not verified "
            f"(e2e status: {e2e_snapshot.get('status') or 'never started'}; auth probe "
            f"{'failed' if e2e_snapshot.get('auth_check') else 'never ran'})"
        ], None
    auth_note = f"Authentication enforcement verified: {(e2e_snapshot.get('auth_check') or {}).get('feedback')}"
    return [], auth_note


def targeted_fix_unresolved_problems(payload: dict[str, Any], run_id: str) -> list[str]:
    """Fold TARGETED_FIX_UNRESOLVED_PATH's parsed content into `problems`, run-id-stamped: a file
    whose `run_id` doesn't match THIS run's is from a prior attempt/run and is silently ignored."""
    if payload.get("run_id") != run_id:
        return []
    return [str(r) for r in (payload.get("reasons") or [])]


# ---------------------------------------------------------------------------------------------
# Stale-blocker filter + final merge_ready decision -- moved verbatim from exit_nodes.py. Needs the
# model's own draft merge_ready/blocking_reasons (Task 10's read-final-json.mjs is how the hook gets
# these; see that module's own docstring).
# ---------------------------------------------------------------------------------------------


def evaluate_merge_readiness(
    problems: list[str], model_blocking_reasons: Any, model_merge_ready: bool | None
) -> dict[str, Any]:
    """Drop STALE deterministic blockers the model carried over from a previous run's report, then
    decide the final merge_ready verdict.

    The metrics regression gate owns a fixed vocabulary of reasons (GATE_OWNED_REASON_MARKERS), and
    it is authoritative: if a reason in that vocabulary is not in THIS run's gate output, this run
    did not have that problem. The drafting model reads the repository, and a previous
    EXIT-REPORT.md is committed in it -- so it can and does copy old blockers forward verbatim.
    Manifest-completeness topics get their own topic-based version of the same drop (the model
    paraphrases these freely rather than copying this module's own short deterministic phrase
    verbatim).

    Returns {"kept_reasons": [...], "stale_reasons": [...], "blocking_reasons": PresenceList-dict
    or None, "merge_ready": bool or None, "feedback": str} -- a None `blocking_reasons`/`merge_ready`
    means "leave the caller's existing content_dict field alone", exactly mirroring
    verify_exit_readiness's own three-branch structure (force False / flip True / untouched)."""
    gate_reasons = set(problems)
    model_reasons = _presence_values(model_blocking_reasons)
    problem_topics = {t for p in problems if (t := manifest_completeness_topic(p)) is not None}
    kept_reasons: list[str] = []
    stale_reasons: list[str] = []
    for reason in model_reasons:
        topic = manifest_completeness_topic(reason)
        if topic is not None:
            stale = topic not in problem_topics
        else:
            owned = any(marker in reason for marker in GATE_OWNED_REASON_MARKERS)
            stale = owned and reason not in gate_reasons
        (stale_reasons if stale else kept_reasons).append(reason)

    new_blocking_reasons: dict[str, Any] | None = None
    if stale_reasons:
        new_blocking_reasons = _presence_from_values(
            kept_reasons,
            empty_reason="deterministic exit checks passed; all model-supplied blocking reasons "
            "were stale gate reasons carried over from an earlier run and have been cleared",
        )

    result: dict[str, Any] = {"kept_reasons": kept_reasons, "stale_reasons": stale_reasons}
    if problems:
        existing = _presence_values(new_blocking_reasons if new_blocking_reasons is not None else model_blocking_reasons)
        new_blocking_reasons = _presence_from_values(
            existing + [p for p in problems if p not in existing],
            empty_reason="unreachable: problems is non-empty in this branch",
        )
        result["merge_ready"] = False
        result["feedback"] = f"merge_ready forced False: {len(problems)} deterministic blocker(s)"
    elif stale_reasons and not kept_reasons and model_merge_ready is False:
        # Every deterministic check passed AND every blocker the model listed was a stale copy of a
        # gate reason this run did not produce -- the False verdict was inherited, not earned.
        result["merge_ready"] = True
        result["feedback"] = "deterministic exit checks passed; cleared stale carried-over blockers"
    else:
        result["merge_ready"] = None
        result["feedback"] = "deterministic exit checks passed (manifest complete, screenshots present for UI, metrics gate clean)"

    result["blocking_reasons"] = new_blocking_reasons
    return result


# ---------------------------------------------------------------------------------------------
# App-discovery classification -- hand-ported subset of app_discovery.py (see this module's own
# docstring for why, and for the exact, bounded scope this port covers). KEEP IN SYNC BY HAND with
# app_discovery.py's `_app_dir`/`_csproj_signals`/`_package_json_signals`/`classify_candidates` if
# their classification RULES ever change.
# ---------------------------------------------------------------------------------------------


def app_dir(path: str) -> str:
    parent = path.rsplit("/", 1)[0] if "/" in path else "."
    # A .NET project's launchSettings.json lives in <app>/Properties/, not <app>/.
    return parent[: -len("/Properties")] if parent.endswith("/Properties") else parent


def csproj_signals(text: str) -> dict[str, Any] | None:
    # Order mirrors app_discovery.py's own _csproj_signals: Blazor WASM before Sdk.Web (a Blazor
    # WASM project must never fall through to the api/library branches).
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.BlazorWebAssembly"', text):
        return {"likely_class": "web", "runtime": "dotnet", "marker": 'Sdk="Microsoft.NET.Sdk.BlazorWebAssembly"'}
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"', text):
        return {"likely_class": "api", "runtime": "dotnet", "marker": 'Sdk="Microsoft.NET.Sdk.Web"'}
    if re.search(r'FrameworkReference\s+Include\s*=\s*"Microsoft\.AspNetCore\.App"', text):
        return {"likely_class": "api", "runtime": "dotnet", "marker": 'FrameworkReference Include="Microsoft.AspNetCore.App"'}
    if re.search(r"<AzureFunctionsVersion>|Microsoft\.NET\.Sdk\.Functions|Microsoft\.Azure\.Functions\.Worker", text):
        return {"likely_class": "azure_function", "runtime": "dotnet", "marker": "Azure Functions SDK reference"}
    if re.search(r"<OutputType>\s*Exe\s*</OutputType>", text):
        return {"likely_class": "cli", "runtime": "dotnet", "marker": "<OutputType>Exe</OutputType>"}
    if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk"', text):
        return {"likely_class": "library", "runtime": "dotnet", "marker": 'Sdk="Microsoft.NET.Sdk" with no OutputType'}
    return None


_MOBILE_DEPS = ("react-native", "expo", "@capacitor/core", "@ionic/")
_WEB_DEPS = ("next", "express", "fastify", "koa", "@nestjs/core", "vite", "nuxt", "@remix-run/")


def package_json_signals(text: str) -> dict[str, Any] | None:
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
            "likely_class": "web", "runtime": "node",
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
    """Candidate marker files (path -> content) -> candidate app records. Pure and deliberately
    conservative -- reports what a marker proves, nothing more. Bounded subset of
    app_discovery.classify_candidates: no port corroboration (this hook only needs a boolean "is
    there any app", never the cosmetic port detail)."""
    candidates: list[dict[str, Any]] = []

    def add(path: str, signals: dict[str, Any]) -> None:
        candidates.append({"path": app_dir(path), "source": path, **signals})

    for path, text in sorted(files.items()):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".csproj"):
            signals = csproj_signals(text)
            if signals:
                if signals["likely_class"] in ("api", "web") and "start_command" not in signals:
                    signals["start_command"] = f"dotnet run --project {app_dir(path)}"
                add(path, signals)
        elif name == "host.json":
            add(path, {"likely_class": "azure_function", "runtime": "unknown", "marker": "host.json present"})
        elif name == "package.json":
            signals = package_json_signals(text)
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
            fastapi_match = re.search(r"(\w+)\s*=\s*FastAPI\(", text)
            if fastapi_match:
                app_var = fastapi_match.group(1)
                module = name.removesuffix(".py")
                add(path, {
                    "likely_class": "api", "runtime": "python",
                    "marker": f"{name} instantiates FastAPI() as `{app_var}`",
                    "start_command": f"python3 -m uvicorn {module}:{app_var} --host 0.0.0.0 --port $PORT",
                })
            elif re.search(r"Flask\(", text):
                add(path, {"likely_class": "api", "runtime": "python", "marker": f"{name} instantiates Flask()"})
        elif name in ("pyproject.toml", "requirements.txt"):
            if _PY_WEB_RE.search(text):
                add(path, {"likely_class": "api", "runtime": "python", "marker": f"{name} declares a Python web framework"})
        elif name == "Procfile":
            if re.search(r"^web:", text, re.MULTILINE):
                add(path, {"likely_class": "web", "runtime": "unknown", "marker": "Procfile web: process"})
    return candidates


_VALID_APP_CLASSES = frozenset({"web", "api", "azure_function", "mobile", "library", "cli", "unknown"})


def candidates_to_app_dicts(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pre-classification candidates -> DiscoveredApp-shaped dicts, same mapping as
    app_discovery.candidates_to_apps -- built as a plain dict here (not `DiscoveredApp(...)
    .model_dump()`) since this module must stay pydantic-free; the hook only ever checks
    truthiness/length of the result, never round-trips it through DiscoveredApp's own validation."""
    apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates:
        path = str(c.get("path") or ".")
        if path in seen:
            continue
        seen.add(path)
        likely = str(c.get("likely_class") or "unknown")
        apps.append({
            "path": path,
            "name": path.rsplit("/", 1)[-1] if path not in (".", "") else "app",
            "app_class": likely if likely in _VALID_APP_CLASSES else "unknown",
            "runtime": str(c.get("runtime") or "unknown"),
            "start_command": c.get("start_command"),
            "port": c.get("port"),
            "evidence": [f"{c.get('source', '?')}: {c.get('marker', '?')}"],
            "confidence": "medium",
        })
    return apps


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.exit_readiness_checks`."""
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "exit_readiness_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            "sandbox-image/hooks/exit_readiness_checks.py has drifted from "
            "src/gates/exit_readiness_checks.py -- re-sync with: cp "
            "src/gates/exit_readiness_checks.py sandbox-image/hooks/exit_readiness_checks.py"
        )

    # _presence_values / _presence_from_values.
    assert _presence_values({"status": "present", "values": ["x"], "reason": ""}) == ["x"]
    assert _presence_values(["legacy", "bare", "list"]) == ["legacy", "bare", "list"]
    assert _presence_values(None) == []
    assert _presence_from_values(["x"], empty_reason="unused") == {"status": "present", "values": ["x"], "reason": ""}
    assert _presence_from_values([], empty_reason="nothing to report") == {
        "status": "absent", "values": [], "reason": "nothing to report",
    }

    # manifest_completeness_topic.
    assert manifest_completeness_topic(
        "manifest.json (.ai-dev-workflow/manifest.json) still got no test_command. Re-read file "
        "direct this turn: only toolchain + app_check keys exist."
    ) == "test_command"
    assert manifest_completeness_topic(
        "manifest.json still no coverage_commands. Re-checked this turn: replay contract for "
        "coverage sit only in sibling file .ai-dev-workflow/coverage-commands.json."
    ) == "coverage_command"
    assert manifest_completeness_topic("manifest.json has no test_command for this stack") == "test_command"
    assert manifest_completeness_topic("manifest.json has no coverage_commands -- coverage is not replayable") == "coverage_command"
    assert manifest_completeness_topic("manifest.json records no runnable app (app_check.apps is empty even after re-scan)") == "runnable app"
    assert manifest_completeness_topic("the test_command in package.json points at a script that no longer exists") is None
    assert manifest_completeness_topic("manifest.json's auth_kind field looks wrong for this repo") is None

    # combined_test_command_from_apps.
    per_app_commands = [
        {"path": "apps/api", "runtime": "python", "test_command": "python3 -m pytest"},
        {"path": "apps/web", "runtime": "node", "test_command": "npm test"},
    ]
    assert combined_test_command_from_apps(per_app_commands) == "cd apps/api && python3 -m pytest && cd apps/web && npm test"
    partial = [{"path": "apps/api", "runtime": "python"}, {"path": "apps/web", "runtime": "node", "test_command": "npm test"}]
    assert combined_test_command_from_apps(partial) == "cd apps/api && python3 -m pytest && cd apps/web && npm test"
    assert combined_test_command_from_apps([]) is None
    assert combined_test_command_from_apps([{"path": "apps/worker", "runtime": "rust"}]) is None

    # resolve_manifest_updates / apply_manifest_updates.
    empty_manifest = {"app_check": {"suitable": True, "apps": []}}
    updates = resolve_manifest_updates(
        empty_manifest, resolved_apps=[{"path": "."}], scan_fingerprint="sha256:abc",
        resolved_test_command="npm test", coverage_entries=[{"command": "x"}],
    )
    assert updates == {
        "app_check": {"apps": [{"path": "."}], "evidence_fingerprint": "sha256:abc"},
        "test_command": "npm test", "coverage_commands": [{"command": "x"}],
    }
    already_complete = {"app_check": {"apps": [{"path": "."}]}, "test_command": "x", "coverage_commands": [{"command": "y"}]}
    assert resolve_manifest_updates(
        already_complete, resolved_apps=[{"path": "ignored"}], scan_fingerprint="ignored",
        resolved_test_command="ignored", coverage_entries=[{"command": "ignored"}],
    ) == {}, "already-complete fields must never be overwritten"
    merged = apply_manifest_updates(
        {"app_check": {"suitable": True, "apps": []}, "onboarded": True}, updates,
    )
    assert merged["app_check"] == {"suitable": True, "apps": [{"path": "."}], "evidence_fingerprint": "sha256:abc"}, (
        "app_check deep-merge must preserve 'suitable' from the pre-update manifest"
    )
    assert merged["onboarded"] is True, "keys outside `updates` must survive untouched"
    assert merged["test_command"] == "npm test" and merged["coverage_commands"] == [{"command": "x"}]

    # manifest_presence_problems.
    assert manifest_presence_problems({}) == [
        "manifest.json records no runnable app (app_check.apps is empty even after re-scan)",
        "manifest.json has no test_command for this stack",
        "manifest.json has no coverage_commands -- coverage is not replayable",
    ]
    assert manifest_presence_problems({"app_check": {"suitable": False}, "test_command": "x", "coverage_commands": [{}]}) == [], (
        "suitable=False (repo explicitly marked unsuitable) must not demand a runnable app"
    )
    assert manifest_presence_problems(
        {"app_check": {"apps": [{"path": "."}]}, "test_command": "x", "coverage_commands": [{"command": "y"}]}
    ) == []
    # test_command_resolvable (Task 14 review fix): suppresses ONLY the test_command problem, even
    # with app_check/coverage_commands still genuinely missing.
    suppressed = manifest_presence_problems({}, test_command_resolvable=True)
    assert "manifest.json has no test_command for this stack" not in suppressed
    assert "manifest.json records no runnable app (app_check.apps is empty even after re-scan)" in suppressed
    assert "manifest.json has no coverage_commands -- coverage is not replayable" in suppressed
    # An already-present test_command is untouched by the flag either way (nothing to suppress).
    assert manifest_presence_problems({"test_command": "x"}, test_command_resolvable=True) == manifest_presence_problems(
        {"test_command": "x"}, test_command_resolvable=False
    )

    # tech_stack_resolves_test_command (Task 14 review fix -- the false-block-on-every-dotnet-repo
    # bug the task-14 review caught: resolve_test_command's FIRST, unconditional branch is dotnet,
    # which the hook's own combined_test_command_from_apps fallback never covers).
    assert tech_stack_resolves_test_command({"dotnet": {"status": "detected", "solution_root": "src"}}) is True
    assert tech_stack_resolves_test_command({"dotnet": {"status": "not_detected"}}) is False
    assert tech_stack_resolves_test_command({"dotnet_detected": True}) is True, "legacy pre-consolidation shape"
    assert tech_stack_resolves_test_command(
        {"languages": {"status": "present", "values": ["TypeScript"], "reason": ""}}
    ) is True
    assert tech_stack_resolves_test_command({"languages": ["Python"]}) is True, "legacy bare-list shape"
    assert tech_stack_resolves_test_command({"languages": {"status": "present", "values": ["Rust"], "reason": ""}}) is False
    assert tech_stack_resolves_test_command({}) is False
    assert tech_stack_resolves_test_command(None) is False

    # screenshot_problems.
    assert screenshot_problems(True, 0) == ["UI application but no e2e screenshots were captured"]
    assert screenshot_problems(True, 3) == []
    assert screenshot_problems(False, 0) == []

    # metrics_problems.
    assert metrics_problems({}, "r1") == (["metrics were not recorded for this run -- the regression gate never passed"], False)
    assert metrics_problems({"run_id": "r0"}, "r1")[1] is False
    clean_metrics = {"run_id": "r1", "regression_gate": {"reasons": []}, "readme": {"owned": True, "problems": []}}
    assert metrics_problems(clean_metrics, "r1") == ([], True)
    dirty_metrics = {
        "run_id": "r1", "regression_gate": {"reasons": ["coverage below threshold"]},
        "readme": {"owned": True, "problems": ["README.md is missing or empty"]},
    }
    problems, matched = metrics_problems(dirty_metrics, "r1")
    assert matched and problems == ["coverage below threshold", "README.md is missing or empty"]
    # readme not owned (human-authored brownfield README) -- advisory only, never folded in.
    unowned = {"run_id": "r1", "regression_gate": {"reasons": []}, "readme": {"owned": False, "problems": ["has no H1 title"]}}
    assert metrics_problems(unowned, "r1") == ([], True)

    # auth_problems.
    assert auth_problems({}, True) == ([], None), "auth not required -- nothing to report"
    required_unverified = {"app_auth": {"auth_mode": "required", "secrets_present": True}, "e2e": {"status": "passed"}}
    problems, note = auth_problems(required_unverified, True)
    assert note is None and "was not verified" in problems[0] and "never ran" in problems[0]
    required_verified = {
        "app_auth": {"auth_mode": "required", "secrets_present": True},
        "e2e": {"status": "passed", "auth_check": {"passed": True, "feedback": "all protected routes 401'd"}},
    }
    assert auth_problems(required_verified, True) == ([], "Authentication enforcement verified: all protected routes 401'd")
    assert auth_problems(required_unverified, False) == ([], None), "AIDW_AUTH_GATE=0 kill-switch disables the whole chain"

    # targeted_fix_unresolved_problems.
    assert targeted_fix_unresolved_problems({"run_id": "r1", "reasons": ["still open"]}, "r1") == ["still open"]
    assert targeted_fix_unresolved_problems({"run_id": "r1", "reasons": ["still open"]}, "r2") == [], (
        "a different run's own targeted-fix reasons are never carried into THIS run's verdict"
    )
    assert targeted_fix_unresolved_problems({"run_id": "r1", "reasons": []}, "r1") == []
    assert targeted_fix_unresolved_problems({}, "r1") == [], "no file/empty payload -- nothing to fold in"

    # evaluate_merge_readiness: real problems force merge_ready False and append (dedup) reasons.
    r = evaluate_merge_readiness(["no test_command"], {"status": "absent", "values": [], "reason": "clean"}, True)
    assert r["merge_ready"] is False and r["blocking_reasons"]["values"] == ["no test_command"]
    assert r["stale_reasons"] == [] and "1 deterministic blocker" in r["feedback"]

    # Stale gate-owned reason, no real problems this run, model said merge_ready=False -> flips True.
    stale_model_reasons = {"status": "present", "values": ["coverage below threshold"], "reason": ""}
    r = evaluate_merge_readiness([], stale_model_reasons, False)
    assert r["merge_ready"] is True and r["stale_reasons"] == ["coverage below threshold"]
    assert r["blocking_reasons"]["status"] == "absent"

    # Stale reason present but model already said merge_ready=True (nothing to flip) -> untouched.
    r = evaluate_merge_readiness([], stale_model_reasons, True)
    assert r["merge_ready"] is None and r["blocking_reasons"]["status"] == "absent"

    # A prose blocker the model reasoned out for itself (not gate-owned vocabulary) is never
    # dropped, even with zero deterministic problems this run.
    own_reasoning = {"status": "present", "values": ["the payment provider's sandbox key is a placeholder"], "reason": ""}
    r = evaluate_merge_readiness([], own_reasoning, False)
    assert r["stale_reasons"] == [] and r["kept_reasons"] == ["the payment provider's sandbox key is a placeholder"]
    assert r["merge_ready"] is None, "a genuine (non-stale) model blocker must not be silently flipped to True"

    # Manifest-completeness topic drop: model's own paraphrase of a topic THIS run's problems no
    # longer raise is stale (topic-based, not exact-substring).
    topic_reason = {"status": "present", "values": ["manifest.json still has no test_command for this repo"], "reason": ""}
    r = evaluate_merge_readiness([], topic_reason, False)
    assert r["stale_reasons"] == topic_reason["values"] and r["merge_ready"] is True

    # No problems, no reasons at all -- nothing forced either way.
    r = evaluate_merge_readiness([], {"status": "absent", "values": [], "reason": "clean"}, True)
    assert r["merge_ready"] is None and r["blocking_reasons"] is None and "manifest complete" in r["feedback"]

    # --- app-discovery classification port ---
    fastapi_main = "from fastapi import FastAPI\napi = FastAPI()\n"
    candidates = classify_candidates({"apps/api/main.py": fastapi_main})
    assert len(candidates) == 1 and candidates[0]["likely_class"] == "api" and candidates[0]["path"] == "apps/api"
    apps = candidates_to_app_dicts(candidates)
    assert len(apps) == 1 and apps[0]["app_class"] == "api" and apps[0]["runtime"] == "python"
    assert "python3 -m uvicorn main:api" in apps[0]["start_command"]

    web_pkg = json.dumps({"dependencies": {"next": "14.0.0"}, "scripts": {"dev": "next dev"}})
    candidates = classify_candidates({"apps/web/package.json": web_pkg})
    assert candidates[0]["likely_class"] == "web" and candidates[0]["start_command"] == "npm run dev"

    library_pkg = json.dumps({"main": "index.js"})
    assert classify_candidates({"packages/lib/package.json": library_pkg})[0]["likely_class"] == "library"

    csproj_web = '<Project Sdk="Microsoft.NET.Sdk.Web">'
    candidates = classify_candidates({"src/Api/Api.csproj": csproj_web})
    assert candidates[0]["likely_class"] == "api" and candidates[0]["start_command"] == "dotnet run --project src/Api"

    # No candidate marker files at all -- correctly empty, never fabricated.
    assert classify_candidates({}) == []
    assert classify_candidates({"README.md": "# hello"}) == []

    # candidates_to_app_dicts de-dupes by path (first candidate wins).
    two_for_one_dir = [
        {"path": "apps/api", "source": "apps/api/main.py", "likely_class": "api", "runtime": "python", "marker": "m1"},
        {"path": "apps/api", "source": "apps/api/pyproject.toml", "likely_class": "api", "runtime": "python", "marker": "m2"},
    ]
    assert len(candidates_to_app_dicts(two_for_one_dir)) == 1

    # --- _run_check_hook integration cases (Task 14 review fix regression coverage) ---

    # A dotnet-stack repo, first metrics-exit turn: manifest has no test_command (nothing else in
    # this codebase writes it before this completion step), app_check.apps not yet populated, and
    # no per-app fallback available either -- exactly the shape that, before this fix, produced a
    # deterministic false "no test_command" block on EVERY dotnet repo. tech_stack_resolves_test_command's
    # dotnet branch must suppress it.
    dotnet_payload = {
        "manifest": {"app_check": {"suitable": True, "apps": [{"path": "."}]}, "coverage_commands": [{"command": "x"}]},
        "scanned_files": {}, "coverage_entries": None, "is_ui": False, "screenshot_count": 0,
        "metrics": {}, "targeted_fix": {}, "run_id": "",
        "tech_stack": {"dotnet": {"status": "detected", "solution_root": ""}},
        "auth_gate_enabled": True,
        "report": {"merge_ready": True, "blocking_reasons": {"status": "absent", "values": [], "reason": "clean"}},
    }
    result = _run_check_hook(dotnet_payload)
    assert result["passed"] is True, result  # would have been False (test_command problem) before this fix
    assert not any("test_command" in r for r in result["reasons"]), result

    # Same shape but tech-stack detects NEITHER dotnet nor node/python/typescript (e.g. a bare-Go
    # repo this pipeline doesn't resolve a command for either) -- the problem correctly still fires,
    # proving the suppression is stack-specific, not a blanket skip of the check.
    unresolvable_payload = {**dotnet_payload, "tech_stack": {"languages": {"status": "present", "values": ["Go"], "reason": ""}}}
    result = _run_check_hook(unresolvable_payload)
    assert result["passed"] is False
    assert any("test_command" in r for r in result["reasons"]), result

    # Missing AIDW_RUN_ID must never produce the "flip to merge_ready=True" nudge (Task 14 review
    # fix, minor #1): the model's own stale-looking gate-owned reason is UNVERIFIED here (the
    # run_id-gated half never ran), not confirmed clean -- telling it to flip to True would be a
    # false all-clear this hook has no basis for.
    no_run_id_payload = {
        "manifest": {"app_check": {"suitable": True, "apps": [{"path": "."}]}, "test_command": "x", "coverage_commands": [{"command": "y"}]},
        "scanned_files": {}, "coverage_entries": None, "is_ui": False, "screenshot_count": 0,
        "metrics": {}, "targeted_fix": {}, "run_id": "", "tech_stack": {}, "auth_gate_enabled": True,
        "report": {
            "merge_ready": False,
            "blocking_reasons": {"status": "present", "values": ["coverage below threshold"], "reason": ""},
        },
    }
    result = _run_check_hook(no_run_id_payload)
    assert result["passed"] is True, result  # nothing the manifest-only half found -- no exit 2
    assert result["reasons"] == [], (
        "with no run_id, this hook must stay silent about the model's stale-looking reason instead "
        "of confidently telling it to flip merge_ready to True -- it never actually checked", result,
    )
    # Sanity: the SAME stale reason, WITH a run_id, DOES get the flip-to-True nudge (already covered
    # end-to-end in the fixture-repo test run; this is the unit-level confirmation of the guard's
    # other side).
    with_run_id_payload = {**no_run_id_payload, "run_id": "r1", "metrics": {"run_id": "r1", "regression_gate": {"reasons": []}, "readme": {"owned": True, "problems": []}}}
    result = _run_check_hook(with_run_id_payload)
    assert result["passed"] is False and "stale" in result["reasons"][0], result

    print("exit_readiness_checks self-check: all assertions passed")


def _run_check_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """The Stop hook's own entry point: a fully-assembled JSON payload (already-read repo files,
    the model's own draft report) on stdin, a verdict on stdout. No file/sandbox I/O of its own --
    everything it needs, the hook already read and handed over as data (same posture as
    adversarial_audit_checks.py's `--check-hook` branch)."""
    manifest = payload.get("manifest") or {}
    scanned_files = payload.get("scanned_files") or {}
    run_id = str(payload.get("run_id") or "")
    auth_gate_enabled = bool(payload.get("auth_gate_enabled"))
    coverage_entries = payload.get("coverage_entries")
    is_ui = bool(payload.get("is_ui"))
    screenshot_count = int(payload.get("screenshot_count") or 0)
    metrics = payload.get("metrics") or {}
    targeted_fix = payload.get("targeted_fix") or {}
    tech_stack = payload.get("tech_stack") or {}
    report = payload.get("report") or {}

    resolved_apps = None
    scan_fingerprint = None
    app_check = manifest.get("app_check") or {}
    if not (app_check.get("apps") or []) and scanned_files:
        candidates = classify_candidates(scanned_files)
        resolved_apps = candidates_to_app_dicts(candidates) or None
    resolved_test_command = None
    if not manifest.get("test_command"):
        resolved_test_command = combined_test_command_from_apps(app_check.get("apps") or [])
    updates = resolve_manifest_updates(
        manifest, resolved_apps=resolved_apps, scan_fingerprint=scan_fingerprint,
        resolved_test_command=resolved_test_command, coverage_entries=coverage_entries,
    )
    completed_manifest = apply_manifest_updates(manifest, updates) if updates else manifest

    problems = manifest_presence_problems(
        completed_manifest, test_command_resolvable=tech_stack_resolves_test_command(tech_stack)
    )

    if run_id:
        # Screenshots are run_id-KEYED (history/<run_id>-screens/) -- without a real run_id the
        # hook cannot know where to even look, so `screenshot_count` is meaningless noise, not a
        # genuine zero. Gating this here (not just in the JS caller, which already skips the
        # readdir without a run_id) means a caller that ever passes screenshot_count=0 alongside an
        # empty run_id by accident still can't manufacture a false "no screenshots" block.
        problems += screenshot_problems(is_ui, screenshot_count)
        metrics_probs, metrics_matched = metrics_problems(metrics, run_id)
        problems += metrics_probs
        if metrics_matched:
            auth_probs, _auth_note = auth_problems(metrics, auth_gate_enabled)
            problems += auth_probs
        problems += targeted_fix_unresolved_problems(targeted_fix, run_id)

    verdict = evaluate_merge_readiness(problems, report.get("blocking_reasons"), report.get("merge_ready"))

    reasons = list(problems)
    # Same-turn value of the stale-reason filter: nothing NEW to fix, but the model's own draft
    # claims a blocker this run's deterministic checks disprove -- worth a nudge to revise the
    # report text itself, even though the real gate will silently correct `merge_ready` regardless.
    #
    # Gated on `run_id` too (Task 14 review fix), not just `not problems`: without a real run_id,
    # the ENTIRE run_id-scoped half above (screenshots/metrics/regression-gate/README/auth/
    # targeted-fix) never ran at all -- `problems` is empty here because this hook COULDN'T check,
    # not because it verified anything is clean. Telling the model "these reasons are definitely
    # stale, flip to merge_ready=True" on that basis would be a confident, false all-clear -- worse
    # than the same-turn hook simply staying silent (which is what happens now: `reasons` still
    # only carries whatever `problems` found, i.e. nothing, from the manifest-only half that DID
    # run). The real gate is unaffected either way: it always has a real run_id and always runs
    # every check for real before its own `evaluate_merge_readiness` call.
    if run_id and not problems and verdict["stale_reasons"] and verdict["merge_ready"] is True:
        stale_list = ", ".join(verdict["stale_reasons"])
        reasons.append(
            "Your draft's blocking_reasons/merge_ready=False is stale: every reason you listed "
            f"({stale_list}) is carried-over text from an earlier run's report and none of this "
            "run's deterministic checks raised it. Revise your report to merge_ready=True with a "
            "clean blocking_reasons before finishing."
        )

    return {"passed": len(reasons) == 0, "reasons": reasons}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        _payload = json.loads(sys.stdin.read())
        json.dump(_run_check_hook(_payload), sys.stdout)
    else:
        _demo()
