"""The shared severity vocabulary (info/low/medium/high/critical) and the ordering every gate
compares against.

Semgrep's own SARIF `level` is trusted directly (error->high, warning->medium, note/none->low)
rather than being re-derived from a score: `level` already reflects the rule author's own severity
judgment, and mapping it one-directionally keeps it auditable.

The Trivy and gitleaks entries that used to live here are gone. Trivy's was a level-based
approximation standing in for a real CVSS-tier extraction, and it is now genuinely unnecessary:
src/repo_scan.py parses Trivy's JSON rather than its SARIF, so it reads the vendor's actual
CRITICAL/HIGH/MEDIUM/LOW field, and computes a CVSS v3.1 base score for OSV findings that publish
only a vector. gitleaks' flat-critical rule survives as a decision, just expressed at the point
where the finding is built (a leaked secret has no legitimate "medium" tier by default).
"""

from __future__ import annotations

SEMGREP_SEVERITY_MAP = {"error": "high", "warning": "medium", "note": "low", "none": "low"}

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]


def meets_or_exceeds(severity: str, floor: str) -> bool:
    try:
        return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(floor)
    except ValueError:
        return False
