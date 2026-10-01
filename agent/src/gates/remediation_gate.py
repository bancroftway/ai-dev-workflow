"""remediation's deterministic verify: check the claims against the scanner's own findings.

The stage reports which findings it fixed (`findings_addressed`), which packages it moved
(`dependencies_upgraded`) and what it deliberately left (`known_gaps`). Until this gate existed
nothing read any of it -- the stage's `content_field` was the summary STRING, so the other four
fields were discarded before they reached disk, and a run could claim to have fixed a critical
pre-auth RCE while changing nothing.

What is measured here, and what is not:

- **Measured.** Whether each still-ACTIONABLE finding (any severity, application code only, quality
  debt only when this pipeline introduced it -- repo_scan.to_dashboard_dict's flag) is either gone
  from a fresh scan or accounted for in `known_gaps`; whether a claimed id exists in the scan at all
  (a fabricated id is the cheapest possible way to look busy); whether the "fix" was to silence the
  scanner rather than the defect.
- **Not measured.** Whether a *reason* in `known_gaps` is a good reason. That is judgement, and this
  gate does not pretend to have it -- it forces the reason to be stated and attached to a real
  finding id, which is what makes it reviewable.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .. import repo_files
from ..chat_model import close_session, lap_role
from ..schemas import presence_values as _presence_values
# accounted_for is re-exported for metrics_nodes.py/exit_nodes.py, both of which do
# `from .gates.remediation_gate import accounted_for` -- that keeps working unmodified since it is
# now just this import binding the same function into this module's own namespace.
from . import remediation_evaluate_checks as _rc
from .checks import Check, CheckLog
from .remediation_evaluate_checks import accounted_for, evaluate_remediation

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

# Stuck-fixer detection (2026-09-11, observed live thread 8242ea6d): rebuild.py's own gate resets
# a fixer session hammering the IDENTICAL scan-delta finding across laps (_scan_finding_fingerprint,
# that module's own comment has the full incident); this gate had no equivalent until now, so a
# redraft that keeps failing on the exact same blocking reason(s) just kept retrying with the same
# (possibly confused, possibly context-poisoned) conversation until max_verify_cycles ran out. A
# plain repo file rather than GraphState: verify_remediation is a StageSpec.deterministic_verify
# callback (thread_id, content_dict, run_id, baseline_commit, provider, chat_provider) with no
# access to this stage's own prior last_verification -- provider/thread_id are the only cross-call
# handles it has. Lives under .ai-dev-workflow/ like every other pipeline artifact, swept into the
# same verify-pass persistence commit spec_ledger.py's own docstring describes.
_VERIFY_FINGERPRINT_PATH = ".ai-dev-workflow/remediation-verify-fingerprint.json"


def _reasons_fingerprint(reasons: list[str]) -> list[str]:
    """Sorted, deduplicated blocking reasons -- pure. Reasons here are built from stable finding
    metadata (id, severity, category, title, location), never a line number or timestamp, so
    (unlike rebuild.py's raw scanner-log fingerprint) no normalization is needed for two calls
    against the same real finding to compare equal."""
    return sorted(set(reasons))


def _stuck_fixer_check(reasons: list[str], raw_prior_fingerprint: str | None) -> tuple[bool, list[str]]:
    """(should_reset, fingerprint_to_persist) -- pure, the whole stuck-fixer decision in isolation
    from the file I/O around it. `raw_prior_fingerprint` is whatever _VERIFY_FINGERPRINT_PATH held
    coming in (None/empty/malformed all treated as "no prior fingerprint", never a crash)."""
    fingerprint = _reasons_fingerprint(reasons)
    try:
        prior = json.loads(raw_prior_fingerprint) if raw_prior_fingerprint else []
    except json.JSONDecodeError:
        prior = []
    should_reset = bool(fingerprint) and fingerprint == prior
    return should_reset, fingerprint

async def scan_and_publish(provider: Any, thread_id: str) -> dict[str, Any]:
    """Run a full scan and write it to `repo-scan-latest.json`, returning the dashboard dict.

    Shared by the pre-draft node and this gate so the stage reads and is judged against the SAME
    artifact. Before this existed nothing wrote that file ahead of remediation -- the metrics stage
    writes it afterwards, and the commit-triggered background refresh deliberately never touches
    committed artifacts -- so the prompt pointed at a file that did not exist.
    """
    from .. import metrics_nodes, repo_files, repo_scan

    gitleaks_stopwords, gitleaks_allow_paths = await repo_scan.org_gitleaks_allowlist()
    report = await repo_scan.run_repo_scan(
        provider, thread_id, profile="full",
        gitleaks_extra_stopwords=gitleaks_stopwords, gitleaks_extra_allow_paths=gitleaks_allow_paths,
    )
    # `actionable` is introduced-aware for quality categories (same split is_gating draws):
    # a brownfield repo's pre-existing lizard/jscpd debt is not this run's to explain, or the
    # fix-everything gate would demand a known_gaps line per legacy finding and deadlock in
    # 3 laps. Greenfield's pre-codegen baseline is empty, so there everything is introduced.
    # Security findings stay absolute regardless. No baseline at all -> None -> the old blunt
    # rule (everything counts), which is also is_gating's own fallback.
    baseline = await metrics_nodes._read_baseline(provider, thread_id)  # noqa: SLF001 -- same package
    introduced_ids: frozenset[str] | None = None
    if baseline is not None:
        baseline_ids = frozenset(str(f.get("id")) for f in baseline.get("findings") or [])
        introduced_ids = frozenset(f.finding_key for f in report.findings) - baseline_ids
    scan = report.to_dashboard_dict(introduced_ids=introduced_ids)
    await repo_files.write_repo_file(
        provider, thread_id, repo_scan.LATEST_PATH, json.dumps(scan, indent=2, default=str) + "\n"
    )
    return scan


async def remediation_scan_node(_state: Any, config: Any) -> dict[str, Any]:
    """Deterministic pre-draft step: publish a current scan for remediation to work from.

    No LLM. Runs immediately before `remediation_draft` so the stage's first attempt sees the real
    findings of the code that minimal-code-to-green just wrote, instead of the greenfield baseline
    (empty by construction) that every run before this one reported "zero findings" from.
    """
    from ..sandbox.factory import get_sandbox_provider

    thread_id = config["configurable"]["thread_id"]
    provider = get_sandbox_provider()
    try:
        scan = await scan_and_publish(provider, thread_id)
    except Exception:  # noqa: BLE001 -- a scan failure must not kill the run; the gate re-scans
        logger.exception("remediation pre-scan failed for thread %s", thread_id)
        return {}
    gating = sum(1 for f in scan.get("findings") or [] if f.get("gating"))
    logger.info(
        "remediation pre-scan: %d finding(s), %d gating", len(scan.get("findings") or []), gating
    )
    return {}


async def _stage_diff(provider: Any, thread_id: str, baseline_commit: str | None) -> tuple[list[str], str]:
    """(changed files, added lines) for what THIS stage did, diffed against its baseline commit.

    Not the working tree: remediation commits through `commit_all`, so by the time this gate runs
    the tree is clean and a working-tree diff would be empty -- suppression detection would then
    silently never fire, which is worse than not having it.
    """
    if baseline_commit is None:
        return [], ""
    names = await provider.exec_in_sandbox(thread_id, f"git diff --name-only {baseline_commit} -- .")
    added = await provider.exec_in_sandbox(
        thread_id, f"git diff -U0 {baseline_commit} -- . | grep '^+' || true"
    )
    changed = [line.strip() for line in str(names.stdout or "").splitlines() if line.strip()]
    return changed, str(added.stdout or "")


async def _prior_finding_ids(provider: Any, thread_id: str, baseline_commit: str | None) -> frozenset[str] | None:
    """Finding ids from the scan file as it stood at the stage's baseline commit.

    `git show <commit>:<path>` rather than reading the file: by verify time the on-disk copy may
    already have been rewritten by the background refresh that fires on commit, and the question
    this answers is "what was this stage actually handed".
    """
    from .. import repo_scan

    if baseline_commit is None:
        return None
    raw = await provider.exec_in_sandbox(
        thread_id, f"git show {baseline_commit}:{repo_scan.LATEST_PATH} 2>/dev/null || true"
    )
    text = str(raw.stdout or "").strip()
    if not text:
        return None
    try:
        prior = json.loads(text)
    except json.JSONDecodeError:
        return None
    return frozenset(str(f.get("id")) for f in (prior.get("findings") or []))


# Task 13b: one line per DISTINCT rejection reason inside evaluate_remediation/verify_remediation
# below. Six, not two: content-missing and no-scan-available are each their own reason, same as
# the three concrete checks (unexplained actionable finding, fabricated claimed id, and the two
# suppression checks -- by file and by inline comment -- counted separately since they are two
# independently-triggered branches).
REMEDIATION_HARD_RULES: tuple[str, ...] = (
    "You must actually produce a report -- an absent report cannot be distinguished from a "
    "stage that never ran and is rejected outright.",
    "A repo scan must be available to verify your claims against -- if none could be taken, "
    "your claims about what you fixed cannot be checked and the stage is rejected.",
    "Every still-ACTIONABLE finding (any severity, application code, and quality debt this "
    "pipeline itself introduced) left open after you ran must be gone from a fresh scan or "
    "named in known_gaps with a REAL reason beyond the bare id -- an unexplained open finding "
    "blocks.",
    "Every id you list in findings_addressed must be a real finding id copied verbatim from "
    "the scan -- a fabricated or mistyped id is rejected.",
    "Never touch a scanner ignore/config file (.trivyignore, .gitleaksignore, gitleaks.toml, "
    "etc.) -- remediation must fix findings, never silence the scanner.",
    "Never add an inline scanner-suppression comment (nosec, noqa, trivy:ignore, "
    "gitleaks:allow, semgrep-disable, eslint-disable, type: ignore, etc.) -- fix the finding "
    "instead of hiding it from the scanner.",
)


REM_CONTENT = Check(
    _rc.CHECK_CONTENT, "Remediation report present",
    "The stage must hand back a report of what it fixed and what it left. Without one there is no way "
    "to tell a finished remediation from one that never ran.", "blocking",
)
REM_SCAN = Check(
    _rc.CHECK_SCAN, "Fresh security scan available",
    "A new scan is taken after the fixes so the claims can be checked against real scanner output. "
    "If no scan can be taken, the claims can't be verified, so the stage does not pass.", "blocking",
)
REM_UNEXPLAINED = Check(
    _rc.CHECK_UNEXPLAINED, "No unexplained open findings",
    "Every actionable finding still in the fresh scan must be listed in known_gaps with a real reason. "
    "Saying a finding was fixed counts for nothing while the scanner still reports it.", "blocking",
)
REM_FABRICATED = Check(
    _rc.CHECK_FABRICATED, "Claimed fixes are real findings",
    "Every finding id the stage claims to have addressed must exist in the scan it was given or the "
    "scan taken after. An invented id is a cheap way to look busy without fixing anything.", "blocking",
    condition="always",
)
REM_IGNORE_FILES = Check(
    _rc.CHECK_IGNORE_FILES, "Scanner ignore files untouched",
    "The stage must not edit scanner ignore or config files such as .trivyignore or gitleaks.toml. "
    "That hides findings from the scanner instead of fixing the code.", "blocking",
)
REM_SUPPRESSION_COMMENTS = Check(
    _rc.CHECK_SUPPRESSION_COMMENTS, "No inline suppression comments",
    "The stage must not add comments like nosec, noqa or eslint-disable. They make the scanner "
    "look away from a line while the defect stays in place.", "blocking",
)
VERIFY_CHECKS: tuple[Check, ...] = (
    REM_CONTENT, REM_SCAN, REM_UNEXPLAINED, REM_FABRICATED, REM_IGNORE_FILES, REM_SUPPRESSION_COMMENTS,
)
_CHECK_MAP = {c.id: c for c in VERIFY_CHECKS}


def _record_checks(
    log: CheckLog, tagged: list[tuple[str, str]], ran: list[str], infra: dict[str, str], prior_ids_read: bool
) -> None:
    """One row per sub-check, in VERIFY_CHECKS order. An infra entry (the scan/diff step that
    feeds this check raised) wins over whatever was computed from the missing data; several
    reasons for one check (e.g. many open findings) join into one row."""
    for check in VERIFY_CHECKS:
        reasons = [reason for check_id, reason in tagged if check_id == check.id]
        if check.id in infra:
            log.infra(check, infra[check.id])
        elif reasons:
            log.record_tagged(_CHECK_MAP, [(check.id, "\n".join(reasons))])
        elif check.id in ran:
            log.passed(check)
        elif check is REM_FABRICATED and REM_IGNORE_FILES.id in ran and not prior_ids_read:
            log.skipped(check, "the scan this stage was handed could not be read at its baseline commit")


async def verify_remediation(
    thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None, provider: Any,
    chat_provider: str, lap: int = 0, _audit_ran_this_lap: bool = True, *, log: CheckLog | None = None,
) -> "VerificationResult":
    from ..graph import VerificationResult

    log = log or CheckLog("remediation_verify", VERIFY_CHECKS)
    scan: dict[str, Any] | None = None
    prior_ids: frozenset[str] | None = None
    changed_files: list[str] = []
    added_lines = ""
    # Checks whose input has not been gathered yet; whatever is left here when the try raises is
    # recorded as infra (the check could not run), not as a content failure. Record-only: the
    # verdict below is computed exactly as before.
    unfed = [REM_SCAN, REM_FABRICATED, REM_IGNORE_FILES, REM_SUPPRESSION_COMMENTS]
    infra: dict[str, str] = {}
    try:
        # A FRESH scan, not `repo-scan-latest.json`: that file is refreshed by a background task
        # fired on commit, so reading it here races the stage's own commit and could block on
        # findings this stage already fixed. The scan is the evidence -- it has to be current.
        # A FRESH scan, republished to LATEST_PATH: a blocked retry then reads exactly what this
        # gate judged it against, rather than the pre-draft scan it has already acted on.
        scan = await scan_and_publish(provider, thread_id)
        unfed.remove(REM_SCAN)
        prior_ids = await _prior_finding_ids(provider, thread_id, baseline_commit)
        unfed.remove(REM_FABRICATED)
        changed_files, added_lines = await _stage_diff(provider, thread_id, baseline_commit)
        unfed.clear()
    except Exception as exc:  # noqa: BLE001 -- an unreadable scan must block, not crash the graph
        logger.exception("remediation gate: could not scan/diff for thread %s", thread_id)
        infra = {c.id: f"could not scan/diff: {type(exc).__name__}: {exc}" for c in unfed}

    passed, tagged, ran = _rc.evaluate_remediation_checks(content_dict, scan, changed_files, added_lines, prior_ids)
    reasons = [reason for _check_id, reason in tagged]
    _record_checks(log, tagged, ran, infra, prior_ids is not None)
    checks = [r.to_dict() for r in log.results()]
    if passed:
        # Clear any stuck-fixer marker left by a prior failing lap -- a clean pass means whatever
        # was stuck got resolved (or never existed), and a stale marker must not survive into a
        # LATER, unrelated ticket's own first verify lap on this same repo.
        try:
            await repo_files.write_repo_file(provider, thread_id, _VERIFY_FINGERPRINT_PATH, "[]\n")
        except Exception:  # noqa: BLE001 -- best-effort bookkeeping, never worth failing a pass over
            logger.warning("remediation gate: could not clear verify fingerprint", exc_info=True)
        return VerificationResult(
            passed=True,
            feedback=(
                f"remediation verified: no gating findings remain unexplained "
                f"({len(_presence_values(content_dict.get('findings_addressed')))} addressed, "
                f"{len(_presence_values(content_dict.get('known_gaps')))} documented gap(s))"
            ),
            report={
                "findings_addressed": _presence_values(content_dict.get("findings_addressed")),
                "known_gaps": _presence_values(content_dict.get("known_gaps")),
            },
            checks=checks,
        )

    logger.info("remediation gate: blocking (%d reason(s))", len(reasons))

    # Stuck-fixer detection: the SAME set of blocking reasons twice running means the last redraft
    # (or fix pass) made no dent in what actually blocks -- continuing that same conversation just
    # repeats whatever confused it the first time. Reset it so the NEXT attempt starts fresh, same
    # remedy rebuild.py's own scan-delta stall detection applies for its own gate.
    try:
        raw_prior = await repo_files.read_repo_file(provider, thread_id, _VERIFY_FINGERPRINT_PATH)
        should_reset, fingerprint = _stuck_fixer_check(reasons, raw_prior)
        if should_reset:
            logger.warning(
                "remediation gate: identical blocking reason(s) twice running for thread_id=%s -- "
                "resetting the stuck draft session",
                thread_id,
            )
            # This lap's draft key (2026-09-19 sweep): the bare "draft" label evicted nothing --
            # the draft node keys its session per lap; see chat_model.lap_role.
            await close_session(thread_id, "remediation", lap_role("draft", run_id, lap), provider=chat_provider)
        await repo_files.write_repo_file(
            provider, thread_id, _VERIFY_FINGERPRINT_PATH, json.dumps(fingerprint, indent=2) + "\n"
        )
    except Exception:  # noqa: BLE001 -- best-effort bookkeeping; a failure here must not mask the real rejection
        logger.warning("remediation gate: stuck-fixer check failed", exc_info=True)

    return VerificationResult(
        passed=False,
        feedback=(
            "Remediation is not complete. Each item below is either a finding that is STILL gating "
            "and unexplained, a claimed fix that does not correspond to a real finding, or an "
            "attempt to silence a scanner. Fix the code, or put the finding in `known_gaps` with "
            "its real reason (an honest gap is a valid outcome; an unexplained one is not):\n"
            + "\n".join(f"- {reason}" for reason in reasons)
        ),
        report={"blocking_reasons": reasons},
        checks=checks,
    )


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.remediation_gate`."""
    scan = {
        "findings": [
            {
                "id": "aaa111",
                "gating": True,
                "severity": "critical",
                "category": "vulnerability",
                "title": "Next.js pre-auth RCE",
                "location": {"path": "package-lock.json"},
                "package": {"fixed_version": "15.4.9"},
            },
            {"id": "bbb222", "gating": False, "severity": "low", "category": "maintainability", "title": "long fn"},
        ]
    }

    # A gating finding nobody mentions blocks, and the feedback carries the fixed_version -- the
    # actionable half. This is the exact case that shipped a critical RCE past the metrics gate.
    passed, reasons = evaluate_remediation({"findings_addressed": [], "known_gaps": []}, scan)
    assert not passed and "aaa111" in reasons[0] and "15.4.9" in reasons[0], reasons

    # Claiming the fix does NOT pass while the finding is still in the scan: the scan is the
    # evidence, the claim is not.
    passed, _ = evaluate_remediation({"findings_addressed": ["aaa111"], "known_gaps": []}, scan)
    assert not passed

    # Gone from the post-fix scan = actually fixed, and claiming it is NOT fabrication even though
    # the id is no longer in that scan. This is the case that must not regress: punishing it would
    # make the honest outcome the one that blocks.
    fixed_scan = {"findings": [scan["findings"][1]]}
    prior = frozenset({"aaa111", "bbb222"})
    assert evaluate_remediation({"findings_addressed": ["aaa111"]}, fixed_scan, prior_ids=prior)[0]
    assert evaluate_remediation({"findings_addressed": ["aaa111"]}, fixed_scan)[0]

    # A documented gap with a real reason passes; the bare id does not.
    reasoned = {"known_gaps": ["aaa111: no fixed version for .NET 10 yet, tracked upstream"]}
    assert evaluate_remediation(reasoned, scan)[0]
    assert not evaluate_remediation({"known_gaps": ["aaa111"]}, scan)[0]

    # Fix-everything: a NON-gating finding flagged `actionable` (low severity, application code,
    # introduced by this pipeline) blocks exactly like a gating one until fixed or explained.
    lowball = {"findings": [
        {"id": "ccc333", "gating": False, "actionable": True, "severity": "low",
         "category": "sast", "title": "possible object injection", "location": {"path": "apps/web/src/q.ts"}},
    ]}
    passed, reasons = evaluate_remediation({"findings_addressed": [], "known_gaps": []}, lowball)
    assert not passed and "ccc333" in reasons[0] and "still open" in reasons[0], reasons
    assert evaluate_remediation({"known_gaps": ["ccc333: sanitized upstream, key is enum-constrained"]}, lowball)[0]
    # ...and an auto-exempt finding (actionable False) never demands an explanation.
    exempt = {"findings": [{"id": "ddd444", "gating": False, "actionable": False, "severity": "low",
                            "category": "maintainability", "title": "pre-existing debt"}]}
    assert evaluate_remediation({"findings_addressed": [], "known_gaps": []}, exempt)[0]
    # The feedback names the first 30 and points at the file for the rest -- named, never drowned.
    flood = {"findings": [
        {"id": f"e{i:05x}", "gating": False, "actionable": True, "severity": "low",
         "category": "sast", "title": f"finding {i}", "location": {"path": "apps/web/src/q.ts"}}
        for i in range(35)
    ]}
    passed, reasons = evaluate_remediation({"findings_addressed": [], "known_gaps": []}, flood)
    assert not passed and len(reasons) == 31 and "and 5 more" in reasons[30], (len(reasons), reasons[-1])

    # Fabricated id: in neither scan.
    passed, reasons = evaluate_remediation(
        {"findings_addressed": ["deadbeef"], "known_gaps": ["aaa111: accepted, see above reason"]},
        scan,
        prior_ids=prior,
    )
    assert not passed and "deadbeef" in reasons[0], reasons

    # Without a readable pre-scan the fabrication check is SKIPPED rather than guessed -- blocking
    # on an id we cannot check would fail honest runs whose baseline file was unreadable.
    assert evaluate_remediation(
        {"findings_addressed": ["deadbeef"], "known_gaps": ["aaa111: accepted, see above reason"]}, scan
    )[0]

    # Fixing a non-gating finding is honest and must not be called fabrication.
    assert evaluate_remediation(
        {"findings_addressed": ["bbb222"], "known_gaps": ["aaa111: accepted, upstream has no fix"]},
        scan,
        prior_ids=prior,
    )[0]

    # Suppression, by file and by comment.
    clean = {"known_gaps": ["aaa111: accepted, upstream has no fix"]}
    passed, reasons = evaluate_remediation(clean, scan, ["repo/.trivyignore"])
    assert not passed and "suppress" in reasons[0]
    passed, reasons = evaluate_remediation(clean, scan, [], "const key = 'x' // gitleaks:allow\n")
    assert not passed and "gitleaks:allow" in reasons[0], reasons

    # No scan = cannot verify = blocks. An unrunnable check is not a clean one.
    assert not evaluate_remediation(clean, None)[0]
    assert not evaluate_remediation(None, scan)[0]

    # Task 11: known_gaps/findings_addressed are now PresenceList-shaped ({"status", "values",
    # "reason"}), not a bare list[str] -- _presence_values must unwrap the REAL shape, not just
    # the legacy bare list every other assertion above uses.
    typed_gap = {
        "findings_addressed": {"status": "absent", "values": [], "reason": "nothing fixed"},
        "known_gaps": {
            "status": "present",
            "values": ["aaa111: no fixed version for .NET 10 yet, tracked upstream"],
            "reason": "",
        },
    }
    assert evaluate_remediation(typed_gap, scan)[0]
    assert not evaluate_remediation(
        {
            "findings_addressed": {"status": "absent", "values": [], "reason": "nothing fixed"},
            "known_gaps": {"status": "absent", "values": [], "reason": "nothing left open"},
        },
        scan,
    )[0]

    # Task 13b: REMEDIATION_HARD_RULES -- one line per real rejection branch in
    # evaluate_remediation/verify_remediation (see the constant's own comment for the count
    # breakdown).
    assert len(REMEDIATION_HARD_RULES) == 6, len(REMEDIATION_HARD_RULES)
    assert all(isinstance(r, str) and r.strip() for r in REMEDIATION_HARD_RULES)

    # _stuck_fixer_check (2026-09-11 fix): pure decision logic, isolated from the file I/O around it.
    assert _reasons_fingerprint(["b", "a", "a"]) == ["a", "b"], "sorted, deduplicated"
    # No prior fingerprint (fresh repo, first-ever failure, or a malformed/absent file) -- never
    # resets on a first failure, whatever the raw value looks like.
    for raw in (None, "", "not json", "[]"):
        should_reset, fp = _stuck_fixer_check(["r1"], raw)
        assert not should_reset and fp == ["r1"], (raw, should_reset, fp)
    # Identical reasons twice running -- resets.
    should_reset, fp = _stuck_fixer_check(["r1", "r2"], json.dumps(["r1", "r2"]))
    assert should_reset and fp == ["r1", "r2"]
    # A genuinely DIFFERENT set (even one changed reason) -- progress was made, never resets.
    should_reset, _ = _stuck_fixer_check(["r1", "r3"], json.dumps(["r1", "r2"]))
    assert not should_reset
    # A verify that CLEARED (empty reasons) is never "stuck" -- vacuously equal empty lists must
    # not trigger a reset (there is nothing to be stuck ON).
    should_reset, fp = _stuck_fixer_check([], json.dumps([]))
    assert not should_reset and fp == []

    # verify_remediation end to end: the SAME single blocking reason on two consecutive calls
    # against the SAME thread must reset the draft session; a first-time failure must not.
    import asyncio

    class _FakeVerifyProvider:
        """Minimal exec_in_sandbox stub -- verify_remediation's own scan/diff helpers only ever
        `cat`/`git diff`/`git show` through it; every unmatched command reads as empty/failed,
        matching a repo with no baseline_commit (both callers already handle that case)."""

        async def exec_in_sandbox(self, _thread_id: str, _command: str):  # noqa: ANN201
            class _R:
                ok = False
                stdout = ""
                stderr = ""

            return _R()

    fake_files: dict[str, str] = {}

    async def _fake_read(_provider: Any, _thread_id: str, path: str) -> str | None:
        return fake_files.get(path)

    async def _fake_write(_provider: Any, _thread_id: str, path: str, content: str) -> None:
        fake_files[path] = content

    close_calls: list[tuple[str, str, str]] = []

    async def _fake_close_session(thread_id: str, stage: str, role: str, *, provider: str) -> None:
        close_calls.append((thread_id, stage, role))

    async def _fake_scan_and_publish(_provider: Any, _thread_id: str) -> dict[str, Any]:
        # One gating finding, never addressed/gapped -- the SAME real blocking reason every call.
        return {"findings": [{
            "id": "stuck123", "gating": True, "actionable": True, "severity": "medium",
            "category": "vulnerability", "title": "stuck finding", "location": {"path": "x.ts"},
        }]}

    global scan_and_publish, close_session
    original_scan_and_publish = scan_and_publish
    original_close_session = close_session
    original_read_repo_file = repo_files.read_repo_file
    original_write_repo_file = repo_files.write_repo_file
    scan_and_publish = _fake_scan_and_publish  # type: ignore[assignment]
    close_session = _fake_close_session  # type: ignore[assignment]
    repo_files.read_repo_file = _fake_read  # type: ignore[assignment]
    repo_files.write_repo_file = _fake_write  # type: ignore[assignment]
    try:
        content = {"findings_addressed": [], "known_gaps": []}
        first = asyncio.run(verify_remediation(
            "t-remediation-selfcheck", content, "r1", None, _FakeVerifyProvider(), "claude",
        ))
        assert not first.passed
        assert close_calls == [], "must never reset on a FIRST failure"

        second = asyncio.run(verify_remediation(
            "t-remediation-selfcheck", content, "r2", None, _FakeVerifyProvider(), "claude",
        ))
        assert not second.passed
        # "draft-r2-0" (chat_model.lap_role), not the bare "draft" -- until 2026-09-19 this
        # asserted the bare label, which the fix below made wrong on purpose: a close_session call
        # for a role nothing populates is a silent no-op, exactly the bug this reset exists to fix
        # (see lap_role's own docstring).
        assert close_calls == [("t-remediation-selfcheck", "remediation", "draft-r2-0")], (
            f"the identical reason twice running must reset the draft session exactly once, got {close_calls}"
        )
        # Per-sub-check rows: no baseline commit -> fabrication skipped, not passed.
        assert [(r["id"], r["status"]) for r in second.checks] == [
            (REM_CONTENT.id, "passed"), (REM_SCAN.id, "passed"), (REM_UNEXPLAINED.id, "failed"),
            (REM_FABRICATED.id, "skipped"), (REM_IGNORE_FILES.id, "passed"),
            (REM_SUPPRESSION_COMMENTS.id, "passed"),
        ], second.checks
        assert "stuck123" in second.checks[2]["detail"]

        gapped = asyncio.run(verify_remediation(
            "t-remediation-selfcheck", {"known_gaps": ["stuck123: upstream has no fix yet, tracked"]},
            "r3", None, _FakeVerifyProvider(), "claude",
        ))
        assert gapped.passed and {r["status"] for r in gapped.checks} == {"passed", "skipped"}, gapped.checks

        # A scan that raises: verdict/feedback exactly as before (blocks on "no repo scan"), but
        # every check the scan/diff would have fed is recorded infra with the exception, not failed.
        async def _raising_scan(_provider: Any, _thread_id: str) -> dict[str, Any]:
            raise RuntimeError("sandbox gone")

        scan_and_publish = _raising_scan  # type: ignore[assignment]
        broken = asyncio.run(verify_remediation("t-rem-infra", content, "r4", None, _FakeVerifyProvider(), "claude"))
        assert not broken.passed and "no repo scan" in broken.feedback
        assert broken.report == {"blocking_reasons": [evaluate_remediation(content, None)[1][0]]}, broken.report
        rows = {r["id"]: r for r in broken.checks}
        assert rows[REM_CONTENT.id]["status"] == "passed" and REM_UNEXPLAINED.id not in rows, rows
        for check in (REM_SCAN, REM_FABRICATED, REM_IGNORE_FILES, REM_SUPPRESSION_COMMENTS):
            assert rows[check.id]["status"] == "infra" and "sandbox gone" in rows[check.id]["detail"], rows
        # Caller-supplied log collects the same rows.
        shared = CheckLog("remediation_verify", VERIFY_CHECKS, strict=True)
        asyncio.run(verify_remediation("t-rem-infra", None, "r5", None, _FakeVerifyProvider(), "claude", log=shared))
        assert [(r.id, r.status) for r in shared.results()] == [(REM_CONTENT.id, "failed")] + [
            (c.id, "infra") for c in (REM_SCAN, REM_FABRICATED, REM_IGNORE_FILES, REM_SUPPRESSION_COMMENTS)
        ], shared.results()
    finally:
        scan_and_publish = original_scan_and_publish
        close_session = original_close_session
        repo_files.read_repo_file = original_read_repo_file
        repo_files.write_repo_file = original_write_repo_file

    # Every declared Check is fed by the helper: its id constant is appended to `ran` or tagged on a
    # reason inside evaluate_remediation_checks (text scan, so a dropped branch fails here).
    import inspect

    helper_src = inspect.getsource(_rc.evaluate_remediation_checks)
    for check in VERIFY_CHECKS:
        name = next(k for k, v in vars(_rc).items() if k.startswith("CHECK_") and v == check.id)
        assert helper_src.count(name) >= 2, f"{check.id} is declared but never tagged/ran by the helper"
    assert set(_CHECK_MAP) == {v for k, v in vars(_rc).items() if k.startswith("CHECK_")}

    print("remediation_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
