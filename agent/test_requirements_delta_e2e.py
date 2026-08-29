#!/usr/bin/env python
"""Two-run requirements-delta E2E: proves that changing requirements between runs produces a
correct delta in the spec, plan, tests, code, and exit report -- the provenance pipeline's
end-to-end acceptance test.

Run 1 (angular-dotnet, greenfield): a counter app -- Story A (increment + display, 2 ACs) and
Story B (reset button + endpoint, 1 AC). Runs the full pipeline to a merge-ready exit.

Run 2 (same thread, --fresh-run): Story A's display criterion is MODIFIED (show the doubled
value), a new Story C is ADDED (persist the count), and Story B is DELETED outright. The pipeline
re-runs as a delta ticket.

Assertions (against the pushed work branch): the ledger classifies new/modified/deleted/unchanged
correctly with coded/tested stamps cleared-and-restamped only for the modified criterion; the
approved spec cites existing ids and retires B; the plan links every step to ACs and drops B's;
no test file references B's ids; B's endpoint is gone from the code; the run-2 exit report's
us_ac rows carry all four change statuses.

Usage:
    cd agent && uv run python test_requirements_delta_e2e.py

Environment: E2E_GITHUB_TOKEN (push access to bancroftway/empty_sample_repo) plus the active
chat provider's own token, same as run_headless.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("test_requirements_delta_e2e")

OWNER = "bancroftway"
REPO = "empty_sample_repo"
TECH_STACK = "angular-dotnet"
RUN_TIMEOUT_SECONDS = int(os.environ.get("DELTA_E2E_RUN_TIMEOUT", "14400"))  # 4h per run

REQUIREMENTS_RUN_1 = """# Counter App

A simple counter application. Angular frontend, .NET backend API.

## Story: Counting
- Clicking the Increment button increases the count by exactly 1 (backend is the source of truth
  via POST /api/counter/increment).
- The current count value is displayed on the page and always reflects the backend's value.

## Story: Reset
- Clicking the Reset button sets the count back to 0 via POST /api/counter/reset.
"""

REQUIREMENTS_RUN_2 = """# Counter App -- revision

The counter app changes as follows. Angular frontend, .NET backend API, as before.

## Story: Counting (changed)
- Clicking the Increment button increases the count by exactly 1 (backend is the source of truth
  via POST /api/counter/increment). (unchanged)
- CHANGED: the page must now display the DOUBLED count value (2x the backend count), clearly
  labelled, and always reflect the backend's value.

## Story: Persistence (new)
- The count survives a full page reload: after reloading the browser, the displayed value equals
  the backend's persisted count.

## Removed
- The Reset feature is REMOVED entirely: no Reset button, no POST /api/counter/reset endpoint,
  and no reset code anywhere.
