"""Shared SARIF 2.1.0 parser -- used by both quality-remediation (Roslyn/SonarAnalyzer) and security-remediation (Semgrep/Trivy),
each of which speaks SARIF, into one common `Finding` shape (plan's own design intent: "one
shared parser"). gitleaks does not emit SARIF -- security-remediation's own scan node parses its native JSON
report separately and constructs `Finding`s directly, reusing `finding_key` for consistency.

`Finding` carries a second, optional tier of fields (category/cve/aliases/package/...) used by
repo_scan.py's cross-tool dedup and dashboard report. They are deliberately optional and are
omitted from `to_dict()` when left at their defaults, so quality-remediation/security-remediation's triage prompts -- which
json.dumps() these dicts straight into an LLM context -- do not grow a tail of nulls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    finding_key: str
    tool: str
    rule_id: str
    severity: str
    raw_severity: str
    file: str
    line: int | None
    message: str
    cwe: str | None = None
    status: str = "open"

    # Optional second tier -- see module docstring. Tuples, not lists: this dataclass is frozen.
    category: str = "sast"
    title: str = ""
    end_line: int | None = None
    cve: str | None = None
    aliases: tuple[str, ...] = ()
    package: dict[str, Any] | None = None
    severity_source: str = "native"
    sources: tuple[str, ...] = ()
    occurrences: int = 1

    def to_dict(self) -> dict[str, Any]:
        base = {
            "finding_key": self.finding_key,
            "tool": self.tool,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "raw_severity": self.raw_severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "cwe": self.cwe,
            "status": self.status,
        }
        for name, default in _OPTIONAL_FIELDS:
            value = getattr(self, name)
            if value != default:
                base[name] = list(value) if isinstance(value, tuple) else value
        return base


_OPTIONAL_FIELDS: tuple[tuple[str, Any], ...] = (
    ("category", "sast"),
    ("title", ""),
    ("end_line", None),
    ("cve", None),
    ("aliases", ()),
    ("package", None),
    ("severity_source", "native"),
    ("sources", ()),
    ("occurrences", 1),
)


def make_finding_key(tool: str, rule_id: str, file: str) -> str:
    """Deliberately excludes line number (per the plan) -- so a finding surviving unrelated line
    drift elsewhere in the file doesn't false-trigger re-triage as if it were a brand-new finding.
    """
    normalized_path = file.replace("\\", "/").lstrip("./")
    digest = hashlib.sha256(f"{tool}:{rule_id}:{normalized_path}".encode("utf-8")).hexdigest()
    return digest[:12]


def parse_sarif(raw_json: str, severity_map: dict[str, str] | None = None) -> list[Finding]:
    """Parses a SARIF log's runs[].results[] into Finding objects.

    severity_map translates a run's own SARIF `level` ("error"/"warning"/"note"/"none") into this
    pipeline's normalized severity vocabulary. quality-remediation doesn't need the low/medium/high/critical tiers
    security-remediation does (its exit gate only cares about "error" vs everything else) -- callers pass whatever
    mapping their own severity.py module defines; unmapped levels pass through unchanged.
    """
    try:
        doc = json.loads(raw_json)
    except json.JSONDecodeError:
        return []

    severity_map = severity_map or {}
    findings: list[Finding] = []

    for run in doc.get("runs", []):
        tool_name = run.get("tool", {}).get("driver", {}).get("name", "unknown")
        for result in run.get("results", []):
            rule_id = result.get("ruleId", "unknown-rule")
            level = result.get("level", "warning")
            message = result.get("message", {}).get("text", "")

            file_path = "unknown"
            line: int | None = None
            locations = result.get("locations") or []
            if locations:
                physical = locations[0].get("physicalLocation", {})
                file_path = physical.get("artifactLocation", {}).get("uri", "unknown")
                region = physical.get("region", {})
                line = region.get("startLine")

            cwe = None
            for tag in (result.get("properties", {}) or {}).get("tags", []) or []:
                if isinstance(tag, str) and tag.upper().startswith("CWE-"):
                    cwe = tag.upper()
                    break

            findings.append(
                Finding(
                    finding_key=make_finding_key(tool_name, rule_id, file_path),
                    tool=tool_name,
                    rule_id=rule_id,
                    severity=severity_map.get(level, level),
                    raw_severity=level,
                    file=file_path,
                    line=line,
                    message=message,
                    cwe=cwe,
                )
            )

    return findings
