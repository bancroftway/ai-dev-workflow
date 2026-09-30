"""AC-lifecycle residue/attribution/protection checks -- extracted from `ac_coverage_gate.py` and
`write_scope_gate.py` (Task 13) specifically so the sandbox's own same-turn Stop hook
(sandbox-image/hooks/check-ac-residue-stop.mjs) can run the REAL checks by shelling out to
`python3` on this ONE file, instead of a hand-ported JavaScript reimplementation drifting from it --
same reasoning as `test_quality_checks.py`'s own extraction (2026-09-19), applied to
`verify_ac_to_tests`'s (`write_scope_gate.py`) OWN hard rules, which until now had zero same-turn
coverage at all: only the test-*content*-quality axis (`test_quality_checks.py`) was hook-covered;
the ledger-integrity/retired-deferred-completed-residue/attribution/screenshot-mode/ui-relevant-e2e
rules were entirely graph-side, so a violation was only ever caught a full draft->audit->verify
round-trip later.

Split into two halves per function:

  - A PURE function (`*_violations`, `unattributed_tests`, `ui_relevant_ac_ids`,
    `ui_relevant_missing_e2e`, `check_screenshot_capture_mode`, `find_ac_id_hits`) that takes
    already-gathered data (ledger entries, a `{path: contents}` test-file dict, a grep-hits dict)
    and returns violation messages. Every one of these is provably a straight code-move from
    `ac_coverage_gate.py`/`write_scope_gate.py` -- see each function's own docstring for its
    original home.
  - An async, `SandboxProvider`-based wrapper (`check_ledger_integrity`, `check_retired_ac_residue`,
    `check_deferred_ac_residue`, `check_completed_ac_protection`) that gathers that data via
    `provider.exec_in_sandbox` (an UNCAPPED `git`/grep scan -- see `_TEST_FILE_LISTING`'s own
    comment for why capping it would silently miss residue past the cap) and delegates the actual
    decision to the pure function above it. `ac_coverage_gate.py`/`write_scope_gate.py` import and
    call these wrappers completely unchanged -- same signature, same external behavior -- this is a
    relocation, not a rewrite.

The Stop hook has no `SandboxProvider` (it runs INSIDE the sandbox already, with direct filesystem
and git access) and gathers its own `test_files`/`ledger_entries`/etc. locally, then calls the SAME
pure functions above via this file's `--check-hook` CLI entry point -- never a second copy of the
decision logic. `find_ac_id_hits` is the hook's own local equivalent of `_grep_test_files_for_ids`
(same matching semantics, `test_quality_checks.ac_ids_in_name`, just operating on an
already-in-memory dict instead of shelling out to grep via a remote provider).

Why a subprocess, not a straight import: this file must stay import-light enough to run standalone
inside the sandbox via bare `python3` (no pip packages beyond what the image already installs for
`test_quality_checks.py`, which is nothing beyond stdlib) -- everything upstream of
`ac_coverage_gate.py`/`write_scope_gate.py` (`schemas.py`, `sandbox/provider.py`, `spec_ledger.py`,
this pipeline's DB drivers and Azure SDKs) lives entirely outside the sandbox and is never copied
into the image. `test_quality_checks` is imported (both modules already ship to
`/opt/aidw-hooks/`, landing in the same flat directory, so `import test_quality_checks` resolves
there the same way a script's own directory is always on `sys.path[0]`) for `count_tests_per_ac`/
`ac_ids_in_name`/`id_variants` -- reusing the ALREADY-hook-safe versions rather than pulling in
`test_results.py` (a `defusedxml` dependency) a second time for logic that is already byte-identical
between the two modules.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

try:
    from .test_quality_checks import ac_ids_in_name, count_tests_per_ac, id_variants
except ImportError:
    # Run standalone (`python3 ac_residue_checks.py --check-hook`, the sandbox's own invocation --
    # see this module's own docstring): no package context, so the relative import above raises;
    # both files land flat in /opt/aidw-hooks/, so a script's own directory (always sys.path[0])
    # resolves this absolute form instead.
    from test_quality_checks import ac_ids_in_name, count_tests_per_ac, id_variants  # type: ignore[no-redef]

# Mirrors spec_ledger.LEDGER_PATH's value -- not imported (spec_ledger.py pulls in repo_files/
# SandboxProvider, well outside what a sandbox-shipped hook module may depend on), same "duplicate
# the path STRING, never the ledger-reading logic" tradeoff check-ledger-sync-stop.mjs's own
# identical `LEDGER_PATH` constant already accepts for the same reason, JS side.
LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json"

# One definition of "the test files in this tree" -- ported verbatim from ac_coverage_gate.py's own
# `_TEST_FILE_LISTING` (moved here unchanged; that module now imports this one). `test-?results`
# with -i, not the old case-sensitive bare `TestResults`: that spelling is .NET's, and Playwright
# writes its failure artifacts to `test-results/` (hyphen, lowercase) which sailed straight
# through -- one e2e run's screenshots then crowded real sources out of the depth listing, and
# reading a `test-failed-1.png` as source crashed the run outright. The binary-extension denylist
# exists because artifacts can be named anything and still match (test|spec).
_TEST_FILE_LISTING = (
    "git ls-files -co --exclude-standard | grep -iE '(test|spec)' "
    r"| grep -viE '(^|/)(node_modules|\.playwright-browsers|bin|obj|dist|build|\.next|\.venv|vendor|test-?results|coverage|\.ai-dev-workflow|agent-work)/' "
    r"| grep -viE '\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tar|mp4|webm|woff2?|ttf|eot|dll|exe|so|dylib|pyc|class|jar)$'"
)

# The subset of a "test declaration" that carries a NAME (a `test(...)`/`it(...)`/`describe(...)`
# call, or a method signature) -- a bare `[Fact]`/`[Theory]` attribute line matches neither alone,
# it is the line ABOVE the name in every generated .NET suite. Ported verbatim from
# ac_coverage_gate.py.
_NAMED_TEST_DECL_RE = re.compile(
    r"\b(?:test|it|describe)\s*\("
    r"|\b(?:public|internal|private)\s+(?:async\s+)?[\w<>\[\],\s]+?\s+\w+\s*\(",
    re.IGNORECASE,
)
_TEST_ATTRIBUTE_RE = re.compile(r"^\s*\[\s*(Fact|Theory|Test|TestMethod|TestCase)\b", re.IGNORECASE)
_JS_TEST_CALL_RE = re.compile(r"\b(?:test|it)\s*(?:\.\w+)?\s*\(\s*['\"`]", re.IGNORECASE)

# `screenshot: 'on'` or `screenshot: "on"`, either quote style, whitespace-tolerant around the
# colon. Ported verbatim from write_scope_gate.py.
_SCREENSHOT_ON_RE = re.compile(r"""screenshot\s*:\s*['"]on['"]""")


