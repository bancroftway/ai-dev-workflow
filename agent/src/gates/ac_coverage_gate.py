"""P4's AC-coverage half of the deterministic_verify gate: every active Acceptance Criterion in
.ai-dev-workflow/spec/ledger.json must have at least one test whose name embeds its AC id, and that test must
currently be FAILING (a passing "new" test before any implementation exists is almost certainly
tautological -- this is TDD's RED step, checked mechanically rather than trusted to the model's
own self-report).

Regex-based test-name/result extraction, not a framework-specific structured-report parser
(trx/junit-xml/playwright-json each have real schemas this could parse properly) -- a pragmatic
simplification given time constraints, noted here rather than silently passed off as more rigorous
than it is. Good enough to catch the two failure modes that matter (zero coverage, tautological
pass) as long as the stack's test runner prints AC ids and pass/fail status in its console output,
which every stack's default reporter does.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .. import repo_files, stack_runner, tech_stack_signals
from .write_scope_gate import _E2E_PATH_RE
from ..sandbox.provider import SandboxProvider
from ..schemas import StageReport
from ..spec_ledger import LEDGER_PATH

# Where the test-run agent tees the suite's complete console output for this gate to read.
AC_TEST_OUTPUT_PATH = "agent-work/ac-test-output.txt"


class AcTestRunReport(StageReport):
    """What the test-run agent must report (prompts/ac_test_run.md)."""

    output_artifact: str = ""
    exit_ok: bool = False


# One line of a test runner's console output naming a test and its outcome -- covers dotnet test's
# default console logger, vitest's default reporter, and jest/playwright's default reporters
# closely enough for this purpose (all print a pass/fail glyph or word beside the test name).
# Outcome markers are matched as WORDS/GLYPHS, never as bare substrings of the whole line. The
# line contains the test's own NAME, and names legitimately contain these words -- observed live:
# `Test_US_0003_1_AssignStaff_WhenCapacityOverlapDuplicateAndWeeklyMaxRulesPass_Succeeds` was a
# stub that threw NotImplementedException (correctly RED), but "RulesPass" made a substring match
# classify it as PASSING, so the gate rejected it as tautological and no redraft could ever fix it.
_FAIL_MARKERS = ("fail", "failed", "✗", "×")
_PASS_MARKERS = ("pass", "passed", "ok", "✓", "√")

# A word boundary that treats identifier characters (including _) as part of a word, so "RulesPass"
# does not match "pass" while "Passed:" and "[PASS]" do.
def _has_marker(line: str, markers: tuple[str, ...]) -> bool:
    lowered = line.lower()
    for marker in markers:
        if marker in ("✗", "×", "✓", "√"):
            if marker in line:
                return True
            continue
        for match in re.finditer(re.escape(marker), lowered):
            before = lowered[match.start() - 1] if match.start() else " "
            after_index = match.end()
            after = lowered[after_index] if after_index < len(lowered) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
    return False



_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PATH_TOKEN_RE = re.compile(r"[\w@./\\-]+\.[A-Za-z0-9]+")


def id_variants(ac_id: str) -> list[str]:
    """Spellings a test name may legitimately use for one ledger id. Models re-prefix US-0003.6
    as AC-0003.6 despite instructions (observed live, run 7), and identifier-safe names replace
    -/. with _ (Test_US_0007_2). Numbering is what identifies the AC; tolerate the spellings.
    Public: metrics_nodes' traceability matrix reuses it so both scans accept the same spellings."""
    variants = {ac_id}
    if ac_id.startswith("US-"):
        variants.add("AC-" + ac_id[3:])
    variants.update(v.replace("-", "_").replace(".", "_") for v in list(variants))
    # Punctuation stripped entirely: a C# method name cannot contain '-' or '.', and the generated
    # .NET suites name tests `TestUS00012ResolveStateDirectory...`. Without this variant the depth
    # counter scored a file holding 14 real tests as ZERO tests for every criterion -- measured live,
    # on apps/api.Tests/CounterApiIntegrationTests.cs.
    variants.update(v.replace("-", "").replace(".", "").replace("_", "") for v in list(variants))
    return sorted(variants)


def _extract_failed_files(lines: list[str]) -> list[str]:
    """File paths named on file-level FAIL/ERROR lines. Greenfield TDD-red tests routinely die at
    IMPORT (the module under test doesn't exist yet, its dependency isn't installed yet) -- the
    runner then prints one FAIL line per file and never reaches per-test name lines, so the ids
    inside those files are invisible to the per-line scan and must be attributed via the files."""
    failed: list[str] = []
    for line in lines:
        upper = line.upper()
        if "FAIL" not in upper and "ERROR" not in upper:
            continue
        for token in _PATH_TOKEN_RE.findall(line):
            if "/" in token and (".test." in token or ".spec." in token or "test" in token.lower()):
                failed.append(token)
    return sorted(set(failed))


