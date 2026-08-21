"""One place to turn a test runner's report file into `{test name -> pass|fail}`.

These parsers previously lived in the two nodes that happened to need them first
(`test_hardening_nodes._parse_trx` / `_parse_vitest_json`, `e2e_nodes._parse_playwright_json`).
repo_scan's eval layer is the third consumer, and three copies of "what did the suite actually do"
is how the three drift apart -- so they moved here and the original modules import them.

Everything in this module is pure: a string in, a dict out. No sandbox, no I/O, self-checked at the
bottom. That matters because the eval layer's headline number ("how many acceptance criteria are
verified by a test that actually passes") is only as trustworthy as these parsers, and a parser
nobody can run in isolation is a parser nobody checks.
"""

from __future__ import annotations

import json
import re
from typing import Any

# defusedxml, not stdlib ElementTree: a .trx is produced inside the sandbox by a test suite an LLM
# wrote, so it is not trusted input, and stdlib's parser is vulnerable to entity-expansion and
# external-entity attacks by default. test_hardening_nodes already used defusedxml here -- the
# stdlib import was a regression introduced while moving this function, not a decision.
import defusedxml.ElementTree as ET

# US-0001.2 / US-0001-2 / "US-0001.2:" -- the forms an AC id takes inside a test NAME, which is
# where the runner reports it. Deliberately tolerant of the '.'/'-' split because .NET test names
# cannot contain '.' in some attribute forms and models emit both.
# Every spelling an AC id takes inside a test NAME. All three forms below are real, taken from this
# pipeline's own generated suites:
#
#   US-0001.2   the canonical ledger form (Playwright titles, which are free text)
#   US_0001_2   identifier-safe (a C# method name cannot contain '-' or '.')
#   US00012     punctuation stripped entirely -- `TestUS00012ResolveStateDirectory...`
#
# The third one is why this regex exists in this shape. A matcher that knew only the first two
# attributed ZERO of ten real passing .NET tests to any criterion, and then reported "10 criteria
# never exercised" -- a wrong answer indistinguishable from a true one.
#
# Separators are optional, so the story is pinned at 4 digits and everything after it is the
# criterion number: `US00012` is unambiguously US-0001.2, `US000112` is US-0001.12. The trailing
# `(?!\d)` stops `US00012` from matching inside `US000123`.
#
# No leading boundary, deliberately: the compact form appears mid-identifier (`TestUS00012...`), so
# requiring one would miss exactly the case this was written for. The cost is that a name like
# `BUS00012` would match; crediting a criterion that is not there is a worse-than-ideal trade, but
# strictly better than crediting nothing, and no real test name looks like that.
#
# Separators are ANY run of non-alphanumerics (or none) on both sides of the story number, rather
# than an enumerated set. Listing them is how this stayed broken: `[-_]?` handled
# `TestUS00012Resolve` and `us-0001_2` but silently missed `US 0001.2` and `Test.US.0001.2.Works`,
# and each miss reads as "this criterion has no tests" rather than as a parse failure.
_AC_IN_NAME_RE = re.compile(r"(?:US|AC)[^A-Za-z0-9]{0,2}(\d{4})[^A-Za-z0-9]{0,2}(\d+)(?!\d)", re.IGNORECASE)


# The canonical form: the ledger id, punctuation intact, bracketed at the front of the test's
# DISPLAY name -- `[US-0001.2] counter loads persisted value`.
#
# This is the primary attribution mechanism, and it is deliberately a display name rather than a
# framework trait. Both alternatives were probed against a real runner before choosing:
#
#   xUnit `[Trait("AC","US-0001.2")]`  -- the value NEVER reaches the .trx. No <Properties>, no
#                                         <TestCategory>; the string "US-0001.2" appears nowhere in
#                                         the file. A parser for it would read a field that does not
#                                         exist.
#   xUnit `[Fact(DisplayName = "...")]` -- becomes the trx's own `testName` attribute, verbatim,
#                                         punctuation and all. Confirmed on a live `dotnet test`.
#
# So the display name IS the structured field here, and it works identically for vitest and
# Playwright, where `test('[US-0001.2] ...')` is already the natural way to write it. Crucially it
# removes the reason the old scheme was brittle: a C# METHOD name cannot contain `-` or `.`, which
# forced `TestUS00012...` and the punctuation-stripped matching that silently failed. A display name
# has no such restriction.
_CANONICAL_AC_RE = re.compile(r"\[(US-\d{4}\.\d+)\]")