def check_screenshot_capture_mode(config_source: str) -> bool:
    """True if a playwright.config's `use` block sets `screenshot: 'on'` -- the setting this
    stage's own prompt mandates (ac_to_tests_draft.md, ac_to_tests_greenfield_segment.md) so a
    PASSING suite still yields visual evidence, not just failures (Playwright's own default is
    only-on-failure). Pure. Moved here unchanged from write_scope_gate.py, which imports it back --
    a standalone content check on `playwright.config.ts`, unlike everything gated on
    `write_scope_checks`'s own changed-paths output."""
    return bool(_SCREENSHOT_ON_RE.search(config_source))


async def _grep_test_files_for_ids(
    provider: Any, thread_id: str, ac_ids: set[str]
) -> dict[str, set[str]]:
    """{test file path -> AC ids named on its lines}, for the given ids only. Moved here unchanged
    from ac_coverage_gate.py (that module imports it back).

    Two-stage matching: `grep -F` with id_variants finds CANDIDATE lines cheaply, then
    ac_ids_in_name re-parses each line boundary-aware -- a raw variant substring test would credit
    US-0001.1 for a line naming only US-0001.12 (the `(?!\\d)` tail is what the variants list cannot
    express in `grep -F`)."""
    import shlex

    if not ac_ids:
        return {}
    patterns = " ".join(f"-e {shlex.quote(v)}" for ac in sorted(ac_ids) for v in id_variants(ac))
    grep = await provider.exec_in_sandbox(
        thread_id,
        f"{_TEST_FILE_LISTING} | xargs -r -d '\\n' grep -H -n -F {patterns} -- 2>/dev/null || true",
    )
    hits: dict[str, set[str]] = {}
    for line in (grep.stdout or "").splitlines():
        path, _, rest = line.partition(":")
        _lineno, _, text = rest.partition(":")
        found = set(ac_ids_in_name(text)) & ac_ids
        if found and path:
            hits.setdefault(path, set()).update(found)
    return hits


