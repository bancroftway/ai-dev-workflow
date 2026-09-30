"""Shared SAST parsing for bandit/eslint-security's raw tool output.

`agent/src/repo_scan.py` (the real, persisted/graded gate) and `agent/src/gates/quick_scan.py` (an
ephemeral same-turn Stop hook check, staged standalone into the sandbox image) used to each parse
this exact tool output independently -- quick_scan.py's own module docstring already explains the
two output SHAPES are deliberately different (repo_scan.py's `Finding` carries a severity-tier
mapping/finding_key/dedup that quick_scan.py's `QuickFinding` never needed), but the RAW EXTRACTION
underneath -- which JSON fields to read, which eslint rule namespaces count as security-relevant,
path normalization -- was duplicated rather than shared; only the two tools' COMMAND strings were
(quick_scan.py's `BANDIT_COMMAND`/`ESLINT_SECURITY_COMMAND`, which repo_scan.py already imports).

This module holds that one shared extraction step, using repo_scan.py's fuller/more-correct
implementation as the canonical source (it is the one the real gate depends on). `parse_bandit`/
`parse_eslint_security` return `SastHit`s carrying every field either caller needs, with severity
left in each tool's own RAW vocabulary (bandit: its native HIGH/MEDIUM/LOW string; eslint: both its
native numeric 1|2 AND the rule-id-prefix-derived medium/low tier the two callers already agreed on
identically) -- the further severity-tier mapping repo_scan.py's `Finding` wants (`normalize_tier`)
and the raw-passthrough `QuickFinding` wants are each still that caller's own concern, applied on
top of the shared hit.

Deliberately stdlib-only (json/dataclasses), like quick_scan.py itself, and copied byte-identically
into the sandbox image (see the Dockerfile's staged-copy convention, same as
coverage_parsing.py/test_quality_checks.py/quick_scan.py) so quick_scan.py's standalone
`--check-hook` execution (no `agent.src` package on its sys.path there) can still import it -- see
quick_scan.py's own try/except import for why a plain top-level `import sast_parsers` is what makes
that work in both a package and a standalone script context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class SastHit:
    """One finding, in the tool's own raw vocabulary -- before either caller's own severity-tier
    mapping / finding_key / dedup is applied."""

    tool: str  # "bandit" | "eslint-security"
    rule_id: str
    file: str
    line: int | None
    raw_severity: str
    """The tool's own NATIVE severity representation, unmapped:
    bandit: `issue_severity` ("HIGH"/"MEDIUM"/"LOW"/"" ); eslint: the numeric `severity` (1|2) as a
    string ("1"/"2"/""). This is repo_scan.py's `Finding.raw_severity` field verbatim for eslint;
    bandit's own `normalize_tier`/passthrough mapping is each caller's job, applied to this value.
    """
    derived_severity: str = ""
    """eslint only: the rule-id-prefix-derived tier ("medium" for a `security/` rule, "low" for
    `sonarjs/`) -- both repo_scan.py's `Finding.severity` and quick_scan.py's `QuickFinding.severity`
    already computed this identically (from the rule id, not from eslint's own raw severity), so it
    is genuinely shared, not caller-specific. Empty for bandit -- bandit's tier is a real per-caller
    decision (repo_scan.py's `normalize_tier`, quick_scan.py's raw passthrough), not one this parser
    can make for both.
    """
    message: str = ""
    title: str = ""
    cwe_id: str | None = None  # bandit only (bare numeric id, e.g. "78"); always None for eslint.


# Only these rule namespaces from the pipeline-owned ESLint config count as security-relevant: the
# config also carries style/correctness rules (typescript-eslint, react-hooks) that belong to the
# BUILD, not a security scan -- turning them into findings would make remediation re-litigate lint
# style. `security/` rules are pattern-based possible-injection/unsafe-API detections -> medium;
# `sonarjs/` hotspots -> low. Both parsers already independently duplicated this identically; shared
# here so they cannot silently diverge.
ESLINT_SECURITY_PREFIXES = ("security/", "sonarjs/")


def norm_path(path: str | None) -> str:
    """Tool-reported path -> repo-relative, forward-slash form.

    Strips a genuine leading `./` PREFIX, in a loop (never `str.lstrip("./")`, which strips the
    CHARACTER SET {'.', '/'} repeatedly from the left, not the two-character prefix -- that
    silently ate the leading dot off every dotfile/dotdir path a tool reported relative to the scan
    root, `./.git/...` -> `git/...`, which made `is_non_application_path`'s regex -- anchored on the
    literal dot -- unable to recognize them, ever). Also strips eslint `-f json`'s absolute
    `/workspace/repo/` prefix (the sandbox clone's own root -- see the sandbox README's "The sandbox
    filesystem"), a no-op for bandit's already-relative paths.

    This is repo_scan.py's fuller implementation (its own `_norm_path`, used here as the canonical
    one per this module's docstring) -- quick_scan.py's prior copy only stripped a SINGLE leading
    `./` and applied the `/workspace/repo/` strip in the opposite order; harmless for every
    realistic input (a path is either relative or absolute, never both), but this loop is strictly
    more correct for a pathological multi-`./` input.
    """
    normalized = (path or "unknown").replace("\\", "/")
    if normalized.startswith("/workspace/repo/"):
        normalized = normalized[len("/workspace/repo/"):]
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def parse_bandit(raw: str) -> list[SastHit]:
    """bandit -f json: {"results": [{filename, line_number, test_id, test_name, issue_severity,
    issue_text, issue_cwe: {id, link}}, ...]}. Python SAST -- the licence-clean replacement for the
    official semgrep python pack (see repo_scan.py's module docstring's licence rule)."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    hits: list[SastHit] = []
    for result in doc.get("results") or []:
        if not isinstance(result, dict):
            continue
        line = result.get("line_number") if isinstance(result.get("line_number"), int) else None
        cwe = (result.get("issue_cwe") or {}).get("id") if isinstance(result.get("issue_cwe"), dict) else None
        hits.append(
            SastHit(
                tool="bandit",
                rule_id=str(result.get("test_id") or "bandit"),
                file=norm_path(str(result.get("filename") or "unknown")),
                line=line,
                raw_severity=str(result.get("issue_severity") or ""),
                message=str(result.get("issue_text") or result.get("test_name") or "bandit finding"),
                title=str(result.get("test_name") or result.get("issue_text") or "bandit finding"),
                cwe_id=str(cwe) if cwe else None,
            )
        )
    return hits


def parse_eslint_security(raw: str) -> list[SastHit]:
    """`eslint -f json`: [{filePath, messages: [{ruleId, severity(1|2), message, line}]}]. Keeps
    only security-relevant namespaces (see ESLINT_SECURITY_PREFIXES)."""
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    hits: list[SastHit] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = norm_path(str(entry.get("filePath") or "unknown"))
        for message in entry.get("messages") or []:
            if not isinstance(message, dict):
                continue
            rule_id = str(message.get("ruleId") or "")
            if not rule_id.startswith(ESLINT_SECURITY_PREFIXES):
                continue
            line = message.get("line") if isinstance(message.get("line"), int) else None
            text = str(message.get("message") or rule_id)
            hits.append(
                SastHit(
                    tool="eslint-security",
                    rule_id=rule_id,
                    file=path,
                    line=line,
                    raw_severity=str(message.get("severity") or ""),
                    derived_severity="medium" if rule_id.startswith("security/") else "low",
                    message=text,
                    title=text,
                )
            )
    return hits


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.gates.sast_parsers`
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "sast_parsers.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            "sandbox-image/hooks/sast_parsers.py has drifted from src/gates/sast_parsers.py -- "
            "re-sync with: cp src/gates/sast_parsers.py sandbox-image/hooks/sast_parsers.py"
        )

    # --- bandit: native severity + cwe carried through, unmapped -------------------------------
    hits = parse_bandit(json.dumps({
        "results": [
            {"filename": "./apps/api/app.py", "line_number": 12, "test_id": "B602",
             "test_name": "subprocess_popen_with_shell_equals_true", "issue_severity": "HIGH",
             "issue_text": "subprocess call with shell=True identified.",
             "issue_cwe": {"id": 78, "link": "https://cwe.mitre.org/data/definitions/78.html"}},
            {"filename": "./apps/api/util.py", "line_number": 3, "test_id": "B404",
             "test_name": "blacklist", "issue_severity": "LOW",
             "issue_text": "Consider possible security implications."},
        ],
    }))
    assert len(hits) == 2, hits
    assert hits[0].file == "apps/api/app.py" and hits[0].raw_severity == "HIGH"
    assert hits[0].cwe_id == "78" and hits[0].tool == "bandit"
    assert hits[0].title == "subprocess_popen_with_shell_equals_true"
    assert hits[1].cwe_id is None, "no issue_cwe on this result -- must not fabricate one"
    assert parse_bandit("not json") == [] and parse_bandit("[]") == []

    # --- eslint-security: only security/* and sonarjs/* namespaces surface, derived tier shared ---
    ehits = parse_eslint_security(json.dumps([
        {"filePath": "/workspace/repo/apps/web/src/lib/query.ts", "messages": [
            {"ruleId": "security/detect-object-injection", "severity": 2,
             "message": "Generic Object Injection Sink", "line": 42},
            {"ruleId": "sonarjs/no-hardcoded-passwords", "severity": 2,
             "message": "Review this hard-coded password.", "line": 7},
            {"ruleId": "no-console", "severity": 2, "message": "Unexpected console statement.", "line": 1},
            {"ruleId": None, "severity": 2, "message": "Parsing error", "line": 1},
        ]},
    ]))
    assert len(ehits) == 2, ehits
    assert ehits[0].file == "apps/web/src/lib/query.ts", "absolute sandbox path must become repo-relative"
    assert ehits[0].derived_severity == "medium" and ehits[1].derived_severity == "low"
    assert ehits[0].raw_severity == "2", "the RAW eslint severity is the numeric string, not the derived tier"
    assert parse_eslint_security("not json") == [] and parse_eslint_security("{}") == []

    # --- norm_path: multi-`./` and the workspace-absolute prefix, independent of each other -------
    assert norm_path("././apps/api/app.py") == "apps/api/app.py"
    assert norm_path("/workspace/repo/apps/web/x.ts") == "apps/web/x.ts"
    assert norm_path(None) == "unknown"
    assert norm_path("") == "unknown"

    print("sast_parsers self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
