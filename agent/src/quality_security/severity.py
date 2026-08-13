"""P10's severity normalization -- one shared vocabulary (low/medium/high/critical/info) across
three different tools with three different native severity models.

Stated honestly, a simplification from the plan's stated intent: Semgrep's own SARIF `level`
field is trusted directly (error->high, warning->medium, note/info->low) rather than reading a
richer CVSS-derived score, since Semgrep's `level` already reflects its own rule-authored severity
judgment -- this matches the plan's own reasoning ("keeps the mapping auditable/one-directional").
Trivy's mapping uses the SAME level-based scheme as Semgrep here (not a real CVSS-tier extraction
from `properties["security-severity"]`, which Trivy's SARIF output does populate but this module
doesn't yet parse) -- a real gap versus "Trivy's CVSS-derived tiers map 1:1" as originally planned.
gitleaks findings are flat-critical unconditionally, exactly as designed (a leaked secret has no
legitimate "medium" tier by default).
"""

from __future__ import annotations

SEMGREP_SEVERITY_MAP = {"error": "high", "warning": "medium", "note": "low", "none": "low"}

# Not a true CVSS-tier mapping (see module docstring) -- a pragmatic level-based approximation.
TRIVY_SEVERITY_MAP = {"error": "high", "warning": "medium", "note": "low", "none": "low"}

GITLEAKS_SEVERITY = "critical"

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]


def meets_or_exceeds(severity: str, floor: str) -> bool:
    try:
        return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(floor)
    except ValueError:
        return False
