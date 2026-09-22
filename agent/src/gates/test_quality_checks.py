"""Pure, dependency-free (stdlib `re`/`difflib` only) test-body quality checks -- extracted from
`ac_coverage_gate.py` (2026-09-19) specifically so the sandbox's own same-turn Stop hook
(sandbox-image/hooks/check-test-quality-stop.mjs) can run the REAL check by shelling out to
`python3` on this ONE file, instead of a hand-ported JavaScript reimplementation drifting from it.

Why a subprocess, not a straight import: `ac_coverage_gate.py` (and everything upstream of it --
`schemas.py`, `sandbox/provider.py`, `chat_model.py`, this pipeline's DB drivers and Azure SDKs)
lives entirely outside the sandbox and is never copied into the image; even if it were, importing
that module pulls in a dependency graph with no business running inside a per-turn Stop hook. This
module is the answer to "duplicate the logic, or ship the real thing": everything below is provably
pure (a string in, a list out, confirmed by this file's own self-check) and needs nothing beyond
what `python3`'s standard library already provides, so it is the ONE file this pipeline ships into
`/opt/aidw-hooks/` and calls directly -- the real implementation, not a port of it.

`ac_coverage_gate.py` imports every name below UNCHANGED (this is a pure code-move, not a fork);
its own self-check is the proof this extraction changed no behavior.

CLI mode (`python3 test_quality_checks.py`, stdin: JSON `{"path": "contents", ...}`, stdout: JSON
`{"absence_only": [label, ...], "fiat_stubs": [label, ...], "duplicates": [[dup_label, orig_label],
...], "ac_depth": {ac_id: {"non_e2e_count": int, "shortfalls": [str, ...]}, ...},
"non_testid_locators": {path: [snippet, ...], ...}}`) is what the Stop hook actually invokes. `absence_only`/`fiat_stubs` are unscoped by Acceptance Criterion on
purpose (the hook has no ledger to attribute tests to; a bad test is a bad test whichever criterion
it claims), unlike `ac_coverage_gate.py`'s own per-AC callers which filter `_iter_tests`' output
down to one AC's own tests via `_tests_for_ac`. `duplicates` is NOT unscoped this same way -- see
`duplicate_pairs_by_ac`'s own docstring for the live incident that proved near-duplicate detection
needs per-criterion grouping even without a ledger. `ac_depth` (2026-09-21) IS scoped, per AC, the
same way -- but its universe of AC ids comes from what the test files THEMSELVES claim
(`_ac_id_of` on each test's own declared label), not a ledger the hook has no access to; see
`ac_depth_report`'s own docstring.
"""

from __future__ import annotations

import json
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

# How similar two test bodies may be before they count as one test. 0.92 is deliberately high --
# tests for one criterion legitimately share scaffolding, and the target is copy-paste-with-a-
# renamed-variable, not family resemblance. Read by ac_coverage_gate.py too (moved here unchanged,
# same env var name -- AGENTS.md's own rule: relocating a value that already reads its own env var
# keeps that exact name).
MAX_TEST_BODY_SIMILARITY = float(os.environ.get("MAX_TEST_BODY_SIMILARITY", "0.92"))