def resolve_test_command(tech_stack: dict[str, Any]) -> str | None:
    """Public: exit's manifest completion records this as the manifest's test_command."""
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if tech_stack.get("dotnet_detected"):
        return f"{tech_stack_signals.dotnet_root_prefix(tech_stack)}dotnet test --logger 'console;verbosity=normal'"
    if "typescript" in languages or "javascript" in languages:
        return "npx --yes vitest run --reporter=verbose || npx --yes jest --verbose"
    if "python" in languages:
        return "python -m pytest -v"
    return None


# --- per-AC test DEPTH -------------------------------------------------------------------------
# The gate below used to ask only "does at least one test name this AC" -- true for a single
# happy-path assertion, which is not a tested criterion.
#
# The threshold is PHASE-DEPENDENT, and that is the whole point. The full requirement is 2 tests
# below the browser layer per criterion, but ac-to-tests writes every test BEFORE any implementation
# exists: there is no module to unit-test yet, so the model reaches for the one level that can be
# written against nothing -- a Playwright spec. Enforcing 2 there produced three consecutive fresh
# runs that escalated at the cap with the same finding ("only 0 test(s) below the browser layer"),
# having written `Api.Tests.csproj` and no `.cs` files at all. A gate no run can pass is a gate
# people switch off, so the RED phase asks for 1 and the GREEN phase (minimal-code-to-green, where
# the code exists) asks for 2. This is the plan's own audit finding A3, applied.
MIN_NON_E2E_TESTS_PER_AC = int(os.environ.get("MIN_NON_E2E_TESTS_PER_AC", "2"))

# ZERO at the RED phase, and this is a deliberate REDUCTION in strictness with a compensating
# control, not an oversight -- so here is the evidence and the trade.
#
# Measured, not assumed: across every ac-to-tests session of one thread the model made 20 `view`,
# 12 `skill`, 6 `glob` and 3 `apply_patch` calls. It writes -- it just writes the Playwright spec
# and `Api.Tests.csproj` and stops, never authoring a `.cs` test. Four consecutive fresh runs
# escalated at the verify cap on "only 0 test(s) below the browser layer", at thresholds of both 2
# and 1, with the requirement stated explicitly in the prompt. It is not a permissions problem (the
# write-scope allowlist permits every realistic .NET test path) and not a measurement problem (the
# filesystem confirms no `.cs` file exists).
#
# The compensating control is `test_coverage_gate.check_ac_depth`, which enforces the FULL
# MIN_NON_E2E_TESTS_PER_AC at minimal-code-to-green -- where the implementation exists and a unit
# test is a thing that can actually be written. That is the plan's own audit finding A3.
#
# What ac-to-tests still enforces: at least one test naming every criterion, those tests actually
# failing (TDD red, checked mechanically), an e2e test for every criterion the stage marks
# ui_relevant, and the anti-padding checks. Raise this above 0 only with evidence that the drafting
# model has started writing below-browser tests at this phase.
MIN_NON_E2E_TESTS_PER_AC_RED = int(os.environ.get("MIN_NON_E2E_TESTS_PER_AC_RED", "0"))

# Symbols that make a .NET/JS test an INTEGRATION test. Detected by symbol, never by directory: a
# .NET repo keeps unit and integration tests in ONE project (apps/api.Tests/CounterApiTests.cs), so
# the path proves nothing. e2e is the one level a path does prove, which is why it is the only level
# with a hard threshold here.
_INTEGRATION_SYMBOLS = ("WebApplicationFactory", "TestServer", "HttpClient", "createServer", "supertest", "TestClient")


def classify_test_level(path: str, contents: str) -> str:
    """'e2e' | 'integration' | 'unit'. Pure."""
    if _E2E_PATH_RE.search(path):
        return "e2e"
    if any(symbol in contents for symbol in _INTEGRATION_SYMBOLS):
        return "integration"
    return "unit"


def count_tests_per_ac(ac_ids: list[str], test_files: dict[str, str]) -> dict[str, dict[str, int]]:
    """Per AC: how many tests name it, split by level.

    A "test" is counted per test-declaring line mentioning the id, not per file: one file commonly
    holds several tests for the same criterion, and per-file counting would read three tests in one
    file as one.
    """
    counts = {ac: {"unit": 0, "integration": 0, "e2e": 0} for ac in ac_ids}
    for path, contents in test_files.items():
        level = classify_test_level(path, contents)
        for line in contents.splitlines():
            # Shared with the anti-padding checks below, so "what is a test" is defined once.
            if not _TEST_DECL_RE.search(line):
                continue
            for ac in ac_ids:
                if any(variant in line for variant in id_variants(ac)):
                    counts[ac][level] += 1
    return counts


# --- anti-padding -------------------------------------------------------------------------------
# A bare count invites padding, and this pipeline has already produced all three forms of it: a
# placeholder page.tsx, a localStorage-only "backend", and four consecutive turns claiming Playwright
# specs had been written with zero write calls. So the count is necessary and not sufficient: three
# tests asserting the same expression are one test, and three tests with the same body are one test.
#
# What this CANNOT do, stated plainly rather than implied away: it cannot tell whether a test is
# GOOD. The prompt demands quality; these checks make padding expensive, not impossible.
MIN_DISTINCT_ASSERTIONS_PER_AC = int(os.environ.get("MIN_DISTINCT_ASSERTIONS_PER_AC", "2"))