def attributed_ac_ids(test_name: str) -> tuple[list[str], str]:
    """`(ids, mechanism)` where mechanism is 'canonical' | 'fallback' | 'none'.

    Canonical bracketed ids win outright. The tolerant name matcher below is kept as a FALLBACK for
    suites written before the convention (and for pytest, whose method names cannot carry
    punctuation either), but the mechanism is returned so callers can report how often the reliable
    path was actually used -- a fallback nobody measures is a fallback that quietly becomes the norm.
    """
    canonical = _CANONICAL_AC_RE.findall(test_name or "")
    if canonical:
        deduped: list[str] = []
        for ac_id in canonical:
            if ac_id.upper() not in {d.upper() for d in deduped}:
                deduped.append(ac_id.upper())
        return deduped, "canonical"
    tolerant = ac_ids_in_name(test_name)
    return tolerant, "fallback" if tolerant else "none"


def attribution_health(test_names: list[str]) -> dict[str, int]:
    """How each test in a suite was attributed. Surfaced so the convention's adoption is visible."""
    tally = {"canonical": 0, "fallback": 0, "unattributed": 0}
    for name in test_names:
        _, mechanism = attributed_ac_ids(name)
        tally["unattributed" if mechanism == "none" else mechanism] += 1
    return tally


def ac_ids_in_name(test_name: str) -> list[str]:
    """Every AC id mentioned in a test name, normalised to `US-0001.2`.

    One test can legitimately cover several criteria, so this returns a list rather than the first
    match -- crediting only the first would under-count verification for exactly the well-written
    integration tests we want to encourage.
    """
    seen: list[str] = []
    for story, criterion in _AC_IN_NAME_RE.findall(test_name or ""):
        # Always normalised to the ledger's `US-` prefix: models re-prefix ids as `AC-0003.6`
        # despite instructions, and the NUMBERING is what identifies the criterion -- the same
        # rule ac_coverage_gate.id_variants applies.
        normalized = f"US-{story}.{criterion}"
        if normalized not in seen:
            seen.append(normalized)
    return seen


