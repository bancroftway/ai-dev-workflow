"""P4's AC-coverage half of the deterministic_verify gate: every active Acceptance Criterion in
.ai-dev-workflow/spec/ledger.json must have at least one test whose name embeds its AC id, and that test must
currently be FAILING (a passing "new" test before any implementation exists is almost certainly
tautological -- this is TDD's RED step, checked mechanically rather than trusted to the model's
own self-report).

Pass/fail now comes from the runners' own STRUCTURED reports (`.trx`, vitest/jest JSON, Playwright
JSON) via `status_from_structured_reports`. Console-text scraping remains only as a fallback for a
suite whose runner offers no machine-readable reporter, and whatever the structured reports decide is
final -- the text pass cannot overwrite it.

That ordering is the fix for a specific, real defect this module's docstring used to disclaim: a stub
named `Test_US_0003_1_..._WhenCapacityOverlapDuplicateAndWeeklyMaxRulesPass_Succeeds` threw
NotImplementedException, so it was correctly RED, but "RulesPass" made the text-marker match read it
as PASSING -- the gate then rejected it as tautological and no redraft could ever fix it. A `.trx`
carries `outcome="Failed"` and cannot be misread that way. Console output has a layout; a report has
a schema.

Attribution (which test covers which criterion) prefers the canonical `[US-0001.2]` id in the test's
DISPLAY name, which every runner reports verbatim; see `test_results.attributed_ac_ids` for why that
beats both a mangled method name and an xUnit `[Trait]` (whose value never reaches the trx at all).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .. import repo_files, stack_runner, tech_stack_signals, test_results, workflow_persistence
from .write_scope_gate import _E2E_PATH_RE
from ..sandbox.provider import SandboxProvider
from ..schemas import StageReport
from ..spec_ledger import LEDGER_PATH, own_ac_ids_from_specification

logger = logging.getLogger(__name__)

# Where the test-run agent tees the suite's complete console output for this gate to read.
AC_TEST_OUTPUT_PATH = "agent-work/ac-test-output.txt"


class AcTestRunReport(StageReport):
    """What the test-run agent must report (prompts/ac_test_run.md)."""

    output_artifact: str = ""
    exit_ok: bool = False
    result_artifacts: list[str] = []
    """Machine-readable runner reports (.trx / vitest-json / playwright-json).

    These are preferred over `output_artifact` for deciding which tests passed. Console text has a
    LAYOUT, not a schema: deciding pass/fail from it means matching words like "pass" in lines that
    also contain test names, and a test called `...RulesPass_Succeeds` was once read as passing when
    it was a stub throwing NotImplementedException. A `.trx` says `outcome="Failed"`.
    """


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
            # One general matcher, not the enumerated `id_variants` list. The list held six
            # spellings, each added reactively after a run had already reported "0 tests" for a
            # criterion that was tested -- a naming convention nobody controls cannot be covered by
            # enumeration. `id_variants` survives only for the sandbox grep, which needs literal
            # strings to pass to `grep -F`.
            named = set(test_results.ac_ids_in_name(line))
            for ac in ac_ids:
                if ac in named:
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

# The subset that actually carries a test NAME: a `test(...)`/`it(...)`/`describe(...)` call, or a
# method signature. A bare `[Fact]` / `[Theory]` attribute line matches _TEST_DECL_RE but names
# nothing -- it is the line ABOVE the name in every generated .NET suite.
_NAMED_TEST_DECL_RE = re.compile(
    r"\b(?:test|it|describe)\s*\("
    r"|\b(?:public|internal|private)\s+(?:async\s+)?[\w<>\[\],\s]+?\s+\w+\s*\(",
    re.IGNORECASE,
)

# How each xUnit-family framework MARKS a method as a test. Used to tell a test from a helper:
# without it, a constructor or a Dispose reads as an unnamed test.
_TEST_ATTRIBUTE_RE = re.compile(r"^\s*\[\s*(Fact|Theory|Test|TestMethod|TestCase)\b", re.IGNORECASE)
_JS_TEST_CALL_RE = re.compile(r"\b(?:test|it)\s*(?:\.\w+)?\s*\(\s*['\"`]", re.IGNORECASE)


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


def _tests_for_ac(ac_id: str, test_files: dict[str, str]) -> list[tuple[str, str]]:
    """(label, normalised body) per test naming this AC. The label ("path :: decl line") exists so
    a failed check can NAME the test to rewrite -- observed live: feedback that only counted
    near-duplicates sent the model rewriting the Playwright spec for 6 laps while the duplicate
    pair sat in the .NET unit file."""
    tests: list[tuple[str, str]] = []
    variants = id_variants(ac_id)
    for path, contents in test_files.items():
        current: list[str] | None = None
        decl = ""
        for line in contents.splitlines():
            if _TEST_DECL_RE.search(line):
                if current:
                    tests.append((decl, "\n".join(current)))
                if any(variant in line for variant in variants):
                    # Seed with whatever follows the opening brace, so a one-line test
                    # (`public void X(){ Assert.Equal(1, c.Value); }`) has a body at all -- without
                    # this its assertion was invisible and it counted as a non-asserting stub.
                    # Deliberately NOT the whole line: the test NAME must stay out of the body, or
                    # two identical clones with different names stop looking like duplicates.
                    inline = line.split("{", 1)[1] if "{" in line else ""
                    current = [inline.strip()] if inline.strip() else []
                    decl = f"{path} :: {line.strip()[:100]}"
                else:
                    current = None
            elif current is not None:
                current.append(line.strip())
        if current:
            tests.append((decl, "\n".join(current)))
    return [
        (decl, re.sub(r"\s+", " ", body).strip())
        for decl, body in tests
        if body.strip()
    ]


def _test_bodies(ac_id: str, test_files: dict[str, str]) -> list[str]:
    """Each test body (as normalised text) belonging to tests that name this AC."""
    return [body for _, body in _tests_for_ac(ac_id, test_files)]


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
    return len(duplicate_test_pairs(ac_id, test_files))


def duplicate_test_pairs(ac_id: str, test_files: dict[str, str]) -> list[tuple[str, str]]:
    """(duplicate test label, original test label) per near-duplicate, so feedback names both."""
    pairs: list[tuple[str, str]] = []
    kept: list[tuple[str, str]] = []
    for decl, body in _tests_for_ac(ac_id, test_files):
        original = next(
            (k_decl for k_decl, k_body in kept
             if SequenceMatcher(None, body, k_body).ratio() >= MAX_TEST_BODY_SIMILARITY),
            None,
        )
        if original is not None:
            pairs.append((decl, original))
        else:
            kept.append((decl, body))
    return pairs


# A fiat-failure call: an assertion that fails unconditionally. Full-call patterns, case-sensitive
# on each language's own keyword casing, so a REAL assertion whose message merely mentions "false"
# is not caught.
_FIAT_FAIL_RE = re.compile(
    r"Assert\s*\.\s*(?:Is)?True\(\s*false\b[^)]*\)"      # xunit/nunit/mstest Assert.True(false, ...)
    r"|Assert\s*\.\s*Fail\s*\("                          # Assert.Fail("not implemented")
    r"|expect\(\s*true\s*\)\s*\.\s*toBe\(\s*false\s*\)"  # jest/vitest fiat
    r"|\b(?:expect|assert)\s*\.\s*fail\s*\("             # chai/node/vitest expect.fail()
    r"|pytest\s*\.\s*fail\s*\("                          # pytest.fail("...")
    r"|(?<![\w.])assert\s+False\b"                       # bare python assert False
)


def fiat_stub_tests(ac_id: str, test_files: dict[str, str]) -> int:
    """How many of this AC's tests fail by fiat -- their only assertion is an unconditional failure.

    Distinct from the assertion-FREE stub asserting_test_count already tolerates: an empty skeleton
    is the documented RED-phase form, while `Assert.True(false, "RED: ...")` is red paint -- it makes
    the suite fail without encoding any behavior, pads the depth counts, and (being a one-liner)
    trips the near-duplicate check with feedback the drafting model has proven unable to act on
    (observed live: 96 fiat stubs, 6 laps burned re-wording two stub messages). A body with at
    least one real assertion alongside a fiat one is NOT counted -- e.g. Assert.Fail inside a catch
    block guarding a genuine act-assert path.
    """
    return len(fiat_stub_labels(ac_id, test_files))


def fiat_stub_labels(ac_id: str, test_files: dict[str, str]) -> list[str]:
    """The fiat-failing tests' labels ("path :: decl line"), so feedback names what to rewrite."""
    labels: list[str] = []
    for decl, body in _tests_for_ac(ac_id, test_files):
        if not _FIAT_FAIL_RE.search(body):
            continue
        if not _ASSERTION_RE.search(_FIAT_FAIL_RE.sub("", body)):
            labels.append(decl)
    return labels


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