def find_ac_id_hits(test_files: dict[str, str], ac_ids: set[str]) -> dict[str, set[str]]:
    """The Stop hook's own local equivalent of `_grep_test_files_for_ids`, for a `test_files` dict
    already gathered from disk (no `SandboxProvider`, no grep subprocess -- the hook already read
    every file's content to build this dict, so it scans it directly). Pure.

    Same matching semantics as `_grep_test_files_for_ids` (line-by-line `ac_ids_in_name`, scoped to
    `ac_ids`), just without that function's cheap `grep -F` candidate-filtering pre-pass -- with the
    whole file already in memory there is nothing to pre-filter for."""
    if not ac_ids:
        return {}
    hits: dict[str, set[str]] = {}
    for path, contents in (test_files or {}).items():
        found: set[str] = set()
        for line in contents.splitlines():
            found.update(set(ac_ids_in_name(line)) & ac_ids)
        if found:
            hits[path] = found
    return hits


def ledger_integrity_violations(diff_stdout: str) -> list[str]:
    """Pure decision half of `check_ledger_integrity`: given `git diff --name-only -- ledger.json`'s
    stdout, the violation message (empty if the diff is clean)."""
    if not (diff_stdout or "").strip():
        return []
    return [
        f"{LEDGER_PATH} was modified during this stage -- the spec ledger is pipeline-owned and "
        "never writable by an agent; the change has been reverted. Do not touch it."
    ]


async def check_ledger_integrity(provider: Any, thread_id: str) -> list[str]:
    """The spec ledger is pipeline-owned truth every gate reads, yet it sits inside the write-scope
    whitelist (.ai-dev-workflow/) any agent can write to. Every pipeline writer commits its own
    ledger writes, so an UNCOMMITTED diff on it at gate time is agent tampering: revert it and fail
    the lap so the feedback says so. Moved here unchanged from ac_coverage_gate.py (that module
    imports it back), now delegating its message-building to `ledger_integrity_violations` above."""
    import shlex

    diff = await provider.exec_in_sandbox(thread_id, f"git diff --name-only -- {shlex.quote(LEDGER_PATH)}")
    violations = ledger_integrity_violations(diff.stdout)
    if violations:
        await provider.exec_in_sandbox(thread_id, f"git checkout -- {shlex.quote(LEDGER_PATH)}")
    return violations


def retired_ac_residue_violations(hits: dict[str, set[str]]) -> list[str]:
    """Pure decision half of `check_retired_ac_residue`: given {test file path -> retired AC ids it
    still names}, the violation message (empty if nothing hit)."""
    if not hits:
        return []
    detail = "; ".join(f"{path}: {', '.join(sorted(ids))}" for path, ids in sorted(hits.items()))
    return [
        "test files still reference retired AC ids -- these criteria were removed from the "
        f"Specification, so delete those test cases (delete the file if it holds nothing else): {detail}"
    ]


async def check_retired_ac_residue(
    provider: Any, thread_id: str, entries: list[dict[str, Any]]
) -> list[str]:
    """Deletion propagation, test side: once a Specification retires an AC, no test file may still
    reference its id. Runs at ac-to-tests verify AND again at the last rebuild gate before metrics
    (rebuild._scan_regression_reasons) -- later stages can write tests too. Moved here unchanged
    from ac_coverage_gate.py (that module imports it back), now delegating its message-building to
    `retired_ac_residue_violations` above."""
    retired = {
        e["id"]
        for e in entries
        if e.get("kind") == "acceptance_criterion" and e.get("status") == "retired"
    }
    hits = await _grep_test_files_for_ids(provider, thread_id, retired)
    return retired_ac_residue_violations(hits)


def deferred_ac_residue_violations(hits: dict[str, set[str]]) -> list[str]:
    """Pure decision half of `check_deferred_ac_residue`: given {test file path -> NEVER-DELIVERED
    deferred AC ids it still names}, the violation message (empty if nothing hit)."""
    if not hits:
        return []
    detail = "; ".join(f"{path}: {', '.join(sorted(ids))}" for path, ids in sorted(hits.items()))
    return [
        "test files reference DEFERRED criteria that were never built -- deferred scope is parked, "
        f"not in this ticket: delete those test cases (no code may be demanded for them): {detail}"
    ]


