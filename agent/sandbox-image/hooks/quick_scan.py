"""Fast, low-risk, safe-to-run-INLINE checks: a narrow SAST slice (bandit + eslint-security),
reusing repo_scan.py's own command strings verbatim -- see this module's own `_demo()` drift
guard, same convention `coverage_parsing.py`/`test_quality_checks.py` already established --
extracted so the sandbox's own same-turn Stop hook
(`sandbox-image/hooks/check-quick-scan-stop.mjs`) can run the REAL check by shelling out to
python3 on this ONE file instead of a hand-ported JS reimplementation drifting from it.

repo_scan.py's own `TOOLS` tuple imports `BANDIT_COMMAND`/`ESLINT_SECURITY_COMMAND` from here
directly (not a re-typed copy) -- this is the "referenced from multiple locations, including
remediation" requirement made real: change a command here and both the mctg-time hook and
remediation's own full scan pick it up from the same place, they cannot silently diverge on
flags/excludes/output paths.

Deliberately narrower than repo_scan.py's own `parse_bandit`/`parse_eslint`: no severity-tier
mapping, no `finding_key`/dedup, no persisted `Finding` object -- this hook's whole job is "tell
the drafting model about something fixable RIGHT NOW, in this turn," using the tool's own raw
severity string as plain text. repo_scan.py's parsers stay the source of truth for anything
persisted/graded; this is a genuinely simpler, ephemeral-use parse, not a competing
implementation of the same concern. The RAW extraction the two used to duplicate independently
now lives in `sast_parsers.py` (Task 6) -- `parse_bandit`/`parse_eslint_security` below are thin
re-exports of it, reshaped into this module's own `QuickFinding`.

CLI mode (`python3 quick_scan.py --check-hook`, stdin: JSON `{"bandit_json": str|null,
"eslint_json": str|null}`, stdout: JSON `{"findings": [{"tool":..., "rule_id":..., "file":...,
"line":..., "severity":..., "message":...}, ...]}`) is what the Stop hook invokes AFTER running
these tools itself (same "hook runs the command, hands raw output to this file for parsing"
split `coverage_parsing.py` already uses -- this file never touches the sandbox filesystem
itself).

Formatting (`ruff format`/`prettier`) has no branch here at all: both are 100% mechanical (a real
diff IS the fix, nothing to parse or decide), so the hook runs `--write`/`--fix` directly and
never asks this module anything about it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

try:  # package context (agent/src/gates) -- the normal import path for every in-process caller
    # (this module's own _demo(), any future caller from within the app).
    from .sast_parsers import ESLINT_SECURITY_PREFIXES, parse_bandit as _parse_bandit_hits
    from .sast_parsers import parse_eslint_security as _parse_eslint_hits
except ImportError:  # standalone script context: staged alone into /opt/aidw-hooks/ (see this
    # module's own _demo() drift guard and the Dockerfile's byte-identical staged-copy convention,
    # same as sast_parsers.py itself, staged as a SIBLING file there) -- a relative import has no
    # parent package to resolve against when this file is executed directly
    # (`python3 quick_scan.py --check-hook`), so fall back to a flat import of the sibling module
    # Python's own script-directory sys.path entry already finds.
    from sast_parsers import ESLINT_SECURITY_PREFIXES, parse_bandit as _parse_bandit_hits  # type: ignore[no-redef]
    from sast_parsers import parse_eslint_security as _parse_eslint_hits  # type: ignore[no-redef]

# Kept as a module attribute (same name as before this refactor) for any external reader that
# imports it directly from here, though the derivation itself now lives in sast_parsers.py.
_ESLINT_SECURITY_PREFIXES = ESLINT_SECURITY_PREFIXES

# Verbatim reuse target: repo_scan.py's own bandit/eslint-security ToolSpec.command strings (see
# this module's _demo() for the drift guard). repo_scan.py imports these two constants directly
# for its own TOOLS tuple rather than defining a second copy.
BANDIT_COMMAND = (
    "bandit -r . -f json -o agent-work/bandit.json -s B101 "
    "-x './node_modules,./.venv,./apps/*/.venv,./agent-work,./.ai-dev-workflow' --exit-zero"
)
ESLINT_SECURITY_COMMAND = (
    "/opt/aidw/lint/node_modules/.bin/eslint --no-config-lookup "
    "--config /opt/aidw/lint/eslint.config.mjs --no-error-on-unmatched-pattern "
    "-f json -o agent-work/eslint.json . || true"
)


@dataclass(frozen=True)
class QuickFinding:
    tool: str
    rule_id: str
    file: str
    line: int | None
    severity: str  # the tool's own raw severity string, uninterpreted
    message: str


def parse_bandit(raw: str) -> list[QuickFinding]:
    """bandit -f json: {"results": [{filename, line_number, test_id, issue_severity, issue_text}, ...]}.

    Thin re-export of sast_parsers.parse_bandit's shared extraction, reshaped into this module's
    own (deliberately simpler, RAW-severity) QuickFinding -- see module docstring.
    """
    return [
        QuickFinding(
            tool=hit.tool, rule_id=hit.rule_id, file=hit.file, line=hit.line,
            severity=hit.raw_severity or "UNKNOWN", message=hit.message,
        )
        for hit in _parse_bandit_hits(raw)
    ]


def parse_eslint_security(raw: str) -> list[QuickFinding]:
    """`eslint -f json`: [{filePath, messages: [{ruleId, severity(1|2), message, line}]}]. Keeps
    only security-relevant namespaces -- see _ESLINT_SECURITY_PREFIXES.

    Thin re-export of sast_parsers.parse_eslint_security -- see module docstring. `severity` here
    is the shared rule-id-derived tier ("medium"/"low"), not eslint's own raw numeric severity --
    same value this function always reported, per `SastHit.derived_severity`'s own docstring.
    """
    return [
        QuickFinding(
            tool=hit.tool, rule_id=hit.rule_id, file=hit.file, line=hit.line,
            severity=hit.derived_severity, message=hit.message,
        )
        for hit in _parse_eslint_hits(raw)
    ]


def evaluate(bandit_json: str | None, eslint_json: str | None) -> dict:
    """The Stop hook's actual question: what, if anything, should the drafting model fix right
    now? Never raises -- malformed/absent input from either tool just yields no findings from
    that tool, matching every parser's own fail-soft-on-malformed-input convention."""
    findings: list[QuickFinding] = []
    if bandit_json:
        findings.extend(parse_bandit(bandit_json))
    if eslint_json:
        findings.extend(parse_eslint_security(eslint_json))
    return {
        "findings": [
            {
                "tool": f.tool, "rule_id": f.rule_id, "file": f.file,
                "line": f.line, "severity": f.severity, "message": f.message,
            }
            for f in findings
        ]
    }


