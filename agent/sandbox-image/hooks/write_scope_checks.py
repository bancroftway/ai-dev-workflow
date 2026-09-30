"""Pure, dependency-free path classifiers extracted from `write_scope_gate.py` (Task 13) --
specifically so the sandbox's own same-turn Stop hook
(sandbox-image/hooks/check-ac-residue-stop.mjs) can run the REAL write-scope/test-pyramid/
e2e-presence checks by shelling out to `python3` on this ONE file, instead of a hand-ported
JavaScript reimplementation drifting from it. Same reasoning as `ac_residue_checks.py`'s own
extraction (see that module's docstring) and `test_quality_checks.py`'s before it.

Everything in this module is a straight, unchanged code-move: `_is_test_path`, `_is_pipeline_owned`,
`is_plan_scratch_path`, `is_plan_pipeline_owned`, `_classify_e2e_paths`, and `_has_non_e2e_test` were
already 100% pure in `write_scope_gate.py` (no `SandboxProvider`, no I/O at all) -- moving them here
changes nothing about their behavior; `write_scope_gate.py` imports them back unchanged.

What did NOT move (deliberately, ponytail-simplification for the Stop hook's own same-turn use,
never for the real gate): `check_write_scope`'s own retirement carve-out (a deleted test file naming
a retired AC is in-scope by definition -- needs a `git show <baseline>:<path>` probe per violating
path) and the tech-stack-dependent `_resolve_web_root`/`_stack_has_ui`/`_e2e_dir_is_gitignored`
(need `SandboxProvider`/`repo_files`/`tech_stack_signals`, well outside what a sandbox-shipped hook
module may import -- see `ac_residue_checks.py`'s own docstring for why). The real gate's
`check_write_scope`/`verify_ac_to_tests` (write_scope_gate.py) still run the FULL versions of both,
unchanged, at verify time -- authoritative either way. The Stop hook's own `run_check_hook` below
instead:
  - skips the retirement carve-out entirely (`ponytail: a deleted test file naming a retired AC
    reads as a same-turn violation nudge even though the real gate will correctly allow it; upgrade
    by passing `retired_ac_ids` + each violating path's baseline content if this proves noisy live`)
  - calls `_classify_e2e_paths(changed_paths, None)` -- the function's OWN built-in graceful
    degrade for "no confidently-resolved root available" (see that function's docstring), the exact
    fallback it already uses for the genuinely-ambiguous case
  - takes `has_ui` as a plain bool the hook computes locally and passes in, rather than resolving it
    itself (see `run_check_hook`'s own docstring for that boundary)
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# Each pattern is matched against a repo-relative path with re.search (not fullmatch) -- deliberately
# permissive about surrounding path segments, strict about the filename/directory shape itself.
_DOTNET_TEST_PATTERNS = [
    r"(^|/)[A-Za-z0-9_.]+\.Tests(/|$)",
    r"(^|/)[A-Za-z0-9_.]+Tests\.csproj$",
    r"(^|/)[A-Za-z0-9_.]*Tests?\.cs$",
]
_TS_TEST_PATTERNS = [
    r"\.test\.tsx?$",
    r"\.spec\.tsx?$",
    r"(^|/)(tests|__tests__|test|e2e)/",
    # [cm]?[jt]s covers the ESM/CJS flavours (`.mts`, `.mjs`, `.cts`, `.cjs`, plain `.js`) a
    # package's own "type" field can REQUIRE -- observed live: legitimate `vitest.config.mts`
    # files at apps/jobs and packages/db were silently reverted because only `.ts(x)` matched,
    # while _E2E_PATH_RE (write_scope_gate.py) already accepted `playwright.config.[jt]sx?`.
    # Configs the stage is explicitly told to write must never be deleted over their extension.
    r"(^|/)playwright\.config\.[cm]?[jt]sx?$",
    r"(^|/)vitest\.config\.[cm]?[jt]sx?$",
    # The vitest/Angular setup file its config points at (setupFiles: ["src/test-setup.ts"]) --
    # observed live (2026-08-30): quarantined every lap because the hyphenated name matches none
    # of the patterns above, and the model (never told) rewrote it every lap.
    r"(^|/)test-setup\.[cm]?[jt]s$",
]
_PY_TEST_PATTERNS = [
    r"(^|/)test_[A-Za-z0-9_]+\.py$",
    r"(^|/)[A-Za-z0-9_]+_test\.py$",
    r"(^|/)tests?/",
    r"(^|/)conftest\.py$",
]

_ALL_PATTERNS = [re.compile(p) for p in _DOTNET_TEST_PATTERNS + _TS_TEST_PATTERNS + _PY_TEST_PATTERNS]


def _is_test_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in _ALL_PATTERNS)


# Paths the PIPELINE ITSELF writes and commits between the baseline commit and this gate's diff
# (stage artifacts, the action ledger, the spec id ledger). Observed live: every ac-to-tests
# verify cycle flagged `.ai-dev-workflow/ac-to-tests.draft.json` etc. as scope violations the
# model could never fix -- it didn't write them, workflow persistence did -- deadlocking the
# stage at the verify cap. The scope rule is about the MODEL's writes only.
_PIPELINE_OWNED_PREFIXES = (".ai-dev-workflow/", "APPROVALS.md", "AGENTS.md")

# Artifacts the coverage gate's own test run produces (runner reports and Playwright's failure
# dumps) at whatever depth the test roots live. They are the GATE's requested evidence, not model
# writes -- quarantining them each lap deleted the very reports ac_coverage_gate prefers (observed
# live 2026-08-30: apps/web/ac-run-playwright.json + test-results/ reverted every lap). The
# coverage gate deletes them itself before each fresh run, so staleness is handled there.
_RUNNER_ARTIFACT_RE = re.compile(r"(^|/)(ac-run-[^/]*\.json$|test-results/|TestResults/)|\.trx$")


def _is_pipeline_owned(path: str) -> bool:
    return path.startswith(_PIPELINE_OWNED_PREFIXES) or bool(_RUNNER_ARTIFACT_RE.search(path))


def is_plan_scratch_path(path: str) -> bool:
    """File-based-editing plan, Part 2 sect. 7: plan's own write-scope allowlist. Unlike
    ac-to-tests (which has no legitimate reason to write anywhere under .ai-dev-workflow/), plan's
    model now writes real content to its own scratch dir plus the resolved clean output tier (Part 2
    sect. 1's second file tier) -- everything else, including plan's own approved/draft snapshots
    and every other stage's files, stays out of scope."""
    return path.startswith((
        ".ai-dev-workflow/plan/_draft/",
        ".ai-dev-workflow/plan/diagrams/",
        ".ai-dev-workflow/plan/wireframes/",
    ))


def is_plan_pipeline_owned(path: str) -> bool:
    """Narrower than the default `_is_pipeline_owned` (ac-to-tests' own predicate): still exempts
    what the pipeline's own code writes during a plan-stage run (ledger.jsonl, APPROVALS.md,
    AGENTS.md, plan's own persisted snapshots), but deliberately does NOT blanket-exempt the whole
    `.ai-dev-workflow/` prefix the way the default does -- that would silently let a plan-stage edit
    to `.ai-dev-workflow/spec/ledger.json` or the approved specification through unflagged, exactly
    the risk this write-scope guard exists to close. The specification stage has already finished
    and committed before plan ever runs, so the pipeline itself has no reason to touch
    `.ai-dev-workflow/spec/**` (the ledger/sketchpad) or `.ai-dev-workflow/03-specification.*` (the
    numbered stage files workflow_persistence._stage_file writes -- NOT under spec/, a top-level
    sibling) during a plan turn -- excluding both from the exemption costs nothing legitimate."""
    if path.startswith(".ai-dev-workflow/spec/") or path.startswith(".ai-dev-workflow/03-specification"):
        return False
    return _is_pipeline_owned(path)


# A Playwright end-to-end spec: either it sits in an e2e directory, or it's the playwright config
# itself. Matched by LOCATION rather than by reading imports -- `tests/e2e/` is the convention this
# stage's own prompt mandates, and the coverage gate relies on the same split to exclude browser
# specs from a unit run.
_E2E_PATH_RE = re.compile(r"(^|/)e2e(/|$)|(^|/)playwright\.config\.[jt]sx?$|\.e2e\.[jt]sx?$", re.IGNORECASE)


def _classify_e2e_paths(changed_paths: list[str], resolved_root: str | None) -> tuple[bool, str]:
    """(has_e2e, diagnosis) -- diagnosis is "present" exactly when has_e2e is True, else one of
    "missing" (nothing e2e-ish changed at all) or "misplaced" (something e2e-ish changed but not
    under `{resolved_root}/tests/e2e/`).

    `resolved_root` is None for the genuinely-ambiguous case: with no confidently known directory
    to be strict against, this falls back to the old location-only regex a bare boolean used to
    return -- deliberately no stricter than before for that one case. Otherwise (`resolved_root` is
    `""` for repo-root-is-web-app, or a real subdirectory) checks membership under the exact
    directory the pipeline's own byte-for-byte-fixed `playwright.config.ts` template hardcodes as
    `testDir` -- a config alone never counts (it runs zero tests and yields no screenshots), and
    neither does an e2e-shaped file sitting anywhere else: Playwright's `testDir` would never
    discover it, so crediting it as "present" would silently under-report a UI story with zero real
    browser coverage."""
    e2e_ish = [
        p
        for p in changed_paths
        if _is_test_path(p)
        and _E2E_PATH_RE.search(p)
        and not _is_pipeline_owned(p)
        and not p.endswith(("playwright.config.ts", "playwright.config.js"))
    ]
    if resolved_root is None:
        return bool(e2e_ish), ("present" if e2e_ish else "missing")
    expected_prefix = f"{resolved_root}/tests/e2e/" if resolved_root else "tests/e2e/"
    if any(p.startswith(expected_prefix) for p in e2e_ish):
        return True, "present"
    return False, ("misplaced" if e2e_ish else "missing")


def _has_non_e2e_test(changed_paths: list[str]) -> bool:
    """True when at least one written test lives below the browser layer (unit/integration/
    subcutaneous). Pipeline artifacts never count as tests."""
    return any(
        _is_test_path(p) and not _E2E_PATH_RE.search(p) and not _is_pipeline_owned(p)
        for p in changed_paths
    )


def write_scope_violations(changed_paths: list[str]) -> list[str]:
    """The out-of-scope paths (not a test file, not pipeline-owned) among `changed_paths`. Pure.

    Deliberately WITHOUT `check_write_scope`'s own retirement carve-out (a deleted test file naming
    a retired AC is in-scope by definition) -- see this module's own docstring for why the hook path
    accepts that gap rather than reproducing the git-show-per-path probe it needs. A false positive
    here costs the model an unnecessary "are you sure" moment; the real gate (write_scope_gate.
    check_write_scope, unchanged) still applies the carve-out correctly at verify time."""
    return [p for p in changed_paths if not _is_test_path(p) and not _is_pipeline_owned(p)]


def run_check_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Everything the `--check-hook` CLI reports, computed once over one shared payload.

    `payload` keys:
      - `changed_paths`: this turn's own changed-paths set (untracked + `git diff --name-only
        {AIDW_BASELINE_COMMIT}`), gathered locally by the hook -- the same union
        `write_scope_gate.check_write_scope` computes via `provider.exec_in_sandbox`, just run
        directly since the hook is already inside the sandbox.
      - `has_ui` (bool, optional, default False): whether this repo has a UI framework at all --
        computed by the HOOK itself from the repo's own tech-stack JSON (a plain file read, no
        Python needed for a single boolean), rather than resolved here, so this module never needs
        `tech_stack_signals`/`repo_files` (see this module's own docstring). Gates the "missing
        e2e" direction only -- the "e2e-only" direction below applies regardless.
      - `no_eligible_work` (bool, optional, default False): mirrors `verify_ac_to_tests`'s own
        work-queue-scoping guard -- when every one of this ticket's own criteria is already
        delivered (or the ticket only retires criteria), writing no new tests is CORRECT, and
        neither `wrote_nothing_real` nor `e2e_only` should fire. Without this, the hook would nag a
        legitimate deletion-only/all-delivered ticket to "write tests" for work the pipeline
        forbids re-doing.
    """
    changed_paths = payload.get("changed_paths") or []
    has_ui = bool(payload.get("has_ui"))
    no_eligible_work = bool(payload.get("no_eligible_work"))

    real_changes = [p for p in changed_paths if not _is_pipeline_owned(p)]

    result: dict[str, Any] = {
        "violating_paths": write_scope_violations(changed_paths),
        "wrote_nothing_real": len(real_changes) == 0 and not no_eligible_work,
    }

    if real_changes and not no_eligible_work:
        result["e2e_only"] = not _has_non_e2e_test(real_changes)
        if has_ui:
            has_e2e, diagnosis = _classify_e2e_paths(real_changes, None)
            result["missing_e2e"] = not has_e2e
            result["e2e_diagnosis"] = diagnosis

    return result


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.gates.write_scope_checks` (no sandbox, no DB --
    every function here is pure)."""
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "write_scope_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/write_scope_checks.py hooks/write_scope_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    # --- _is_test_path / _is_pipeline_owned --------------------------------------------------------
    assert _is_test_path("apps/api.Tests/TaskTests.cs")
    assert not _is_test_path("apps/api/Startup.cs")
    assert _is_test_path("apps/web/src/test-setup.ts")
    assert _is_pipeline_owned("apps/web/ac-run-playwright.json")
    assert _is_pipeline_owned("apps/api.Tests/TestResults/run.trx")
    assert not _is_pipeline_owned("apps/web/src/app/app.ts")

    # --- plan scope predicates ----------------------------------------------------------------------
    assert is_plan_scratch_path(".ai-dev-workflow/plan/_draft/steps.json")
    assert not is_plan_scratch_path(".ai-dev-workflow/spec/ledger.json")
    assert not is_plan_pipeline_owned(".ai-dev-workflow/spec/ledger.json")
    assert is_plan_pipeline_owned("APPROVALS.md")
    assert _is_pipeline_owned(".ai-dev-workflow/spec/ledger.json"), (
        "the DEFAULT predicate still blanket-exempts .ai-dev-workflow/ -- only plan's narrower one rejects it"
    )

    # --- _classify_e2e_paths / _has_non_e2e_test -----------------------------------------------------
    assert _classify_e2e_paths(["apps/web/tests/e2e/a.spec.ts"], None) == (True, "present")
    assert _classify_e2e_paths(["apps/web/playwright.config.ts"], None) == (False, "missing")
    assert _classify_e2e_paths(["apps/web/tests/e2e/a.spec.ts"], "apps/web") == (True, "present")
    assert _classify_e2e_paths(["e2e/login.spec.ts"], "apps/web") == (False, "misplaced")
    assert not _has_non_e2e_test(["apps/web/tests/e2e/task-tracker.ac.spec.ts"])
    assert _has_non_e2e_test(["apps/web/tests/e2e/a.spec.ts", "apps/api.Tests/TaskTests.cs"])

    # --- write_scope_violations: known-bad flags, known-good doesn't ---------------------------------
    assert write_scope_violations([]) == []
    assert write_scope_violations(["apps/api.Tests/TaskTests.cs", ".ai-dev-workflow/x.json"]) == []
    assert write_scope_violations(["scripts/helper.sh", "apps/api.Tests/TaskTests.cs"]) == ["scripts/helper.sh"]

    # --- run_check_hook: the CLI entry point's own shared function -----------------------------------
    empty = run_check_hook({})
    assert empty == {"violating_paths": [], "wrote_nothing_real": True}, empty

    wrote_ok = run_check_hook({"changed_paths": ["apps/api.Tests/TaskTests.cs", "apps/web/tests/e2e/a.spec.ts"]})
    assert wrote_ok["violating_paths"] == []
    assert wrote_ok["wrote_nothing_real"] is False
    assert wrote_ok["e2e_only"] is False
    assert "missing_e2e" not in wrote_ok, "has_ui defaults False -- missing_e2e is not even evaluated"

    e2e_only = run_check_hook({"changed_paths": ["apps/web/tests/e2e/a.spec.ts"], "has_ui": True})
    assert e2e_only["e2e_only"] is True
    assert e2e_only["missing_e2e"] is False, "an e2e spec DOES exist -- only the pyramid shape is wrong"

    no_e2e = run_check_hook({"changed_paths": ["apps/api.Tests/TaskTests.cs"], "has_ui": True})
    assert no_e2e["missing_e2e"] is True
    assert no_e2e["e2e_diagnosis"] == "missing"

    scope_violation = run_check_hook({"changed_paths": ["scripts/helper.sh"]})
    assert scope_violation["violating_paths"] == ["scripts/helper.sh"]

    # no_eligible_work: a deletion-only/all-delivered ticket writing NO new tests is CORRECT --
    # neither wrote_nothing_real nor e2e_only may fire, mirroring verify_ac_to_tests's own
    # work-queue-scoping guard.
    all_done = run_check_hook({"changed_paths": [], "no_eligible_work": True})
    assert all_done == {"violating_paths": [], "wrote_nothing_real": False}, all_done

    print("write_scope_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        raw = sys.stdin.read()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        print(json.dumps(run_check_hook(data)))
    else:
        _demo()