async def check_deferred_ac_residue(
    provider: Any, thread_id: str, entries: list[dict[str, Any]]
) -> list[str]:
    """Deferral containment, test side (user requirement 2026-08-31): a NEVER-DELIVERED deferred
    criterion must have no test naming it -- a red test citing a deferred id would drag the whole
    parked feature into the build, because minimal-code-to-green's job is 'make every failing
    test pass'. Delivered-then-deferred criteria (coded_run_id set) keep their tests on purpose:
    the code stays in the tree, parked, and its regression tests with it. Runs at the same two
    call sites as check_retired_ac_residue (ac-to-tests verify, last rebuild gate). Moved here
    unchanged from ac_coverage_gate.py (that module imports it back), now delegating its
    message-building to `deferred_ac_residue_violations` above."""
    parked_unbuilt = {
        e["id"]
        for e in entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") == "deferred"
        and not e.get("coded_run_id")
    }
    hits = await _grep_test_files_for_ids(provider, thread_id, parked_unbuilt)
    return deferred_ac_residue_violations(hits)


def completed_ac_protection_violations(completed: set[str], present: set[str]) -> list[str]:
    """Pure decision half of `check_completed_ac_protection`: given the completed AC ids and the
    ids still PRESENT somewhere in the test tree, the violation message(s) for whichever completed
    ids are no longer present anywhere (empty if none)."""
    problems: list[str] = []
    for ac_id in sorted(completed - present):
        problems.append(
            f"no test file names completed criterion {ac_id} any more -- its regression tests were "
            "deleted or renamed; restore them (completed criteria keep their tests)"
        )
    return problems


async def check_completed_ac_protection(
    provider: Any, thread_id: str, baseline_commit: str | None, entries: list[dict[str, Any]]
) -> list[str]:
    """Completed criteria (coded_run_id stamped by a healthy metrics run) are settled: their
    regression tests must survive. Incidental shared-code/file edits are deliberately NOT policed
    -- the regression suite guards behavior, and this stage's own tooling (create/edit, no delete)
    routinely rewrites a whole test file to add new cases, which a line-level diff cannot tell
    apart from genuine rework of the untouched ones sitting in the same file. Id-presence (does any
    test file still name the id), never runner-reported test-name grepping: runner names are
    FQNs/joined titles that don't exist verbatim in source. `baseline_commit` is unused now (kept in
    the signature -- callers already pass it, and a future precision check may want it again).
    Moved here unchanged from ac_coverage_gate.py (that module imports it back), now delegating its
    message-building to `completed_ac_protection_violations` above."""
    del baseline_commit
    completed = {
        e["id"]
        for e in entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") in ("active", "revised")
        and e.get("coded_run_id")
    }
    if not completed:
        return []
    present: set[str] = set()
    for ids in (await _grep_test_files_for_ids(provider, thread_id, completed)).values():
        present.update(ids)
    return completed_ac_protection_violations(completed, present)


def unattributed_tests(ac_ids: list[str], test_files: dict[str, str]) -> dict[str, int]:
    """Per file, how many test declarations carry NO recognisable AC id. Pure. Moved here unchanged
    from ac_coverage_gate.py (that module imports it back).

    The generic safety net for this whole class of defect. Attribution works by finding an AC id
    inside a model-authored test name, and no pattern can cover a convention nobody controls -- four
    spellings had to be added reactively, each discovered only after a run had already reported "0
    tests" for criteria that were tested. The failure mode is what makes it dangerous: an unmatched
    name is indistinguishable from an untested criterion, so the gate reports a confident zero.

    A file full of test declarations where NOTHING matched is therefore reported as an attribution
    problem, not as an absence of tests. That distinction is the whole point: "I could not read this"
    and "there is nothing here" must never look the same.
    """
    out: dict[str, int] = {}
    known = set(ac_ids)
    for path, contents in (test_files or {}).items():
        unmatched = 0
        attributed_above = False
        for line in contents.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _TEST_ATTRIBUTE_RE.search(stripped):
                attributed_above = True
                continue
            # _NAMED_TEST_DECL_RE, not a bare test-declaration regex: a bare `[Fact]` attribute line
            # matches neither alone but carries no name -- it is the line ABOVE the name in every
            # generated .NET suite.
            is_js_test = bool(_JS_TEST_CALL_RE.search(stripped))
            is_attributed_method = attributed_above and bool(_NAMED_TEST_DECL_RE.search(stripped))
            attributed_above = False
            # A method signature with no test attribute above it is a HELPER -- a constructor, a
            # Dispose, a CreateClient factory. Measured on a real suite: counting those reported 4
            # orphans in a file where every actual test was correctly named, which would have sent a
            # redraft chasing an attribution problem that did not exist.
            if not (is_js_test or is_attributed_method):
                continue
            if not (set(ac_ids_in_name(stripped)) & known):
                unmatched += 1
        if unmatched:
            out[path] = unmatched
    return out