# The distinct-assertion check only applies once an AC has this many tests. Below it, the count
# thresholds already govern and this check produces false positives on legitimate work: literals are
# normalised (so `Assert.Equal(1, c.Value)` and `Assert.Equal(0, c.Value)` are ONE target), and for a
# value-based criterion -- "increment shows 1", "decrement shows 0" -- that pair is exactly how the
# behaviour is meant to be tested. Requiring three tests first keeps the check aimed at what it was
# written for: three tests that are really one test.
MIN_TESTS_BEFORE_ASSERTION_CHECK = int(os.environ.get("MIN_TESTS_BEFORE_ASSERTION_CHECK", "3"))

# How similar two test bodies may be before they count as one test. 0.92 is deliberately high --
# tests for one criterion legitimately share scaffolding, and the target is copy-paste-with-a-renamed
# -variable, not family resemblance.
MAX_TEST_BODY_SIMILARITY = float(os.environ.get("MAX_TEST_BODY_SIMILARITY", "0.92"))

_ASSERTION_RE = re.compile(
    r"(?:expect|assert|Assert\.\w+|should)\s*\(\s*([^;\n]{3,120}?)\s*\)",
    re.IGNORECASE,
)
# What counts as "a line declaring a test". Two families, because two very different things are:
#
#   JS/TS:  test('...'), it('...'), describe('...')          -- keyword, then the name as a string
#   C#:     [Fact] on one line, `public void TestUS00012...` on the NEXT
#
# The C# half is why the second alternative exists. `\b(test|...)\b` cannot match inside
# `TestUS00012ResolveStateDirectory` (no word boundary between "Test" and "US"), and the `[Fact]`
# line carries no criterion id -- so a file holding 14 real tests scored ZERO for every criterion.
# Measured live on apps/api.Tests/CounterApiIntegrationTests.cs.
_TEST_DECL_RE = re.compile(
    r"\b(test|it|Fact|Theory|describe)\b"
    r"|\b(?:public|internal|private)\s+(?:async\s+)?[\w<>\[\],\s]+?\s+\w+\s*\(",
    re.IGNORECASE,
)


def _normalise_assertion(target: str) -> str:
    """Collapse whitespace, quotes and numeric literals so `expect(count).toBe(1)` and
    `expect(count).toBe(2)` read as ONE assertion target -- they exercise the same expression."""
    collapsed = re.sub(r"\s+", "", target)
    collapsed = re.sub(r"[\"']", "", collapsed)
    return re.sub(r"\d+", "N", collapsed).lower()


def distinct_assertion_targets(ac_id: str, test_files: dict[str, str]) -> set[str]:
    """The distinct expressions asserted by tests naming this AC. Pure.

    Scoped to the lines following each test declaration that names the AC, so assertions belonging
    to a DIFFERENT criterion in the same file are not credited to this one.
    """
    targets: set[str] = set()
    variants = id_variants(ac_id)
    for contents in test_files.values():
        lines = contents.splitlines()
        inside = False
        for line in lines:
            if _TEST_DECL_RE.search(line):
                # A new test declaration ends the previous test's scope.
                inside = any(variant in line for variant in variants)
            if inside:
                for match in _ASSERTION_RE.finditer(line):
                    normalised = _normalise_assertion(match.group(1))
                    if normalised:
                        targets.add(normalised)
    return targets


def _test_bodies(ac_id: str, test_files: dict[str, str]) -> list[str]:
    """Each test body (as normalised text) belonging to tests that name this AC."""
    bodies: list[str] = []
    variants = id_variants(ac_id)
    for contents in test_files.values():
        current: list[str] | None = None
        for line in contents.splitlines():
            if _TEST_DECL_RE.search(line):
                if current:
                    bodies.append("\n".join(current))
                if any(variant in line for variant in variants):
                    # Seed with whatever follows the opening brace, so a one-line test
                    # (`public void X(){ Assert.Equal(1, c.Value); }`) has a body at all -- without
                    # this its assertion was invisible and it counted as a non-asserting stub.
                    # Deliberately NOT the whole line: the test NAME must stay out of the body, or
                    # two identical clones with different names stop looking like duplicates.
                    inline = line.split("{", 1)[1] if "{" in line else ""
                    current = [inline.strip()] if inline.strip() else []
                else:
                    current = None
            elif current is not None:
                current.append(line.strip())
        if current:
            bodies.append("\n".join(current))
    return [re.sub(r"\s+", " ", body).strip() for body in bodies if body.strip()]


def asserting_test_count(ac_id: str, test_files: dict[str, str]) -> int:
    """How many of this AC's tests contain at least one assertion. Pure.

    This is the denominator the diversity check must use, not the raw test count. At the RED phase
    the generated .NET tests are deliberately `throw new NotImplementedException("US-0001.1: ...")`
    stubs -- they assert nothing, because their job is to fail until the code exists. Counting them
    made a criterion look like "5 tests, 1 distinct assertion target" (the single target coming from
    the one real Playwright `expect`), and the run escalated after exhausting all six verify cycles
    on work that was correct for its phase.
    """
    return sum(1 for body in _test_bodies(ac_id, test_files) if _ASSERTION_RE.search(body))