def _demo() -> None:  # pragma: no cover -- `cd agent && python -m src.gates.quick_scan`
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "quick_scan.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            "sandbox-image/hooks/quick_scan.py has drifted from src/gates/quick_scan.py -- "
            "re-sync with: cp src/gates/quick_scan.py sandbox-image/hooks/quick_scan.py"
        )

    # The one place this module's constants are cross-checked against repo_scan.py's own ToolSpec
    # commands -- a LOCAL import (never at module scope, which would make this file unstageable
    # into the sandbox image, see module docstring) purely for this self-check.
    from .. import repo_scan
    assert repo_scan.TOOLS_BY_NAME["bandit"].command == BANDIT_COMMAND, (
        "BANDIT_COMMAND has drifted from repo_scan.py's own bandit ToolSpec -- these must be the "
        "SAME string (repo_scan.py should import this constant, not redefine it)"
    )
    assert repo_scan.TOOLS_BY_NAME["eslint-security"].command == ESLINT_SECURITY_COMMAND, (
        "ESLINT_SECURITY_COMMAND has drifted from repo_scan.py's own eslint-security ToolSpec"
    )

    staged_sast = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "sast_parsers.py"
    if staged_sast.exists():
        src_sast = Path(__file__).resolve().parent / "sast_parsers.py"
        assert staged_sast.read_bytes() == src_sast.read_bytes(), (
            "sandbox-image/hooks/sast_parsers.py has drifted from src/gates/sast_parsers.py -- "
            "re-sync with: cp src/gates/sast_parsers.py sandbox-image/hooks/sast_parsers.py"
        )

    # Task 6 REGRESSION: repo_scan.py's parse_bandit/parse_eslint and this module's
    # parse_bandit/parse_eslint_security now both derive from the one shared sast_parsers hit --
    # confirm each still produces exactly its OWN pre-consolidation shape for identical input
    # (the two were deliberately different shapes before this refactor, and must still be).
    cross_bandit = json.dumps({"results": [
        {"filename": "./apps/api/app.py", "line_number": 12, "test_id": "B602",
         "test_name": "subprocess_popen_with_shell_equals_true", "issue_severity": "HIGH",
         "issue_text": "subprocess call with shell=True identified.", "issue_cwe": {"id": 78}},
    ]})
    quick_bandit = parse_bandit(cross_bandit)
    gate_bandit, _ = repo_scan.parse_bandit(cross_bandit)
    assert quick_bandit[0].severity == "HIGH", "quick_scan must keep bandit's RAW severity string, unmapped"
    assert gate_bandit[0].severity == "high" and gate_bandit[0].cwe == "CWE-78", (
        "repo_scan.py must still tier-map the severity and format the cwe id -- that opinion is "
        "this gate's own, not shared"
    )
    assert quick_bandit[0].file == gate_bandit[0].file == "apps/api/app.py"
    assert quick_bandit[0].rule_id == gate_bandit[0].rule_id == "B602"

    cross_eslint = json.dumps([
        {"filePath": "/workspace/repo/apps/web/src/lib/query.ts", "messages": [
            {"ruleId": "security/detect-object-injection", "severity": 2,
             "message": "Generic Object Injection Sink", "line": 42},
        ]},
    ])
    quick_eslint = parse_eslint_security(cross_eslint)
    gate_eslint, _ = repo_scan.parse_eslint(cross_eslint)
    assert quick_eslint[0].severity == "medium" == gate_eslint[0].severity, (
        "both callers already agreed on this rule-id-derived tier before the refactor"
    )
    assert gate_eslint[0].raw_severity == "2", "repo_scan.py alone still keeps eslint's raw numeric severity"
    assert quick_eslint[0].file == gate_eslint[0].file == "apps/web/src/lib/query.ts"

    bandit_sample = json.dumps({"results": [
        {"filename": "./app.py", "line_number": 12, "test_id": "B301", "issue_severity": "HIGH", "issue_text": "pickle load is unsafe"},
    ]})
    findings = parse_bandit(bandit_sample)
    assert len(findings) == 1 and findings[0].file == "app.py" and findings[0].severity == "HIGH", findings

    eslint_sample = json.dumps([
        {"filePath": "/workspace/repo/src/x.ts", "messages": [
            {"ruleId": "security/detect-eval-with-expression", "severity": 2, "message": "eval is unsafe", "line": 5},
            {"ruleId": "@typescript-eslint/no-unused-vars", "severity": 1, "message": "unused var", "line": 9},
        ]},
    ])
    efindings = parse_eslint_security(eslint_sample)
    assert len(efindings) == 1 and efindings[0].file == "src/x.ts" and efindings[0].severity == "medium", efindings
    # A non-security-namespace rule (typescript-eslint) must never surface -- same filter
    # repo_scan.py's own parse_eslint applies, for the same reason (style/correctness belongs to
    # the build, not a security scan).
    assert all(f.rule_id.startswith(_ESLINT_SECURITY_PREFIXES) for f in efindings)

    result = evaluate(bandit_sample, eslint_sample)
    assert len(result["findings"]) == 2, result

    assert evaluate(None, None) == {"findings": []}
    assert evaluate("not json", "also not json") == {"findings": []}

    print("quick_scan self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"bandit_json":..., "eslint_json":...} on stdin,
        # JSON result on stdout. Deliberately the ONLY thing this branch does -- no sandbox access
        # beyond what the hook already ran and handed over as data.
        payload = json.loads(sys.stdin.read())
        json.dump(evaluate(payload.get("bandit_json"), payload.get("eslint_json")), sys.stdout)
    else:
        _demo()
