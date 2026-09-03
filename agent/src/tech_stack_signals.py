"""Shared UI-framework detection signal.

Used by graph.py (P3's wireframe requirement, P4's Playwright MCP, session_options gates) AND
e2e_nodes.py (e2e_gate_check_node). Pulled out into its own leaf module -- rather than duplicated,
or imported one way -- because graph.py imports e2e_nodes to wire it in, so e2e_nodes importing
graph.py back would be circular; a shared leaf with no dependency on either avoids the cycle.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from . import repo_files
from .schemas import TechStack

logger = logging.getLogger(__name__)

UI_FRAMEWORK_MARKERS = ("react", "vue", "angular", "blazor", "svelte", "next", "nuxt", "flutter", "swiftui", "jetpack compose")


def frameworks_have_ui(frameworks: list[Any]) -> bool:
    """The pure check behind tech_stack_has_ui_framework, for callers that hold a frameworks list
    but no graph state (exit's deterministic verify reads tech-stack.approved.json off disk)."""
    lowered = [str(f).lower() for f in frameworks or []]
    return any(marker in fw for fw in lowered for marker in UI_FRAMEWORK_MARKERS)


def load_tech_stack(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalizes a tech-stack dict to TechStack's CURRENT shape before anything below reads it --
    whether `raw` is already that shape, a genuinely LEGACY on-disk shape (bare-list PresenceList
    fields, the old separate dotnet_detected/dotnet_solution_root keys, dict-shaped
    convention_roots -- what every already-onboarded repo's tech-stack.approved.json/draft.json
    actually contains until the migration ships), or missing/malformed.

    Routes through TechStack's own model_validator(mode="before") coercion chain (schemas.py,
    Tasks 1-2) instead of re-implementing that legacy-shape knowledge here a second time -- same
    "validate through the schema, don't hand-roll it" pattern preflight_nodes._extract_cache_get
    already uses. Never raises: a validation failure (including a bare `{}`, since every field
    below is required) falls back to `{}`, which every reader in this module already treats as
    "nothing to report" -- the fail-open convention every call site here already relies on for a
    missing file.
    """
    try:
        return TechStack.model_validate(raw or {}).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 -- malformed/legacy-but-uncoercible input is "nothing to read"
        if raw:
            # `raw` being non-empty means there WAS something to read -- unlike a missing file
            # (raw=None) or a genuinely blank sidecar (raw={}), this is a real tech-stack dict
            # that failed whole-model validation for a reason _coerce_legacy_shape didn't
            # anticipate. Silently returning {} here previously made every downstream reader
            # (frameworks_have_ui, dotnet_detected, both coverage gates, wireframe/Playwright
            # provisioning) see "nothing detected at all" with zero trace of why -- log it loudly
            # so an uncoercible shape this fix didn't foresee is at least diagnosable.
            logger.warning(
                "load_tech_stack: tech-stack dict failed validation, falling back to {} "
                "(downstream readers will see NOTHING detected for this repo) -- keys=%s error=%s",
                sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
                exc,
            )
        return {}


def presence_values(tech_stack: dict[str, Any], field: str) -> list[str]:
    """The `values` list of a PresenceList-shaped TechStack field (languages/frameworks/
    package_managers/testing_frameworks/conventions/config_inventory), tolerating a missing field,
    a bare dict, or a genuinely legacy bare-list value (normalized by load_tech_stack first) the
    same way the old `tech_stack.get(field) or []` shape used to tolerate a missing key. Empty both
    when the field is absent from `tech_stack` entirely AND when it's present with status="absent"
    -- callers that need to tell "not checked" from "checked, found none" apart should read
    `tech_stack[field]["status"]` themselves instead (on the normalized dict, not the raw one)."""
    return (load_tech_stack(tech_stack).get(field) or {}).get("values") or []


def dotnet_detected(tech_stack: dict[str, Any]) -> bool:
    """True when TechStack.dotnet reports status="detected" -- the bool the old top-level
    `dotnet_detected` field used to carry directly, before it was folded into the `dotnet` object.
    Normalizes legacy shape first (load_tech_stack), so this is also correct for a repo whose
    on-disk sidecar still has the old dotnet_detected/dotnet_solution_root pair instead of `dotnet`."""
    return (load_tech_stack(tech_stack).get("dotnet") or {}).get("status") == "detected"


def dotnet_solution_root(tech_stack: dict[str, Any]) -> str | None:
    """TechStack.dotnet.solution_root, normalizing legacy shape first (same as dotnet_detected).
    None means either not-detected or detected-but-low-confidence -- the "skip rather than guess"
    contract every existing caller already relies on."""
    return (load_tech_stack(tech_stack).get("dotnet") or {}).get("solution_root")


def tech_stack_has_ui_framework(state: dict[str, Any]) -> bool:
    """Shared signal for P3's wireframe requirement, P4's Playwright MCP, and e2e's gate check --
    all three key off whether this repo has a UI framework at all, using tech-stack's own
    TechStack.frameworks report (a real, deliberate simplification from "only for UI-relevant
    content specifically" to "only for UI-framework repos at all")."""
    tech_stack = (state.get("stages") or {}).get("tech-stack", {}).get("approved_content") or {}
    return frameworks_have_ui(presence_values(tech_stack, "frameworks"))


def is_greenfield_repo(state: dict[str, Any]) -> bool:
    """No candidate the deterministic scan found -- a genuinely blank (or non-startable-only)
    repository. Replaces the removed interrupt-driven state["greenfield"] dict; pure function of
    the scan already taken at app_discovery_pre_node, no separate state channel needed."""
    return not (state.get("app_scan") or {}).get("candidates")


def convention_root(tech_stack: dict[str, Any], ecosystem: str) -> str | None:
    """The repo-relative root recorded for a non-.NET ecosystem's `convention_roots` entry, or
    None when that ecosystem has no entry at all or is recorded status="absent". "" is a
    legitimate present-at-repo-root value, distinct from None ("nothing to join, fall back to
    whatever the caller treats as its own default"). Normalizes legacy shape first (load_tech_stack)
    -- a genuinely legacy sidecar has convention_roots as a bare dict[str, str], not this list."""
    for entry in load_tech_stack(tech_stack).get("convention_roots") or []:
        if entry.get("ecosystem") == ecosystem:
            return entry.get("root") if entry.get("status") == "present" else None
    return None


def dotnet_root_prefix(tech_stack: dict[str, Any]) -> str:
    """Shell prefix that `cd`s into TechStack.dotnet.solution_root before a bare `dotnet` command,
    for every quality/test/finding-cluster/gate node that shells out to dotnet build/format/test.
    Brownfield repos happened to have their .sln/.csproj at the repo root; a greenfield monorepo
    (solution under e.g. "apps/") doesn't, so a bare `dotnet build` at repo root dies with MSB1003
    in ~2s (observed live) before any real work happens. Empty/missing root -- no confidently
    determined subdirectory, including the common repo-root case -- returns "" so every existing
    call site is byte-identical to before this existed.

    Validation reuses repo_files.validate_repo_relative_path's own distrust: solution_root is
    model-reported (TechStack.dotnet's own field) and must not be trusted to shell out unchecked,
    same as any other repo-relative path from that source.
    """
    return _cd_prefix(dotnet_solution_root(tech_stack), "dotnet.solution_root")


def ecosystem_root_prefix(tech_stack: dict[str, Any], ecosystem: str) -> str:
    """dotnet_root_prefix's twin for the non-.NET ecosystems: `cd`s into
    TechStack.convention_roots[ecosystem].root (e.g. node=apps/web) before a bare npm/tsc/ruff
    command. Same rationale and same validation -- a greenfield monorepo has no root package.json,
    so a bare `npx tsc --noEmit` at repo root prints tsc's help text and fails the rebuild gate by
    construction (observed live, headless sc1). Empty/missing/unsafe root returns ""."""
    return _cd_prefix(convention_root(tech_stack, ecosystem), f"convention_roots[{ecosystem}]")


def _cd_prefix(root: Any, field: str) -> str:
    root = root or ""
    if not root:
        return ""
    try:
        repo_files.validate_repo_relative_path(root)
    except ValueError:
        logger.warning("ignoring unsafe %s %r", field, root)
        return ""
    return f"cd {shlex.quote(str(root).strip('/'))} && "


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.tech_stack_signals`."""
    assert is_greenfield_repo({}) is True
    assert is_greenfield_repo({"app_scan": {}}) is True
    assert is_greenfield_repo({"app_scan": {"candidates": []}}) is True
    assert is_greenfield_repo({"app_scan": {"candidates": [{"path": "."}]}}) is False

    def _dotnet(solution_root: str | None) -> dict[str, Any]:
        return {"status": "detected", "solution_root": solution_root, "reason": "" if solution_root else "test fixture"}

    def _roots(**by_ecosystem: str) -> list[dict[str, Any]]:
        return [{"ecosystem": eco, "status": "present", "root": root, "reason": ""} for eco, root in by_ecosystem.items()]

    def _full(**overrides: Any) -> dict[str, Any]:
        # A full, valid new-shape TechStack dict -- every field below is REQUIRED (Task 2 dropped
        # their defaults), and load_tech_stack now validates the WHOLE object before any read, so
        # a partial dict (e.g. just {"dotnet": ...}) fails validation and falls back to {} instead
        # of exercising the field under test. Every real caller already hands these functions a
        # full TechStack dump (a sidecar file's whole contents, or approved_content), never a
        # hand-picked subset -- this fixture matches that, overriding only what each test cares about.
        base: dict[str, Any] = {
            "summary": "s",
            "languages": {"status": "absent", "reason": "test fixture"},
            "frameworks": {"status": "absent", "reason": "test fixture"},
            "package_managers": {"status": "absent", "reason": "test fixture"},
            "testing_frameworks": {"status": "absent", "reason": "test fixture"},
            "conventions": {"status": "absent", "reason": "test fixture"},
            "dotnet": {"status": "not_detected", "reason": "test fixture"},
            "convention_roots": [],
            "conventions_applied": [],
            "auth_kind": "none",
            "config_inventory": {"status": "absent", "reason": "test fixture"},
        }
        return {**base, **overrides}

    assert dotnet_root_prefix(_full(dotnet=_dotnet("apps"))) == "cd apps && "
    assert dotnet_root_prefix(_full(dotnet=_dotnet("apps/backend"))) == "cd apps/backend && "
    assert dotnet_root_prefix(_full(dotnet=_dotnet(""))) == ""
    assert dotnet_root_prefix({}) == ""
    assert dotnet_root_prefix(_full(dotnet=_dotnet(None))) == ""
    assert dotnet_root_prefix(_full(dotnet=_dotnet("../evil"))) == ""
    assert dotnet_root_prefix(_full(dotnet=_dotnet("/abs"))) == ""
    assert ecosystem_root_prefix(_full(convention_roots=_roots(node="apps/web")), "node") == "cd apps/web && "
    assert ecosystem_root_prefix(_full(convention_roots=_roots(node="apps/web")), "python") == ""
    assert ecosystem_root_prefix(_full(convention_roots=_roots(python="../evil")), "python") == ""
    assert ecosystem_root_prefix({}, "node") == ""

    # convention_root: status="absent" and "no entry at all" both mean None, not "".
    absent_roots = [{"ecosystem": "node", "status": "absent", "root": "", "reason": "no package.json"}]
    assert convention_root(_full(convention_roots=absent_roots), "node") is None
    assert convention_root(_full(convention_roots=absent_roots), "python") is None
    assert convention_root(_full(convention_roots=_roots(node="")), "node") == ""

    # presence_values/dotnet_detected: the two other shape-reads every consumer shares.
    assert presence_values(_full(frameworks={"status": "present", "values": ["Express"]}), "frameworks") == ["Express"]
    assert presence_values(_full(frameworks={"status": "absent", "values": [], "reason": "none found"}), "frameworks") == []
    assert presence_values({}, "frameworks") == []
    assert dotnet_detected(_full(dotnet={"status": "detected", "solution_root": "src"})) is True
    assert dotnet_detected(_full(dotnet={"status": "not_detected", "reason": "no .csproj"})) is False
    assert dotnet_detected({}) is False

    # --- Genuinely LEGACY on-disk shape: every already-onboarded repo's tech-stack.approved.json
    # looks like this today (bare-list PresenceList fields, the old dotnet_detected/
    # dotnet_solution_root pair, dict-shaped convention_roots) until the migration ships. Every
    # reader above must normalize it via load_tech_stack (TechStack's own before-validator coercion
    # chain), not crash (AttributeError: 'list'/'str' object has no attribute 'get') or silently
    # under-report (dotnet_detected() returning False for a repo that IS dotnet).
    legacy = {
        "summary": "legacy sidecar",
        "languages": ["Python", "TypeScript"],
        "frameworks": ["Express"],
        "package_managers": ["npm"],
        "testing_frameworks": [],
        "conventions": [],
        "dotnet_detected": True,
        "dotnet_solution_root": "src/Api",
        "convention_roots": {"node": "apps/web", "python": "apps/api"},
        "conventions_applied": [],
        "auth_kind": "none",
        "config_inventory": ["DATABASE_URL"],
    }
    assert presence_values(legacy, "languages") == ["Python", "TypeScript"]
    assert presence_values(legacy, "frameworks") == ["Express"]
    assert presence_values(legacy, "testing_frameworks") == [], "legacy empty list -> absent -> []"
    assert presence_values(legacy, "config_inventory") == ["DATABASE_URL"]
    assert dotnet_detected(legacy) is True, "legacy dotnet_detected=True must not silently read as False"
    assert dotnet_solution_root(legacy) == "src/Api"
    assert dotnet_root_prefix(legacy) == "cd src/Api && "
    assert convention_root(legacy, "node") == "apps/web"
    assert convention_root(legacy, "python") == "apps/api"
    assert ecosystem_root_prefix(legacy, "node") == "cd apps/web && "

    # A legacy repo with dotnet NOT detected -- the other half of the old bool/pair.
    legacy_no_dotnet = {**legacy, "dotnet_detected": False, "dotnet_solution_root": None, "convention_roots": {}}
    assert dotnet_detected(legacy_no_dotnet) is False
    assert dotnet_solution_root(legacy_no_dotnet) is None
    assert dotnet_root_prefix(legacy_no_dotnet) == ""
    assert convention_root(legacy_no_dotnet, "node") is None

    # Important 2 (final review fix wave): an off-enum legacy auth_kind must no longer fail-open
    # the WHOLE tech stack to {} -- schemas.py's TechStack._coerce_legacy_shape now bridges it to
    # "none" before whole-model validation runs, so every unrelated field (frameworks, dotnet, ...)
    # still reads correctly instead of every downstream reader seeing "nothing detected at all"
    # over one bad field.
    legacy_bad_auth_kind = {**legacy, "auth_kind": "azure-ad"}
    coerced = load_tech_stack(legacy_bad_auth_kind)
    assert coerced, "an off-enum legacy auth_kind must no longer fail-open the whole tech stack"
    assert coerced["auth_kind"] == "none"
    assert presence_values(legacy_bad_auth_kind, "frameworks") == ["Express"], (
        "an unrelated field must still read correctly once auth_kind coerces instead of failing"
    )

    # Malformed/uncoercible input must fail open (empty), never raise.
    assert load_tech_stack(None) == {}
    assert load_tech_stack({"not": "a tech stack at all"}) == {}
    assert presence_values({"not": "a tech stack at all"}, "languages") == []
    assert dotnet_detected({"not": "a tech stack at all"}) is False

    # ...and now LOUD about it: a genuinely non-empty, uncoercible shape must log a warning naming
    # the failure (the whole point of this fix -- silence is what let the regression go unnoticed),
    # while a missing/blank input (nothing to report to begin with) logs nothing.
    _warn_calls: list[tuple[Any, ...]] = []
    _orig_warning = logger.warning
    logger.warning = lambda *a, **k: _warn_calls.append(a)  # type: ignore[method-assign]
    try:
        assert load_tech_stack({"not": "a tech stack at all"}) == {}
    finally:
        logger.warning = _orig_warning  # type: ignore[method-assign]
    assert _warn_calls, "a genuinely uncoercible non-empty input must log a warning, not fail silently"

    _warn_calls_blank: list[tuple[Any, ...]] = []
    logger.warning = lambda *a, **k: _warn_calls_blank.append(a)  # type: ignore[method-assign]
    try:
        assert load_tech_stack(None) == {}
        assert load_tech_stack({}) == {}
    finally:
        logger.warning = _orig_warning  # type: ignore[method-assign]
    assert not _warn_calls_blank, "a missing/blank tech stack is 'nothing to report' -- must not warn"
    assert convention_root({"not": "a tech stack at all"}, "node") is None

    print("tech_stack_signals self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.tech_stack_signals`
    _demo()
