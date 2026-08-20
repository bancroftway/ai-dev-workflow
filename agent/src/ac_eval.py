"""The Eval layer: how many acceptance criteria are actually verified by tests that actually pass.

repo_scan answers "what is wrong with this code". This answers the question the pipeline is really
judged on -- "is the thing we were asked to build demonstrably built" -- and it does so for any
repository with a ledger and a test suite, without an LLM anywhere in the loop.

Two halves, deliberately kept apart:

- **`ac_verification` -- static.** Which criteria have tests naming them, and at which levels. Pure
  analysis of the worktree, so it is part of the scan's `content_hash`: an unchanged repo must
  produce an unchanged verification report.
- **`ac_execution` -- dynamic.** What happened when the suites actually ran, per criterion, over
  several attempts so flakiness is visible. This CANNOT be part of `content_hash` -- repo_scan's
  contract is that two runs over an unchanged worktree hash identically, and a flaky test breaks
  that by definition. It is excluded exactly as `generated_at` and per-tool durations are.

The headline number is `solidly_verified`: criteria that are linked to a test AND green AND not
flaky. "Linked" alone is the number that flatters; this one is the number that means something.

**Failure is reported, never faked.** Every way this can fail to measure -- no ledger, no tests, no
runnable command, a runner that crashes -- yields `status: "not_evaluated"` with a stable reason
code. A fabricated zero and a real zero must never look alike, which is the same rule the coverage
gate learned when stale artifacts summed to a fictional 88.9%.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from typing import Any

from . import repo_files, spec_ledger, test_results
from .test_results import ac_ids_in_name
from .gates.ac_coverage_gate import classify_test_level, count_tests_per_ac, id_variants
from .gates.test_coverage_gate import _with_timeout
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# How many times each suite runs. Flakiness is invisible at 1, and the per-AC flake rate is one of
# the three metrics this layer exists to produce.
EVAL_ATTEMPTS = int(os.environ.get("EVAL_ATTEMPTS", "3"))

# Bounds one suite invocation. A hung test run must not hang the scan.
EVAL_TIMEOUT_SECONDS = int(os.environ.get("EVAL_TIMEOUT_SECONDS", "900"))

_TEST_LISTING_COMMAND = (
    "git ls-files -co --exclude-standard | grep -iE '(test|spec)' "
    r"| grep -vE '(^|/)(node_modules|\.playwright-browsers|bin|obj|dist|build|\.next|\.venv|vendor|TestResults|coverage|\.ai-dev-workflow|agent-work)/' "
    "| head -200 || true"
)


# ------------------------------------------------------------------------------------------------
# Pure half -- self-checked at the bottom of this module.
# ------------------------------------------------------------------------------------------------


def verification_summary(ac_ids: list[str], test_files: dict[str, str]) -> dict[str, Any]:
    """Static: which criteria are named by tests, and at which levels.

    `levels` counts CRITERIA per level, not tests: "9 criteria have an e2e test" is the pyramid
    people want to read, whereas a test count is dominated by whichever suite happens to be
    chattiest. Per-criterion test counts stay available in `counts_per_ac`.
    """
    counts = count_tests_per_ac(ac_ids, test_files) if test_files else {}
    levels = {"unit": 0, "integration": 0, "e2e": 0}
    linked: list[str] = []
    unverified: list[str] = []
    e2e_only: list[str] = []
    for ac_id in ac_ids:
        per_level = counts.get(ac_id) or {}
        present = [level for level in ("unit", "integration", "e2e") if per_level.get(level, 0) > 0]
        for level in present:
            levels[level] += 1
        if present:
            linked.append(ac_id)
            if present == ["e2e"]:
                e2e_only.append(ac_id)
        else:
            unverified.append(ac_id)
    return {
        "total": len(ac_ids),
        "linked": len(linked),
        "unverified": unverified,
        "levels": levels,
        "e2e_only": e2e_only,
        "counts_per_ac": counts,
    }


def attribute_outcomes(ac_ids: list[str], outcomes: dict[str, str]) -> dict[str, str]:
    """`{ac_id -> 'pass'|'fail'}` for one attempt, from `{test name -> pass|fail}`.

    A criterion is `pass` for an attempt only when EVERY test naming it passed. One failing test
    naming the criterion means the criterion is not demonstrated, regardless of how many others
    passed -- the alternative rewards adding a second, easier test next to a failing one.
    """
    known = set(ac_ids)
    per_ac: dict[str, str] = {}
    for test_name, result in outcomes.items():
        # test_results.ac_ids_in_name, not substring matching against id_variants: the real .NET
        # names strip all punctuation (`TestUS00012...`), which no variant of `US-0001.2` contains.
        # Substring matching returned zero attributions for a fully passing suite.
        for ac_id in ac_ids_in_name(test_name):
            if ac_id not in known:
                continue
            if per_ac.get(ac_id) == "fail" or result == "fail":
                per_ac[ac_id] = "fail"
            else:
                per_ac[ac_id] = "pass"
    return per_ac


def execution_summary(ac_ids: list[str], attempts: list[dict[str, str]]) -> dict[str, Any]:
    """Dynamic: per-criterion run/pass counts, flake flags, and the solidly-verified headline.

    `attempts` is one `{test name -> pass|fail}` map per run of the suites.
    """
    if not attempts:
        return {"status": "not_evaluated", "reason": "no_attempts_recorded"}

    per_attempt = [attribute_outcomes(ac_ids, outcomes) for outcomes in attempts]
    per_ac: dict[str, dict[str, Any]] = {}
    passing = failing = not_run = 0
    solidly_verified: list[str] = []
    for ac_id in ac_ids:
        seen = [results[ac_id] for results in per_attempt if ac_id in results]
        if not seen:
            not_run += 1
            per_ac[ac_id] = {"runs": 0, "passed": 0, "flaky": False, "status": "not_run"}
            continue
        passed = sum(1 for result in seen if result == "pass")
        flaky = 0 < passed < len(seen)
        status = "pass" if passed == len(seen) else "fail"
        if status == "pass":
            passing += 1
            solidly_verified.append(ac_id)
        else:
            failing += 1
        per_ac[ac_id] = {"runs": len(seen), "passed": passed, "flaky": flaky, "status": status}

    flaky_ids = sorted(ac_id for ac_id, row in per_ac.items() if row["flaky"])
    return {
        "status": "evaluated",
        "attempts": len(attempts),
        "passing": passing,
        "failing": failing,
        "not_run": not_run,
        "flaky": flaky_ids,
        # Linked AND green AND not flaky. A criterion that passes 2 of 3 runs is NOT solidly
        # verified -- it is a criterion whose test sometimes agrees with it.
        "solidly_verified": len(solidly_verified),
        "per_ac": per_ac,
    }


def not_evaluated(reason: str) -> dict[str, Any]:
    """The only shape allowed to represent "could not measure". Never a zero."""
    return {"status": "not_evaluated", "reason": reason}


def result_command(root: str, command: str) -> tuple[str, str, str] | None:
    """Turn a coverage-commands entry into one that also emits a parseable RESULT file.

    Returns `(shell command, artifact path, format)`, or None when the runner is not one whose
    result format we can parse. The mapping is deliberately narrow -- guessing build commands per
    stack is what this pipeline spent 25 runs removing -- so an unrecognised runner degrades to
    `not_evaluated` rather than to an invented command.
    """
    lowered = command.lower()
    prefix = f"cd {shlex.quote(root)} && " if root and root not in (".", "./") else ""
    if "dotnet test" in lowered:
        artifact = f"{root.rstrip('/')}/TestResults/ac-eval.trx" if root not in ("", ".", "./") else "TestResults/ac-eval.trx"
        return (
            f'{prefix}dotnet test --logger "trx;LogFileName=ac-eval.trx" --results-directory TestResults',
            artifact,
            "trx",
        )
    if "playwright test" in lowered:
        artifact = f"{root.rstrip('/')}/ac-eval-playwright.json" if root not in ("", ".", "./") else "ac-eval-playwright.json"
        return (
            f"{prefix}npx playwright test --reporter=json > ac-eval-playwright.json 2>/dev/null || true",
            artifact,
            "playwright",
        )
    if "vitest" in lowered or "jest" in lowered or "npm test" in lowered or "npm run test" in lowered:
        artifact = f"{root.rstrip('/')}/ac-eval-vitest.json" if root not in ("", ".", "./") else "ac-eval-vitest.json"
        return (
            f"{prefix}npx vitest run --reporter=json --outputFile=ac-eval-vitest.json || true",
            artifact,
            "vitest",
        )
    return None


def parse_artifact(fmt: str, raw: str) -> dict[str, str]:
    if fmt == "trx":
        return test_results.parse_trx(raw)
    if fmt == "vitest":
        return test_results.parse_vitest_json(raw)
    if fmt == "playwright":
        return test_results.playwright_outcomes(raw)
    return {}


# ------------------------------------------------------------------------------------------------
# Sandbox-I/O half
# ------------------------------------------------------------------------------------------------


async def _collect_test_files(provider: SandboxProvider, thread_id: str) -> dict[str, str]:
    listing = await provider.exec_in_sandbox(thread_id, _TEST_LISTING_COMMAND)
    paths = [line.strip() for line in (listing.stdout or "").splitlines() if line.strip()]
    files: dict[str, str] = {}
    for path in paths:
        contents = await repo_files.read_repo_file(provider, thread_id, path)
        if contents is not None:
            files[path] = contents
    return files


async def _ac_ids(provider: SandboxProvider, thread_id: str) -> list[str]:
    entries = await spec_ledger.load_ledger(provider, thread_id)
    ids: list[str] = []
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        # Criterion-level ids only (US-0001.2), not story-level ones (US-0001): a story is not a
        # testable assertion, and counting it would inflate the denominator.
        if "." in entry_id:
            ids.append(entry_id)
    return sorted(set(ids))


async def _run_suites(
    provider: SandboxProvider, thread_id: str, entries: list[dict[str, Any]], attempts: int
) -> tuple[list[dict[str, str]], list[str]]:
    """Run every recognised suite `attempts` times. Returns (per-attempt outcome maps, notes)."""
    runnable: list[tuple[str, str, str]] = []
    notes: list[str] = []
    for entry in entries:
        root = str(entry.get("root") or "")
        command = str(entry.get("command") or "")
        resolved = result_command(root, command)
        if resolved is None:
            notes.append(f"unrecognised runner, skipped: {command!r} in {root or '.'}")
            continue
        runnable.append(resolved)

    per_attempt: list[dict[str, str]] = []
    for attempt in range(attempts):
        outcomes: dict[str, str] = {}
        for command, artifact, fmt in runnable:
            # exec_in_sandbox takes no timeout argument -- bounding is done in the shell, via the
            # coverage gate's existing `_with_timeout` (which wraps the WHOLE command in `sh -c` so
            # a chained `cd X && dotnet test` is bounded as one unit rather than just the `cd`).
            result = await provider.exec_in_sandbox(thread_id, _with_timeout(command, EVAL_TIMEOUT_SECONDS))
            raw = await repo_files.read_repo_file(provider, thread_id, artifact)
            if raw is None:
                notes.append(f"attempt {attempt + 1}: no result artifact at {artifact}")
                continue
            parsed = parse_artifact(fmt, raw)
            if not parsed:
                notes.append(f"attempt {attempt + 1}: {artifact} produced no parseable results")
                continue
            outcomes = test_results.merge_outcomes(outcomes, parsed)
            if not result.ok:
                # A non-zero exit with parseable results is normal (tests failed). Recorded, not
                # treated as an infrastructure error -- the results are the evidence either way.
                logger.debug("eval: %s exited %s with parseable results", command, result.returncode)
        if outcomes:
            per_attempt.append(outcomes)
        else:
            notes.append(f"attempt {attempt + 1}: no suite produced results")
    return per_attempt, notes


async def _browser_outcomes(provider: SandboxProvider, thread_id: str) -> tuple[dict[str, str], str | None]:
    """This run's Playwright results, READ from its report rather than re-run. `({}, None)` if absent.

    Deliberately not executed here. A browser suite needs the application booted -- which the e2e
    cluster does, with port reservation, readiness polling and API env injection -- so running it
    from inside a scan would fail every browser test for want of a server and report criteria as
    BROKEN when they are merely unexercised. Reading the report attributes real browser evidence
    without pretending this layer can produce it.
    """
    listing = await provider.exec_in_sandbox(
        thread_id,
        "find . -path ./node_modules -prune -o -name 'e2e-report.json' -print 2>/dev/null | head -5 || true",
    )
    outcomes: dict[str, str] = {}
    found: str | None = None
    for line in (listing.stdout or "").splitlines():
        # repo_relative strips the "./" prefix itself. Doing it here with lstrip("./") would be a
        # bug: lstrip removes any leading '.' or '/' CHARACTER, so "./.ai-dev-workflow/x" becomes
        # "ai-dev-workflow/x" -- a path that does not exist.
        path = test_results.repo_relative(line.strip())
        if not path:
            continue
        raw = await repo_files.read_repo_file(provider, thread_id, path)
        if not raw:
            continue
        parsed = test_results.playwright_outcomes(raw)
        if parsed:
            outcomes = test_results.merge_outcomes(outcomes, parsed)
            found = path
    return outcomes, found


async def evaluate(
    provider: SandboxProvider, thread_id: str, *, attempts: int = EVAL_ATTEMPTS, run_suites: bool = True
) -> dict[str, Any]:
    """`{"ac_verification": {...}, "ac_execution": {...}}`.

    `run_suites=False` gives the static half only, which is what a caller wanting a hashable report
    without paying for N suite runs should ask for.
    """
    ac_ids = await _ac_ids(provider, thread_id)
    if not ac_ids:
        return {"ac_verification": not_evaluated("no_ledger_entries"), "ac_execution": not_evaluated("no_ledger_entries")}

    test_files = await _collect_test_files(provider, thread_id)
    verification = verification_summary(ac_ids, test_files)
    if not test_files:
        return {"ac_verification": verification, "ac_execution": not_evaluated("no_test_files")}
    if not run_suites:
        return {"ac_verification": verification, "ac_execution": not_evaluated("execution_not_requested")}

    from .gates.test_coverage_gate import COVERAGE_COMMANDS_PATH

    raw_commands = await repo_files.read_repo_file(provider, thread_id, COVERAGE_COMMANDS_PATH)
    if raw_commands is None:
        return {"ac_verification": verification, "ac_execution": not_evaluated("no_coverage_commands_contract")}
    try:
        entries = (json.loads(raw_commands) or {}).get("entries") or []
    except json.JSONDecodeError:
        return {"ac_verification": verification, "ac_execution": not_evaluated("coverage_commands_unparseable")}
    if not entries:
        return {"ac_verification": verification, "ac_execution": not_evaluated("no_test_commands_declared")}

    per_attempt, notes = await _run_suites(provider, thread_id, entries, attempts)

    # Browser evidence is folded into the FIRST attempt only, because it IS one observation -- the
    # report was written by a single e2e run. Spreading it across all attempts would claim three
    # runs' worth of confidence (and a flake rate) from one, which is the sort of arithmetic that
    # makes a metric worse than no metric.
    browser, browser_path = await _browser_outcomes(provider, thread_id)
    if browser:
        notes.append(f"browser results read from {browser_path} (1 observation, not re-run)")
        if per_attempt:
            per_attempt[0] = test_results.merge_outcomes(per_attempt[0], browser)
        else:
            per_attempt = [browser]

    if not per_attempt:
        execution = not_evaluated("no_suite_produced_results")
        execution["notes"] = notes[:20]
    else:
        execution = execution_summary(ac_ids, per_attempt)
        if notes:
            execution["notes"] = notes[:20]
    return {"ac_verification": verification, "ac_execution": execution}


def _demo() -> None:
    """`cd agent && uv run python -m src.ac_eval`."""
    ac_ids = ["US-0001.1", "US-0001.2", "US-0002.1", "US-0003.2"]
    test_files = {
        "apps/api.Tests/CounterTests.cs": (
            "using Microsoft.AspNetCore.Mvc.Testing;\n"
            "public class T { [Fact] public void Increment_US-0001.1_Works() {} \n"
            "[Fact] public void Reset_US_0002_1_Works() {} }"
        ),
        "apps/web/e2e/counter.spec.ts": "test('US-0001.2 persists', async () => {});",
    }
    verification = verification_summary(ac_ids, test_files)
    assert verification["total"] == 4, verification
    # US-0003.2 is named by no test at all.
    assert verification["unverified"] == ["US-0003.2"], verification
    assert verification["linked"] == 3, verification
    # US-0001.2 lives only in the e2e spec -- proven at the browser layer and nowhere else, which is
    # a fact worth surfacing rather than averaging away.
    assert verification["e2e_only"] == ["US-0001.2"], verification
    assert verification["levels"]["e2e"] == 1, verification["levels"]

    # A criterion is pass for an attempt only if EVERY test naming it passed.
    outcomes = {"Increment_US-0001.1_Works": "pass", "Other_US-0001.1_Case": "fail"}
    assert attribute_outcomes(ac_ids, outcomes)["US-0001.1"] == "fail"

    # Flake detection across attempts, and the headline excluding the flaky one.
    attempts = [
        {"Increment_US-0001.1_Works": "pass", "US-0002.1 thing": "pass"},
        {"Increment_US-0001.1_Works": "fail", "US-0002.1 thing": "pass"},
        {"Increment_US-0001.1_Works": "pass", "US-0002.1 thing": "pass"},
    ]
    summary = execution_summary(ac_ids, attempts)
    assert summary["per_ac"]["US-0001.1"] == {"runs": 3, "passed": 2, "flaky": True, "status": "fail"}, summary["per_ac"]
    assert summary["per_ac"]["US-0002.1"]["status"] == "pass"
    assert summary["flaky"] == ["US-0001.1"], summary
    # Only US-0002.1 is linked AND green AND stable.
    assert summary["solidly_verified"] == 1, summary
    # Criteria no test touched are not_run, never counted as passing.
    assert summary["per_ac"]["US-0003.2"]["status"] == "not_run"
    assert summary["not_run"] == 2, summary  # US-0001.2 and US-0003.2

    # "Could not measure" is never a zero.
    assert execution_summary(ac_ids, []) == {"status": "not_evaluated", "reason": "no_attempts_recorded"}
    assert not_evaluated("no_test_files")["status"] == "not_evaluated"

    # Command derivation: recognised runners only, and each emits a parseable artifact.
    dotnet = result_command("apps/api.Tests", 'dotnet test --collect:"XPlat Code Coverage"')
    assert dotnet is not None and dotnet[2] == "trx"
    assert dotnet[1] == "apps/api.Tests/TestResults/ac-eval.trx", dotnet
    assert dotnet[0].startswith("cd apps/api.Tests && "), dotnet[0]
    # A root needing quoting gets it -- the command is assembled into a shell string, so a path with
    # a space must not split into two arguments.
    spaced = result_command("apps/my tests", "dotnet test")
    assert spaced[0].startswith("cd 'apps/my tests' && "), spaced[0]
    assert result_command("apps/web", "npx playwright test")[2] == "playwright"
    assert result_command("apps/web", "npm run test:unit")[2] == "vitest"
    # An unrecognised runner yields None -- NOT an invented command.
    assert result_command("svc", "bazel test //...") is None
    assert result_command("svc", "make check") is None

    # Parsers are reached through the shared module, so a format we claim to support really parses.
    trx = (
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
        '<UnitTestResult testName="Increment_US-0001.1_Works" outcome="Passed" />'
        "</Results></TestRun>"
    )
    assert parse_artifact("trx", trx) == {"Increment_US-0001.1_Works": "pass"}
    assert parse_artifact("unknown-format", trx) == {}

    # classify_test_level is imported rather than reimplemented -- assert the contract we rely on.
    assert classify_test_level("apps/web/e2e/x.spec.ts", "") == "e2e"

    # Attribution must handle the punctuation-stripped .NET form. These are REAL test names from a
    # live run whose 10 passing tests attributed to ZERO criteria before ac_ids_in_name was used
    # here -- and then reported "10 criteria never exercised", which read exactly like a true result.
    real_names = {
        "Api.Tests.CounterApiIntegrationTests.TestUS00012ResolveStateDirectoryUsesExplicitSetting": "pass",
        "Api.Tests.CounterApiIntegrationTests.TestUS00034DecrementAtZeroIsRejectedByLowerBound": "pass",
        "Api.Tests.ProgramCoverageTests.ProgramProtectedConstructorIsReachableForCoverage": "pass",
    }
    real_ids = ["US-0001.2", "US-0003.4", "US-0009.9"]
    attributed = attribute_outcomes(real_ids, real_names)
    assert attributed == {"US-0001.2": "pass", "US-0003.4": "pass"}, attributed
    # A test naming no criterion is not attributed to one, and an unexercised criterion stays not_run.
    assert execution_summary(real_ids, [real_names])["per_ac"]["US-0009.9"]["status"] == "not_run"

    print("ac_eval self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
