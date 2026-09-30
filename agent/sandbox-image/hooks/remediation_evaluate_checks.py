"""remediation's pure verdict logic, extracted from remediation_gate.py (2026-09-29, Task 10) so
the sandbox's own same-turn Stop hook (sandbox-image/hooks/check-remediation-stop.mjs) can run the
REAL check by shelling out to `python3` on this ONE file, instead of a hand-ported JavaScript
reimplementation drifting from it -- same rationale as `coverage_parsing.py`'s own extraction from
`test_coverage_gate.py`; see that module's docstring for the full argument.

`remediation_gate.py` itself cannot be the staged copy: it imports `..repo_files`/`..chat_model`
(sandbox exec / session bookkeeping) and `..schemas.presence_values` (pydantic-backed, not
installed in the sandbox) for its host-side scan/diff/stuck-fixer logic. This module needs only
the stdlib, so `_presence_values` below is a small, deliberately duplicated copy of
`schemas.presence_values`'s own body -- same duplication this module's adversarial-compliance
sibling, `adversarial_audit_checks.py`, already accepts (see that module's own docstring).

`remediation_gate.py` imports `evaluate_remediation`/`accounted_for`/`SUPPRESSION_PATHS`/
`_SUPPRESSION_COMMENT_RE`/`_mentions_id` from here UNCHANGED (a pure code-move, not a fork; its own
self-check is the proof this extraction changed no behavior). `metrics_nodes.py` and
`exit_nodes.py` both do `from .gates.remediation_gate import accounted_for` -- that keeps working
unmodified, since `remediation_gate.accounted_for` is now just this module's own function
re-exported by that same unchanged import.

CLI mode (`python3 remediation_evaluate_checks.py --check-hook`, stdin: JSON `{"content":
{...}|null, "scan": {...}|null, "changed_files": [str, ...], "added_lines": str, "prior_ids":
[str, ...]|null, "scan_is_fresh": bool}`, stdout: JSON `{"passed": bool, "reasons": [str, ...]}`)
is what the Stop hook actually invokes. The hook gathers `scan` from the already-on-disk
repo-scan-latest.json (written pre-draft by `remediation_scan_node`) and `changed_files`/
`added_lines`/`prior_ids` via `git diff`/`git show` against `AIDW_BASELINE_COMMIT` -- it does not
re-run the scan itself. Since that pre-draft scan is stale by construction for THIS turn's own
in-flight fixes, the hook always sends `scan_is_fresh: false` (see `evaluate_remediation`'s own
docstring) -- `remediation_gate.py`'s host-side call site never sends this field at all and gets
the function's default (`True`), since its own scan is always freshly taken.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# Editing these is how you make a scanner quiet without making the code safe. Remediation has write
# access, so this is a real temptation and not a hypothetical one: the deleted `security_nodes`
# cluster carried the same never-suppress rule in its prompt, where nothing enforced it.
SUPPRESSION_PATHS = (
    ".trivyignore",
    ".gitleaksignore",
    ".semgrepignore",
    ".osv-scanner-ignore",
    "gitleaks.toml",
    ".gitleaks.toml",
    "trivy.yaml",
    ".trivyignore.yaml",
    "jscpd.json",
    ".jscpd.json",
)
_SUPPRESSION_COMMENT_RE = re.compile(
    r"(?:#\s*nosec|//\s*nosec|#\s*noqa(?!:\s*E\d)|trivy:ignore|gitleaks:allow|semgrep-disable|"
    r"jscpd:ignore|eslint-disable(?!-next-line\s+@typescript)|#\s*type:\s*ignore)",
    re.IGNORECASE,
)


def _presence_values(entry: Any) -> list[Any]:
    """Copy of `schemas.presence_values`'s own body (see this module's docstring for why this one
    small function is duplicated rather than imported): the `values` list of a PresenceList-shaped
    dict, tolerating a legacy bare list or a missing/None field."""
    if isinstance(entry, dict):
        return list(entry.get("values") or [])
    if isinstance(entry, list):
        return list(entry)
    return []


def _mentions_id(text: str, finding_id: str) -> bool:
    return finding_id.lower() in text.lower()


def accounted_for(finding_id: str, known_gaps: list[str]) -> bool:
    """A gap entry counts only if it names the finding id AND says something beyond the id.

    Requiring more than the bare id is deliberate: `known_gaps: ["a1b2c3d4e5f6"]` is a list of
    excuses with the excuses left out, and it would otherwise be the one-token way to pass this
    gate for every finding at once.
    """
    for gap in known_gaps:
        text = str(gap)
        if _mentions_id(text, finding_id) and len(text.strip()) > len(finding_id) + 8:
            return True
    return False


def evaluate_remediation(
    content: dict[str, Any] | None,
    scan: dict[str, Any] | None,
    changed_files: list[str] | None = None,
    added_lines: str = "",
    prior_ids: frozenset[str] | None = None,
    *,
    scan_is_fresh: bool = True,
) -> tuple[bool, list[str]]:
    """(passed, reasons) -- `evaluate_remediation_checks` with the check ids stripped. This is the
    shape the Stop hook's CLI and every pre-existing caller consume; see that function for the
    rules themselves."""
    passed, tagged, _ran = evaluate_remediation_checks(
        content, scan, changed_files, added_lines, prior_ids, scan_is_fresh=scan_is_fresh
    )
    return passed, [reason for _check_id, reason in tagged]


# Sub-check ids, as plain strings: this file is stdlib-only (sandbox copy), so it cannot import
# gates/checks.py's Check. remediation_gate.py declares the matching Check objects.
CHECK_CONTENT = "remediation.content"
CHECK_SCAN = "remediation.scan"
CHECK_UNEXPLAINED = "remediation.unexplained_findings"
CHECK_FABRICATED = "remediation.fabricated_ids"
CHECK_IGNORE_FILES = "remediation.ignore_files"
CHECK_SUPPRESSION_COMMENTS = "remediation.suppression_comments"


def evaluate_remediation_checks(
    content: dict[str, Any] | None,
    scan: dict[str, Any] | None,
    changed_files: list[str] | None = None,
    added_lines: str = "",
    prior_ids: frozenset[str] | None = None,
    *,
    scan_is_fresh: bool = True,
) -> tuple[bool, list[tuple[str, str]], list[str]]:
    """(passed, [(check_id, reason), ...], ran_check_ids). Pure -- `scan` is the dashboard dict
    repo_scan already writes. `ran_check_ids` lists every sub-check actually evaluated, in order,
    so a caller can record the ones with no reason as passed.

    `scan` is the scan taken AFTER remediation ran; `prior_ids` are the finding ids from the scan it
    was handed BEFORE it ran. Both are needed and they are not interchangeable: a finding that was
    genuinely fixed is absent from the post-fix scan, so validating claimed ids against `scan` would
    flag every real fix as a fabrication (it did -- the self-check below caught exactly that).
    `prior_ids=None` means that pre-scan could not be read, and the fabrication check is then
    skipped rather than guessed at.

    A missing post-fix scan does NOT pass: this stage exists to act on findings, so "no findings
    file" means the check could not run, and an unrunnable check must never read as a clean one.

    `scan_is_fresh` (default True, so `remediation_gate.py`'s own call site -- which always hands a
    genuinely fresh, just-taken scan -- is completely unaffected): set to False by
    check-remediation-stop.mjs, whose only available `scan` is the PRE-DRAFT
    repo-scan-latest.json -- a snapshot of findings from BEFORE this same turn's fixes, by
    construction stale for the "still open after remediation" question. When False, check 1 below
    (unexplained-actionable-findings) is skipped entirely -- it would otherwise flag every finding
    this very turn just fixed as still-open, since it can only ever see the pre-fix snapshot. Checks
    2 (fabrication) and 3 (suppression) are unaffected: a claimed id's validity and a scanner-ignore
    file/comment are both true or false regardless of which scan is on hand.
    """
    if content is None:
        return False, [(CHECK_CONTENT, "the remediation stage produced no report at all")], [CHECK_CONTENT]
    if not scan:
        return False, [(
            CHECK_SCAN,
            (
                "no repo scan was available to verify remediation against -- the stage's claims about "
                "which findings it fixed cannot be checked, and an unverifiable claim is not an approval"
            ),
        )], [CHECK_CONTENT, CHECK_SCAN]

    findings = scan.get("findings") or []
    # The fix-everything contract: every `actionable` finding -- ANY severity, application code
    # only, quality debt only when this pipeline introduced it (see repo_scan.to_dashboard_dict)
    # -- must be gone from the post-fix scan or explained in known_gaps. Older scans (pre-v3)
    # carry no `actionable` key; `gating` is the honest fallback there, never a silent pass.
    actionable = [f for f in findings if f.get("actionable", f.get("gating"))]
    known_gaps = [str(g) for g in _presence_values(content.get("known_gaps"))]
    claimed = [str(c) for c in _presence_values(content.get("findings_addressed"))]
    all_ids = {str(f.get("id")) for f in findings}

    reasons: list[tuple[str, str]] = []
    ran = [CHECK_CONTENT, CHECK_SCAN]

    # 1. Every actionable finding still open after this stage ran must be explained. This is the
    #    check that actually blocks: it reads the CURRENT scan, so a claim that a finding was
    #    fixed is worth exactly as much as the finding's absence from it. Skipped entirely when
    #    `scan` is known to be stale (`scan_is_fresh=False`) -- see this function's own docstring.
    if scan_is_fresh:
        ran.append(CHECK_UNEXPLAINED)
        unexplained: list[str] = []
        for finding in actionable:
            finding_id = str(finding.get("id"))
            if accounted_for(finding_id, known_gaps):
                continue
            location = (finding.get("location") or {}).get("path") or "unknown path"
            unexplained.append(
                f"finding {finding_id} [{finding.get('severity')}/{finding.get('category')}] is still "
                f"open after remediation and is not in known_gaps: "
                f"{finding.get('title')} at {location}"
                + (f" (fixed_version {finding['package'].get('fixed_version')})" if (finding.get("package") or {}).get("fixed_version") else "")
            )
        # Cap what the feedback carries -- 60 unexplained findings would drown the fix prompt, and
        # the model reads repo-scan-latest.json itself (its own prompt says so). Named-not-counted
        # still holds: the first 30 are named, the remainder is a pointer to the exact file/flag.
        if len(unexplained) > 30:
            unexplained = unexplained[:30] + [(
                f"...and {len(unexplained) - 30} more -- every `actionable: true` entry in "
                f"repo-scan-latest.json must be fixed or explained in known_gaps"
            )]
        reasons.extend((CHECK_UNEXPLAINED, reason) for reason in unexplained)

    # 2. A claimed id that appears in NEITHER the scan it was handed nor the scan taken after is a
    #    fabrication, not a fix. Both sets count: a fixed finding leaves the post-fix scan, and a
    #    finding fixed non-gatingly stays in it. Union, not intersection.
    if prior_ids is not None:
        ran.append(CHECK_FABRICATED)
        known_ids = all_ids | set(prior_ids)
        for finding_id in claimed:
            if finding_id not in known_ids:
                reasons.append((
                    CHECK_FABRICATED,
                    (
                        f"findings_addressed names {finding_id!r}, which is not the id of any finding "
                        f"in the scan -- ids must be copied verbatim from repo-scan-latest.json"
                    ),
                ))

    # 3. Silencing the scanner is not remediation.
    ran.append(CHECK_IGNORE_FILES)
    for path in changed_files or []:
        normalized = path.replace("\\", "/")
        if any(normalized.endswith(candidate) for candidate in SUPPRESSION_PATHS):
            reasons.append((
                CHECK_IGNORE_FILES,
                (
                    f"{path} is a scanner ignore/config file -- remediation must fix findings, never "
                    f"suppress them; revert this and address the finding itself"
                ),
            ))
    ran.append(CHECK_SUPPRESSION_COMMENTS)
    suppressions = sorted(set(_SUPPRESSION_COMMENT_RE.findall(added_lines)))
    if suppressions:
        reasons.append((
            CHECK_SUPPRESSION_COMMENTS,
            "added inline scanner-suppression comment(s) "
            + ", ".join(repr(s) for s in suppressions)
            + " -- fix the finding instead of hiding it",
        ))

    return not reasons, reasons, ran


def _demo() -> None:
    """`cd agent && uv run python -m src.gates.remediation_evaluate_checks`."""
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "remediation_evaluate_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            "sandbox-image/hooks/remediation_evaluate_checks.py has drifted from "
            "src/gates/remediation_evaluate_checks.py -- re-sync with: cp "
            "src/gates/remediation_evaluate_checks.py sandbox-image/hooks/remediation_evaluate_checks.py"
        )

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

    # scan_is_fresh=False (check-remediation-stop.mjs's own posture, since its only `scan` is the
    # PRE-DRAFT snapshot): a finding still open in that stale scan is NOT reported -- the hook
    # cannot tell "genuinely still open" from "just fixed this turn, scan hasn't caught up yet".
    # Fabrication and suppression checks are unaffected.
    passed, reasons = evaluate_remediation({"findings_addressed": [], "known_gaps": []}, scan, scan_is_fresh=False)
    assert passed and reasons == [], reasons
    passed, reasons = evaluate_remediation(
        {"findings_addressed": ["deadbeef"], "known_gaps": []}, scan, prior_ids=prior, scan_is_fresh=False
    )
    assert not passed and "deadbeef" in reasons[0] and len(reasons) == 1, reasons
    passed, reasons = evaluate_remediation({"known_gaps": []}, scan, ["repo/.trivyignore"], scan_is_fresh=False)
    assert not passed and "suppress" in reasons[0] and len(reasons) == 1, reasons

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

    # Tagged variant: every reason carries the sub-check that raised it, and `ran` lists exactly
    # the sub-checks evaluated -- the early returns stop the list, prior_ids=None / a stale scan
    # leave their check out.
    assert evaluate_remediation_checks(None, scan) == (
        False, [(CHECK_CONTENT, "the remediation stage produced no report at all")], [CHECK_CONTENT]
    )
    passed, tagged, ran = evaluate_remediation_checks(clean, None)
    assert not passed and [t[0] for t in tagged] == [CHECK_SCAN] and ran == [CHECK_CONTENT, CHECK_SCAN]
    passed, tagged, ran = evaluate_remediation_checks(
        {"findings_addressed": ["deadbeef"], "known_gaps": []}, scan, ["repo/.trivyignore"],
        "x # nosec\n", prior_ids=prior,
    )
    assert not passed and [t[0] for t in tagged] == [
        CHECK_UNEXPLAINED, CHECK_FABRICATED, CHECK_IGNORE_FILES, CHECK_SUPPRESSION_COMMENTS
    ], tagged
    assert ran == [CHECK_CONTENT, CHECK_SCAN, CHECK_UNEXPLAINED, CHECK_FABRICATED, CHECK_IGNORE_FILES,
                   CHECK_SUPPRESSION_COMMENTS], ran
    passed, tagged, ran = evaluate_remediation_checks(clean, scan, scan_is_fresh=False)
    assert passed and tagged == [] and ran == [CHECK_CONTENT, CHECK_SCAN, CHECK_IGNORE_FILES,
                                               CHECK_SUPPRESSION_COMMENTS], ran
    # The flood cap tags the "...and N more" pointer with the same check id.
    _, tagged, _ = evaluate_remediation_checks({"known_gaps": []}, flood)
    assert len(tagged) == 31 and {t[0] for t in tagged} == {CHECK_UNEXPLAINED}

    print("remediation_evaluate_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"content", "scan", "changed_files", "added_lines",
        # "prior_ids", "scan_is_fresh"} on stdin, JSON result on stdout. Deliberately the ONLY thing
        # this branch does -- no sandbox access beyond what the hook already read and handed over as
        # data. "scan_is_fresh" defaults to True (absent) to match evaluate_remediation's own
        # default -- only check-remediation-stop.mjs ever sends it explicitly (as False).
        payload = json.loads(sys.stdin.read())
        prior_ids_raw = payload.get("prior_ids")
        passed, reasons = evaluate_remediation(
            payload.get("content"),
            payload.get("scan"),
            payload.get("changed_files") or [],
            payload.get("added_lines") or "",
            frozenset(prior_ids_raw) if prior_ids_raw is not None else None,
            scan_is_fresh=payload.get("scan_is_fresh", True),
        )
        json.dump({"passed": passed, "reasons": reasons}, sys.stdout)
    else:
        _demo()