def ui_relevant_ac_ids(content_dict: dict[str, Any], active_ac_ids: list[str]) -> set[str]:
    """ACs the stage itself marked user-facing, from its own coverage_plan. Pure. Moved here
    unchanged from ac_coverage_gate.py's `_ui_relevant_ac_ids` (that module imports it back under
    this same public name).

    The model's `ui_relevant` flag is used here rather than a guess from the AC text: it already has
    to decide this to choose a test kind, and requiring an e2e test for a criterion nobody considers
    user-facing would force browser tests onto pure calculation rules.
    """
    plan = ((content_dict or {}).get("test_suite") or {}).get("coverage_plan") or []
    flagged = {str(entry.get("ac_id")) for entry in plan if entry.get("ui_relevant")}
    return {ac for ac in active_ac_ids if ac in flagged}


def ui_relevant_missing_e2e(ac: str, per_level: dict[str, int], ui_relevant: set[str]) -> list[str]:
    """Pure: the one-line "no end-to-end test, and this criterion is user-facing" shortfall,
    extracted out of `ac_coverage_gate.depth_shortfalls`'s own per-AC problem list (that function
    calls this directly instead of keeping its own inline copy) so a same-turn hook can run just
    this one check, given only what the test files themselves show, without the rest of
    `depth_shortfalls`'s GREEN-phase-only/count-based checks."""
    if ac in ui_relevant and per_level["e2e"] < 1:
        return ["no end-to-end test, and this criterion is user-facing"]
    return []


