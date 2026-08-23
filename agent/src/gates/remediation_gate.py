"""remediation's deterministic verify: check the claims against the scanner's own findings.

The stage reports which findings it fixed (`findings_addressed`), which packages it moved
(`dependencies_upgraded`) and what it deliberately left (`known_gaps`). Until this gate existed
nothing read any of it -- the stage's `content_field` was the summary STRING, so the other four
fields were discarded before they reached disk, and a run could claim to have fixed a critical
pre-auth RCE while changing nothing.

What is measured here, and what is not:

- **Measured.** Whether each still-gating finding is either gone from a fresh scan or accounted for
  in `known_gaps`; whether a claimed id exists in the scan at all (a fabricated id is the cheapest
  possible way to look busy); whether the "fix" was to silence the scanner rather than the defect.
- **Not measured.** Whether a *reason* in `known_gaps` is a good reason. That is judgement, and this
  gate does not pretend to have it -- it forces the reason to be stated and attached to a real
  finding id, which is what makes it reviewable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

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
) -> tuple[bool, list[str]]:
    """(passed, reasons). Pure -- `scan` is the dashboard dict repo_scan already writes.

    `scan` is the scan taken AFTER remediation ran; `prior_ids` are the finding ids from the scan it
    was handed BEFORE it ran. Both are needed and they are not interchangeable: a finding that was
    genuinely fixed is absent from the post-fix scan, so validating claimed ids against `scan` would
    flag every real fix as a fabrication (it did -- the self-check below caught exactly that).
    `prior_ids=None` means that pre-scan could not be read, and the fabrication check is then
    skipped rather than guessed at.

    A missing post-fix scan does NOT pass: this stage exists to act on findings, so "no findings
    file" means the check could not run, and an unrunnable check must never read as a clean one.
    """
    if content is None:
        return False, ["the remediation stage produced no report at all"]
    if not scan:
        return False, [
            "no repo scan was available to verify remediation against -- the stage's claims about "
            "which findings it fixed cannot be checked, and an unverifiable claim is not an approval"
        ]

    findings = scan.get("findings") or []
    gating = [f for f in findings if f.get("gating")]
    known_gaps = [str(g) for g in (content.get("known_gaps") or [])]
    claimed = [str(c) for c in (content.get("findings_addressed") or [])]
    all_ids = {str(f.get("id")) for f in findings}

    reasons: list[str] = []

    # 1. Every finding still gating after this stage ran must be explained. This is the check that
    #    actually blocks: it reads the CURRENT scan, so a claim that a finding was fixed is worth
    #    exactly as much as the finding's absence from it.
    for finding in gating:
        finding_id = str(finding.get("id"))
        if accounted_for(finding_id, known_gaps):
            continue
        location = (finding.get("location") or {}).get("path") or "unknown path"
        reasons.append(
            f"finding {finding_id} [{finding.get('severity')}/{finding.get('category')}] is still "
            f"gating after remediation and is not in known_gaps: "
            f"{finding.get('title')} at {location}"
            + (f" (fixed_version {finding['package'].get('fixed_version')})" if (finding.get("package") or {}).get("fixed_version") else "")
        )

    # 2. A claimed id that appears in NEITHER the scan it was handed nor the scan taken after is a
    #    fabrication, not a fix. Both sets count: a fixed finding leaves the post-fix scan, and a
    #    finding fixed non-gatingly stays in it. Union, not intersection.
    if prior_ids is not None:
        known_ids = all_ids | set(prior_ids)
        for finding_id in claimed:
            if finding_id not in known_ids:
                reasons.append(
                    f"findings_addressed names {finding_id!r}, which is not the id of any finding "
                    f"in the scan -- ids must be copied verbatim from repo-scan-latest.json"
                )

    # 3. Silencing the scanner is not remediation.
    for path in changed_files or []:
        normalized = path.replace("\\", "/")
        if any(normalized.endswith(candidate) for candidate in SUPPRESSION_PATHS):
            reasons.append(
                f"{path} is a scanner ignore/config file -- remediation must fix findings, never "
                f"suppress them; revert this and address the finding itself"
            )
    suppressions = sorted(set(_SUPPRESSION_COMMENT_RE.findall(added_lines)))
    if suppressions:
        reasons.append(
            "added inline scanner-suppression comment(s) "
            + ", ".join(repr(s) for s in suppressions)
            + " -- fix the finding instead of hiding it"
        )

    return not reasons, reasons


async def scan_and_publish(provider: Any, thread_id: str) -> dict[str, Any]:
    """Run a full scan and write it to `repo-scan-latest.json`, returning the dashboard dict.

    Shared by the pre-draft node and this gate so the stage reads and is judged against the SAME
    artifact. Before this existed nothing wrote that file ahead of remediation -- the metrics stage
    writes it afterwards, and the commit-triggered background refresh deliberately never touches
    committed artifacts -- so the prompt pointed at a file that did not exist.
    """
    from .. import repo_files, repo_scan

    report = await repo_scan.run_repo_scan(provider, thread_id, profile="full")
    scan = report.to_dashboard_dict()
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


async def verify_remediation(
    thread_id: str, content_dict: dict[str, Any], _run_id: str, baseline_commit: str | None, provider: Any,
    _chat_provider: str,
) -> "VerificationResult":
    # _chat_provider (StageSpec.deterministic_verify's Ruling-4 addition) is unused: this check has
    # no chat-model dispatch call of its own.
    from .. import repo_files, repo_scan
    from ..graph import VerificationResult

    scan: dict[str, Any] | None = None
    prior_ids: frozenset[str] | None = None
    changed_files: list[str] = []
    added_lines = ""
    try:
        # A FRESH scan, not `repo-scan-latest.json`: that file is refreshed by a background task
        # fired on commit, so reading it here races the stage's own commit and could block on
        # findings this stage already fixed. The scan is the evidence -- it has to be current.
        # A FRESH scan, republished to LATEST_PATH: a blocked retry then reads exactly what this
        # gate judged it against, rather than the pre-draft scan it has already acted on.
        scan = await scan_and_publish(provider, thread_id)
        prior_ids = await _prior_finding_ids(provider, thread_id, baseline_commit)
        changed_files, added_lines = await _stage_diff(provider, thread_id, baseline_commit)
    except Exception:  # noqa: BLE001 -- an unreadable scan must block, not crash the graph
        logger.exception("remediation gate: could not scan/diff for thread %s", thread_id)

    passed, reasons = evaluate_remediation(content_dict, scan, changed_files, added_lines, prior_ids)
    if passed:
        return VerificationResult(
            passed=True,
            feedback=(
                f"remediation verified: no gating findings remain unexplained "
                f"({len(content_dict.get('findings_addressed') or [])} addressed, "
                f"{len(content_dict.get('known_gaps') or [])} documented gap(s))"
            ),
            report={
                "findings_addressed": content_dict.get("findings_addressed") or [],
                "known_gaps": content_dict.get("known_gaps") or [],
            },
        )

    logger.info("remediation gate: blocking (%d reason(s))", len(reasons))
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

    print("remediation_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