def status_from_structured_reports(
    ac_ids: list[str], reports: dict[str, str]
) -> tuple[dict[str, str], dict[str, int]]:
    """`({ac_id -> 'pass'|'fail'}, attribution tally)` from runner reports keyed by path. Pure.

    The authoritative path. `reports` maps a file path to its contents; the format is chosen by
    extension, so an unrecognised file is skipped rather than guessed at. An AC fails if ANY test
    attributed to it failed -- the same rule the eval layer applies, for the same reason.

    Replaces deciding pass/fail from console text, which the module docstring has long flagged as a
    known shortcut. That shortcut had a real failure: a stub named
    `Test_US_0003_1_..._WhenCapacityOverlapDuplicateAndWeeklyMaxRulesPass_Succeeds` threw
    NotImplementedException -- correctly RED -- but "RulesPass" made a marker match classify it as
    PASSING, so the gate rejected it as tautological and no redraft could fix it. A `.trx` carries
    `outcome="Failed"` and cannot be misread that way.
    """
    known = set(ac_ids)
    per_ac: dict[str, str] = {}
    tally = {"canonical": 0, "fallback": 0, "unattributed": 0}
    for path, contents in (reports or {}).items():
        lowered = path.lower()
        if lowered.endswith(".trx"):
            outcomes = test_results.parse_trx(contents)
        elif lowered.endswith(".json"):
            # vitest/jest and playwright both emit JSON with different shapes; try both and take
            # whichever actually parsed, rather than inferring intent from the filename.
            outcomes = test_results.parse_vitest_json(contents) or test_results.playwright_outcomes(contents)
        else:
            continue
        for name, result in outcomes.items():
            ids, mechanism = test_results.attributed_ac_ids(name)
            tally["unattributed" if mechanism == "none" else mechanism] += 1
            for ac_id in ids:
                if ac_id not in known:
                    continue
                if per_ac.get(ac_id) == "fail" or result == "fail":
                    per_ac[ac_id] = "fail"
                else:
                    per_ac[ac_id] = "pass"
    return per_ac, tally


