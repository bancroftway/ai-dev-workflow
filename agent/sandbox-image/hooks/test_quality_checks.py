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
...]}`) is what the Stop hook actually invokes. `absence_only`/`fiat_stubs` are unscoped by
Acceptance Criterion on purpose (the hook has no ledger to attribute tests to; a bad test is a bad
test whichever criterion it claims), unlike `ac_coverage_gate.py`'s own per-AC callers which filter
`_iter_tests`' output down to one AC's own tests via `_tests_for_ac`. `duplicates` is NOT unscoped
this same way -- see `duplicate_pairs_by_ac`'s own docstring for the live incident that proved
near-duplicate detection needs per-criterion grouping even without a ledger.
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


def run_all_checks(test_files: dict[str, str]) -> dict[str, object]:
    """Everything the CLI entry point reports, computed once over one shared parse -- the same
    function a unit test can call directly, so the CLI wrapper below has no logic of its own to
    drift from what a caller importing this module gets."""
    tests = all_tests(test_files)
    return {
        "absence_only": absence_only_labels(tests),
        "fiat_stubs": fiat_stub_labels(tests),
        "duplicates": [list(pair) for pair in duplicate_pairs_by_ac(tests)],
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