def run_check_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Everything the `--check-hook` CLI reports, computed once over one shared payload -- the same
    function a unit test can call directly, so the CLI wrapper below has no logic of its own to
    drift from what a caller importing this module gets.

    `payload` keys (all optional -- a missing key just skips that check, same fail-open-per-check
    posture every sibling hook takes):
      - `ledger_diff`: raw stdout of `git diff --name-only -- <ledger path>` (ledger integrity)
      - `ledger_entries`: the ledger's own `entries` list (retired/deferred/completed residue,
        unattributed-tests' own "any real ledger AC id, ever" universe)
      - `test_files`: {path: contents} for every test file the hook could read this turn
      - `playwright_config`: a playwright.config.* file's contents, or None if none was found
      - `coverage_plan`: the ac-to-tests draft's own `test_suite.coverage_plan` list (ui_relevant
        flags) -- may be stale by one turn (the draft is persisted by the orchestrator between
        turns, not by the model itself), same "best-effort nudge, never the authority" posture as
        every provider-based check's own real-time verify counterpart.
    """
    ledger_entries = payload.get("ledger_entries") or []
    test_files = payload.get("test_files") or {}

    retired = {
        e["id"] for e in ledger_entries
        if e.get("kind") == "acceptance_criterion" and e.get("status") == "retired"
    }
    deferred = {
        e["id"] for e in ledger_entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") == "deferred"
        and not e.get("coded_run_id")
    }
    completed = {
        e["id"] for e in ledger_entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") in ("active", "revised")
        and e.get("coded_run_id")
    }
    all_ledger_ac_ids = [e["id"] for e in ledger_entries if e.get("kind") == "acceptance_criterion"]
    active_ac_ids = [
        e["id"] for e in ledger_entries
        if e.get("kind") == "acceptance_criterion" and e.get("status") in ("active", "revised")
    ]

    present: set[str] = set()
    for ids in find_ac_id_hits(test_files, completed).values():
        present.update(ids)

    result: dict[str, Any] = {
        "ledger_integrity": ledger_integrity_violations(payload.get("ledger_diff") or ""),
        "retired_residue": retired_ac_residue_violations(find_ac_id_hits(test_files, retired)),
        "deferred_residue": deferred_ac_residue_violations(find_ac_id_hits(test_files, deferred)),
        "completed_protection": completed_ac_protection_violations(completed, present),
        "unattributed_tests": unattributed_tests(all_ledger_ac_ids, test_files),
    }

    playwright_config = payload.get("playwright_config")
    if playwright_config is not None:
        result["screenshot_missing"] = not check_screenshot_capture_mode(playwright_config)

    coverage_plan = payload.get("coverage_plan")
    if coverage_plan is not None and test_files:
        ac_ids_in_tests = sorted({ac for ac in active_ac_ids} | set(all_ledger_ac_ids))
        counts = count_tests_per_ac(ac_ids_in_tests, test_files)
        ui_relevant = ui_relevant_ac_ids({"test_suite": {"coverage_plan": coverage_plan}}, active_ac_ids)
        missing_e2e: dict[str, list[str]] = {}
        for ac in active_ac_ids:
            problems = ui_relevant_missing_e2e(ac, counts.get(ac, {"unit": 0, "integration": 0, "e2e": 0}), ui_relevant)
            if problems:
                missing_e2e[ac] = problems
        result["ui_relevant_missing_e2e"] = missing_e2e

    return result


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.gates.ac_residue_checks` (no sandbox, no DB --
    every function here except the four provider-based wrappers is pure, and those four are
    self-checked by ac_coverage_gate.py's own `_demo_provenance_checks` against a fake provider,
    unchanged by this move)."""
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "ac_residue_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/ac_residue_checks.py hooks/ac_residue_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    # --- ledger_integrity_violations -------------------------------------------------------------
    assert ledger_integrity_violations("") == []
    assert ledger_integrity_violations("   \n") == []
    tampered = ledger_integrity_violations(f" M {LEDGER_PATH}\n")
    assert tampered and LEDGER_PATH in tampered[0], tampered

    # --- retired/deferred/completed, pure halves --------------------------------------------------
    assert retired_ac_residue_violations({}) == []
    retired_hit = retired_ac_residue_violations({"apps/web/x.spec.ts": {"US-0002.1"}})
    assert retired_hit and "US-0002.1" in retired_hit[0], retired_hit

    assert deferred_ac_residue_violations({}) == []
    deferred_hit = deferred_ac_residue_violations({"apps/web/y.spec.ts": {"US-0004.1"}})
    assert deferred_hit and "US-0004.1" in deferred_hit[0], deferred_hit

    assert completed_ac_protection_violations({"US-0001.1"}, {"US-0001.1"}) == []
    deleted = completed_ac_protection_violations({"US-0001.1"}, set())
    assert deleted and "US-0001.1" in deleted[0], deleted
    # A completed AC surviving in a file that ALSO grew unrelated new tests is still fine -- presence
    # is the whole check, and this is the shape that used to false-flag before protection-B's removal.
    assert completed_ac_protection_violations({"US-0001.1", "US-0003.1"}, {"US-0001.1", "US-0003.1"}) == []

    # --- find_ac_id_hits: the hook's local equivalent of _grep_test_files_for_ids -----------------
    assert find_ac_id_hits({}, {"US-0001.1"}) == {}
    assert find_ac_id_hits({"t.spec.ts": "irrelevant"}, set()) == {}
    hit_files = {
        "apps/web/x.spec.ts": "test('[US-0002.1] old feature', () => {});",
        "apps/web/y.spec.ts": "test('[US-0009.9] unrelated', () => {});",
    }
    assert find_ac_id_hits(hit_files, {"US-0002.1"}) == {"apps/web/x.spec.ts": {"US-0002.1"}}
    assert find_ac_id_hits(hit_files, {"US-0002.1", "US-0009.9"}) == {
        "apps/web/x.spec.ts": {"US-0002.1"}, "apps/web/y.spec.ts": {"US-0009.9"},
    }

    # --- unattributed_tests: known-good input doesn't flag, known-bad does ------------------------
    named_ok = {"t/T.cs": "[Fact]\npublic void TestUS00011Works(){ Assert.True(x); }\n"}
    assert unattributed_tests(["US-0001.1"], named_ok) == {}
    anonymous = {
        "t/CounterTests.cs": (
            "[Fact]\npublic void IncrementAddsOne(){ Assert.Equal(1, c.Value); }\n"
            "[Fact]\npublic void DecrementSubtractsOne(){ Assert.Equal(0, c.Value); }\n"
        )
    }
    assert unattributed_tests(["US-0001.1"], anonymous) == {"t/CounterTests.cs": 2}
    helpers_only = {"t/T.cs": "public CounterTests(){ }\npublic void Dispose(){ }\n"}
    assert unattributed_tests(["US-0001.1"], helpers_only) == {}, "helpers are not tests"

    # --- ui_relevant_ac_ids / ui_relevant_missing_e2e ----------------------------------------------
    plan = {"test_suite": {"coverage_plan": [
        {"ac_id": "US-0001.1", "ui_relevant": True},
        {"ac_id": "US-0002.1", "ui_relevant": False},
    ]}}
    assert ui_relevant_ac_ids(plan, ["US-0001.1", "US-0002.1"]) == {"US-0001.1"}
    assert ui_relevant_missing_e2e(
        "US-0001.1", {"unit": 3, "integration": 0, "e2e": 0}, {"US-0001.1"}
    ) == ["no end-to-end test, and this criterion is user-facing"]
    # Not ui_relevant -- no e2e requirement even with zero e2e tests.
    assert ui_relevant_missing_e2e("US-0002.1", {"unit": 3, "integration": 0, "e2e": 0}, {"US-0001.1"}) == []
    # ui_relevant WITH an e2e test -- clears.
    assert ui_relevant_missing_e2e("US-0001.1", {"unit": 0, "integration": 0, "e2e": 1}, {"US-0001.1"}) == []

    # --- check_screenshot_capture_mode: content check, not path check ------------------------------
    assert check_screenshot_capture_mode("use: { screenshot: 'on', baseURL: process.env.BASE_URL }")
    assert check_screenshot_capture_mode('use: { screenshot: "on" }')
    assert not check_screenshot_capture_mode("use: { baseURL: process.env.BASE_URL }")
    assert not check_screenshot_capture_mode("use: { screenshot: 'only-on-failure' }")

    # --- run_check_hook: the CLI entry point's own shared function ---------------------------------
    ledger_entries = [
        {"id": "US-0001", "kind": "user_story", "status": "active"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active", "coded_run_id": "r1"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "retired"},
        {"id": "US-0003.1", "kind": "acceptance_criterion", "status": "deferred"},
    ]
    payload = {
        "ledger_diff": "",
        "ledger_entries": ledger_entries,
        "test_files": {
            "apps/web/x.spec.ts": (
                "test('[US-0002.1] retired feature still here', () => {});\n"
                "test('[US-0003.1] deferred feature still here', () => {});\n"
            ),
            "apps/api.Tests/OrphanTests.cs": "[Fact]\npublic void SomeUnrelatedHelperTest(){ Assert.True(true); }\n",
        },
        "playwright_config": "use: { baseURL: process.env.BASE_URL }",
        "coverage_plan": [{"ac_id": "US-0001.1", "ui_relevant": True}],
    }
    out = run_check_hook(payload)
    assert out["ledger_integrity"] == []
    assert out["retired_residue"] and "US-0002.1" in out["retired_residue"][0]
    assert out["deferred_residue"] and "US-0003.1" in out["deferred_residue"][0]
    # US-0001.1 is completed (coded_run_id) but names no test anywhere in this payload -- flagged.
    assert out["completed_protection"] and "US-0001.1" in out["completed_protection"][0]
    assert out["unattributed_tests"] == {"apps/api.Tests/OrphanTests.cs": 1}
    assert out["screenshot_missing"] is True
    assert "US-0001.1" in out["ui_relevant_missing_e2e"], out["ui_relevant_missing_e2e"]

    # A minimal payload (nothing gathered yet -- e.g. an in-flight first turn) must not crash and
    # must report nothing.
    empty_out = run_check_hook({})
    assert empty_out["ledger_integrity"] == []
    assert empty_out["retired_residue"] == []
    assert empty_out["unattributed_tests"] == {}
    assert "screenshot_missing" not in empty_out
    assert "ui_relevant_missing_e2e" not in empty_out

    print("ac_residue_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: a JSON payload (see run_check_hook's own docstring for
        # its shape) on stdin, JSON results on stdout. No sandbox access, no provider, no DB --
        # everything it needs was already gathered locally by the (already-inside-the-sandbox) hook.
        raw = sys.stdin.read()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        print(json.dumps(run_check_hook(data)))
    else:
        _demo()
