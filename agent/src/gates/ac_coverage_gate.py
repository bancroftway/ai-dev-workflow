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
import re
import shlex
from dataclasses import dataclass
from typing import Any

from .. import repo_files, stack_runner, tech_stack_signals
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

    if missing or tautological:
        reasons = []
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