def duplicate_test_bodies(ac_id: str, test_files: dict[str, str]) -> int:
    """How many of this AC's tests are near-duplicates of an earlier one. Pure.

    difflib rather than jscpd: jscpd runs in the sandbox over the whole repo and reports a repo-wide
    percentage, which cannot answer "are THIS criterion's three tests actually three tests".
    """
    bodies = _test_bodies(ac_id, test_files)
    duplicates = 0
    kept: list[str] = []
    for body in bodies:
        if any(SequenceMatcher(None, body, seen).ratio() >= MAX_TEST_BODY_SIMILARITY for seen in kept):
            duplicates += 1
        else:
            kept.append(body)
    return duplicates


def category_spread(content_dict: dict[str, Any], ac_id: str) -> set[str]:
    """The test categories the stage itself claims for this AC, from its own coverage_plan."""
    plan = ((content_dict or {}).get("test_suite") or {}).get("coverage_plan") or []
    categories: set[str] = set()
    for entry in plan:
        if str(entry.get("ac_id")) != ac_id:
            continue
        for key in ("categories", "category"):
            value = entry.get(key)
            if isinstance(value, str):
                categories.add(value)
            elif isinstance(value, list):
                categories.update(str(v) for v in value)
    return {c for c in categories if c}


def depth_shortfalls(
    counts: dict[str, dict[str, int]],
    ui_relevant: set[str],
    min_non_e2e: int = MIN_NON_E2E_TESTS_PER_AC,
    test_files: dict[str, str] | None = None,
    content_dict: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Per AC, the depth requirements it fails. Pure, so the thresholds are testable without a repo.

    `test_files`/`content_dict` are optional: without them only the count-based checks run, which is
    what the caller does when it could not read the test files at all.
    """
    shortfalls: dict[str, list[str]] = {}
    for ac, per_level in counts.items():
        problems: list[str] = []
        non_e2e = per_level["unit"] + per_level["integration"]
        total_tests = non_e2e + per_level["e2e"]
        if non_e2e < min_non_e2e:
            problems.append(
                f"only {non_e2e} test(s) below the browser layer (need {min_non_e2e}: unit and/or "
                f"integration -- a browser test cannot prove a rule beneath the UI)"
            )
        if ac in ui_relevant and per_level["e2e"] < 1:
            problems.append("no end-to-end test, and this criterion is user-facing")

        # Anti-padding, only where there are tests to inspect: an AC with no tests already failed
        # above, and reporting "0 distinct assertions" alongside that is noise.
        # Counted over tests that ACTUALLY ASSERT, not all tests: RED-phase stubs assert nothing by
        # design, and including them turns correct TDD work into a padding finding.
        asserting = asserting_test_count(ac, test_files) if test_files else 0
        if asserting >= MIN_TESTS_BEFORE_ASSERTION_CHECK:
            targets = distinct_assertion_targets(ac, test_files)
            if len(targets) < MIN_DISTINCT_ASSERTIONS_PER_AC:
                problems.append(
                    f"{asserting} asserting test(s) but only {len(targets)} distinct assertion "
                    f"target(s) (need {MIN_DISTINCT_ASSERTIONS_PER_AC}) -- tests asserting the same "
                    f"expression with a different literal are one test, not several"
                )
            duplicates = duplicate_test_bodies(ac, test_files)
            if duplicates:
                problems.append(
                    f"{duplicates} of its test(s) are near-duplicate bodies of another test for the "
                    f"same criterion (>= {int(MAX_TEST_BODY_SIMILARITY * 100)}% similar)"
                )

        if content_dict is not None and total_tests > 0:
            categories = category_spread(content_dict, ac)
            if categories and categories <= {"happy_path"}:
                problems.append(
                    "every declared test category is happy_path -- a criterion needs at least one "
                    "negative, edge or adversarial case to be considered tested"
                )

        if problems:
            shortfalls[ac] = problems
    return shortfalls


def _ui_relevant_ac_ids(content_dict: dict[str, Any], active_ac_ids: list[str]) -> set[str]:
    """ACs the stage itself marked user-facing, from its own coverage_plan.

    The model's `ui_relevant` flag is used here rather than a guess from the AC text: it already has
    to decide this to choose a test kind, and requiring an e2e test for a criterion nobody considers
    user-facing would force browser tests onto pure calculation rules.
    """
    plan = ((content_dict or {}).get("test_suite") or {}).get("coverage_plan") or []
    flagged = {str(entry.get("ac_id")) for entry in plan if entry.get("ui_relevant")}
    return {ac for ac in active_ac_ids if ac in flagged}


@dataclass(frozen=True)
class AcCoverageOutcome:
    passed: bool
    feedback: str
    report: dict[str, Any]


async def check_ac_coverage(provider: SandboxProvider, thread_id: str, content_dict: dict[str, Any]) -> AcCoverageOutcome:
    raw_ledger = await repo_files.read_repo_file(provider, thread_id, LEDGER_PATH)
    active_ac_ids: list[str] = []
    if raw_ledger is not None:
        try:
            entries = json.loads(raw_ledger).get("entries", [])
            active_ac_ids = [
                e["id"] for e in entries if e.get("kind") == "acceptance_criterion" and e.get("status") in ("active", "revised")
            ]
        except json.JSONDecodeError:
            pass

    if not active_ac_ids:
        return AcCoverageOutcome(
            passed=False,
            feedback=".ai-dev-workflow/spec/ledger.json has no active Acceptance Criteria -- P2 must be approved with real ACs before P4 can run.",
            report={},
        )

    # A GHCP session finds every test root and runs it, teeing the complete console output to a
    # file this gate then reads. Replaces "an audit model guesses a test command, Python execs it"
    # -- that guess kept running the wrong tool from the wrong directory on generated monorepos,
    # producing an MSB1003-style error instead of any test output, which read here as "no AC is
    # covered" and deadlocked the stage at its verify cap.
    await provider.exec_in_sandbox(thread_id, f"rm -f {shlex.quote(AC_TEST_OUTPUT_PATH)}")
    run_report = await stack_runner.run_and_report(
        thread_id,
        stage_key="ac-test-run",
        prompt_name="ac_test_run",
        schema=AcTestRunReport,
        output_path=AC_TEST_OUTPUT_PATH,
    )
    output = await repo_files.read_repo_file(provider, thread_id, AC_TEST_OUTPUT_PATH)
    if output is None:
        return AcCoverageOutcome(
            passed=False,
            feedback=(
                "The test suite could not be run, so AC coverage cannot be verified -- this is an "
                f"infra failure, not a coverage gap: {run_report.error or 'no test output was captured'}"
            ),
            report={"infra_error": "test_run_failed", "run_summary": run_report.summary},
        )
    # The suite is expected RED at this stage; exit_ok is the runner's own exit status, which the
    # tree-grep fallback below keys off exactly as the old exec's returncode did.
    result_ok = run_report.exit_ok
    # Strip ANSI color codes -- vitest/jest colorize even without a TTY here, and escapes sitting
    # inside a line break naive marker/path matching.
    lines = [_ANSI_RE.sub("", line) for line in output.splitlines()]

    # Per AC id: does any line naming it look like a FAIL, a PASS, or neither (ambiguous/no match).
    # Matched by substring against the ledger's OWN ids, never a hardcoded id-format regex --
    # observed live (headless run 3): the ledger mints US-0001.1-style ids while an AC-\d{4}
    # regex found nothing, so every AC read as uncovered and the stage deadlocked at the cap.
    ac_line_status: dict[str, str] = {}
    variants_by_id = {ac_id: id_variants(ac_id) for ac_id in active_ac_ids}
    for line in lines:
        ac_ids_in_line = {ac_id for ac_id, variants in variants_by_id.items() if any(v in line for v in variants)}
        if not ac_ids_in_line:
            continue
        lowered = line.lower()
        is_fail = _has_marker(line, _FAIL_MARKERS)
        is_pass = _has_marker(line, _PASS_MARKERS)
        for ac_id in ac_ids_in_line:
            if is_fail:
                ac_line_status[ac_id] = "fail"
            elif is_pass and ac_line_status.get(ac_id) != "fail":
                ac_line_status[ac_id] = "pass"
            elif ac_id not in ac_line_status:
                ac_line_status[ac_id] = "unknown"

    missing = [ac for ac in active_ac_ids if ac not in ac_line_status]

    # Primary fallback for anything the per-line scan missed, and only while the suite as a
    # whole is RED (result not ok -- in this stage's TDD-red contract it always is): an id
    # embedded in ANY test file in the tree counts as covered-and-failing. Runner reporters
    # differ in which per-file/per-test lines they print (observed live, run 9: one file's ids
    # never appeared in the output the gate captured while a replay saw them) -- what actually
    # matters, "the test exists and nothing is green", is checkable from the tree + exit code
    # without trusting reporter formatting at all. Tautological (green) ids can't hide here:
    # a green test printed its ✓ line and was classified "pass" above.
    if missing and not result_ok:
        # Piped into xargs, not interpolated: `git ls-files -co` lists UNTRACKED files too, so a
        # vendored directory (a browser npm downloaded into apps/web, say) can contribute thousands
        # of /test|spec/ matches and push a single command line past the OS argv limit -- which
        # killed metrics_compute's identical scan with "[WinError 206] The filename or extension is
        # too long". Fixed here as well because the bug is the pattern, not the one call site.
        id_patterns = " ".join(f"-e {shlex.quote(v)}" for ac in missing for v in variants_by_id[ac])
        excluded = "/(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|TestResults|coverage)/"
        grep = await provider.exec_in_sandbox(
            thread_id,
            "git ls-files -co --exclude-standard "
            "| grep -iE '(test|spec)' "
            f"| grep -vE {shlex.quote(excluded)} "
            f"| xargs -r -d '\\n' grep -h -o -F {id_patterns} -- 2>/dev/null | sort -u || true",
        )
        found_tokens = set((grep.stdout or "").split())
        for ac in missing:
            if found_tokens & set(variants_by_id[ac]):
                ac_line_status[ac] = "fail"
        missing = [ac for ac in active_ac_ids if ac not in ac_line_status]

    tautological = [ac for ac in active_ac_ids if ac_line_status.get(ac) == "pass"]

    # DEPTH: a criterion with one happy-path assertion is not a tested criterion. Read the test files
    # themselves rather than the runner output, because levels are decided by content (an integration
    # test is one that stands up a real host -- see _INTEGRATION_SYMBOLS) and .NET keeps every level
    # in one project, so a path proves nothing except e2e.
    depth_report: dict[str, Any] = {}
    listing = await provider.exec_in_sandbox(
        thread_id,
        "git ls-files -co --exclude-standard | grep -iE '(test|spec)' "
        r"| grep -vE '(^|/)(node_modules|\.playwright-browsers|bin|obj|dist|build|\.next|\.venv|vendor|TestResults|coverage|\.ai-dev-workflow|agent-work)/' "
        "| head -60 || true",
    )
    test_paths = [line.strip() for line in (listing.stdout or "").splitlines() if line.strip()]
    test_files: dict[str, str] = {}
    for path in test_paths:
        contents = await repo_files.read_repo_file(provider, thread_id, path)
        if contents is not None:
            test_files[path] = contents
    depth_shortfall: dict[str, list[str]] = {}
    if test_files:
        counts = count_tests_per_ac(active_ac_ids, test_files)
        ui_relevant = _ui_relevant_ac_ids(content_dict, active_ac_ids)
        # RED-phase threshold: this gate runs at ac-to-tests, before any implementation exists.
        # See MIN_NON_E2E_TESTS_PER_AC_RED for why it is not the full requirement.
        depth_shortfall = depth_shortfalls(
            counts,
            ui_relevant,
            min_non_e2e=MIN_NON_E2E_TESTS_PER_AC_RED,
            test_files=test_files,
            content_dict=content_dict,
        )
        depth_report = {"counts_per_ac": counts, "ui_relevant": sorted(ui_relevant)}

    if missing or tautological or depth_shortfall:
        reasons = []
        if depth_shortfall:
            reasons.append(
                "these ACs are not tested deeply enough: "
                + "; ".join(f"{ac}: {' and '.join(problems)}" for ac, problems in sorted(depth_shortfall.items()))
            )
        if missing:
            reasons.append(f"no test found covering: {missing}")
        if tautological:
            reasons.append(
                f"these ACs' tests are already PASSING with no implementation yet, which almost "
                f"certainly means they're tautological (assertion-free or trivially true): {tautological}"
            )
        return AcCoverageOutcome(
            passed=False,
            feedback="; ".join(reasons),
            report={
                "missing": missing,
                "tautological": tautological,
                "depth": depth_report,
                "depth_shortfall": depth_shortfall,
                "active_ac_ids": active_ac_ids,
                # Diagnostics: enough to reconstruct WHY the scan missed an id without rerunning.
                "runner_exit_ok": result_ok,
                "failed_files_seen": _extract_failed_files(lines),
                "output_tail": "\n".join(lines[-40:]),
            },
        )

    return AcCoverageOutcome(
        passed=True,
        feedback=f"All {len(active_ac_ids)} active AC(s) have a covering test, correctly failing pre-implementation.",
        report={"active_ac_ids": active_ac_ids},
    )


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.ac_coverage_gate`."""
    # Level classification: e2e by PATH (the only level a path proves), integration by SYMBOL,
    # because .NET keeps unit and integration tests in one project and often one file.
    assert classify_test_level("apps/web/tests/e2e/a.spec.ts", "x") == "e2e"
    assert classify_test_level("apps/api.Tests/T.cs", "new WebApplicationFactory<Program>();") == "integration"
    assert classify_test_level("apps/api.Tests/T.cs", "Assert.Equal(1, s.Value);") == "unit"

    # Counting is per TEST, not per file: one file routinely holds several tests for one criterion.
    files = {
        "apps/api.Tests/CounterTests.cs":
            "[Fact] public void Test_US_0001_1_A(){}\n[Fact] public void Test_US_0001_1_B(){}",
        "apps/api.Tests/IntegrationTests.cs":
            "new WebApplicationFactory<Program>();\n[Fact] public void Test_US_0002_1_C(){}",
        "apps/web/tests/e2e/a.spec.ts": "test('[US-0001.1] loads', async () => {});",
    }
    counts = count_tests_per_ac(["US-0001.1", "US-0002.1"], files)
    assert counts["US-0001.1"] == {"unit": 2, "integration": 0, "e2e": 1}, counts
    assert counts["US-0002.1"] == {"unit": 0, "integration": 1, "e2e": 0}, counts

    # US-0001.1 satisfies both thresholds; US-0002.1 fails depth AND has no browser test.
    short = depth_shortfalls(counts, ui_relevant={"US-0001.1", "US-0002.1"})
    assert "US-0001.1" not in short
    assert len(short["US-0002.1"]) == 2, short
    # A non-user-facing AC is NOT required to have an e2e test -- otherwise every calculation rule
    # would be forced through a browser.
    assert len(depth_shortfalls(counts, ui_relevant=set())["US-0002.1"]) == 1

    # The e2e requirement follows the stage's own ui_relevant flag, not a guess from the AC text.
    plan = {"test_suite": {"coverage_plan": [
        {"ac_id": "US-0001.1", "ui_relevant": True},
        {"ac_id": "US-0002.1", "ui_relevant": False},
    ]}}
    assert _ui_relevant_ac_ids(plan, ["US-0001.1", "US-0002.1"]) == {"US-0001.1"}

    # The .NET shape this pipeline actually generates: `[Fact]` on one line, the criterion id inside
    # a PascalCase method name on the next, with all punctuation stripped. Every part of this was
    # invisible to the counter until both the compact id variant and the method-declaration pattern
    # existed -- a real file with 14 tests scored 0 for every criterion, so the depth gate would have
    # demanded tests that were already written.
    dotnet_real = {
        "apps/api.Tests/CounterApiIntegrationTests.cs": (
            "using Microsoft.AspNetCore.Mvc.Testing;\n"
            "public class CounterApiIntegrationTests : IClassFixture<WebApplicationFactory<Program>> {\n"
            "    [Fact]\n"
            "    public void TestUS00011ResolveStateDirectoryFallsBackToAppData() {\n"
            "        Assert.Equal(expected, actual.Path);\n"
            "    }\n"
            "    [Fact]\n"
            "    public void TestUS00012ResolveStateDirectoryUsesExplicitSetting() {\n"
            "        Assert.True(result.IsExplicit);\n"
            "    }\n"
            "}\n"
        )
    }
    dotnet_counts = count_tests_per_ac(["US-0001.1", "US-0001.2"], dotnet_real)
    assert dotnet_counts["US-0001.1"]["integration"] == 1, dotnet_counts
    assert dotnet_counts["US-0001.2"]["integration"] == 1, dotnet_counts
    assert id_variants("US-0001.2") == ["AC-0001.2", "AC00012", "AC_0001_2", "US-0001.2", "US00012", "US_0001_2"], (
        id_variants("US-0001.2")
    )

    # --- anti-padding ---------------------------------------------------------------------------
    # Three tests, one assertion target: the same expression with a different literal each time.
    # This is the shape a count-only gate rewards, so it must NOT pass.
    padded = {
        "t/PadTests.cs": (
            "[Fact] public void Test_US_0009_1_A(){ Assert.Equal(1, counter.Value); }\n"
            "[Fact] public void Test_US_0009_1_B(){ Assert.Equal(2, counter.Value); }\n"
            "[Fact] public void Test_US_0009_1_C(){ Assert.Equal(3, counter.Value); }\n"
        )
    }
    assert len(distinct_assertion_targets("US-0009.1", padded)) == 1, distinct_assertion_targets("US-0009.1", padded)
    padded_counts = count_tests_per_ac(["US-0009.1"], padded)
    padded_short = depth_shortfalls(padded_counts, ui_relevant=set(), test_files=padded)
    assert any("distinct assertion target" in p for p in padded_short["US-0009.1"]), padded_short

    # TWO value-based tests on one criterion must NOT be called padding. Literals are normalised, so
    # "increment shows 1" and "decrement shows 0" collapse to a single assertion target -- and that
    # is the correct way to test a counter, not a trick. Below MIN_TESTS_BEFORE_ASSERTION_CHECK the
    # count thresholds govern instead.
    value_based = {
        "t/CounterTests.cs": (
            "[Fact] public void TestUS00021IncrementShowsOne(){ Assert.Equal(1, c.Value); }\n"
            "[Fact] public void TestUS00021DecrementShowsZero(){ Assert.Equal(0, c.Value); }\n"
        )
    }
    assert len(distinct_assertion_targets("US-0002.1", value_based)) == 1  # one target, by design
    value_counts = count_tests_per_ac(["US-0002.1"], value_based)
    assert value_counts["US-0002.1"]["unit"] == 2, value_counts
    value_short = depth_shortfalls(value_counts, ui_relevant=set(), test_files=value_based)
    assert not any("distinct assertion" in p for p in value_short.get("US-0002.1", [])), value_short
    # RED-phase stubs must NOT read as padding. These are the real generated shapes from a live run
    # that escalated on this exact case: four .NET tests that throw NotImplementedException (no
    # assertions at all) plus one Playwright test with a single real expect. "5 tests, 1 distinct
    # target" looked like padding and was correct TDD for its phase.
    red_phase = {
        "apps/api.Tests/CounterApiIntegrationTests.cs": (
            "public class T : IClassFixture<WebApplicationFactory<Program>> {\n"
            "    [Fact]\n    public void Test_US_0001_1_CountReadIsReturned() {\n"
            '        throw new NotImplementedException("US-0001.1: GET /counter not implemented yet.");\n    }\n'
            "    [Fact]\n    public void Test_US_0001_1_NumericContract() {\n"
            '        throw new NotImplementedException("US-0001.1: contract validation not implemented yet.");\n    }\n'
            "    [Fact]\n    public void Test_US_0001_1_ThirdStub() {\n"
            '        throw new NotImplementedException("US-0001.1: not implemented yet.");\n    }\n}\n'
        ),
        "apps/web/tests/e2e/counter.spec.ts": (
            "test('[US-0001.1] loads default', async ({ page }) => {\n"
            "  await expect(page.getByTestId('counter-value')).toHaveText('0');\n});\n"
        ),
    }
    assert asserting_test_count("US-0001.1", red_phase) == 1, asserting_test_count("US-0001.1", red_phase)
    red_counts = count_tests_per_ac(["US-0001.1"], red_phase)
    assert red_counts["US-0001.1"]["integration"] + red_counts["US-0001.1"]["e2e"] >= 4, red_counts
    red_short = depth_shortfalls(
        red_counts, ui_relevant={"US-0001.1"}, test_files=red_phase
    )
    assert not any("distinct assertion" in p for p in red_short.get("US-0001.1", [])), red_short

    # Add a third test in the same shape and the check DOES fire -- three tests that are one test.
    padded_three = {
        "t/CounterTests.cs": value_based["t/CounterTests.cs"]
        + "[Fact] public void TestUS00021AlsoShowsTwo(){ Assert.Equal(2, c.Value); }\n"
    }
    three_short = depth_shortfalls(
        count_tests_per_ac(["US-0002.1"], padded_three), ui_relevant=set(), test_files=padded_three
    )
    assert any("distinct assertion" in p for p in three_short["US-0002.1"]), three_short

    # Genuinely different assertions on the same criterion PASS -- the check must not punish real
    # tests that happen to share a criterion.
    real = {
        "t/RealTests.cs": (
            "[Fact] public void Test_US_0010_1_A(){ Assert.Equal(1, counter.Value); }\n"
            "[Fact] public void Test_US_0010_1_B(){ Assert.True(counter.CanReset); }\n"
        )
    }
    assert len(distinct_assertion_targets("US-0010.1", real)) == 2
    real_short = depth_shortfalls(count_tests_per_ac(["US-0010.1"], real), ui_relevant=set(), test_files=real)
    assert not any("distinct assertion" in p for p in real_short.get("US-0010.1", [])), real_short

    # Copy-pasted bodies count once. Identical bodies under two names is the cheapest padding there
    # is, and the count-based check alone cannot see it.
    cloned = {
        "t/CloneTests.cs": (
            "[Fact] public void Test_US_0011_1_A(){\n  var c = new Counter();\n  c.Increment();\n  Assert.Equal(1, c.Value);\n}\n"
            "[Fact] public void Test_US_0011_1_B(){\n  var c = new Counter();\n  c.Increment();\n  Assert.Equal(1, c.Value);\n}\n"
        )
    }
    assert duplicate_test_bodies("US-0011.1", cloned) == 1, duplicate_test_bodies("US-0011.1", cloned)
    assert duplicate_test_bodies("US-0010.1", real) == 0

    # Category spread: all-happy_path is flagged; a mixed set is not.
    happy_only = {"test_suite": {"coverage_plan": [{"ac_id": "US-0012.1", "categories": ["happy_path"]}]}}
    mixed = {"test_suite": {"coverage_plan": [{"ac_id": "US-0012.1", "categories": ["happy_path", "negative"]}]}}
    spread_files = {
        "t/SpreadTests.cs": (
            "[Fact] public void Test_US_0012_1_A(){ Assert.Equal(1, c.Value); }\n"
            "[Fact] public void Test_US_0012_1_B(){ Assert.True(c.Ok); }\n"
        )
    }
    spread_counts = count_tests_per_ac(["US-0012.1"], spread_files)
    flagged = depth_shortfalls(spread_counts, set(), test_files=spread_files, content_dict=happy_only)
    assert any("happy_path" in p for p in flagged["US-0012.1"]), flagged
    unflagged = depth_shortfalls(spread_counts, set(), test_files=spread_files, content_dict=mixed)
    assert not any("happy_path" in p for p in unflagged.get("US-0012.1", [])), unflagged

    # With no test files readable, only the count-based checks run -- an unreadable worktree must not
    # manufacture assertion-padding findings.
    assert not any(
        "distinct assertion" in p for p in depth_shortfalls(padded_counts, set()).get("US-0009.1", [])
    )

    # min_non_e2e=0 (the RED-phase setting) disables the below-browser requirement entirely, while
    # the e2e requirement for a user-facing criterion still applies. Both halves asserted, because
    # the whole point of the phase split is that one relaxes and the other does not.
    e2e_only_counts = {"US-0013.1": {"unit": 0, "integration": 0, "e2e": 1}}
    assert depth_shortfalls(e2e_only_counts, ui_relevant={"US-0013.1"}, min_non_e2e=0) == {}
    assert "below the browser layer" in depth_shortfalls(
        e2e_only_counts, ui_relevant=set(), min_non_e2e=2
    )["US-0013.1"][0]
    no_e2e_counts = {"US-0013.2": {"unit": 3, "integration": 0, "e2e": 0}}
    assert "no end-to-end test" in depth_shortfalls(
        no_e2e_counts, ui_relevant={"US-0013.2"}, min_non_e2e=0
    )["US-0013.2"][0]

    print("ac_coverage_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