_ASSERTION_RE = re.compile(
    r"(?:expect|assert|Assert\.\w+|should)\s*\(\s*([^;\n]{3,120}?)\s*\)",
    re.IGNORECASE,
)
# What counts as "a line declaring a test". Three families, because three very different things
# are:
#
#   JS/TS:  test('...'), it('...'), describe('...')          -- keyword, CALLED (must see `(`)
#   C#:     [Fact] on one line, `public void TestUS00012...` on the NEXT
#
# The C# half is why the second alternative exists. `\b(test|...)\b` cannot match inside
# `TestUS00012ResolveStateDirectory` (no word boundary between "Test" and "US"), and the `[Fact]`
# line carries no criterion id -- so a file holding 14 real tests scored ZERO for every criterion.
# Measured live on apps/api.Tests/CounterApiIntegrationTests.cs.
#
# The JS/TS keywords require a following `(` -- root-caused 2026-09-19 (income-investor session
# f0fef8ba, LIVE, on the newly-added UNSCOPED path below): a bare `\btest\b`/`\bit\b` matches
# ordinary code that merely CONTAINS the word, with no call at all -- `import { test, expect } from
# '@playwright/test';` and `request.post('/api/test-support/tickers', ...)` were both misread as
# test-declaration lines, seeding a spurious "test" whose body (everything until the next real
# decl) was near-identical across files (every file's import line looks the same) and got reported
# as duplicates of each other. ac_coverage_gate.py's own per-AC callers never surfaced this: a
# false decl line almost never happens to also contain the ONE AC id being filtered for, so
# `_tests_for_ac` silently dropped it. `Fact`/`Theory` stay bare-word (a C# `[Fact]` attribute is
# never followed by `(`).
_TEST_DECL_RE = re.compile(
    r"\b(?:test|it|describe)\s*\("
    r"|\b(Fact|Theory)\b"
    r"|\b(?:public|internal|private)\s+(?:async\s+)?[\w<>\[\],\s]+?\s+\w+\s*\(",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\d+|\S")


def _alpha_profile(body: str) -> tuple[list[str], frozenset[str]]:
    """(alpha token stream, alpha-normalised assertion-target set) for one test body.

    Type-2 clone normalisation with one deliberate deviation: identifiers are replaced by
    first-occurrence indexes (i1, i2, ...) so renaming a local/method never defeats the comparison
    -- EXCEPT tokens that immediately follow a '.', which stay literal. A member access names the
    API surface under test: alpha-mapping it would collapse `r.Count` and `r.Total` into the same
    stream, re-creating the tiny-test false positive this function exists to avoid (run d8b09f43,
    US-0005.1), while keeping it literal still catches the copy-and-rename-the-local dodge
    (`r.Count` vs `result.Count` -- receiver indexed, member identical). Numbers -> 'n', string
    literals -> 's', punctuation kept: structure stays, spelling doesn't."""
    stripped = re.sub(r'"[^"]*"|\'[^\']*\'', " s ", body)
    mapping: dict[str, str] = {}

    def alpha(tokens: list[str]) -> list[str]:
        out: list[str] = []
        prev = ""
        for token in tokens:
            if token.isdigit():
                out.append("n")
            elif re.match(r"[A-Za-z_]", token) and prev != ".":
                out.append(mapping.setdefault(token, f"i{len(mapping) + 1}"))
            else:
                out.append(token.lower())
            prev = token
        return out

    stream = alpha(_TOKEN_RE.findall(stripped))
    asserts = frozenset(
        " ".join(alpha(_TOKEN_RE.findall(re.sub(r'"[^"]*"|\'[^\']*\'', " s ", m.group(1)))))
        for m in _ASSERTION_RE.finditer(body)
    ) - {""}
    return stream, asserts


def _iter_tests(test_files: dict[str, str]) -> list[tuple[str, str, str]]:
    """(label, normalised body, raw decl line) per test-declaring line in every file -- the ONE
    parse both `all_tests` below and ac_coverage_gate.py's own `_tests_for_ac` build on, so the
    line-scanning/inline-body-seeding/whitespace-normalisation logic (all load-bearing, confirmed
    against real captured suites -- see git history) exists in exactly one place.

    The raw, UNTRUNCATED decl line is kept alongside the (already-truncated-to-100-chars) label
    specifically so a caller filtering by "does this AC id appear in the declaration" -- ledger
    attribution needs the real line, not what survived truncation -- gets the right answer even
    for a declaration line longer than the label's own display truncation."""
    tests: list[tuple[str, str, str]] = []
    for path, contents in test_files.items():
        current: list[str] | None = None
        decl = ""
        raw_line = ""
        for line in contents.splitlines():
            if _TEST_DECL_RE.search(line):
                if current is not None:
                    tests.append((decl, "\n".join(current), raw_line))
                # Seed with whatever follows the opening brace, so a one-line test
                # (`public void X(){ Assert.Equal(1, c.Value); }`) has a body at all -- without
                # this its assertion was invisible and it counted as a non-asserting stub.
                # Deliberately NOT the whole line: the test NAME must stay out of the body, or two
                # identical clones with different names stop looking like duplicates.
                inline = line.split("{", 1)[1] if "{" in line else ""
                current = [inline.strip()] if inline.strip() else []
                decl = f"{path} :: {line.strip()[:100]}"
                raw_line = line
            elif current is not None:
                current.append(line.strip())
        if current:
            tests.append((decl, "\n".join(current), raw_line))
    return [
        (decl, re.sub(r"\s+", " ", body).strip(), raw_line)
        for decl, body, raw_line in tests
        if body.strip()
    ]


def all_tests(test_files: dict[str, str]) -> list[tuple[str, str]]:
    """(label, normalised body) per test-declaring line in every file -- the unscoped counterpart
    of ac_coverage_gate.py's own `_tests_for_ac`, which filters `_iter_tests`' identical walk down
    to lines mentioning one Acceptance Criterion id."""
    return [(decl, body) for decl, body, _raw_line in _iter_tests(test_files)]


# An assertion that something is ABSENT. On a page that never rendered, every one of these is
# trivially true -- so a test built only from them passes against a blank screen and proves nothing.
# Observed live (blazor-dotnet, US-0006.1 "no sign-in UI is present anywhere"): `goto('/')` followed
# by four `toHaveCount(0)` checks and nothing else. It passed while its screenshot was a 5,482-byte
# blank, and would have passed identically had the app been completely broken.
_ABSENCE_ASSERTION_RE = re.compile(
    r"toHaveCount\s*\(\s*0\s*\)"
    r"|\.not\s*\.\s*to\w+"
    r"|toBeNull\s*\(\s*\)"
    r"|toBeUndefined\s*\(\s*\)"
    r"|toBeEmpty\s*\(\s*\)"
    r"|Assert\.(?:Null|Empty|False|DoesNotContain)"
    r"|assertIsNone|assertFalse|assertNotIn",
    re.IGNORECASE,
)

# An assertion that something IS there -- the anchor that makes the absence checks meaningful,
# because it cannot pass until the app has actually rendered.
#
# `toContain\(` (plain, without "Text") added 2026-09-19 -- root-caused LIVE (income-investor
# session f0fef8ba): a unit test anchoring on `expect(SOCIAL_LOGIN_PROVIDERS).toContain('google')`
# BEFORE its own `.not.toContain('twitter')` absence check -- already exactly the discipline this
# check exists to enforce -- was still flagged absence-only, because vitest/jest's real array/
# string membership matcher (`toContain`) was missing; only Playwright's locator-content matcher
# (`toContainText`) was listed.
_PRESENCE_ASSERTION_RE = re.compile(
    r"toBeVisible\s*\(\s*\)"
    r"|toHaveText\s*\(|toContainText\s*\(|toContain\s*\(|toHaveValue\s*\(|toHaveAttribute\s*\("
    r"|toBeEnabled\s*\(\s*\)|toBeChecked\s*\(\s*\)|toBeFocused\s*\(\s*\)"
    r"|toHaveCount\s*\(\s*[1-9]"
    r"|toBe\s*\(|toEqual\s*\(|toMatch\s*\("
    r"|Assert\.(?:NotNull|NotEmpty|True|Equal|Contains)"
    r"|assertEqual|assertTrue|assertIn|assertIsNotNone",
    re.IGNORECASE,
)


def absence_only_labels(tests: list[tuple[str, str]]) -> list[str]:
    """Labels of tests that assert ONLY absence, with no presence anchor.

    A test qualifies only if it asserts at least one absence and zero presences: a test with both
    is fine (the presence assertion forces a render before the absence checks are evaluated), and a
    test asserting neither is a RED-phase stub a separate (count-based) check already owns.

    `.not.to*` is treated as absence even though `expect(x).not.toBe(y)` is a value comparison: on
    an unrendered page a locator-based `.not.` assertion is exactly the trivially-true shape this
    exists to catch, and a test that ALSO makes a positive assertion is cleared regardless."""
    labels: list[str] = []
    for decl, body in tests:
        if not _ABSENCE_ASSERTION_RE.search(body):
            continue
        if _PRESENCE_ASSERTION_RE.search(body):
            continue
        labels.append(decl)
    return labels


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


def fiat_stub_labels(tests: list[tuple[str, str]]) -> list[str]:
    """Labels of tests that fail by fiat -- their only assertion is an unconditional failure.

    Distinct from the assertion-FREE stub a separate count-based check already tolerates: an empty
    skeleton is the documented RED-phase form, while `Assert.True(false, "RED: ...")` is red paint
    -- it makes the suite fail without encoding any behavior. A body with at least one real
    assertion alongside a fiat one is NOT counted -- e.g. Assert.Fail inside a catch block guarding
    a genuine act-assert path."""
    labels: list[str] = []
    for decl, body in tests:
        if not _FIAT_FAIL_RE.search(body):
            continue
        if not _ASSERTION_RE.search(_FIAT_FAIL_RE.sub("", body)):
            labels.append(decl)
    return labels


def duplicate_pairs(tests: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(duplicate test label, original test label) per near-duplicate, so feedback names both.

    A pair is a duplicate only when the alpha token streams are similar AND the alpha-normalised
    assertion targets match (see `_alpha_profile` for what alpha means and why member names stay
    literal). Raw-text similarity alone false-positives on tiny tests: bodies are ~90% shared
    plumbing, so SequenceMatcher saturates past 0.92 for ANY two short tests -- observed live (run
    d8b09f43, US-0005.1): six laps rejected, by lap 6 flagging a singleton-registration store test
    against an accumulation-across-connections controller test. Different assertion targets =
    different tests, no matter how much scaffolding they share; renamed locals = the same test, no
    matter how thorough the rename."""
    pairs: list[tuple[str, str]] = []
    kept: list[tuple[str, list[str], frozenset[str]]] = []
    for decl, body in tests:
        stream, asserts = _alpha_profile(body)
        original = next(
            (k_decl for k_decl, k_stream, k_asserts in kept
             if asserts == k_asserts
             and SequenceMatcher(None, stream, k_stream).ratio() >= MAX_TEST_BODY_SIMILARITY),
            None,
        )
        if original is not None:
            pairs.append((decl, original))
        else:
            kept.append((decl, stream, asserts))
    return pairs


# The canonical bracketed form AC_TO_TESTS_NAMING_RULES (ac_coverage_gate.py) requires at the
# START of every test's display name -- `[US-0001.2] does X`. Deliberately NOT id_variants'
# (ac_coverage_gate.py) full six-spelling match: that function answers "does this body cite id X",
# which needs every spelling a test might use INTERNALLY; this answers "which criterion does this
# test's own declared name claim", which only ever needs the one canonical form the naming rule
# mandates authors write.
_AC_ID_IN_LABEL_RE = re.compile(r"\[([A-Za-z]{2,4}-\d{3,6}(?:\.\d+)?)\]")


def _ac_id_of(label: str) -> str | None:
    """The Acceptance Criterion id a test's own label claims, or None if it names none."""
    m = _AC_ID_IN_LABEL_RE.search(label)
    return m.group(1).upper() if m else None


def duplicate_pairs_by_ac(tests: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Same as `duplicate_pairs`, but grouped by each test's OWN claimed Acceptance Criterion id
    before comparing.

    Root-caused 2026-09-19 (income-investor session f0fef8ba, ac-to-tests cycle 1, LIVE):
    check-test-quality-stop.mjs's first real run flagged 35 "near-duplicates" that were tests for
    35 DIFFERENT criteria -- e.g. US-0003.2 (non-admin blocked from Admin Settings) "duplicating"
    US-0003.1 (non-admin blocked from Funds Universe Admin): two different screens, same
    authorization-guard shape. `duplicate_pairs` alone has no way to know that, since alpha
    normalisation deliberately erases the very literals (route paths, field names) that would
    distinguish them -- it was built to compare ONE criterion's OWN tests against each other
    (ac_coverage_gate.py's per-AC callers always pre-filter via `_tests_for_ac` before calling it),
    never a whole suite at once. `run_all_checks`/the Stop hook's unscoped CLI mode has no ledger
    to pre-filter by, so THIS function does the equivalent grouping from each test's own declared
    name instead. Tests naming no id (or a body seeded from a helper/setup line the naming rule
    doesn't reach) fall into one None-keyed group and are compared only against each other -- still
    strictly narrower, never broader, than the old unscoped comparison."""
    groups: dict[str | None, list[tuple[str, str]]] = {}
    for decl, body in tests:
        groups.setdefault(_ac_id_of(decl), []).append((decl, body))
    pairs: list[tuple[str, str]] = []
    for group_tests in groups.values():
        pairs.extend(duplicate_pairs(group_tests))
    return pairs


# --- AC depth (2026-09-21, extracted from ac_coverage_gate.py the same way as everything above:
# so check-coverage-stop.mjs's minimal-code-to-green sibling, check-test-quality-stop.mjs itself
# once its stage gate is widened, can run test_coverage_gate.check_ac_depth's SAME per-AC checks
# in-turn instead of costing a whole audit+redraft round trip to discover a shortfall this
# deterministic, non-LLM logic already knows about at draft time. Only the GREEN-phase subset:
# check_ac_depth always calls the host's own `depth_shortfalls` with `ui_relevant=set()` and
# `content_dict=None`, so the e2e-required-for-UI-criteria branch and the category_spread
# (happy-path-only) branch can never fire there and are correctly NOT ported here either -- see
# that function's own docstring in ac_coverage_gate.py for why.
_E2E_PATH_RE = re.compile(r"(^|/)e2e(/|$)|(^|/)playwright\.config\.[jt]sx?$|\.e2e\.[jt]sx?$", re.IGNORECASE)

# Symbols that make a .NET/JS test an INTEGRATION test. Detected by symbol, never by directory: a
# .NET repo keeps unit and integration tests in ONE project, so the path proves nothing. e2e is the
# one level a path does prove, which is why it is the only level matched by path above.
_INTEGRATION_SYMBOLS = ("WebApplicationFactory", "TestServer", "HttpClient", "createServer", "supertest", "TestClient")

_AC_IN_NAME_RE = re.compile(r"(?:US|AC)[^A-Za-z0-9]{0,2}(\d{4})[^A-Za-z0-9]{0,2}(\d+)(?!\d)", re.IGNORECASE)

# data-testid-only locator convention (2026-09-21, income-investor run c1458b23): every Playwright
# call of the form `<receiver>.<method>(<args>)` for the query methods below. `locator` is
# special-cased in `non_testid_locators` (allowed ONLY when its own argument string contains
# "data-testid") rather than being flagged outright, since `page.locator('[data-testid="x"]')` is
# the CSS-attribute-selector spelling of the same convention, not a violation of it.
_LOCATOR_METHOD_RE = re.compile(
    r"\.(getByRole|getByText|getByLabel|getByPlaceholder|getByAltText|getByTitle|getByTestId|locator)\s*\(([^)]*)\)"
)
# Legacy Playwright/Puppeteer-style shorthand (`page.$('sel')` / `page.$$('sel')`) -- rare in
# generated code but not caught by the method-name regex above (`$`/`$$` are not identifiers), and
# a real escape hatch around the convention if left unflagged.
_DOLLAR_LOCATOR_RE = re.compile(r"\.\${1,2}\s*\(")

# GREEN phase (minimal-code-to-green, where the implementation exists and a unit test is a thing
# that can actually be written) -- see ac_coverage_gate.py's own MIN_NON_E2E_TESTS_PER_AC/
# MIN_NON_E2E_TESTS_PER_AC_RED comment for the full RED-vs-GREEN reasoning and the live incidents
# that set these numbers. Only the GREEN constant is needed here: this module's ac_depth_report is
# invoked exclusively from check-coverage-stop.mjs's minimal-code-to-green gate, never ac-to-tests'
# (RED-phase depth is far looser and enforced differently -- see that file's own comment).
MIN_NON_E2E_TESTS_PER_AC = int(os.environ.get("MIN_NON_E2E_TESTS_PER_AC", "2"))
MIN_DISTINCT_ASSERTIONS_PER_AC = int(os.environ.get("MIN_DISTINCT_ASSERTIONS_PER_AC", "2"))
MIN_TESTS_BEFORE_ASSERTION_CHECK = int(os.environ.get("MIN_TESTS_BEFORE_ASSERTION_CHECK", "3"))


def id_variants(ac_id: str) -> list[str]:
    """Spellings a test name may legitimately use for one ledger id. Models re-prefix US-0003.6
    as AC-0003.6 despite instructions, and identifier-safe names replace -/. with _
    (Test_US_0007_2). Numbering is what identifies the AC; tolerate the spellings. Public:
    metrics_nodes.py's traceability matrix reuses it (via ac_coverage_gate.py's re-export) so both
    scans accept the same spellings."""
    variants = {ac_id}
    if ac_id.startswith("US-"):
        variants.add("AC-" + ac_id[3:])
    variants.update(v.replace("-", "_").replace(".", "_") for v in list(variants))
    variants.update(v.replace("-", "").replace(".", "").replace("_", "") for v in list(variants))
    return sorted(variants)


def ac_ids_in_name(test_name: str) -> list[str]:
    """Every AC id mentioned in a test name/line, normalised to `US-0001.2`. One test/line can
    legitimately cover several criteria, so this returns a list rather than the first match."""
    seen: list[str] = []
    for story, criterion in _AC_IN_NAME_RE.findall(test_name or ""):
        normalized = f"US-{story}.{criterion}"
        if normalized not in seen:
            seen.append(normalized)
    return seen


def classify_test_level(path: str, contents: str, resolved_root: str | None = None) -> str:
    """'e2e' | 'integration' | 'unit'. Pure.

    `resolved_root` is the same tech-stack root `write_scope_gate._resolve_web_root` resolves.
    Optional and defaulting to None (the old location-only-regex behavior, unchanged): the
    GREEN-phase caller (`ac_depth_report`) never has a resolved root to check against and always
    omits it; ac_coverage_gate.py's own RED-phase depth check passes it explicitly so an e2e-shaped
    path outside `{resolved_root}/tests/e2e/` no longer counts as "e2e" -- the exact flattening bug
    this pipeline exists to catch, where crediting it here would let a UI story pass depth
    thresholds on a browser test that Playwright's own `testDir` will never actually run."""
    if _E2E_PATH_RE.search(path):
        if resolved_root is None:
            return "e2e"
        expected_prefix = f"{resolved_root}/tests/e2e/" if resolved_root else "tests/e2e/"
        if path.startswith(expected_prefix):
            return "e2e"
    if any(symbol in contents for symbol in _INTEGRATION_SYMBOLS):
        return "integration"
    return "unit"


def non_testid_locators(test_files: dict[str, str]) -> dict[str, list[str]]:
    """path -> [violating locator snippet, ...] for every disallowed (non-`data-testid`) Playwright
    locator call in each E2E-classified file. Pure.

    Scoped to `classify_test_level(...) == "e2e"` files only (via the SAME classifier
    `count_tests_per_ac`/`ac_depth_report` already use, `resolved_root=None` -- no caller here has
    one to pass, same as `ac_depth_report`'s own call): a unit/integration test using Testing
    Library's role/label queries is following a DIFFERENT, legitimate convention (those queries
    double as an accessibility check at that layer), so this rule only applies where DOM stability
    across hydration/routing matters more than accessibility-query fidelity -- the browser layer.

    Root-caused live (income-investor run c1458b23): `page.locator("input")` matched a Next.js
    Server Action's own hidden `<input type="hidden" name="$ACTION_ID_...">` -- rendered by the
    FRAMEWORK, ahead of the real form field -- instead of the field the test meant to check.
    `getByRole`/`getByText`/`getByLabel` LOOK safer than a raw CSS/tag locator but carry the exact
    same risk: any of them matches whatever the framework happens to render that satisfies the
    query, not necessarily the element the test author had in mind. `data-testid` is the one
    surface nothing but the app's own author writes onto an element, so it is the only locator this
    convention allows. `page.locator(...)` is exempted ONLY when its own argument text contains
    "data-testid" (the CSS-attribute-selector spelling of the same convention, e.g.
    `locator('[data-testid="save-button"]')`), not flagged as a bare-CSS violation."""
    violations: dict[str, list[str]] = {}
    for path, contents in test_files.items():
        if classify_test_level(path, contents) != "e2e":
            continue
        found: list[str] = []
        for match in _LOCATOR_METHOD_RE.finditer(contents):
            method, args = match.group(1), match.group(2)
            if method == "getByTestId":
                continue
            if method == "locator" and "data-testid" in args:
                continue
            found.append(match.group(0).strip()[:80])
        for match in _DOLLAR_LOCATOR_RE.finditer(contents):
            found.append(match.group(0).strip()[:80])
        if found:
            violations[path] = found
    return violations


def count_tests_per_ac(
    ac_ids: list[str], test_files: dict[str, str], resolved_root: str | None = None
) -> dict[str, dict[str, int]]:
    """Per AC: how many tests name it, split by level. A "test" is counted per test-declaring line
    mentioning the id, not per file: one file commonly holds several tests for the same criterion."""
    counts = {ac: {"unit": 0, "integration": 0, "e2e": 0} for ac in ac_ids}
    for path, contents in test_files.items():
        level = classify_test_level(path, contents, resolved_root)
        for line in contents.splitlines():
            if not _TEST_DECL_RE.search(line):
                continue
            named = set(ac_ids_in_name(line))
            for ac in ac_ids:
                if ac in named:
                    counts[ac][level] += 1
    return counts


def _normalise_assertion(target: str) -> str:
    """Collapse whitespace, quotes and numeric literals so `expect(count).toBe(1)` and
    `expect(count).toBe(2)` read as ONE assertion target -- they exercise the same expression."""
    collapsed = re.sub(r"\s+", "", target)
    collapsed = re.sub(r"[\"']", "", collapsed)
    return re.sub(r"\d+", "N", collapsed).lower()


def _tests_for_ac(ac_id: str, test_files: dict[str, str]) -> list[tuple[str, str]]:
    """(label, normalised body) per test naming this AC -- filters `_iter_tests`' shared parse by
    the UNTRUNCATED decl line so a declaration longer than the label's own 100-char truncation
    still matches."""
    variants = id_variants(ac_id)
    return [
        (decl, body)
        for decl, body, raw_line in _iter_tests(test_files)
        if any(variant in raw_line for variant in variants)
    ]


def distinct_assertion_targets(ac_id: str, test_files: dict[str, str]) -> set[str]:
    """The distinct expressions asserted by tests naming this AC. Pure. Scoped to the lines
    following each test declaration that names the AC, so assertions belonging to a DIFFERENT
    criterion in the same file are not credited to this one."""
    targets: set[str] = set()
    variants = id_variants(ac_id)
    for contents in test_files.values():
        inside = False
        for line in contents.splitlines():
            if _TEST_DECL_RE.search(line):
                inside = any(variant in line for variant in variants)
            if inside:
                for match in _ASSERTION_RE.finditer(line):
                    normalised = _normalise_assertion(match.group(1))
                    if normalised:
                        targets.add(normalised)
    return targets


def asserting_test_count(ac_id: str, test_files: dict[str, str]) -> int:
    """How many of this AC's tests contain at least one assertion. Pure -- the denominator the
    diversity check must use, not the raw test count (a RED-phase stub asserts nothing)."""
    return sum(1 for _decl, body in _tests_for_ac(ac_id, test_files) if _ASSERTION_RE.search(body))


def ac_depth_report(test_files: dict[str, str]) -> dict[str, dict[str, object]]:
    """Per AC (drawn from what the test files THEMSELVES claim via each test's own `[US-####.#]`
    label, not a ledger this hook has no access to): the GREEN-phase depth shortfalls
    test_coverage_gate.check_ac_depth would find. Pure.

    Mirrors `depth_shortfalls`'s GREEN-phase call shape exactly (ui_relevant=set(),
    content_dict=None) -- see this module's own header comment for why those two branches are
    correctly absent here, not merely deferred."""
    tests = all_tests(test_files)
    ac_ids = sorted({ac for decl, _ in tests if (ac := _ac_id_of(decl))})
    report: dict[str, dict[str, object]] = {}
    if not ac_ids:
        return report
    counts = count_tests_per_ac(ac_ids, test_files)
    for ac in ac_ids:
        problems: list[str] = []
        non_e2e = counts[ac]["unit"] + counts[ac]["integration"]
        if non_e2e < MIN_NON_E2E_TESTS_PER_AC:
            problems.append(
                f"only {non_e2e} test(s) below the browser layer (need "
                f"{MIN_NON_E2E_TESTS_PER_AC}: unit and/or integration -- a browser test cannot "
                "prove a rule beneath the UI)"
            )

        ac_tests = _tests_for_ac(ac, test_files)
        stub_labels = fiat_stub_labels(ac_tests)
        if stub_labels:
            named = "; ".join(stub_labels[:3]) + (f"; and {len(stub_labels) - 3} more" if len(stub_labels) > 3 else "")
            problems.append(f"{len(stub_labels)} of its test(s) fail by fiat: {named}")

        absence_labels = absence_only_labels(ac_tests)
        if absence_labels:
            named = "; ".join(absence_labels[:3]) + (f"; and {len(absence_labels) - 3} more" if len(absence_labels) > 3 else "")
            problems.append(f"{len(absence_labels)} of its test(s) assert ONLY absence: {named}")

        asserting = asserting_test_count(ac, test_files)
        if asserting >= MIN_TESTS_BEFORE_ASSERTION_CHECK:
            targets = distinct_assertion_targets(ac, test_files)
            if len(targets) < MIN_DISTINCT_ASSERTIONS_PER_AC:
                problems.append(
                    f"{asserting} asserting test(s) but only {len(targets)} distinct assertion "
                    f"target(s) (need {MIN_DISTINCT_ASSERTIONS_PER_AC})"
                )
            pairs = duplicate_pairs(ac_tests)
            if pairs:
                named = "; ".join(f"'{dup}' duplicates '{orig}'" for dup, orig in pairs[:3]) + (
                    f"; and {len(pairs) - 3} more" if len(pairs) > 3 else ""
                )
                problems.append(f"{len(pairs)} of its test(s) are near-duplicate bodies: {named}")

        if problems:
            report[ac] = {"non_e2e_count": non_e2e, "shortfalls": problems}
    return report


def run_all_checks(test_files: dict[str, str]) -> dict[str, object]:
    """Everything the CLI entry point reports, computed once over one shared parse -- the same
    function a unit test can call directly, so the CLI wrapper below has no logic of its own to
    drift from what a caller importing this module gets."""
    tests = all_tests(test_files)
    return {
        "absence_only": absence_only_labels(tests),
        "fiat_stubs": fiat_stub_labels(tests),
        "duplicates": [list(pair) for pair in duplicate_pairs_by_ac(tests)],
        # Computed unconditionally (cheap -- pure string ops, no real cost) but only ACTED on by
        # whichever hook is scoped to minimal-code-to-green (check-coverage-stop.mjs) -- the
        # ac-to-tests hook (check-test-quality-stop.mjs) reads this same JSON and simply never
        # looks at this field, since GREEN-phase thresholds (MIN_NON_E2E_TESTS_PER_AC) do not apply
        # at RED phase (MIN_NON_E2E_TESTS_PER_AC_RED). See ac_depth_report's own docstring.
        "ac_depth": ac_depth_report(test_files),
        # Computed unconditionally too -- ACTED on by every stage that can write/edit an e2e spec
        # (ac-to-tests, minimal-code-to-green, e2e-fix), not just one of them, since a bad locator
        # introduced by any of the three is the same real defect. See non_testid_locators' own
        # docstring for the live incident this exists for.
        "non_testid_locators": non_testid_locators(test_files),
    }


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.gates.test_quality_checks` (no sandbox, no DB
    -- every function here is pure). ac_coverage_gate.py's own self-check re-proves these same
    functions produce identical results through ITS per-AC callers -- this file's job is only to
    prove the newly-added unscoped path (`all_tests`/`run_all_checks`) behaves the same way.

    Also re-proves the sandbox-image STAGING COPY (sandbox-image/hooks/test_quality_checks.py,
    what the Dockerfile actually COPYs into /opt/aidw-hooks/ -- see check-test-quality-stop.mjs's
    own header for why a physical copy exists at all: Docker's build context there is
    agent/sandbox-image, which cannot COPY a path outside itself) is byte-identical to THIS file.
    Skipped, not failed, when the staged copy is absent -- this file is also imported standalone by
    ac_coverage_gate.py in contexts (packaging, a checkout without sandbox-image/) where that path
    was never expected to exist."""
    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "test_quality_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/test_quality_checks.py hooks/test_quality_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    absence_only_body = "await page.goto('/'); await expect(page.getByText('Sign in')).toHaveCount(0);"
    anchored_body = "await expect(page.getByText('Admin')).toBeVisible(); await expect(page.getByText('Sign in')).toHaveCount(0);"
    files = {
        "apps/web/tests/e2e/nav.spec.ts": (
            f"test('[US-0006.1] no sign-in UI', async ({{ page }}) => {{ {absence_only_body} }});\n"
            f"test('[US-0006.2] admin sees no sign-in UI', async ({{ page }}) => {{ {anchored_body} }});\n"
        ),
    }
    tests = all_tests(files)
    assert len(tests) == 2, tests
    absence_only = absence_only_labels(tests)
    assert len(absence_only) == 1 and "US-0006.1" in absence_only[0], absence_only
    assert absence_only_labels([("x", anchored_body)]) == [], "a test with a presence anchor must clear"

    fiat_body = "expect(true).toBe(false);"
    real_body = "expect(computeTotal(cart)).toBe(42);"
    assert fiat_stub_labels([("fiat", fiat_body)]) == ["fiat"]
    assert fiat_stub_labels([("real", real_body)]) == []
    assert fiat_stub_labels([("guarded", f"try {{ risky(); }} catch {{ {fiat_body} }} {real_body}")]) == [], (
        "a fiat call alongside a real assertion must not be flagged"
    )

    clone_a = "const r = optimize(inputs); expect(r.Count).toBe(3);"
    clone_b = "const result = optimize(otherInputs); expect(result.Count).toBe(3);"
    different_target = "const r = optimize(inputs); expect(r.Total).toBe(3);"
    dup_tests = [("a", clone_a), ("b", clone_b), ("c", different_target)]
    pairs = duplicate_pairs(dup_tests)
    assert pairs == [("b", "a")], pairs  # renamed locals collapse; a different assertion TARGET does not

    # Root-caused 2026-09-19 (income-investor session f0fef8ba, ac-to-tests cycle 1, LIVE): the
    # unscoped `duplicate_pairs` call `run_all_checks` used to make flagged 35 tests for 35
    # DIFFERENT criteria as duplicates of each other -- two unrelated authorization-guard tests
    # for two different screens share arrange/act/assert SHAPE without being redundant coverage of
    # anything. duplicate_pairs_by_ac must group by each test's own claimed id first.
    guard_body = "await page.goto('/screen'); await expect(page).toHaveURL('/');"
    same_ac_variant_a = "apps/web/tests/e2e/guard.spec.ts :: test('[US-0003.1] non-admin blocked, phrasing A', async ({ page }) => {"
    same_ac_variant_b = "apps/web/tests/e2e/guard.spec.ts :: test('[US-0003.1] non-admin blocked, phrasing B', async ({ page }) => {"
    cross_ac_a = "apps/web/tests/e2e/guard.spec.ts :: test('[US-0003.1] a non-admin is blocked from screen A', async ({ page }) => {"
    cross_ac_b = "apps/web/tests/e2e/guard.spec.ts :: test('[US-0013.1] a non-admin is blocked from screen B', async ({ page }) => {"

    same_ac_tests = [(same_ac_variant_a, guard_body), (same_ac_variant_b, guard_body)]
    assert len(duplicate_pairs_by_ac(same_ac_tests)) == 1, "two tests for the SAME criterion, identical body, must still be flagged"

    cross_ac_tests = [(cross_ac_a, guard_body), (cross_ac_b, guard_body)]
    assert len(duplicate_pairs(cross_ac_tests)) == 1, "sanity: the flat, unscoped check WOULD flag this pair"
    assert duplicate_pairs_by_ac(cross_ac_tests) == [], (
        "two DIFFERENT criteria sharing one guard-test shape must not be flagged as duplicates of each other"
    )

    result = run_all_checks(files)
    assert result["absence_only"] == absence_only
    assert result["fiat_stubs"] == []
    assert result["duplicates"] == []
    cross_ac_source = (
        f"test('[US-0003.1] a non-admin is blocked from screen A', async ({{ page }}) => {{ {guard_body} }});\n"
        f"test('[US-0013.1] a non-admin is blocked from screen B', async ({{ page }}) => {{ {guard_body} }});\n"
    )
    assert run_all_checks({"apps/web/tests/e2e/guard.spec.ts": cross_ac_source})["duplicates"] == [], (
        "run_all_checks must use the AC-grouped path end to end, not the flat one"
    )

    # ac_depth_report: GREEN-phase per-AC checks (test_coverage_gate.check_ac_depth's own logic,
    # ui_relevant=set()/content_dict=None call shape).
    assert classify_test_level("apps/web/tests/e2e/nav.spec.ts", "") == "e2e"
    assert classify_test_level("apps/api.Tests/FooTests.cs", "var f = new WebApplicationFactory<Program>();") == "integration"
    assert classify_test_level("src/lib/foo.test.ts", "expect(1).toBe(1);") == "unit"
    assert ac_ids_in_name("public void TestUS00012ResolveStateDirectory()") == ["US-0001.2"]
    assert "AC-0003_6" in id_variants("US-0003.6") or "AC-0003.6" in id_variants("US-0003.6")

    one_unit_test = {
        "src/lib/__tests__/calc.test.ts": (
            "test('[US-0009.1] adds two numbers', () => { expect(add(1, 2)).toBe(3); });\n"
        ),
    }
    depth = ac_depth_report(one_unit_test)
    assert "US-0009.1" in depth and depth["US-0009.1"]["non_e2e_count"] == 1, depth
    assert any("below the browser layer" in p for p in depth["US-0009.1"]["shortfalls"]), depth

    two_unit_tests = {
        "src/lib/__tests__/calc.test.ts": (
            "test('[US-0009.1] adds two positive numbers', () => { expect(add(1, 2)).toBe(3); });\n"
            "test('[US-0009.1] adds a negative number', () => { expect(add(1, -2)).toBe(-1); });\n"
        ),
    }
    assert "US-0009.1" not in ac_depth_report(two_unit_tests), (
        "two below-browser tests for the same AC, distinct assertion targets, must clear the depth check"
    )

    padded_tests = {
        "src/lib/__tests__/calc.test.ts": (
            "test('[US-0009.2] adds 1', () => { expect(add(1, 1)).toBe(2); });\n"
            "test('[US-0009.2] adds 2', () => { expect(add(1, 2)).toBe(3); });\n"
            "test('[US-0009.2] adds 3', () => { expect(add(1, 3)).toBe(4); });\n"
        ),
    }
    padded_depth = ac_depth_report(padded_tests)
    assert "US-0009.2" in padded_depth, "three tests asserting the SAME normalised target must be flagged as padding"
    assert any("distinct assertion" in p for p in padded_depth["US-0009.2"]["shortfalls"]), padded_depth

    assert "ac_depth" in run_all_checks(one_unit_test), "run_all_checks must surface ac_depth for the coverage hook to read"

    # non_testid_locators: the live incident (Next.js Server Action's hidden $ACTION_ID_ input
    # colliding with a bare `input` locator) plus every other disallowed query method.
    bad_e2e = {
        "apps/web/tests/e2e/settings.spec.ts": (
            "test('[US-0013.2] admin settings inputs visible', async ({ page }) => {\n"
            "  await expect(page.locator('input')).toBeVisible();\n"
            "  await expect(page.getByRole('button', { name: 'Save' })).toBeVisible();\n"
            "});\n"
        )
    }
    bad_violations = non_testid_locators(bad_e2e)
    assert "apps/web/tests/e2e/settings.spec.ts" in bad_violations, bad_violations
    assert len(bad_violations["apps/web/tests/e2e/settings.spec.ts"]) == 2, bad_violations

    good_e2e = {
        "apps/web/tests/e2e/settings.spec.ts": (
            "test('[US-0013.2] admin settings inputs visible', async ({ page }) => {\n"
            "  await expect(page.getByTestId('risk-free-rate-input')).toBeVisible();\n"
            "  await expect(page.locator('[data-testid=\"save-button\"]')).toBeVisible();\n"
            "});\n"
        )
    }
    assert non_testid_locators(good_e2e) == {}, non_testid_locators(good_e2e)

    # Scoped to e2e files only: the SAME getByRole call in a unit test (Testing Library's own
    # legitimate convention there) must never be flagged.
    unit_with_role_query = {
        "src/components/__tests__/SettingsForm.test.tsx": (
            "test('[US-0013.2] renders the save button', () => {\n"
            "  expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();\n"
            "});\n"
        )
    }
    assert non_testid_locators(unit_with_role_query) == {}, non_testid_locators(unit_with_role_query)

    assert "non_testid_locators" in run_all_checks(one_unit_test), (
        "run_all_checks must surface non_testid_locators for the same-turn hook to read"
    )

    print("test_quality_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"path": "contents", ...} on stdin, JSON results on
        # stdout. Deliberately the ONLY thing this branch does -- no sandbox access, no repo_files,
        # no network; the hook already read every file itself and hands the contents over as data.
        payload = json.loads(sys.stdin.read())
        json.dump(run_all_checks(payload), sys.stdout)
    else:
        _demo()