"""


def _run_pipeline(branch: str, thread_id: str, requirements: str, fresh_run: bool) -> dict:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(requirements)
        req_file = f.name
    cmd = [
        "uv", "run", "python", "run_headless.py", OWNER, REPO, branch,
        "--requirements-file", req_file,
        "--greenfield-stack", TECH_STACK,
        "--thread", thread_id,
    ]
    if fresh_run:
        cmd.append("--fresh-run")
    # run_headless.py writes its own outcome report as the LAST thing it does -- an unhandled
    # exception anywhere in the astream loop propagates past that write, and the previous
    # invocation's stale report (if any) would otherwise be silently misread as this run's
    # result. Remove it first so a stale report can never survive a crashed subprocess.
    report_path = Path(__file__).parent / "agent-work" / f"headless-{thread_id}.json"
    report_path.unlink(missing_ok=True)
    logger.info("running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
            cwd=Path(__file__).parent, encoding="utf-8", errors="replace",
        )
    finally:
        try:
            os.unlink(req_file)
        except OSError:
            pass
    outcome: dict = {}
    if report_path.exists():
        outcome = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        outcome["crashed_without_report"] = True
    outcome["returncode"] = result.returncode
    outcome["tail"] = (result.stdout + "\n" + result.stderr)[-6000:]
    return outcome


def _clone_work_branch(work_branch: str, dest: Path) -> None:
    token = os.environ["E2E_GITHUB_TOKEN"]
    url = f"https://x-access-token:{token}@github.com/{OWNER}/{REPO}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", work_branch, url, str(dest)],
        check=True, capture_output=True, text=True,
    )


_ID_RE = re.compile(r"(?:US|AC)[^A-Za-z0-9]{0,2}(\d{4})[^A-Za-z0-9]{0,2}(\d+)(?!\d)")


def _ids_in_text(text: str) -> set[str]:
    return {f"US-{s}.{c}" for s, c in _ID_RE.findall(text)}


def _test_files(repo_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in repo_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(repo_dir).as_posix()
        if any(seg in rel for seg in ("node_modules/", ".ai-dev-workflow/", "bin/", "obj/", "dist/", ".angular/")):
            continue
        if re.search(r"(test|spec)", rel, re.IGNORECASE) and p.suffix in (".cs", ".ts", ".tsx", ".js", ".py", ".csproj"):
            out.append(p)
    return out


def _source_files(repo_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in repo_dir.rglob("*"):
        if not p.is_file() or p.suffix not in (".cs", ".ts", ".tsx", ".html", ".js"):
            continue
        rel = p.relative_to(repo_dir).as_posix()
        if any(seg in rel for seg in ("node_modules/", ".ai-dev-workflow/", "bin/", "obj/", "dist/", ".angular/", ".playwright")):
            continue
        out.append(p)
    return out


def _assert(condition: bool, label: str, failures: list[str], detail: str = "") -> None:
    if condition:
        logger.info("PASS: %s", label)
    else:
        logger.error("FAIL: %s %s", label, detail)
        failures.append(f"{label} {detail}".strip())


def validate_after_run_2(repo_dir: Path, run1_id: str, run2_id: str, failures: list[str]) -> None:
    aidw = repo_dir / ".ai-dev-workflow"
    ledger = json.loads((aidw / "spec" / "ledger.json").read_text(encoding="utf-8"))["entries"]
    by_id = {e["id"]: e for e in ledger}
    acs = [e for e in ledger if e.get("kind") == "acceptance_criterion"]
    stories = [e for e in ledger if e.get("kind") == "user_story"]

    def find_ac(*needles: str) -> dict | None:
        for e in acs:
            desc = (e.get("description") or "").lower()
            if all(n in desc for n in needles):
                return e
        return None

    increment_ac = find_ac("increment")
    display_ac = find_ac("doubl") or find_ac("display")
    persist_ac = find_ac("reload") or find_ac("surviv") or find_ac("persist")
    reset_entries = [e for e in ledger if "reset" in (e.get("description") or e.get("title") or "").lower()]

    # --- ledger classification -------------------------------------------------------------
    _assert(increment_ac is not None, "ledger: increment AC exists", failures)
    _assert(display_ac is not None, "ledger: display AC exists", failures)
    _assert(persist_ac is not None, "ledger: persistence AC exists (added run 2)", failures)
    _assert(bool(reset_entries), "ledger: reset entries exist (retired, never deleted)", failures)

    if increment_ac:
        _assert(
            increment_ac.get("status") in ("active", "revised")
            and increment_ac.get("coded_run_id") == run1_id
            and increment_ac.get("last_revised_run_id") != run2_id,
            "ledger: unchanged increment AC keeps run-1 stamps (never re-worked)", failures,
            f"got {increment_ac}",
        )
    if display_ac:
        _assert(
            display_ac.get("last_revised_run_id") == run2_id
            and display_ac.get("coded_run_id") == run2_id,
            "ledger: modified display AC reset + re-delivered by run 2", failures,
            f"got {display_ac}",
        )
    if persist_ac:
        _assert(
            persist_ac.get("first_seen_run_id") == run2_id
            and persist_ac.get("coded_run_id") == run2_id,
            "ledger: new persistence AC first seen + delivered in run 2", failures,
            f"got {persist_ac}",
        )
    retired_reset = [e for e in reset_entries if e.get("status") == "retired"]
    _assert(
        bool(retired_reset) and all(e.get("last_revised_run_id") == run2_id for e in retired_reset),
        "ledger: reset story/AC retired by run 2", failures,
        f"got {reset_entries}",
    )
    tested = [e for e in acs if e.get("status") in ("active", "revised") and e.get("tested_run_id")]
    _assert(bool(tested), "ledger: live ACs carry tested stamps + test_ids", failures)
    _assert(
        all(e.get("test_ids") for e in tested),
        "ledger: every tested AC records its runner test names", failures,
    )

    # --- spec -----------------------------------------------------------------------------
    spec = json.loads((aidw / "03-specification.approved.json").read_text(encoding="utf-8"))
    spec_retired = set(spec.get("retired_us_ids") or []) | set(spec.get("retired_ac_ids") or [])
    retired_ids = {e["id"] for e in retired_reset}
    _assert(
        bool(spec_retired & ({e["id"] for e in reset_entries} | retired_ids)),
        "spec: run-2 spec explicitly retires the reset feature", failures,
        f"retired fields {spec_retired}",
    )
    spec_ac_ids = {
        ac.get("id")
        for s in (spec.get("user_stories") or [])
        for ac in (s.get("acceptance_criteria") or [])
    }
    if display_ac:
        _assert(display_ac["id"] in spec_ac_ids, "spec: modified AC re-cited under its stable id", failures)
    if persist_ac:
        _assert(persist_ac["id"] in spec_ac_ids, "spec: new AC present under a newly minted id", failures)

    # --- plan -----------------------------------------------------------------------------
    plan = json.loads((aidw / "04-plan.approved.json").read_text(encoding="utf-8"))
    steps = plan.get("plan_steps") or []
    _assert(bool(steps), "plan: has steps", failures)
    _assert(
        all((s.get("ac_ids") or s.get("kind") == "infrastructure") for s in steps),
        "plan: every step cites ACs or is infrastructure", failures,
        f"steps {[(s.get('id'), s.get('ac_ids'), s.get('kind')) for s in steps]}",
    )
    cited = {i for s in steps for i in (s.get("ac_ids") or [])}
    _assert(not (cited & retired_ids), "plan: no step cites a retired (deleted) AC", failures, f"cited {cited}")
    if persist_ac:
        _assert(persist_ac["id"] in cited, "plan: new AC covered by >=1 step", failures, f"cited {cited}")
    if display_ac:
        _assert(display_ac["id"] in cited, "plan: modified AC covered by >=1 step", failures, f"cited {cited}")

    # --- tests ----------------------------------------------------------------------------
    residue: dict[str, set[str]] = {}
    increment_hits = 0
    for tf in _test_files(repo_dir):
        text = tf.read_text(encoding="utf-8", errors="replace")
        found = _ids_in_text(text)
        hit = found & retired_ids
        if hit:
            residue[tf.relative_to(repo_dir).as_posix()] = hit
        if increment_ac and increment_ac["id"] in found:
            increment_hits += 1
    _assert(not residue, "tests: no test file references deleted (retired) AC ids", failures, str(residue))
    _assert(increment_hits > 0, "tests: unchanged AC's regression tests survived run 2", failures)

    # --- code -----------------------------------------------------------------------------
    reset_code = [
        p.relative_to(repo_dir).as_posix()
        for p in _source_files(repo_dir)
        if "counter/reset" in p.read_text(encoding="utf-8", errors="replace").lower()
    ]
    _assert(not reset_code, "code: the reset endpoint is gone from the source", failures, str(reset_code))

    # --- exit report ----------------------------------------------------------------------
    report = json.loads((aidw / "history" / f"{run2_id}-report.json").read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in (report.get("us_ac") or [])}
    _assert(bool(rows), "exit: run-2 report carries us_ac provenance rows", failures)
    if persist_ac:
        _assert(rows.get(persist_ac["id"], {}).get("change") == "new", "exit: new AC row says 'new'", failures, str(rows.get(persist_ac["id"] if persist_ac else "")))
    if display_ac:
        _assert(rows.get(display_ac["id"], {}).get("change") == "modified", "exit: modified AC row says 'modified'", failures)
    if increment_ac:
        _assert(rows.get(increment_ac["id"], {}).get("change") == "unchanged", "exit: unchanged AC row says 'unchanged'", failures)
    _assert(
        any(rows.get(i, {}).get("change") == "deleted" for i in retired_ids),
        "exit: deleted feature rows say 'deleted'", failures, str({i: rows.get(i) for i in retired_ids}),
    )
    _assert(
        "## User stories & acceptance criteria this run" in (aidw / "EXIT-REPORT.md").read_text(encoding="utf-8"),
        "exit: EXIT-REPORT.md renders the US/AC section", failures,
    )
    _assert(
        bool((report.get("merge_readiness") or {}).get("merge_ready")),
        "exit: run 2 is merge ready", failures, str(report.get("merge_readiness")),
    )


def main() -> int:
    if not os.environ.get("E2E_GITHUB_TOKEN"):
        # run_headless loads the root .env itself; do the same here for the clone step.
        try:
            from dotenv import find_dotenv, load_dotenv

            load_dotenv(find_dotenv())
        except ImportError:
            pass
    if not os.environ.get("E2E_GITHUB_TOKEN"):
        logger.error("E2E_GITHUB_TOKEN must be set")
        return 2

    # Full canonical uuid4: sessions.session_id is SQL Server `uniqueidentifier` -- a prefixed or
    # truncated id fails touch_run's conversion mid-pipeline.
    # DELTA_E2E_RESUME_THREAD: continue an earlier attempt's run 1 (its sandbox/volume/work-branch
    # already exist and carry approved stages on disk) instead of minting a fresh thread and
    # re-paying for stages already approved -- e.g. after a quota_exhausted run_failure. Resumed
    # exactly like run_headless.py's own --thread contract: same thread id, fresh_run=False so
    # AIDW_RESUME=1 skips every stage already approved on disk.
    resume_thread = os.environ.get("DELTA_E2E_RESUME_THREAD", "").strip()
    thread_id = resume_thread or str(uuid.uuid4())
    # Source branch must EXIST in the remote -- the sandbox entrypoint does
    # `git clone --branch <source>` and dies on a missing ref. The per-session work branch
    # (ai-dev-workflow-session-<thread>) is minted by the entrypoint on top of it.
    branch = "main"
    logger.info(
        "thread=%s source_branch=%s stack=%s%s", thread_id, branch, TECH_STACK,
        " (RESUMING prior run 1)" if resume_thread else "",
    )

    outcome1 = _run_pipeline(branch, thread_id, REQUIREMENTS_RUN_1, fresh_run=False)
    run1_id = outcome1.get("run_id") or ""
    if not outcome1.get("ok"):
        logger.error("run 1 did not reach a merge-ready exit: %s", json.dumps(outcome1, indent=2)[:2000])
        logger.error(
            "to retry without re-paying for approved stages: "
            "DELTA_E2E_RESUME_THREAD=%s uv run python test_requirements_delta_e2e.py",
            thread_id,
        )
        return 1
    logger.info("run 1 ok (run_id=%s)", run1_id)

    outcome2 = _run_pipeline(branch, thread_id, REQUIREMENTS_RUN_2, fresh_run=True)
    run2_id = outcome2.get("run_id") or ""
    if not outcome2.get("ok"):
        logger.error("run 2 did not reach a merge-ready exit: %s", json.dumps(outcome2, indent=2)[:2000])
        return 1
    logger.info("run 2 ok (run_id=%s)", run2_id)

    failures: list[str] = []
    from src import branch_naming  # after run_headless proved the package imports

    work_branch = branch_naming.work_branch_for(thread_id)
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "clone"
        _clone_work_branch(work_branch, dest)
        validate_after_run_2(dest, run1_id, run2_id, failures)

    result = {
        "thread_id": thread_id, "work_branch": work_branch,
        "run1": {k: outcome1.get(k) for k in ("run_id", "ok", "wall_seconds")},
        "run2": {k: outcome2.get(k) for k in ("run_id", "ok", "wall_seconds")},
        "failures": failures, "passed": not failures,
    }
    out_path = Path(__file__).parent / "agent-work" / f"delta-e2e-{thread_id}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    if failures:
        logger.error("DELTA E2E FAILED: %d assertion(s)", len(failures))
        return 1
    logger.info("DELTA E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