def parse_trx(raw_xml: str) -> dict[str, str]:
    """testName -> 'pass'|'fail', from a Visual Studio Test Results (.trx) file."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return {}
    ns = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}
    results: dict[str, str] = {}
    for result in root.findall(".//t:UnitTestResult", ns) or root.findall(".//UnitTestResult"):
        name = result.get("testName", "unknown")
        outcome = result.get("outcome", "")
        results[name] = "pass" if outcome.lower() == "passed" else "fail"
    return results


def parse_vitest_json(raw_json: str) -> dict[str, str]:
    """testName -> 'pass'|'fail', from vitest/jest `--reporter=json`."""
    try:
        doc = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    results: dict[str, str] = {}
    for test_result in doc.get("testResults", []):
        for assertion in test_result.get("assertionResults", []):
            name = assertion.get("fullName") or assertion.get("title", "unknown")
            results[name] = "pass" if assertion.get("status") == "passed" else "fail"
    return results


def _iter_specs(suite: dict[str, Any]) -> Any:
    """Playwright nests suites arbitrarily deep; specs can appear at any level."""
    yield from suite.get("specs") or []
    for child in suite.get("suites") or []:
        yield from _iter_specs(child)


def parse_playwright_json(raw_json: str) -> dict[str, Any]:
    """Playwright's `--reporter=json` output -> {passed, failed_tests: [{title, error}], total}.
    A test's outcome is judged on its LAST result only -- retries produce multiple results for the
    same test, and only the final one decides pass/fail.

    NEVER returns total==0 with an empty failed_tests: that shape is indistinguishable from "ran
    zero tests, so vacuously all passed", which would route the run straight past the fix/escalate
    loop on exactly the failures (a globalSetup throw, a config syntax error, a suite that never
    actually started) it exists to catch. Malformed JSON and a structurally-empty report each get
    their own synthetic failed_tests entry instead; playwright's own top-level `errors` (set when
    something broke before any test could run) are surfaced as real failures when present.
    """
    try:
        doc = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"passed": 0, "total": 0, "failed_tests": [{"title": "e2e report", "error": "e2e-report.json was not valid JSON"}]}

    passed = 0
    total = 0
    failed_tests: list[dict[str, str]] = []
    for suite in doc.get("suites") or []:
        for spec in _iter_specs(suite):
            title = spec.get("title", "unknown")
            for test in spec.get("tests") or []:
                total += 1
                results = test.get("results") or []
                outcome = results[-1] if results else {}
                if outcome.get("status") == "passed":
                    passed += 1
                else:
                    error = ((outcome.get("error") or {}).get("message")) or outcome.get("status") or "unknown failure"
                    # A hung test's top-level message is just "Test timeout of 30000ms exceeded" --
                    # the actionable part (WHICH awaited call hung, with a code frame pointing at
                    # the spec line) lives in results[].errors[].message. Observed live: 8 fix laps
                    # burned on two hanging journeys whose feedback never named the hanging call.
                    for extra in outcome.get("errors") or []:
                        frame = str((extra or {}).get("message") or "")
                        if frame and frame not in error:
                            error = f"{error}\n{frame}"
                    failed_tests.append({"title": title, "error": str(error)})

    if total == 0:
        top_errors = doc.get("errors") or []
        if top_errors:
            for err in top_errors:
                message = err.get("message") if isinstance(err, dict) else str(err)
                failed_tests.append({"title": "e2e suite setup", "error": str(message or err)})
        else:
            failed_tests.append({"title": "e2e suite", "error": "e2e-report.json contained no tests and no top-level errors"})

    return {"passed": passed, "failed_tests": failed_tests, "total": total}


def playwright_outcomes(raw_json: str) -> dict[str, str]:
    """parse_playwright_json reshaped as `{title -> pass|fail}`, for the eval layer.

    A title that both passed and failed within one report (parameterised projects -- the same spec
    run against chromium and firefox) counts as a FAIL: "it passes in one browser" is not the
    property being measured.
    """
    parsed = parse_playwright_json(raw_json)
    failed = {entry["title"] for entry in parsed["failed_tests"]}
    outcomes: dict[str, str] = {}
    try:
        doc = json.loads(raw_json)
    except json.JSONDecodeError:
        return {entry["title"]: "fail" for entry in parsed["failed_tests"]}
    for suite in doc.get("suites") or []:
        for spec in _iter_specs(suite):
            title = spec.get("title", "unknown")
            outcomes[title] = "fail" if title in failed else "pass"
    for entry in parsed["failed_tests"]:  # synthetic setup/empty-report failures have no spec
        outcomes.setdefault(entry["title"], "fail")
    return outcomes


def merge_outcomes(*outcome_maps: dict[str, str]) -> dict[str, str]:
    """Union of several runners' results. A name failing anywhere fails overall."""
    merged: dict[str, str] = {}
    for outcomes in outcome_maps:
        for name, result in outcomes.items():
            if merged.get(name) == "fail" or result == "fail":
                merged[name] = "fail"
            else:
                merged[name] = result
    return merged