def unattributed_tests(ac_ids: list[str], test_files: dict[str, str]) -> dict[str, int]:
    """Per file, how many test declarations carry NO recognisable AC id. Pure.

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
            # _NAMED_TEST_DECL_RE, not _TEST_DECL_RE: a bare `[Fact]` attribute line matches the
            # latter but carries no name -- it is the line ABOVE the name in every generated .NET
            # suite, and counting it reported a phantom orphan per correctly-named test.
            is_js_test = bool(_JS_TEST_CALL_RE.search(stripped))
            is_attributed_method = attributed_above and bool(_NAMED_TEST_DECL_RE.search(stripped))
            attributed_above = False
            # A method signature with no test attribute above it is a HELPER -- a constructor, a
            # Dispose, a CreateClient factory. Measured on a real suite: counting those reported 4
            # orphans in a file where every actual test was correctly named, which would have sent a
            # redraft chasing an attribution problem that did not exist.
            if not (is_js_test or is_attributed_method):
                continue
            if not (set(test_results.ac_ids_in_name(stripped)) & known):
                unmatched += 1
        if unmatched:
            out[path] = unmatched
    return out


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

        # Fiat-failure stubs are named FIRST and directly: they are what the model actually writes
        # when it wants red without work, and letting them fall through to the near-duplicate check
        # produces feedback about similarity the model answers by re-wording messages forever.
        if test_files:
            stub_labels = fiat_stub_labels(ac, test_files)
            if stub_labels:
                named = "; ".join(stub_labels[:3]) + (
                    f"; and {len(stub_labels) - 3} more" if len(stub_labels) > 3 else ""
                )
                problems.append(
                    f"{len(stub_labels)} of its test(s) fail by fiat (Assert.True(false) / "
                    f"Assert.Fail / expect(true).toBe(false) placeholder bodies): {named} -- "
                    "write the real arrange-act-assert against the not-yet-existing API instead; "
                    "a compile or module-resolution failure is the expected RED signal at this stage"
                )

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
            pairs = duplicate_test_pairs(ac, test_files)
            if pairs:
                named = "; ".join(f"'{dup}' duplicates '{orig}'" for dup, orig in pairs[:3]) + (
                    f"; and {len(pairs) - 3} more" if len(pairs) > 3 else ""
                )
                problems.append(
                    f"{len(pairs)} of its test(s) are near-duplicate bodies of another test for the "
                    f"same criterion (>= {int(MAX_TEST_BODY_SIMILARITY * 100)}% similar): {named} -- "
                    "rewrite the named duplicate to assert a different observable behavior"
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


async def check_ac_coverage(
    provider: SandboxProvider, thread_id: str, content_dict: dict[str, Any], *, chat_provider: str
) -> AcCoverageOutcome:
    """`chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is required,
    keyword-only, no default -- threaded straight through to stack_runner.run_and_report below,
    which now requires it itself; not resolved in here."""
    raw_ledger = await repo_files.read_repo_file(provider, thread_id, LEDGER_PATH)
    active_ac_ids: list[str] = []
    all_ledger_ac_ids: list[str] = []
    if raw_ledger is not None:
        try:
            entries = json.loads(raw_ledger).get("entries", [])
            active_ac_ids = [
                e["id"] for e in entries if e.get("kind") == "acceptance_criterion" and e.get("status") in ("active", "revised")
            ]
            # Every status, retired included -- see unattributed_tests's own call site below (Task
            # 10 sweep item #10): that check's question ("did a real human ever write this AC id
            # anywhere in the ledger") doesn't care whether the AC is still active, unlike every
            # other consumer of active_ac_ids in this function.
            all_ledger_ac_ids = [e["id"] for e in entries if e.get("kind") == "acceptance_criterion"]
        except json.JSONDecodeError:
            pass

    # unattributed_tests (below) reads all_ledger_ac_ids, not active_ac_ids -- see that list's own
    # comment above (review finding on the original Ruling 7 fix, sharpened by Task 10 sweep item
    # #10): that check answers "does this test declaration name ANY real ledger AC, ever" --
    # distinguishing "I could not read this test" from "there is nothing here" -- not "does this
    # test belong to my ticket" or even "is this AC still active". Every other consumer below
    # legitimately wants the ticket-scoped, active-only list; that one call site does not.

    # Ruling 7: scope down to THIS TICKET's own ACs, right at the source, before ANY downstream
    # check (missing/tautological/depth/attribution -- all below -- read active_ac_ids uniformly).
    # Unscoped, this list is the WHOLE PROJECT's ledger -- every ticket ever filed. On a project's
    # second ticket, an earlier ticket's own AC is still "active" and its test is legitimately
    # PASSING (shipped) -- the tautological check further down would then flag it as a fake-green
    # RED-phase test forever, deterministically failing every ticket after the first. Same "this
    # ticket's own AC ids" computation spec_ledger.hydrate_ac_to_tests_ticket_mode_context already
    # uses for the identical question; read from the sandbox's own persisted file (this gate has
    # thread_id/provider, never a GraphState) instead of deriving it a second, possibly-diverging
    # way. Falls back to the old, unscoped list if the approved Specification can't be read/parsed
    # (should not happen -- specification is a hard prerequisite stage -- but an infra hiccup here
    # should not manufacture a NEW false coverage gap on top of it).
    raw_spec = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH)
    own_ac_ids: set[str] = set()
    if raw_spec is not None:
        try:
            own_ac_ids = own_ac_ids_from_specification(json.loads(raw_spec))
        except json.JSONDecodeError:
            pass
    if own_ac_ids:
        active_ac_ids = [ac for ac in active_ac_ids if ac in own_ac_ids]

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
        provider=chat_provider,
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
    # PREFERRED: the runners' own structured reports. Console scraping below is the fallback for a
    # suite whose runner offered no machine-readable reporter.
    structured_reports: dict[str, str] = {}
    for artifact in run_report.result_artifacts or []:
        rel = test_results.repo_relative(artifact)
        if not rel:
            continue
        contents = await repo_files.read_repo_file(provider, thread_id, rel)
        if contents:
            structured_reports[rel] = contents

    ac_line_status: dict[str, str] = {}
    attribution_tally: dict[str, int] = {}
    if structured_reports:
        ac_line_status, attribution_tally = status_from_structured_reports(active_ac_ids, structured_reports)
        logger.info(
            "ac coverage: %d AC status(es) from %d structured report(s) (attribution %s)",
            len(ac_line_status), len(structured_reports), attribution_tally,
        )

    # Whatever the structured reports decided is FINAL -- the console pass below must not overwrite
    # it. A schema'd `outcome="Failed"` beats a line of text that happens to contain the word "pass".
    decided_by_schema = set(ac_line_status)
    variants_by_id = {ac_id: id_variants(ac_id) for ac_id in active_ac_ids}
    for line in lines:
        ac_ids_in_line = {ac_id for ac_id, variants in variants_by_id.items() if any(v in line for v in variants)}
        if not ac_ids_in_line:
            continue
        lowered = line.lower()
        is_fail = _has_marker(line, _FAIL_MARKERS)
        is_pass = _has_marker(line, _PASS_MARKERS)
        for ac_id in ac_ids_in_line - decided_by_schema:
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
        # Attribution health, recorded whether or not the gate blocks. A criterion reported as
        # untested while its file is full of tests nothing could attribute is a PARSE failure, and
        # the feedback has to say so -- telling a model to "add tests for US-0001.1" when it already
        # wrote five is how a stage burns its whole budget.
        orphans = unattributed_tests(all_ledger_ac_ids, test_files)
        if orphans:
            depth_report["unattributed_tests"] = orphans

    if missing or tautological or depth_shortfall:
        reasons = []
        if depth_report.get("unattributed_tests") and missing:
            total_orphans = sum(depth_report["unattributed_tests"].values())
            reasons.append(
                f"ATTRIBUTION WARNING: {total_orphans} test declaration(s) carry no recognisable "
                f"AC id ({', '.join(depth_report['unattributed_tests'])}). Criteria listed below as "
                f"untested may in fact be tested by those. Every test must name its criterion in its "
                f"own name (e.g. `Test_US_0001_2_...`, `TestUS00012...`, or `[US-0001.2] ...` in a "
                f"Playwright title) -- a test that covers a criterion without naming it cannot be "
                f"credited to it"
            )
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
    import asyncio

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

    # Fiat-failure stubs are named directly (the real 2026-08-20 shape: DisplayName attribute line,
    # method on the next, body only Assert.True(false, ...)). A real test alongside is untouched,
    # and a fiat call guarding a genuine assertion does not make a test a stub.
    fiat_files = {
        "apps/api.Tests/tests/T.cs": (
            '[Fact(DisplayName = "[US-0006.1] borrower summary lists books")]\n'
            "public void US_0006_1_Lists() {\n"
            '    Assert.True(false, "RED: not implemented yet.");\n'
            "}\n"
            '[Fact(DisplayName = "[US-0006.1] returned books excluded")]\n'
            "public void US_0006_1_Excluded() {\n"
            "    var summary = service.Summarize(borrower);\n"
            "    Assert.Empty(summary.ActiveLoans);\n"
            '    if (wrong) Assert.Fail("unreachable");\n'
            "}\n"
        )
    }
    labels = fiat_stub_labels("US-0006.1", fiat_files)
    assert len(labels) == 1 and "US_0006_1_Lists" in labels[0], labels

    # Near-duplicate pairs carry both names, so the feedback points at the exact tests to rewrite --
    # count-only feedback sent a live run rewriting the Playwright spec for 6 laps while the
    # duplicate pair sat in the .NET unit file.
    dup_files = {
        "apps/api.Tests/tests/D.cs": (
            "[Fact] public void US_0007_1_A(){ var r = svc.Add(book); Assert.Equal(1, r.Count); }\n"
            "[Fact] public void US_0007_1_B(){ var r = svc.Add(book); Assert.Equal(1, r.Count); }\n"
        )
    }
    pairs = duplicate_test_pairs("US-0007.1", dup_files)
    assert len(pairs) == 1 and "US_0007_1_B" in pairs[0][0] and "US_0007_1_A" in pairs[0][1], pairs

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

    # --- structured reports decide pass/fail; console text is only the fallback -------------------
    # Shape and attribute names taken verbatim from a real `dotnet test --logger trx` run, including
    # the DisplayName-derived testName that carries the canonical id.
    real_trx = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
        '<UnitTestResult testName="[US-0001.2] counter loads persisted value" outcome="Passed" />'
        '<UnitTestResult testName="[US-0003.4] decrement is rejected at zero" outcome="Failed" />'
        '<UnitTestResult testName="Api.Tests.T.TestUS00021IncrementPersists" outcome="Passed" />'
        '<UnitTestResult testName="ProgramConstructorIsReachable" outcome="Passed" />'
        "</Results></TestRun>"
    )
    status, tally = status_from_structured_reports(
        ["US-0001.2", "US-0003.4", "US-0002.1"], {"TR/ac-run.trx": real_trx}
    )
    assert status == {"US-0001.2": "pass", "US-0003.4": "fail", "US-0002.1": "pass"}, status
    # Two canonical display names, one punctuation-stripped method name, one test naming no criterion.
    assert tally == {"canonical": 2, "fallback": 1, "unattributed": 1}, tally

    # The failure the console scraper actually made: a stub whose NAME contains "RulesPass" while its
    # outcome is Failed. A schema cannot be misread that way.
    trap = (
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
        '<UnitTestResult testName="[US-0003.1] AssignStaff_WhenWeeklyMaxRulesPass_Succeeds" outcome="Failed" />'
        "</Results></TestRun>"
    )
    trap_status, _ = status_from_structured_reports(["US-0003.1"], {"a.trx": trap})
    assert trap_status == {"US-0003.1": "fail"}, trap_status
    assert _has_marker("AssignStaff_WhenWeeklyMaxRulesPass_Succeeds", _PASS_MARKERS) is False

    # An unrecognised artifact is skipped, never guessed at; no reports means no opinion.
    assert status_from_structured_reports(["US-0001.1"], {"notes.txt": "whatever"}) == ({}, {"canonical": 0, "fallback": 0, "unattributed": 0})
    assert status_from_structured_reports(["US-0001.1"], {})[0] == {}

    # --- attribution health: "could not read it" must never look like "nothing is there" --------
    # A file whose tests all name their criterion: nothing unattributed.
    named_ok = {"t/T.cs": "[Fact]\npublic void TestUS00011Works(){ Assert.True(x); }\n"}
    assert unattributed_tests(["US-0001.1"], named_ok) == {}, unattributed_tests(["US-0001.1"], named_ok)

    # The dangerous shape: three real tests, none naming a criterion. Without this the gate reports a
    # confident "no test found covering US-0001.1" and the redraft is told to write tests that exist.
    anonymous = {
        "t/CounterTests.cs": (
            "[Fact]\npublic void IncrementAddsOne(){ Assert.Equal(1, c.Value); }\n"
            "[Fact]\npublic void DecrementSubtractsOne(){ Assert.Equal(0, c.Value); }\n"
            "[Fact]\npublic void ResetReturnsToZero(){ Assert.Equal(0, c.Value); }\n"
        )
    }
    orphans = unattributed_tests(["US-0001.1"], anonymous)
    assert orphans == {"t/CounterTests.cs": 3}, orphans
    # A criterion mentioned in ANOTHER criterion's test does not silence the warning for this one.
    assert unattributed_tests(["US-0009.9"], named_ok) == {"t/T.cs": 1}
    assert unattributed_tests([], {}) == {}

    # Helpers are not tests. A constructor, a Dispose and a factory method all match the method
    # signature pattern; counting them reported 4 orphans in a real suite where every test WAS
    # correctly named, which would have sent a redraft chasing a problem that did not exist.
    helpers_only = {
        "t/T.cs": (
            "public CounterTests(){ }\n"
            "public void Dispose(){ }\n"
            "private HttpClient CreateClient(){ return null; }\n"
        )
    }
    assert unattributed_tests(["US-0001.1"], helpers_only) == {}, unattributed_tests(["US-0001.1"], helpers_only)
    # A JS test call is name-bearing on its own, with no attribute line above it.
    js_anon = {"t/x.spec.ts": "test('increments the counter', async () => {});\n"}
    assert unattributed_tests(["US-0001.1"], js_anon) == {"t/x.spec.ts": 1}

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

    # --- Ruling 7: check_ac_coverage itself must not flag an EARLIER ticket's shipped, correctly-
    # passing AC as tautological -- reproduces the exact deterministic second-ticket failure this
    # fix closes, end to end, not just at the level of a pure helper. Full assertions live in
    # _demo_ticket_scoping below (needs asyncio + hand-rolled sandbox fakes, kept out of the main
    # body above since every other assertion here is synchronous and pure).
    asyncio.run(_demo_ticket_scoping())

    print("ac_coverage_gate self-check: all assertions passed")


async def _demo_ticket_scoping() -> None:
    """Reproduces Ruling 7's exact bug against check_ac_coverage itself: ticket #1's AC test is
    recorded PASSING (shipped, green, correct) in a structured report; ticket #2's own new AC is
    recorded FAILING (correct RED). Both halves of the fix are asserted, because a scope that
    accidentally swallows the RED-step check entirely would be at least as bad as the bug:

      (a) ticket #1's already-green AC must never be flagged tautological (or even considered --
          it must not appear in active_ac_ids at all once scoped to ticket #2's own Specification).
      (b) if ticket #2's OWN new AC were ALSO suspiciously green pre-implementation, the gate must
          still catch it -- proving the fix narrowed the check's SCOPE, not its existence.
    """
    class _FakeExecResult:
        def __init__(self, ok: bool = True, stdout: str = "") -> None:
            self.ok = ok
            self.stdout = stdout

    class _FakeCoverageProvider:
        """Serves `cat <path>` for the paths this scenario cares about (the project ledger, ticket
        #2's own approved Specification, the console tee, and one .trx). The depth-scan's own
        `git ls-files ... | head -60` listing (distinguished by that unique substring -- it is the
        only exec call this module makes containing it) returns `test_listing` when given one, so
        the attribution-scoping scenario below can seed real test file paths for
        unattributed_tests to read. Every OTHER exec_in_sandbox call (the `rm -f` reset, the
        missing-AC grep fallback) is inert -- ok with empty output -- since this scenario's
        pass/fail decision comes entirely from the structured report, exactly as
        status_from_structured_reports' own docstring says it should."""

        def __init__(self, files: dict[str, str], test_listing: list[str] | None = None) -> None:
            self._files = files
            self._test_listing = test_listing or []

        async def exec_in_sandbox(self, _thread_id: str, command: str):  # noqa: ANN201
            if "head -60" in command:
                return _FakeExecResult(True, "\n".join(self._test_listing))
            for path, content in self._files.items():
                if path in command:
                    return _FakeExecResult(True, content)
            return _FakeExecResult(True, "")

    # The WHOLE PROJECT's ledger: ticket #1's US-0001.1 (shipped, still "active" -- retirement is
    # not what makes an old AC stop needing coverage, see spec_ledger.sync_ledger) plus ticket #2's
    # own, brand-new US-0002.1.
    # US-0003/.1 are RETIRED -- present only for the attribution scenario (c) below, which needs a
    # retired AC with a correctly-named test to prove Task 10 sweep item #10's fix. Retired status
    # already correctly excludes them from active_ac_ids (and therefore from outcome/outcome2/
    # outcome3's own assertions above, which never mention US-0003), so adding them here is safe.
    ledger = json.dumps({"entries": [
        {"id": "US-0001", "kind": "user_story", "status": "active"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0002", "kind": "user_story", "status": "active"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "status": "active"},
        {"id": "US-0003", "kind": "user_story", "status": "retired"},
        {"id": "US-0003.1", "kind": "acceptance_criterion", "status": "retired"},
    ]})
    # Ticket #2's OWN approved Specification cites only its own story -- exactly what
    # own_ac_ids_from_specification (spec_ledger.py) reads.
    ticket2_spec = json.dumps({
        "title": "Ticket 2", "summary": "...",
        "user_stories": [{
            "id": "US-0002", "title": "Ticket 2 story", "narrative": "...",
            "acceptance_criteria": [{"id": "US-0002.1", "description": "Ticket 2's own new rule."}],
        }],
    })
    trx_correct_red = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
        '<UnitTestResult testName="[US-0001.1] ticket 1 feature, already shipped" outcome="Passed" />'
        '<UnitTestResult testName="[US-0002.1] ticket 2 new rule" outcome="Failed" />'
        "</Results></TestRun>"
    )
    base_files = {
        LEDGER_PATH: ledger,
        workflow_persistence.SPECIFICATION_APPROVED_PATH: ticket2_spec,
        AC_TEST_OUTPUT_PATH: "test run finished, suite is red overall\n",
        "TestResults/ac-run.trx": trx_correct_red,
    }

    original_run_and_report = stack_runner.run_and_report

    async def _fake_run_and_report(*_args, **_kwargs) -> AcTestRunReport:
        return AcTestRunReport(exit_ok=False, result_artifacts=["TestResults/ac-run.trx"])

    stack_runner.run_and_report = _fake_run_and_report
    try:
        # (a) Ticket #1's shipped, green AC must be excluded entirely -- not merely un-flagged.
        outcome = await check_ac_coverage(
            _FakeCoverageProvider(base_files), "t", {}, chat_provider="claude"
        )
        assert outcome.report.get("active_ac_ids") == ["US-0002.1"], (
            "ticket #1's own already-shipped AC leaked into a scope that should be ticket #2-only: "
            f"{outcome.report}"
        )
        assert "US-0001.1" not in outcome.report.get("tautological", []), outcome.report
        assert outcome.passed, (
            "ticket #2's own AC is correctly RED and covered -- this must PASS, not be blocked by "
            f"an earlier ticket's unrelated green test: {outcome.feedback}"
        )

        # (b) The negative control: if ticket #2's OWN new AC were ALSO green pre-implementation,
        # the RED-step check must still catch IT -- proving scope narrowed, not disabled.
        trx_tautological = trx_correct_red.replace(
            '[US-0002.1] ticket 2 new rule" outcome="Failed"',
            '[US-0002.1] ticket 2 new rule" outcome="Passed"',
        )
        assert "Passed" in trx_tautological and trx_tautological != trx_correct_red
        tautological_files = {**base_files, "TestResults/ac-run.trx": trx_tautological}
        outcome2 = await check_ac_coverage(
            _FakeCoverageProvider(tautological_files), "t", {}, chat_provider="claude"
        )
        assert not outcome2.passed, "ticket #2's own tautological (fake-green) AC must still block"
        assert outcome2.report.get("tautological") == ["US-0002.1"], outcome2.report

        # Fallback: an unreadable/absent approved Specification must not crash, and must fall back
        # to the pre-fix unscoped list rather than manufacturing a NEW "no active ACs" false gap --
        # this should never happen in practice (specification is a hard prerequisite stage), but
        # this proves the fallback is "old behavior", not a silent, different failure.
        no_spec_files = {k: v for k, v in base_files.items() if k != workflow_persistence.SPECIFICATION_APPROVED_PATH}
        outcome3 = await check_ac_coverage(
            _FakeCoverageProvider(no_spec_files), "t", {}, chat_provider="claude"
        )
        assert outcome3.report.get("active_ac_ids") == ["US-0001.1", "US-0002.1"], outcome3.report
        assert outcome3.report.get("tautological") == ["US-0001.1"], outcome3.report

        # (c) Review finding on this same fix: unattributed_tests must keep reading the WHOLE
        # ledger's active ids, not the now-scoped active_ac_ids -- it answers "is this ANY real AC"
        # (attribution health), not "is this MY ticket's AC". Seed the depth-scan's repo-wide
        # listing (which is never diff/ticket-scoped) with ticket #1's own correctly-named,
        # already-shipped test file alongside a genuinely unattributed one. Ticket #2's own AC is
        # left uncovered here (no trx entry for it) purely to reach the failure branch that
        # actually attaches depth_report to the outcome -- see check_ac_coverage's success return,
        # which omits "depth" entirely when nothing failed.
        attribution_files = {
            LEDGER_PATH: ledger,
            workflow_persistence.SPECIFICATION_APPROVED_PATH: ticket2_spec,
            AC_TEST_OUTPUT_PATH: "test run finished\n",
            "TestResults/ac-run.trx": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
                '<UnitTestResult testName="[US-0001.1] ticket 1 feature, already shipped" outcome="Passed" />'
                "</Results></TestRun>"
            ),
            "apps/api.Tests/Ticket1Tests.cs": "[Fact]\npublic void TestUS00011AlreadyShipped(){ Assert.True(true); }\n",
            "apps/api.Tests/OrphanTests.cs": "[Fact]\npublic void SomeGenuinelyUnrelatedHelperTest(){ Assert.True(true); }\n",
            # Task 10 sweep item #10: a test correctly naming a now-RETIRED AC (US-0003.1) must not
            # be misreported as unattributed either -- unattributed_tests answers "did a real human
            # ever write this AC id anywhere in the ledger", which doesn't stop being true just
            # because the AC was later retired.
            "apps/api.Tests/RetiredTests.cs": "[Fact]\npublic void TestUS00031NowRetired(){ Assert.True(true); }\n",
        }
        outcome4 = await check_ac_coverage(
            _FakeCoverageProvider(
                attribution_files,
                test_listing=["apps/api.Tests/Ticket1Tests.cs", "apps/api.Tests/OrphanTests.cs", "apps/api.Tests/RetiredTests.cs"],
            ),
            "t", {}, chat_provider="claude",
        )
        orphans = outcome4.report.get("depth", {}).get("unattributed_tests", {})
        assert "apps/api.Tests/Ticket1Tests.cs" not in orphans, (
            "ticket #1's own correctly-named, already-shipped test must not be misreported as "
            f"unattributed just because it falls outside ticket #2's own scope: {orphans}"
        )
        assert "apps/api.Tests/RetiredTests.cs" not in orphans, (
            "a test correctly naming a now-RETIRED AC must not be misreported as unattributed "
            f"(Task 10 sweep item #10): {orphans}"
        )
        assert orphans == {"apps/api.Tests/OrphanTests.cs": 1}, (
            f"a genuinely unattributed test (names no real AC id at all) must still be caught -- "
            f"the distinction must be restored, not silenced entirely: {orphans}"
        )
    finally:
        stack_runner.run_and_report = original_run_and_report


if __name__ == "__main__":
    _demo()