def repo_relative(path: str) -> str | None:
    """A model-reported path normalised to repo-relative, or None when it escapes the repo.

    Models report absolute container paths despite being asked for repo-relative ones (observed:
    '/workspace/repo/test-results-0/test-results-0.trx'), and repo_files rejects those with a
    ValueError that used to propagate straight out of the node and kill the run.
    """
    cleaned = (path or "").strip()
    if not cleaned:
        return None
    for prefix in ("/workspace/repo/", "workspace/repo/", "./"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if cleaned.startswith("/") or ".." in cleaned.split("/"):
        return None
    return cleaned or None


def _demo() -> None:
    """`cd agent && uv run python -m src.test_results`."""
    trx = """<?xml version="1.0"?>
    <TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
      <Results>
        <UnitTestResult testName="Increment_US-0001.1_ReturnsOne" outcome="Passed" />
        <UnitTestResult testName="Reset_US-0003.2_ClearsValue" outcome="Failed" />
      </Results>
    </TestRun>"""
    parsed = parse_trx(trx)
    assert parsed == {"Increment_US-0001.1_ReturnsOne": "pass", "Reset_US-0003.2_ClearsValue": "fail"}, parsed

    # AC extraction: both separators, case-insensitive, multiple ids, and no false positives.
    assert ac_ids_in_name("Increment_US-0001.1_ReturnsOne") == ["US-0001.1"]
    assert ac_ids_in_name("covers US-0001-2 and us-0002.3") == ["US-0001.2", "US-0002.3"]
    assert ac_ids_in_name("no ids here") == []
    # A bare story id with no criterion number is NOT an AC id and must not be invented into one.
    assert ac_ids_in_name("US-0001 general behaviour") == []

    vitest = json.dumps({"testResults": [{"assertionResults": [
        {"fullName": "counter US-0002.1 increments", "status": "passed"},
        {"fullName": "counter US-0002.2 resets", "status": "failed"},
    ]}]})
    assert parse_vitest_json(vitest) == {"counter US-0002.1 increments": "pass", "counter US-0002.2 resets": "fail"}

    playwright = json.dumps({"suites": [{"specs": [
        {"title": "US-0005.1 renders", "tests": [{"results": [{"status": "passed"}]}]},
        {"title": "US-0005.2 persists", "tests": [{"results": [{"status": "failed", "error": {"message": "boom"}}]}]},
    ]}]})
    assert playwright_outcomes(playwright) == {"US-0005.1 renders": "pass", "US-0005.2 persists": "fail"}

    # A retried test is judged on its LAST result, and an empty report never reads as "all passed".
    retried = json.dumps({"suites": [{"specs": [
        {"title": "flaky", "tests": [{"results": [{"status": "failed"}, {"status": "passed"}]}]}]}]})
    assert parse_playwright_json(retried)["passed"] == 1
    empty = parse_playwright_json(json.dumps({"suites": []}))
    assert empty["total"] == 0 and empty["failed_tests"], empty

    # Same title passing in one project and failing in another is a FAIL overall.
    cross_project = json.dumps({"suites": [
        {"specs": [{"title": "dual", "tests": [{"results": [{"status": "passed"}]}]}]},
        {"specs": [{"title": "dual", "tests": [{"results": [{"status": "failed"}]}]}]},
    ]})
    assert playwright_outcomes(cross_project) == {"dual": "fail"}

    assert merge_outcomes({"a": "pass"}, {"a": "fail", "b": "pass"}) == {"a": "fail", "b": "pass"}
    assert merge_outcomes({"a": "fail"}, {"a": "pass"}) == {"a": "fail"}

    assert repo_relative("/workspace/repo/x.trx") == "x.trx"
    assert repo_relative("../escape") is None
    assert repo_relative("") is None
    # A dot-directory must survive the "./" strip -- `lstrip("./")` would eat the leading dot and
    # produce a path that does not exist.
    assert repo_relative("./.ai-dev-workflow/history/x.json") == ".ai-dev-workflow/history/x.json"
    assert repo_relative("./agent-work/e2e-report.json") == "agent-work/e2e-report.json"

    # --- canonical attribution (primary) vs tolerant name matching (measured fallback) -----------
    # The exact string a live `dotnet test` put in the trx for [Fact(DisplayName = "[US-0001.2] ...")].
    real_display_name = "[US-0001.2] counter loads persisted value"
    assert attributed_ac_ids(real_display_name) == (["US-0001.2"], "canonical")
    # Playwright's natural form is already canonical.
    assert attributed_ac_ids("[US-0003.2] decrement at zero shows message") == (["US-0003.2"], "canonical")
    # A method name with punctuation stripped still attributes -- via the fallback, and it says so.
    assert attributed_ac_ids("Api.Tests.T.TestUS00012Resolve") == (["US-0001.2"], "fallback")
    # A test naming no criterion is 'none', never silently credited to one.
    assert attributed_ac_ids("ProgramConstructorIsReachable") == ([], "none")
    # Canonical wins outright when both forms are present, so one test is not counted twice over.
    both = attributed_ac_ids("[US-0005.1] covers TestUS00099Thing")
    assert both == (["US-0005.1"], "canonical"), both
    # Several criteria in one display name are all credited.
    assert attributed_ac_ids("[US-0001.1] and [US-0001.2] both")[0] == ["US-0001.1", "US-0001.2"]

    health = attribution_health([real_display_name, "TestUS00012Resolve", "HelperMethod"])
    assert health == {"canonical": 1, "fallback": 1, "unattributed": 1}, health

    print("test_results self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
