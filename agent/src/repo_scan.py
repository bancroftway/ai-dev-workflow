"""Deterministic repository health scan: one engine, several callers.

Runs a licence-vetted, offline tool set inside the sandbox, normalizes every result into the one
`Finding` vocabulary defined by quality_security/sarif.py, **deduplicates across tools**, and
assembles a single structured report suitable for a repo metrics/health dashboard.

Callers select a subset with `profile=` (or an explicit `tools=`):

    quality  -> scc, lizard, jscpd, interrogate, dotnet-docs   (quality-remediation)
    security -> semgrep, trivy, gitleaks, osv-scanner, checkov (security-remediation)
    full     -> those plus git churn/ownership and syft (SBOM) (baseline node, metrics-report)

Licence bar is permissive (MIT / Apache-2.0), with exactly one documented exception: semgrep is
LGPL-2.1. Invoking it as a subprocess creates no derivative work, but it does not meet the bar, so
it is recorded in the report's `tools[]` block as `permissive: false` and driven from a vendored
rule pack rather than the network registry. SonarQube (LGPL-3.0 + server), TruffleHog (AGPL-3.0),
Hadolint (GPL-3.0), cloc (GPL-2.0) and code-maat (GPL-3.0) were evaluated and rejected on licence;
hercules, Dependency-Check, Grype, detect-secrets, ZAP and Nuclei were rejected as redundant or out
of scope. Syft was originally rejected here too ("redundant with Trivy's own SBOM mode") but is now
included as a dedicated tool: Syft is Anchore's purpose-built SBOM cataloger with materially deeper
per-ecosystem coverage than Trivy's SBOM-as-a-byproduct mode, and its output is large enough
(cyclonedx-json, one entry per dependency) that it is persisted as its own artifact
(`SBOM_PATH`) rather than folded into `metrics`, which flows into an 8000-char-truncated LLM
prompt elsewhere in the pipeline. Checkov's `misconfig` findings and interrogate's/dotnet-docs'
`maintainability`/documentation signal reuse `SECURITY_CATEGORIES`/`QUALITY_CATEGORIES` below --
no separate gate code needed for either. OpenSSF Scorecard was evaluated and NOT added: it needs a
live GitHub/GitLab token at scan time, after the untrusted target repo is already checked out --
this sandbox deliberately deletes its one-shot git token before any repo-supplied code executes
(see agent/sandbox-image/local_docker.py), and Scorecard would reintroduce exactly the credential
exposure that design prevents. Revisit only with an explicit scoped-token plan.

Determinism: every command is prefixed `LC_ALL=C`, vulnerability databases are baked into the
image and never updated at scan time, findings are sorted before serialization, and `content_hash`
covers findings + metrics only -- never `generated_at` or per-tool durations. Two runs over an
unchanged worktree in the same image must produce the same `content_hash`.

Findings deliberately carry no tool attribution in `to_dashboard_dict()`, the dashboard artifact.
Gate callers read `ScanReport.findings` directly, where `Finding.to_dict()` still carries `tool`
and `sources` -- security-remediation's never-suppress rule and anyone debugging a false positive need to know who
said what.

Verification status: the pure half (parsers, dedup, severity normalization, scoring, diff) has an
assert-based self-check, runnable with `uv run python -m src.repo_scan`. The sandbox-I/O half has
NOT been exercised against a real container, and the lizard CSV column order and the osv-scanner
v2 subcommand flags are encoded from documentation, not from observed output -- both are pinned by
fixtures in `_demo()` so a mismatch shows up as a parse failure rather than silent zeros.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from langchain_core.runnables import RunnableConfig

from .quality_security.sarif import Finding, parse_sarif
from .quality_security.severity import SEMGREP_SEVERITY_MAP, SEVERITY_ORDER, meets_or_exceeds

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

BASELINE_PATH = ".ai-dev-workflow/repo-scan-baseline.json"
LATEST_PATH = ".ai-dev-workflow/repo-scan-latest.json"
DELTA_PATH = ".ai-dev-workflow/repo-scan-delta.json"
# Syft's full cyclonedx-json output (one entry per dependency) is too large for the metrics dict --
# see the module docstring's SBOM paragraph -- so it is persisted here instead, exactly like the
# three paths above.
SBOM_PATH = ".ai-dev-workflow/sbom.json"
# The SBOM as it stood at the baseline scan, so supply_chain_diff has a "before" to compare
# against -- SBOM_PATH itself is overwritten by every subsequent scan.
SBOM_BASELINE_PATH = ".ai-dev-workflow/sbom-baseline.json"

# Vendored LGPL-2.1 semgrep rules and the baked offline OSV database, both placed by the sandbox
# image. Overridable so a differently-built image can move them without a code change.
SEMGREP_RULES_DIR = os.environ.get("AIDW_SEMGREP_RULES_DIR", "/opt/aidw/semgrep-rules")
OSV_DB_DIR = os.environ.get("AIDW_OSV_DB_DIR", "/opt/aidw/osv-db")

# Thresholds -- module constants with env overrides, matching quality_nodes.py's convention.
MAX_DUPLICATION_PERCENT = float(os.environ.get("QUALITY_MAX_DUPLICATION_PERCENT", "3.0"))
# 20, not lizard's warn-level 15: this is a HARD gate (an introduced finding blocks the run),
# and 15 flags ordinary dense-but-flat code -- observed live: a 14-line option-resolver at CCN 17
# ping-ponged between the quality fixer and the gate, each refactor pushing the complexity into
# a new helper. 15-19 is reviewer-attention territory, not block-the-pipeline territory; real
# monsters (20+) still gate.
LIZARD_MAX_CCN = int(os.environ.get("LIZARD_MAX_CCN", "20"))
LIZARD_HIGH_CCN = int(os.environ.get("LIZARD_HIGH_CCN", "25"))
CHURN_WINDOW_DAYS = int(os.environ.get("REPO_SCAN_CHURN_WINDOW_DAYS", "365"))
# Lenient first-cut floor, not a calibrated target: interrogate's own README default is 80%, but
# that's tuned for a project treating docstrings as a merge gate from day one. Starting at 50% means
# this only fires on repos with substantially undocumented public APIs, not on ordinary gaps.
DOC_COVERAGE_MIN_PERCENT = float(os.environ.get("DOC_COVERAGE_MIN_PERCENT", "50.0"))
SECURITY_SEVERITY_FLOOR = os.environ.get("SECURITY_SEVERITY_FLOOR", "medium")
# Bounds the coverage measurement the baseline node runs alongside the scan -- a hung test command
# must not hang the whole baseline forever. None (gate callers) keeps today's unbounded behavior.
REPO_SCAN_COVERAGE_TIMEOUT_SECONDS = int(os.environ.get("REPO_SCAN_COVERAGE_TIMEOUT_SECONDS", "600"))

# Which categories a security gate is allowed to block on. `duplication`/`maintainability`/
# `sast-quality` are quality-remediation's business and are gated on the baseline delta instead, never absolutely.
SECURITY_CATEGORIES = frozenset({"vulnerability", "secret", "sast", "misconfig", "license"})
QUALITY_CATEGORIES = frozenset({"duplication", "maintainability"})

# The tools whose findings feed SECURITY_CATEGORIES. The health score's security subscore is only
# trustworthy when every one of these that was selected actually ran ok -- a semgrep timeout that
# yields zero findings must read as "unmeasured", never as "clean" (at 40% weight, a tool crash
# would otherwise INFLATE the score).
_SECURITY_TOOL_NAMES = frozenset({"semgrep", "trivy", "gitleaks", "osv-scanner", "checkov"})


def _health_weight(env_name: str, default: float) -> float:
    """Env-overridable weight that can never crash module import on a garbage value."""
    try:
        return float(os.environ.get(env_name, str(default)))
    except ValueError:
        logger.warning("repo_scan: ignoring non-numeric %s", env_name)
        return default


# Health score v2 nominal weights, summing to 1.0. When a subscore is unmeasured (None) its weight
# is redistributed proportionally over the measured ones -- `health_weights_used` on the summary is
# the ground truth for what a given score actually weighed. Documented in README.md "Health score".
HEALTH_WEIGHTS: dict[str, float] = {
    "security": _health_weight("HEALTH_WEIGHT_SECURITY", 0.40),
    "coverage": _health_weight("HEALTH_WEIGHT_COVERAGE", 0.12),
    "dependencies": _health_weight("HEALTH_WEIGHT_DEPENDENCIES", 0.12),
    "ac_verification": _health_weight("HEALTH_WEIGHT_AC_VERIFICATION", 0.10),
    "accessibility": _health_weight("HEALTH_WEIGHT_ACCESSIBILITY", 0.07),
    "complexity": _health_weight("HEALTH_WEIGHT_COMPLEXITY", 0.06),
    "performance": _health_weight("HEALTH_WEIGHT_PERFORMANCE", 0.05),
    "duplication": _health_weight("HEALTH_WEIGHT_DUPLICATION", 0.04),
    "maintainability": _health_weight("HEALTH_WEIGHT_MAINTAINABILITY", 0.04),
}
HEALTH_SCORE_VERSION = 2
# lizard findings score in the complexity subscore and interrogate's percentage is blended directly,
# so their findings must not ALSO count in the maintainability subscore (double-counting).
_MAINTAINABILITY_EXCLUDED_RULE_IDS = frozenset({
    "high-cyclomatic-complexity", "docstring-coverage-under-threshold",
})

_CVE_RE = re.compile(r"^CVE-\d{4}-\d+$", re.IGNORECASE)
_GHSA_RE = re.compile(r"^GHSA-", re.IGNORECASE)
_TRIVY_DB_RE = re.compile(r"UpdatedAt:\s*(.+)")


# ------------------------------------------------------------------------------------------------
# Pure half -- no sandbox, no I/O, self-checked at the bottom of this module.
# ------------------------------------------------------------------------------------------------


def stable_id(category: str, identity: str, path: str) -> str:
    """Deliberately excludes the line number, for the same reason `make_finding_key` does: a
    finding that only drifted down the file is the *same* finding, and a dashboard trend line that
    resets on every reformat is worse than no trend line. Vulnerabilities additionally exclude the
    path, since a lockfile can move without the advisory changing."""
    normalized_path = path.replace("\\", "/").lstrip("./")
    seed = f"{category}:{identity}" if category == "vulnerability" else f"{category}:{identity}:{normalized_path}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _worst(*severities: str) -> str:
    known = [s for s in severities if s in SEVERITY_ORDER]
    return max(known, key=SEVERITY_ORDER.index) if known else "info"


def _norm_path(path: str) -> str:
    return (path or "unknown").replace("\\", "/").lstrip("./") or "."


# --- severity normalization -------------------------------------------------------------------

_CVSS3_WEIGHTS: dict[str, dict[str, float]] = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_CVSS3_PR = {"U": {"N": 0.85, "L": 0.62, "H": 0.27}, "C": {"N": 0.85, "L": 0.68, "H": 0.50}}


def cvss3_base_score(vector: str) -> float | None:
    """CVSS v3.1 base score from a vector string. OSV reports severity as a vector, not a number,
    and without this most osv-only findings would default to `medium` -- which would quietly
    weaken the security gate in both directions."""
    parts = dict(
        piece.split(":", 1) for piece in (vector or "").split("/") if ":" in piece
    )
    try:
        scope = parts["S"]
        av = _CVSS3_WEIGHTS["AV"][parts["AV"]]
        ac = _CVSS3_WEIGHTS["AC"][parts["AC"]]
        pr = _CVSS3_PR[scope][parts["PR"]]
        ui = _CVSS3_WEIGHTS["UI"][parts["UI"]]
        conf = _CVSS3_WEIGHTS["C"][parts["C"]]
        integ = _CVSS3_WEIGHTS["I"][parts["I"]]
        avail = _CVSS3_WEIGHTS["A"][parts["A"]]
    except KeyError:
        return None

    iss = 1 - ((1 - conf) * (1 - integ) * (1 - avail))
    if scope == "U":
        impact = 6.42 * iss
    else:
        impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15)
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    raw = min(impact + exploitability, 10.0) if scope == "U" else min(1.08 * (impact + exploitability), 10.0)
    # CVSS "roundup": round *up* to one decimal, not to-nearest. int arithmetic avoids float drift.
    return math.ceil(round(raw * 100000) / 10000.0) / 10.0


def severity_from_score(score: float | None) -> str | None:
    """Standard NVD qualitative bands."""
    if score is None:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    return "info"


def normalize_tier(raw: str | None) -> str | None:
    """CRITICAL/HIGH/MEDIUM/LOW/UNKNOWN -> this pipeline's vocabulary. UNKNOWN yields None so the
    caller can record `defaulted` rather than pretending UNKNOWN measured something."""
    lowered = (raw or "").strip().lower()
    if lowered in SEVERITY_ORDER:
        return lowered
    return {"unknown": None, "none": "info", "negligible": "info", "moderate": "medium"}.get(lowered)


# --- parsers ----------------------------------------------------------------------------------
# Every parser has the same signature: raw tool output -> (findings, metrics fragment). A parser
# never raises on malformed input; it returns what it could read. `run_repo_scan` records the tool
# as `failed` when nothing came back at all.

ParseResult = tuple[list[Finding], dict[str, Any]]


def parse_scc(raw: str) -> ParseResult:
    try:
        languages = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}
    if not isinstance(languages, list):
        return [], {}

    rows = [
        {
            "name": lang.get("Name", "unknown"),
            "files": int(lang.get("Count", 0) or 0),
            "code": int(lang.get("Code", 0) or 0),
            "comments": int(lang.get("Comment", 0) or 0),
            "lines": int(lang.get("Lines", 0) or 0),
            "complexity": int(lang.get("Complexity", 0) or 0),
        }
        for lang in languages
        if isinstance(lang, dict)
    ]
    rows.sort(key=lambda r: (-r["code"], r["name"]))
    return [], {
        "size": {
            "total_loc": sum(r["lines"] for r in rows),
            "code": sum(r["code"] for r in rows),
            "comments": sum(r["comments"] for r in rows),
            "files": sum(r["files"] for r in rows),
            "cyclomatic_total": sum(r["complexity"] for r in rows),
            "languages": [{"name": r["name"], "files": r["files"], "code": r["code"]} for r in rows],
        }
    }


# lizard --csv emits no header row. Column order per lizard's own documentation.
_LIZARD_COLUMNS = ("nloc", "ccn", "token_count", "param_count", "length", "location", "file", "function")


def parse_lizard(raw: str) -> ParseResult:
    functions: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < len(_LIZARD_COLUMNS):
            continue
        try:
            nloc, ccn = int(row[0]), int(row[1])
        except ValueError:
            continue  # lizard's trailing summary lines are not data rows
        functions.append(
            {"path": _norm_path(row[6]), "function": row[7].strip(), "ccn": ccn, "nloc": nloc}
        )

    # lizard has no ignore list (unlike jscpd two ToolSpecs down), so without this filter mean_ccn
    # is the mean over node_modules/, .venv/ and build output -- thousands of trivial vendored
    # functions dragging the mean toward 2 and pinning the complexity signal at "fine" regardless
    # of what the application code looks like. METRICS ONLY: findings still come from the full
    # set, preserving the existing reported-but-non-gating contract for vendored paths (is_gating
    # already excludes them) and avoiding a batch of phantom "fixed" findings in the first v2
    # delta.
    app_functions = [f for f in functions if not is_non_application_path(f["path"])]

    if not functions:
        return [], {}

    ccns = [f["ccn"] for f in app_functions] or [f["ccn"] for f in functions]
    over = [f for f in functions if f["ccn"] > LIZARD_MAX_CCN]
    over.sort(key=lambda f: (-f["ccn"], f["path"], f["function"]))
    app_over = [f for f in over if not is_non_application_path(f["path"])]

    findings = [
        Finding(
            finding_key=stable_id("maintainability", f["function"], f["path"]),
            tool="lizard",
            rule_id="high-cyclomatic-complexity",
            severity="high" if f["ccn"] > LIZARD_HIGH_CCN else "medium",
            raw_severity=str(f["ccn"]),
            file=f["path"],
            line=None,
            message=(
                f"Function `{f['function']}` has cyclomatic complexity {f['ccn']} "
                f"(threshold {LIZARD_MAX_CCN}), {f['nloc']} lines."
            ),
            category="maintainability",
            title=f"Complex function: {f['function']}",
            severity_source="derived",
            sources=("lizard",),
        )
        for f in over
    ]

    return findings, {
        "complexity": {
            # Application-scoped (app_functions/app_over) -- see the filter comment above. The
            # `or`-fallback in `ccns` keeps a pure-vendored tree measurable rather than crashing.
            "mean_ccn": round(sum(ccns) / len(ccns), 2),
            "max_ccn": max(ccns),
            "functions_total": len(app_functions or functions),
            "functions_over_threshold": len(app_over),
            "threshold": LIZARD_MAX_CCN,
            "worst": app_over[:10],
            # Consumed by the churn join, not serialized -- see _assemble_metrics.
            "_by_path": _max_ccn_by_path(app_functions or functions),
        }
    }


def _max_ccn_by_path(functions: Iterable[dict[str, Any]]) -> dict[str, int]:
    by_path: dict[str, int] = {}
    for fn in functions:
        by_path[fn["path"]] = max(by_path.get(fn["path"], 0), fn["ccn"])
    return by_path


def parse_jscpd(raw: str) -> ParseResult:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}

    total = (doc.get("statistics") or {}).get("total") or {}
    percent = float(total.get("percentage") or 0.0)
    duplicates = doc.get("duplicates") or []

    clones = sorted(
        (
            {
                "path": _norm_path((dup.get("firstFile") or {}).get("name", "")),
                "start_line": (dup.get("firstFile") or {}).get("start"),
                "duplicate_of": _norm_path((dup.get("secondFile") or {}).get("name", "")),
                "lines": dup.get("lines"),
            }
            for dup in duplicates
            if isinstance(dup, dict)
        ),
        key=lambda c: (-(c["lines"] or 0), c["path"], c["start_line"] or 0),
    )

    findings: list[Finding] = []
    if percent > MAX_DUPLICATION_PERCENT:
        # One aggregate finding, not one per clone: jscpd reports every pair, so per-clone findings
        # would flood the dashboard with N^2 noise for a single copy-pasted block. The individual
        # sites are still in metrics.duplication.clones.
        worst = clones[0]["path"] if clones else "."
        findings.append(
            Finding(
                finding_key=stable_id("duplication", "repo-duplication", worst),
                tool="jscpd",
                rule_id="duplication-over-threshold",
                severity="medium",
                raw_severity=f"{percent:.2f}%",
                file=worst,
                line=None,
                message=(
                    f"Copy-paste duplication is {percent:.2f}% of the codebase "
                    f"(threshold {MAX_DUPLICATION_PERCENT}%), across {len(duplicates)} clone pairs."
                ),
                category="duplication",
                title="Duplication over threshold",
                severity_source="derived",
                sources=("jscpd",),
            )
        )

    return findings, {
        "duplication": {
            "percent": round(percent, 2),
            "duplicated_lines": total.get("duplicatedLines"),
            "clone_count": len(duplicates),
            "threshold": MAX_DUPLICATION_PERCENT,
            "clones": clones[:20],
        }
    }


def parse_gitleaks(raw: str) -> ParseResult:
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}
    if not isinstance(entries, list):
        return [], {}

    # Build/vendor output is machine-generated after clone and NEVER a leaked credential --
    # Next.js writes random internal keys into .next/*manifest*.json on every build, which
    # gitleaks flags as critical "generic-api-key" secrets. Secrets are unsuppressable by policy,
    # so scanning generated output would deadlock the security gate on every built tree
    # (observed live). Real secrets live in files humans/models AUTHOR, which are all scanned.
    generated_prefixes = (".next/", "dist/", "build/", "out/", "node_modules/", "coverage/", ".wrangler/", ".ai-dev-workflow/", "agent-work/")

    findings = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = _norm_path(entry.get("File", ""))
        if path.startswith(generated_prefixes) or "/.next/" in path or "/node_modules/" in path or "/dist/" in path:
            continue
        rule_id = entry.get("RuleID", "gitleaks-secret")
        findings.append(
            Finding(
                finding_key=stable_id("secret", rule_id, path),
                tool="gitleaks",
                rule_id=rule_id,
                # A leaked credential has no legitimate "medium" tier, matching severity.py's
                # existing GITLEAKS_SEVERITY decision.
                severity="critical",
                raw_severity="leak",
                file=path,
                line=entry.get("StartLine"),
                end_line=entry.get("EndLine"),
                message=entry.get("Description") or "Potential secret detected",
                category="secret",
                title=f"Secret: {rule_id}",
                severity_source="derived",
                sources=("gitleaks",),
            )
        )
    return findings, {}


def parse_trivy(raw: str) -> ParseResult:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}

    findings: list[Finding] = []
    for result in doc.get("Results") or []:
        if not isinstance(result, dict):
            continue
        target = _norm_path(result.get("Target", ""))
        findings.extend(_trivy_vulnerabilities(result, target))
        findings.extend(_trivy_misconfigurations(result, target))
        findings.extend(_trivy_secrets(result, target))
        findings.extend(_trivy_licenses(result, target))
    return findings, {}


def _trivy_vulnerabilities(result: dict[str, Any], target: str) -> list[Finding]:
    findings = []
    for vuln in result.get("Vulnerabilities") or []:
        if not isinstance(vuln, dict):
            continue
        advisory_id = vuln.get("VulnerabilityID", "unknown")
        pkg_name = vuln.get("PkgName", "unknown")
        tier = normalize_tier(vuln.get("Severity"))
        cwes = vuln.get("CweIDs") or []
        findings.append(
            Finding(
                finding_key=stable_id("vulnerability", f"{advisory_id}:{pkg_name}", target),
                tool="trivy",
                rule_id=advisory_id,
                severity=tier or "medium",
                raw_severity=str(vuln.get("Severity", "")),
                file=target,
                line=None,
                message=vuln.get("Description") or vuln.get("Title") or advisory_id,
                cwe=cwes[0] if cwes else None,
                category="vulnerability",
                title=vuln.get("Title") or advisory_id,
                cve=advisory_id if _CVE_RE.match(advisory_id) else None,
                aliases=(advisory_id,),
                package={
                    "ecosystem": result.get("Type"),
                    "name": pkg_name,
                    "version": vuln.get("InstalledVersion"),
                    "fixed_version": vuln.get("FixedVersion") or None,
                    "purl": (vuln.get("PkgIdentifier") or {}).get("PURL"),
                },
                severity_source="native" if tier else "defaulted",
                sources=("trivy",),
            )
        )
    return findings


def _trivy_misconfigurations(result: dict[str, Any], target: str) -> list[Finding]:
    findings = []
    for miscfg in result.get("Misconfigurations") or []:
        if not isinstance(miscfg, dict):
            continue
        rule_id = miscfg.get("ID", "unknown")
        cause = miscfg.get("CauseMetadata") or {}
        tier = normalize_tier(miscfg.get("Severity"))
        findings.append(
            Finding(
                finding_key=stable_id("misconfig", rule_id, target),
                tool="trivy",
                rule_id=rule_id,
                severity=tier or "medium",
                raw_severity=str(miscfg.get("Severity", "")),
                file=target,
                line=cause.get("StartLine"),
                end_line=cause.get("EndLine"),
                message=miscfg.get("Description") or miscfg.get("Title") or rule_id,
                category="misconfig",
                title=miscfg.get("Title") or rule_id,
                severity_source="native" if tier else "defaulted",
                sources=("trivy",),
            )
        )
    return findings


def _trivy_secrets(result: dict[str, Any], target: str) -> list[Finding]:
    findings = []
    # Same generated-output filter as parse_gitleaks: build artifacts are not authored secrets,
    # and an unsuppressable "secret" in .next/ manifests deadlocks the security gate.
    normalized_target = _norm_path(target)
    if normalized_target.startswith((".next/", "dist/", "build/", "out/", "node_modules/", "coverage/", ".wrangler/", ".ai-dev-workflow/", "agent-work/")) or "/.next/" in normalized_target or "/node_modules/" in normalized_target or "/dist/" in normalized_target:
        return findings
    for secret in result.get("Secrets") or []:
        if not isinstance(secret, dict):
            continue
        rule_id = secret.get("RuleID", "trivy-secret")
        findings.append(
            Finding(
                finding_key=stable_id("secret", rule_id, target),
                tool="trivy",
                rule_id=rule_id,
                severity="critical",
                raw_severity=str(secret.get("Severity", "")),
                file=target,
                line=secret.get("StartLine"),
                end_line=secret.get("EndLine"),
                message=secret.get("Title") or "Potential secret detected",
                category="secret",
                title=f"Secret: {rule_id}",
                severity_source="derived",
                sources=("trivy",),
            )
        )
    return findings


def direct_dependency_names(lock_text: str) -> set[str]:
    """Packages the PROJECT chose, from an npm lockfile's own root entry. Authoritative.

    `packages[""]` records `dependencies` and `devDependencies` exactly as `package.json` declares
    them, so "did we choose this or did a framework drag it in" is a lookup, not an inference.

    This replaces a path heuristic. `is_transitive_dependency_file` answers the question by asking
    whether the finding's FILE looks like a lock file, which conflates two different things: a
    licence obligation on `next` (chosen here, actionable, should gate) and one on
    `@img/sharp-libvips-linux-x64` (dragged in by `next`, nothing this repo can do) both surface
    against `package-lock.json` and were both treated as advisory.

    It is also the plan's SBOM ancestry goal, reached from a source that actually carries the data:
    syft's CycloneDX `dependencies` graph was too sparse to use (816 components, 20 edges), while the
    lockfile states it outright.
    """
    try:
        doc = json.loads(lock_text)
    except (json.JSONDecodeError, TypeError):
        return set()
    root = (doc.get("packages") or {}).get("") or {}
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        section = root.get(key)
        if isinstance(section, dict):
            names.update(str(name) for name in section)
    return names


def uninstallable_lock_packages(lock_text: str, target_os: str, target_arch: str) -> set[str]:
    """Package names an npm lockfile marks OPTIONAL for a platform that is not the scan target.

    Authoritative, from the lockfile's own `os`/`cpu` constraints -- not a guess from the package
    name. npm records every platform variant of an optional native dependency and installs only the
    one matching the host, so a lockfile on a linux/x64 target legitimately contains entries for
    win32, darwin, freebsd and half a dozen CPU architectures that can never be present.

    This exists because a licence scan read those entries as if they were installed: a Click Counter
    app with no image handling at all was reported as carrying three high-severity LGPL obligations
    from `@img/sharp-win32-arm64` and friends. Measured on that repo, 16 of 27 `@img/*` lock entries
    were for platforms that cannot install there, and `ls node_modules/@img` confirmed only the
    linux/x64 and wasm32 variants existed. A licence that cannot apply to the artifact being built
    is not a finding about it.
    """
    try:
        doc = json.loads(lock_text)
    except (json.JSONDecodeError, TypeError):
        return set()
    excluded: set[str] = set()
    for path, entry in (doc.get("packages") or {}).items():
        if not isinstance(entry, dict) or not entry.get("optional"):
            continue
        oses = [str(o).lower() for o in (entry.get("os") or [])]
        cpus = [str(c).lower() for c in (entry.get("cpu") or [])]
        # A constraint list that excludes the target, honouring npm's "!platform" negation form.
        os_blocked = bool(oses) and not (
            target_os in oses or (any(o.startswith("!") for o in oses) and f"!{target_os}" not in oses)
        )
        cpu_blocked = bool(cpus) and not (
            target_arch in cpus or (any(c.startswith("!") for c in cpus) and f"!{target_arch}" not in cpus)
        )
        if os_blocked or cpu_blocked:
            name = path.split("node_modules/", 1)[-1] if "node_modules/" in path else path
            if name:
                excluded.add(name)
    return excluded


def _trivy_licenses(result: dict[str, Any], target: str) -> list[Finding]:
    findings = []
    for lic in result.get("Licenses") or []:
        if not isinstance(lic, dict):
            continue
        name = lic.get("Name", "unknown")
        path = _norm_path(lic.get("FilePath") or target)
        tier = normalize_tier(lic.get("Severity"))
        findings.append(
            Finding(
                finding_key=stable_id("license", name, path),
                tool="trivy",
                rule_id=name,
                severity=tier or "medium",
                raw_severity=str(lic.get("Severity", "")),
                file=path,
                line=None,
                message=f"{lic.get('Category', 'license')} licence {name} on package {lic.get('PkgName', 'unknown')}",
                category="license",
                title=f"Licence: {name}",
                severity_source="native" if tier else "defaulted",
                sources=("trivy",),
                # Structured, so a licence finding can be matched against the lockfile's own
                # platform constraints rather than by parsing it back out of `message`.
                package={"name": str(lic.get("PkgName") or "")} if lic.get("PkgName") else None,
            )
        )
    return findings


def parse_osv(raw: str) -> ParseResult:
    """osv-scanner v2 `scan source --format json`. OSV is the one tool that publishes explicit
    `aliases`, which is what seeds the cross-tool advisory reconciliation in `dedupe`."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}

    findings: list[Finding] = []
    for result in doc.get("results") or []:
        source_path = _norm_path(((result or {}).get("source") or {}).get("path", ""))
        for entry in (result or {}).get("packages") or []:
            if not isinstance(entry, dict):
                continue
            pkg = entry.get("package") or {}
            pkg_name = pkg.get("name", "unknown")
            for vuln in entry.get("vulnerabilities") or []:
                if not isinstance(vuln, dict):
                    continue
                findings.append(_osv_finding(vuln, pkg, pkg_name, source_path))
    return findings, {}


def _osv_finding(vuln: dict[str, Any], pkg: dict[str, Any], pkg_name: str, source_path: str) -> Finding:
    advisory_id = vuln.get("id", "unknown")
    aliases = tuple(a for a in (vuln.get("aliases") or []) if isinstance(a, str))

    tier = normalize_tier(((vuln.get("database_specific") or {}).get("severity")))
    severity_source = "native" if tier else "defaulted"
    if tier is None:
        for entry in vuln.get("severity") or []:
            if isinstance(entry, dict) and str(entry.get("type", "")).startswith("CVSS_V3"):
                tier = severity_from_score(cvss3_base_score(str(entry.get("score", ""))))
                if tier:
                    severity_source = "derived"
                    break

    return Finding(
        finding_key=stable_id("vulnerability", f"{advisory_id}:{pkg_name}", source_path),
        tool="osv-scanner",
        rule_id=advisory_id,
        severity=tier or "medium",
        raw_severity=str((vuln.get("database_specific") or {}).get("severity", "")),
        file=source_path,
        line=None,
        message=vuln.get("details") or vuln.get("summary") or advisory_id,
        category="vulnerability",
        title=vuln.get("summary") or advisory_id,
        cve=advisory_id if _CVE_RE.match(advisory_id) else next((a for a in aliases if _CVE_RE.match(a)), None),
        aliases=(advisory_id,) + aliases,
        package={
            "ecosystem": pkg.get("ecosystem"),
            "name": pkg_name,
            "version": pkg.get("version"),
            "fixed_version": None,
            "purl": None,
        },
        severity_source=severity_source,
        sources=("osv-scanner",),
    )


def parse_semgrep(raw: str) -> ParseResult:
    """Semgrep speaks SARIF, so this reuses the parser quality-remediation/security-remediation already share rather than adding a
    second one. Only the category and source tagging are repo_scan-specific."""
    findings = [
        replace(f, category="sast", title=f.rule_id, sources=("semgrep",), severity_source="native")
        for f in parse_sarif(raw, SEMGREP_SEVERITY_MAP)
    ]
    return findings, {}


def parse_git_churn(raw: str) -> ParseResult:
    """`git log --no-merges --numstat` with a `C|<sha>|<author>|<iso>` commit header line.

    Replaces hercules/code-maat entirely: hercules is unmaintained and code-maat is GPL-3.0, and
    churn/ownership is a `git log` parse, not a dependency.
    """
    per_file: dict[str, dict[str, Any]] = {}
    author = ""
    commits_seen: set[str] = set()

    for line in raw.splitlines():
        if line.startswith("C|"):
            parts = line.split("|")
            if len(parts) >= 3:
                commits_seen.add(parts[1])
                author = parts[2]
            continue
        columns = line.split("\t")
        if len(columns) != 3:
            continue
        added, deleted, path = columns
        path = _norm_path(path)
        entry = per_file.setdefault(path, {"commits": 0, "lines_changed": 0, "authors": {}})
        entry["commits"] += 1
        entry["lines_changed"] += _numstat_int(added) + _numstat_int(deleted)
        entry["authors"][author] = entry["authors"].get(author, 0) + 1

    if not per_file:
        return [], {}

    single_owner = sorted(
        path
        for path, entry in per_file.items()
        if entry["authors"] and max(entry["authors"].values()) / entry["commits"] >= 0.8
    )

    return [], {
        "churn": {
            "window_days": CHURN_WINDOW_DAYS,
            "commits": len(commits_seen),
            "files_touched": len(per_file),
            # Author names are deliberately not serialized -- ownership concentration is the signal,
            # naming individuals in a committed dashboard artifact is not.
            "ownership": {"bus_factor_files": len(single_owner), "single_owner_files": single_owner[:50]},
            "_per_file": per_file,
        }
    }


def _numstat_int(value: str) -> int:
    return 0 if value.strip() in ("-", "") else int(value)


def parse_checkov(raw: str) -> ParseResult:
    """Checkov's `-o json` prints either one framework-result dict, or a list of them when it
    auto-detects more than one IaC framework in the repo (Terraform + Dockerfile + Kubernetes,
    say) -- both shapes are handled here. Findings are tagged `misconfig`, the same category Trivy's
    own IaC scanning already uses, so they gate through the existing `SECURITY_CATEGORIES` check in
    `is_gating` with no new gate code."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}
    entries = doc if isinstance(doc, list) else [doc] if isinstance(doc, dict) else []

    findings: list[Finding] = []
    total_failed = 0
    total_passed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        results = entry.get("results") or {}
        failed = results.get("failed_checks") or []
        total_failed += len(failed)
        total_passed += len(results.get("passed_checks") or [])
        for check in failed:
            if not isinstance(check, dict):
                continue
            rule_id = check.get("check_id", "unknown")
            path = _norm_path(check.get("file_path", ""))
            line_range = check.get("file_line_range") or [None, None]
            tier = normalize_tier(check.get("severity"))
            findings.append(
                Finding(
                    finding_key=stable_id("misconfig", rule_id, path),
                    tool="checkov",
                    rule_id=rule_id,
                    # OSS Checkov leaves `severity` null on most checks (it's a paid-platform field);
                    # defaulting to medium matches Trivy's own misconfig parser above rather than
                    # inventing a different convention for the same category.
                    severity=tier or "medium",
                    raw_severity=str(check.get("severity") or ""),
                    file=path,
                    line=line_range[0] if len(line_range) > 0 else None,
                    end_line=line_range[1] if len(line_range) > 1 else None,
                    message=check.get("check_name") or rule_id,
                    category="misconfig",
                    title=check.get("check_name") or rule_id,
                    severity_source="native" if tier else "defaulted",
                    sources=("checkov",),
                )
            )
    if not entries:
        return [], {}
    return findings, {"iac": {"failed_checks": total_failed, "passed_checks": total_passed}}


_INTERROGATE_RESULT_RE = re.compile(r"actual:\s*([\d.]+)\s*%")


def parse_interrogate(raw: str) -> ParseResult:
    """Parses interrogate's documented final summary line (`RESULT: PASSED/FAILED (minimum: X%,
    actual: Y%)`), falling back to the last bare percentage in the output if that line is ever
    missing -- interrogate has no JSON output mode, so this is text parsing against a documented
    but not schema-guaranteed format, same caveat this module's own docstring already carries for
    lizard's CSV column order."""
    match = _INTERROGATE_RESULT_RE.search(raw)
    if match:
        percent = float(match.group(1))
    else:
        candidates = re.findall(r"([\d.]+)\s*%", raw)
        if not candidates:
            return [], {}
        percent = float(candidates[-1])

    findings: list[Finding] = []
    if percent < DOC_COVERAGE_MIN_PERCENT:
        # One aggregate finding for the whole repo, not one per undocumented symbol -- same
        # reasoning as jscpd's aggregate duplication finding above: a per-symbol flood would drown
        # the dashboard, and the per-file breakdown already lives in interrogate's own -v output.
        findings.append(
            Finding(
                finding_key=stable_id("maintainability", "docstring-coverage", "."),
                tool="interrogate",
                rule_id="docstring-coverage-under-threshold",
                severity="low",
                raw_severity=f"{percent:.1f}%",
                file=".",
                line=None,
                message=(
                    f"Python docstring coverage is {percent:.1f}% "
                    f"(threshold {DOC_COVERAGE_MIN_PERCENT}%)."
                ),
                category="maintainability",
                title="Docstring coverage under threshold",
                severity_source="derived",
                sources=("interrogate",),
            )
        )
    return findings, {
        "documentation": {"python_docstring_coverage_percent": round(percent, 1), "python_threshold": DOC_COVERAGE_MIN_PERCENT}
    }


# MSB1003 (no project/solution found) / MSB1011 (multiple found, ambiguous) mean this repo has no
# single buildable .NET target -- reporting 0 CS1591 warnings in that case would misread as "fully
# documented" rather than "not a .NET repo", so this is treated as no measurement at all.
_DOTNET_NO_PROJECT_RE = re.compile(r"MSB1003|MSB1011")
_CS1591_RE = re.compile(r"\bCS1591\b")


def parse_dotnet_docs(raw: str) -> ParseResult:
    """Counts Roslyn's CS1591 ("missing XML comment for publicly visible member") from a dedicated
    build invocation that overrides `TreatWarningsAsErrors`/`GenerateDocumentationFile` on the
    command line only (`/p:...`, MSBuild's highest-precedence property source) -- it deliberately
    never touches `templates/dotnet/Directory.Build.props`, which sets `TreatWarningsAsErrors=true`
    repo-wide and is explicitly marked "DO NOT MODIFY DURING FEATURE WORK". Reports a raw count, not
    a percentage: unlike interrogate, nothing here also counts total public members, and a bare
    count isn't comparable across repos of different sizes -- visibility only, no aggregate finding."""
    if _DOTNET_NO_PROJECT_RE.search(raw):
        return [], {}
    return [], {"documentation": {"dotnet_undocumented_public_members": len(_CS1591_RE.findall(raw))}}


# Fraction of SBOM components the dependency graph must actually mention before ancestry is
# reported at all. Syft emits a near-empty graph for some ecosystems (816 components / 20 edges on
# this pipeline's own branch), and a split derived from that describes the tool, not the project.
MIN_GRAPH_COVERAGE = 0.5


def sbom_ancestry(doc: dict[str, Any]) -> dict[str, Any]:
    """Direct vs transitive dependencies, and licences, from a CycloneDX document. Pure.

    The `dependencies` graph makes ancestry a FACT -- "you chose this" vs "Next.js chose this" --
    where the alternative is `is_transitive_dependency_file`, which infers it from whether the
    finding's path looks like a lock file. That heuristic is right often and wrong silently: a
    direct dependency pinned in a lock file reads as inherited, and an inherited one surfaced
    against `package.json` reads as chosen.

    Returns `{}` when the document carries no USABLE dependency graph, so callers can tell
    "measured, and everything is direct" apart from "no graph to measure" -- the licence gate must
    not treat an absent graph as proof that nothing is inherited.

    "Usable" is a real threshold, not a formality. Measured on this pipeline's own branch, syft's
    CycloneDX output held **816 components and 20 dependency entries** -- 97% of components appear
    nowhere in the graph. Attributing ancestry from that yields "773 direct, 43 transitive", which is
    not a finding about the project; it is a restatement of which components syft happened to link.
    Below MIN_GRAPH_COVERAGE the honest answer is that ancestry was not measured.
    """
    dependencies = doc.get("dependencies")
    components = doc.get("components") or []
    if not isinstance(dependencies, list) or not dependencies:
        return {}

    all_refs = {c.get("bom-ref") for c in components if isinstance(c, dict) and c.get("bom-ref")}
    parents: dict[str, set[str]] = {}
    for entry in dependencies:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("ref")
        if not isinstance(ref, str):
            continue
        for child in entry.get("dependsOn") or []:
            if isinstance(child, str):
                parents.setdefault(child, set()).add(ref)

    # Direct = no COMPONENT depends on it. Its only parent (if any) is the document's subject, which
    # CycloneDX carries in `metadata.component` and not in `components` -- so "chosen by the project"
    # falls out of the graph without needing to identify the root at all. Keying off
    # `metadata.component.bom-ref` instead looked equivalent and was not: a document whose subject
    # ref is absent or spelled differently then yields ZERO direct dependencies and reports every
    # top-level package as inherited.
    # Sparsity guard: how many components the graph actually says anything about.
    mentioned = {ref for ref in parents if ref in all_refs}
    for entry in dependencies:
        if isinstance(entry, dict) and isinstance(entry.get("ref"), str) and entry["ref"] in all_refs:
            mentioned.add(entry["ref"])
    coverage = len(mentioned) / len(all_refs) if all_refs else 0.0
    if coverage < MIN_GRAPH_COVERAGE:
        return {}

    direct = {ref for ref in all_refs if not (parents.get(ref, set()) & all_refs)}
    transitive = {ref for ref in all_refs if ref not in direct}

    licences: dict[str, list[str]] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        names: list[str] = []
        for entry in component.get("licenses") or []:
            if not isinstance(entry, dict):
                continue
            licence = entry.get("license") or {}
            name = licence.get("id") or licence.get("name") or entry.get("expression")
            if name:
                names.append(str(name))
        if names:
            licences[str(component.get("bom-ref"))] = sorted(set(names))

    return {
        "direct": sorted(direct),
        "transitive_count": len(transitive),
        "direct_count": len(direct),
        "licences_by_ref": licences,
        "with_licence": len(licences),
    }


def sbom_component_purls(doc: dict[str, Any] | None) -> dict[str, str]:
    """`{ecosystem/name -> version}` for every component with a real package identity (a purl).

    Keyed on the purl's name rather than bom-ref because refs are per-document hashes -- comparing
    two scans by ref reports every component as both removed and added. The version is kept so an
    upgrade reads as an upgrade rather than as a remove/add pair.

    Components WITHOUT a purl are skipped, and that is the whole subtlety. Syft catalogues build
    output as components too: measured on one branch, 404 of 803 entries had no purl and their
    `name` was an absolute file path (`/workspace/repo/apps/api.Tests/bin/Debug/net10.0/Api.dll`).
    A name-based fallback therefore turned a dependency diff into a list of DLLs -- it reported
    "+796 components added" for a run that added roughly 399 packages, which is worse than useless
    because it looks precise. No purl means no package identity, so there is nothing to diff.
    """
    purls: dict[str, str] = {}
    for component in (doc or {}).get("components") or []:
        if not isinstance(component, dict):
            continue
        match = re.match(r"^pkg:([^/]+)/(.+?)@([^?#]+)", component.get("purl") or "")
        if match:
            ecosystem, name, version = match.groups()
            purls[f"{ecosystem}/{name}"] = version
    return purls


def supply_chain_diff(baseline_sbom: dict[str, Any] | None, current_sbom: dict[str, Any] | None) -> dict[str, Any] | None:
    """Which packages this run added, removed or moved. None when either side is unavailable.

    Independent of the `dependencies` graph on purpose -- component identity is what syft reports
    reliably, and `sbom_ancestry` has to decline on a sparse graph. This is the part of "put the SBOM
    to work" that survives contact with syft's actual output.
    """
    if baseline_sbom is None or current_sbom is None:
        return None
    before, after = sbom_component_purls(baseline_sbom), sbom_component_purls(current_sbom)
    added = sorted(name for name in after.keys() - before.keys())
    removed = sorted(name for name in before.keys() - after.keys())
    changed = sorted(
        f"{name}: {before[name]} -> {after[name]}"
        for name in before.keys() & after.keys()
        if before[name] != after[name]
    )
    return {
        "added": added,
        "removed": removed,
        "version_changed": changed,
        "added_count": len(added),
        "removed_count": len(removed),
        "net_change": len(after) - len(before),
    }


def parse_syft(raw: str) -> ParseResult:
    """Summarizes Syft's cyclonedx-json SBOM into a handful of numbers for the metrics dict; the
    full document is persisted separately (`SBOM_PATH`, written by `run_repo_scan` below) rather
    than expanded here -- see the module docstring's SBOM paragraph for why."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}
    components = doc.get("components")
    if not isinstance(components, list):
        return [], {}

    ecosystems: dict[str, int] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        match = re.match(r"pkg:([^/]+)/", component.get("purl") or "")
        ecosystem = match.group(1) if match else "unknown"
        ecosystems[ecosystem] = ecosystems.get(ecosystem, 0) + 1

    ancestry = sbom_ancestry(doc)
    sbom: dict[str, Any] = {
        "component_count": len(components),
        "ecosystems": dict(sorted(ecosystems.items(), key=lambda kv: (-kv[1], kv[0]))),
        "format": "cyclonedx-json",
    }
    if ancestry:
        # Counts only, plus the direct set: `licences_by_ref` and the full ref lists are large and
        # this fragment lands in the hashed metrics body.
        sbom["direct_count"] = ancestry["direct_count"]
        sbom["transitive_count"] = ancestry["transitive_count"]
        sbom["components_with_licence"] = ancestry["with_licence"]
    else:
        # Distinguishable from "all direct" on purpose -- see sbom_ancestry.
        sbom["ancestry"] = "no_dependency_graph"
    return [], {"sbom": sbom}


_OUTDATED_SECTION_RE = re.compile(r"^### (npm|dotnet|pypi)\b")
_DOTNET_OUTDATED_ROW_RE = re.compile(r"^\s*>\s+(\S+)")


def parse_outdated(raw: str) -> ParseResult:
    """Counts outdated packages per ecosystem from the marker-sectioned output the `outdated`
    ToolSpec's compound script writes. Strictly fail-open: a section only counts as MEASURED when
    the probe's own output actually parses -- a bare `### npm` marker whose probe produced nothing
    (registry unreachable, no node_modules, MSB error) contributes neither a count nor a
    "measured" claim, so the dependencies subscore reads null (weight redistributes), never a
    fabricated 100. No measured sections at all returns `{}`.

    npm sections hold `npm outdated --json` (dict keyed by package; an `error` key is a FAILED
    probe, not one outdated package named "error"), pypi sections hold `pip list --outdated
    --format=json` (a list), dotnet sections hold the human-readable `dotnet list package
    --outdated` table -- credited only when the table header or the explicit "no updates" phrasing
    is present, with `> ` rows deduped by package name (multi-project solutions repeat them)."""
    counts = {"npm": 0, "nuget": 0, "pypi": 0}
    measured: list[str] = []
    section: str | None = None
    buffer: list[str] = []

    def _mark(name: str) -> None:
        if name not in measured:
            measured.append(name)

    def _flush() -> None:
        nonlocal buffer, section
        if section is None:
            buffer = []
            return
        text = "\n".join(buffer).strip()
        buffer = []
        if section == "dotnet":
            if "Top-level Package" in text or "no updates given" in text or "has no updates" in text:
                packages = {m.group(1) for line in text.splitlines() if (m := _DOTNET_OUTDATED_ROW_RE.match(line))}
                counts["nuget"] += len(packages)
                _mark("dotnet")
            return
        if not text:
            return
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return
        if section == "npm" and isinstance(doc, dict):
            if "error" in doc:
                return  # npm's own failure report on stdout -- a failed probe, not a package
            counts["npm"] += sum(1 for v in doc.values() if isinstance(v, (dict, list)))
            _mark("npm")
        elif section == "pypi" and isinstance(doc, list):
            counts["pypi"] += len(doc)
            _mark("pypi")

    for line in raw.splitlines():
        match = _OUTDATED_SECTION_RE.match(line)
        if match:
            _flush()
            section = match.group(1)
        else:
            buffer.append(line)
    _flush()

    if not measured:
        return [], {}
    return [], {"outdated": {**counts, "total": sum(counts.values()), "checked": measured}}


# --- dedup ------------------------------------------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            # Lexicographic tie-break keeps the structure independent of insertion order, which is
            # what makes `canonical_advisory_id` deterministic across runs.
            low, high = sorted((root_a, root_b))
            self._parent[high] = low


def canonical_advisory_id(ids: Iterable[str]) -> str:
    """CVE beats GHSA beats everything else; ties broken lexicographically. Deterministic by
    construction -- no dependence on which tool reported first."""
    candidates = sorted({i for i in ids if i})
    if not candidates:
        return "unknown"
    return (
        next((i for i in candidates if _CVE_RE.match(i)), None)
        or next((i for i in candidates if _GHSA_RE.match(i)), None)
        or candidates[0]
    )


def build_alias_map(findings: Sequence[Finding]) -> dict[str, str]:
    """advisory id -> canonical id, over the union of every alias set any tool reported.

    This is the whole point of running trivy *and* osv-scanner: they agree on most advisories but
    name them differently (CVE-2024-1234 vs GHSA-xxxx-yyyy-zzzz), and only OSV publishes the alias
    list that links them.
    """
    union = _UnionFind()
    for finding in findings:
        if finding.category != "vulnerability":
            continue
        ids = [i for i in (finding.rule_id, *finding.aliases) if i]
        for other in ids[1:]:
            union.union(ids[0], other)
        for identifier in ids:
            union.find(identifier)

    groups: dict[str, set[str]] = {}
    for identifier in list(union._parent):
        groups.setdefault(union.find(identifier), set()).add(identifier)
    return {
        identifier: canonical_advisory_id(members)
        for members in groups.values()
        for identifier in members
    }


def _package_key(finding: Finding) -> str:
    pkg = finding.package or {}
    purl = pkg.get("purl")
    if purl:
        return str(purl).lower().split("?", 1)[0]
    return f"{(pkg.get('ecosystem') or '').lower()}:{(pkg.get('name') or '').lower()}"


def _dedup_key(finding: Finding, alias_map: dict[str, str]) -> tuple[str, ...]:
    if finding.category == "vulnerability":
        canonical = alias_map.get(finding.rule_id, finding.rule_id)
        return ("vulnerability", _package_key(finding), canonical)
    if finding.category == "secret":
        # Two tools flagging the same line are the same leaked credential, whatever they call the rule.
        return ("secret", finding.file, str(finding.line))
    return (finding.category, finding.file, str(finding.line), finding.rule_id or finding.cwe or "")


def _merge(group: Sequence[Finding], alias_map: dict[str, str]) -> Finding:
    primary = max(group, key=lambda f: (SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 0, len(f.message)))
    aliases = sorted({a for f in group for a in f.aliases if a})
    sources = tuple(sorted({s for f in group for s in f.sources}))

    package: dict[str, Any] | None = None
    for finding in group:
        if not finding.package:
            continue
        package = {**(package or {}), **{k: v for k, v in finding.package.items() if v}}

    canonical = alias_map.get(primary.rule_id, primary.rule_id) if primary.category == "vulnerability" else primary.rule_id
    cve = next((a for a in ([canonical] + aliases) if _CVE_RE.match(a or "")), None) or primary.cve

    return replace(
        primary,
        finding_key=(
            stable_id("vulnerability", f"{canonical}:{(package or {}).get('name', 'unknown')}", primary.file)
            if primary.category == "vulnerability"
            else primary.finding_key
        ),
        rule_id=canonical,
        severity=_worst(*(f.severity for f in group)),
        cve=cve,
        aliases=tuple(a for a in aliases if a != cve),
        package=package,
        cwe=next((f.cwe for f in group if f.cwe), None),
        # "native" from any tool beats a derived or defaulted guess from another.
        severity_source=next(
            (s for s in ("native", "derived", "defaulted") if any(f.severity_source == s for f in group)),
            "defaulted",
        ),
        sources=sources,
        occurrences=len(group),
    )


def dedupe(findings: Sequence[Finding]) -> tuple[list[Finding], int]:
    """Returns (deduped findings, number of findings collapsed away)."""
    alias_map = build_alias_map(findings)
    groups: dict[tuple[str, ...], list[Finding]] = {}
    for finding in findings:
        groups.setdefault(_dedup_key(finding, alias_map), []).append(finding)
    merged = [_merge(group, alias_map) for group in groups.values()]
    return sort_findings(merged), len(findings) - len(merged)


def sort_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Total order, so two runs over an unchanged tree serialize byte-identically."""
    return sorted(
        findings,
        key=lambda f: (
            -(SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 0),
            f.category,
            f.file,
            f.line if f.line is not None else -1,
            f.finding_key,
        ),
    )


# --- report assembly --------------------------------------------------------------------------


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def health_score(
    *,
    security_by_severity: dict[str, int],
    security_measured: bool,
    maintainability_count: int,
    metrics: dict[str, Any],
    ac_verification: dict[str, Any] | None = None,
    ac_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One explicit formula in one place: nine 0-100 subscores, each None when its input was not
    measured, combined by HEALTH_WEIGHTS with unmeasured weights redistributed proportionally.
    Returns {"score": int|None, "subscores": {...}, "weights_used": {...}} -- `weights_used` is what
    the score actually weighed and is the comparability key for the regression gate (a pre-build
    baseline and a post-build latest legitimately measure different sets).

    Deliberate shapes, documented in README.md "Health score":
      * security counts SECURITY_CATEGORIES findings only (a lizard complexity "high" is not a
        security issue) and is None -- not 100 -- when a security tool failed to run.
      * coverage/duplication are the raw measurements, not distance-to-the-gate: those thresholds
        are already hard gates elsewhere, and a leg that saturates at 100 on every passing run
        measures nothing.
      * dependency CVEs and licence findings score in `security`; the dependencies leg is
        staleness only (no double-counting).
      * three criticals zero the security leg and cap the composite at 60 -- intended.

    v1 (removed): 100 - (25*crit + 10*high + 3*med + 0.5*low over ALL findings)
                  - 2*(dup% over 3) - 3*(mean_ccn over 5).
    """
    complexity = metrics.get("complexity") or {}
    coverage = metrics.get("coverage") or {}
    lighthouse = metrics.get("lighthouse") or {}
    outdated = metrics.get("outdated") or {}

    security: float | None = None
    if security_measured:
        security = _clamp(
            100.0
            - 40 * security_by_severity.get("critical", 0)
            - 15 * security_by_severity.get("high", 0)
            - 5 * security_by_severity.get("medium", 0)
            - 1 * security_by_severity.get("low", 0)
        )

    line, branch = coverage.get("line_rate"), coverage.get("branch_rate")
    coverage_sub: float | None = None
    if isinstance(line, (int, float)):
        if isinstance(branch, (int, float)):
            coverage_sub = _clamp(0.75 * line + 0.25 * branch)
        else:
            coverage_sub = _clamp(float(line))

    dependencies: float | None = None
    if isinstance(outdated.get("total"), int):
        dependencies = _clamp(100.0 - min(100.0, 5.0 * outdated["total"]))

    ac_sub: float | None = None
    total_acs = (ac_verification or {}).get("total")
    execution = ac_execution or {}
    if (
        isinstance(total_acs, int) and total_acs > 0
        and execution.get("status") not in (None, "not_evaluated")
        and isinstance(execution.get("solidly_verified"), int)
    ):
        flaky = len(execution.get("flaky") or [])
        ac_sub = _clamp(100.0 * (execution["solidly_verified"] + 0.5 * flaky) / total_acs)

    accessibility = lighthouse.get("accessibility")
    accessibility = _clamp(float(accessibility)) if isinstance(accessibility, (int, float)) else None
    performance = lighthouse.get("performance")
    performance = _clamp(float(performance)) if isinstance(performance, (int, float)) else None

    complexity_sub: float | None = None
    mean_ccn, max_ccn = complexity.get("mean_ccn"), complexity.get("max_ccn")
    if isinstance(mean_ccn, (int, float)):
        mean_part = _clamp(100.0 - 10.0 * max(0.0, mean_ccn - 5.0))
        max_part = _clamp(100.0 - 2.0 * max(0.0, (max_ccn or 0) - 15.0))
        complexity_sub = _clamp(0.7 * mean_part + 0.3 * max_part)

    duplication_pct = (metrics.get("duplication") or {}).get("percent")
    duplication_sub = (
        _clamp(100.0 - 3.0 * duplication_pct) if isinstance(duplication_pct, (int, float)) else None
    )

    # Findings-count part is always computable; the docstring percentage blends in when present.
    maintainability_sub = _clamp(100.0 - 3.0 * maintainability_count)
    docstring_pct = (metrics.get("documentation") or {}).get("python_docstring_coverage_percent")
    if isinstance(docstring_pct, (int, float)):
        maintainability_sub = _clamp((maintainability_sub + docstring_pct) / 2.0)

    subscores: dict[str, float | None] = {
        "security": security,
        "coverage": coverage_sub,
        "dependencies": dependencies,
        "ac_verification": ac_sub,
        "accessibility": accessibility,
        "complexity": complexity_sub,
        "performance": performance,
        "duplication": duplication_sub,
        "maintainability": maintainability_sub,
    }

    present = {name: HEALTH_WEIGHTS[name] for name, sub in subscores.items() if sub is not None}
    total_weight = sum(present.values())
    if not present or total_weight <= 0:
        return {"score": None, "subscores": subscores, "weights_used": {}}
    weights_used = {name: round(w / total_weight, 4) for name, w in present.items()}
    score = round(sum(subscores[name] * weights_used[name] for name in weights_used))
    return {
        "score": max(0, min(100, score)),
        "subscores": {k: (round(v, 1) if v is not None else None) for k, v in subscores.items()},
        "weights_used": weights_used,
    }


# Paths whose findings are REPORTED but never block a merge: they are not the application. Three
# kinds, all observed gating a real run -- 53 of its 68 gating findings came from here:
#   * the pipeline's own scratch output. `agent-work/gitleaks.json` IS a secret-scanner report, so
#     scanning it finds 48 "Base64 High Entropy String" secrets -- the pipeline flagging its own
#     evidence of scanning, and by far the loudest signal in the run.
#   * build outputs and restored artifacts (bin/, obj/, .next/, dist/), which are regenerated.
#   * vendored third-party payloads -- node_modules and, memorably, a downloaded Chromium's bundled
#     accessibility scripts under .playwright-browsers/.
# Nothing here is code a human wrote or can meaningfully fix, and a gate that blocks on it teaches
# people to ignore the gate.
_NON_APPLICATION_PATH_RE = re.compile(
    r"(^|/)("
    r"agent-work|\.ai-dev-workflow|node_modules|\.playwright-browsers|"
    r"bin|obj|dist|build|out|\.next|\.nuxt|\.venv|vendor|TestResults|coverage"
    r")/",
)


def is_non_application_path(path: str | None) -> bool:
    """True for pipeline scratch, build output, and vendored payloads. Pure; self-checked below."""
    if not path:
        return False
    return bool(_NON_APPLICATION_PATH_RE.search(path if path.startswith("/") else f"/{path}"))


# Rule namespaces that describe PORTABILITY/STYLE rather than a defect. semgrep ships an i18n pack
# whose `jsx-not-internationalized` fires on every literal string in a component -- 7 gating findings
# on a counter app, for text no requirement asked to be translatable. Reported, never blocking:
# these say "you have not internationalised this", which is a product decision, not a security or
# correctness problem, and a gate that blocks on it blocks every UI app forever.
_NON_GATING_RULE_RE = re.compile(
    r"\.portability\.|i18next|jsx-not-internationalized"
    # Fires once per dependency block in package.json -- a generic "you have dependencies" lint
    # rather than a finding about this code. Six identical copies gated one run.
    r"|package-dependencies-check"
)

# Lock files list TRANSITIVE dependencies -- packages this application never chose. A licence
# obligation on one of them is a decision for a human (and often has no action available at all:
# @img/sharp-* arrives with Next.js itself and cannot be removed from it), so it is reported and
# never blocking. A licence finding on the MANIFEST, which the app author does control, still gates.
_LOCK_FILE_RE = re.compile(r"(^|/)(package-lock\.json|packages\.lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum)$")


def is_transitive_dependency_file(path: str | None) -> bool:
    """True for lock files, whose contents are resolved transitively rather than authored. Pure."""
    return bool(path) and bool(_LOCK_FILE_RE.search(path))


def is_advisory_rule(rule_id: str | None) -> bool:
    """True for rules that report a stylistic/portability preference, not a defect. Pure."""
    return bool(rule_id) and bool(_NON_GATING_RULE_RE.search(rule_id))


def is_gating(
    finding: Finding,
    *,
    severity_floor: str,
    introduced_ids: frozenset[str] | None,
    direct_dependencies: frozenset[str] | None = None,
    known_gap_ids: frozenset[str] | None = None,
) -> bool:
    """Security gates absolutely, at or above the floor -- an inherited CVE is still exploitable.
    Quality gates only on what this pipeline introduced, so a brownfield repo's pre-existing debt
    cannot deadlock its first gate. With no baseline (greenfield) `introduced_ids` is None and
    every quality finding gates, which is the same rule.

    `known_gap_ids` is the finding_keys remediation's own `known_gaps` already explained (see
    gates/remediation_gate.accounted_for) -- security or quality, same as remediation's own gate
    draws no category distinction there. `None` (the default) excludes nothing, so every existing
    caller that predates this parameter is unaffected."""
    # Checked before category: a finding outside the application never gates, however severe it
    # looks, because there is nothing in the product to fix.
    if is_non_application_path(finding.file):
        return False
    if is_advisory_rule(finding.rule_id):
        return False
    if known_gap_ids is not None and finding.finding_key in known_gap_ids:
        return False
    # A licence obligation inherited through a lock file is not actionable in this repository -- but
    # only when it really is inherited. With the lockfile's own direct-dependency set available, a
    # licence on a package THIS project chose still gates: it is a decision someone here made and
    # can unmake. Without that set (`None`), every lock-file licence stays advisory, which is the
    # older, blunter behaviour.
    if finding.category == "license" and is_transitive_dependency_file(finding.file):
        package_name = (finding.package or {}).get("name") if finding.package else None
        if direct_dependencies is None or not package_name or package_name not in direct_dependencies:
            return False
    if finding.category in SECURITY_CATEGORIES:
        return meets_or_exceeds(finding.severity, severity_floor)
    if finding.category in QUALITY_CATEGORIES:
        # The floor applies here TOO. Without it a `low` finding gates while a `low` security
        # finding does not -- observed: "Docstring coverage under threshold" (low) blocking a run
        # whose floor was `medium`, which is not a defensible reason to refuse a merge.
        if not meets_or_exceeds(finding.severity, severity_floor):
            return False
        return introduced_ids is None or finding.finding_key in introduced_ids
    return False


@dataclass(frozen=True)
class ScanReport:
    findings: tuple[Finding, ...]
    metrics: dict[str, Any]
    tools: tuple[dict[str, Any], ...]
    repo: dict[str, Any]
    deduped_count: int
    # The Eval layer (ac_eval.py), absent unless a caller asked for it. Split in two because only
    # one half can live inside content_hash: `ac_verification` is static analysis of the worktree,
    # while `ac_execution` runs the suites and is therefore legitimately non-deterministic.
    ac_verification: dict[str, Any] | None = None
    ac_execution: dict[str, Any] | None = None
    # Packages the project itself declared, read from the lockfile's root entry. Lets is_gating tell
    # "we chose this LGPL package" from "our framework did" -- see direct_dependency_names.
    direct_dependencies: frozenset[str] | None = None

    def summary(
        self,
        *,
        severity_floor: str = SECURITY_SEVERITY_FLOOR,
        introduced_ids: frozenset[str] | None = None,
        known_gap_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        by_severity = {level: 0 for level in SEVERITY_ORDER}
        by_category: dict[str, int] = {}
        # Security-only severity tally -- kept separate from `by_severity` above (which is every
        # finding, quality included) because a quality-remediation lizard "high" complexity finding
        # is not a security issue. Reusing that confusion is the live bug measures.security fixes.
        security_by_severity = {level: 0 for level in SEVERITY_ORDER}
        gating = 0
        maintainability_count = 0
        for finding in self.findings:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
            by_category[finding.category] = by_category.get(finding.category, 0) + 1
            if finding.category in SECURITY_CATEGORIES:
                security_by_severity[finding.severity] = security_by_severity.get(finding.severity, 0) + 1
            elif (
                finding.category == "maintainability"
                and finding.rule_id not in _MAINTAINABILITY_EXCLUDED_RULE_IDS
            ):
                maintainability_count += 1
            if is_gating(
                finding,
                severity_floor=severity_floor,
                introduced_ids=introduced_ids,
                direct_dependencies=self.direct_dependencies,
                known_gap_ids=known_gap_ids,
            ):
                gating += 1
        # Enum is `none|info|low|medium|high|critical` -- the full SEVERITY_ORDER vocabulary plus
        # "none" for zero open security findings. "info" is a real, reachable value (not clamped
        # to "low" or hidden as "none"): Trivy reports NONE/NEGLIGIBLE severities that
        # normalize_tier() maps to "info" on SECURITY_CATEGORIES findings (vulnerability/misconfig/
        # license), and hiding or inflating that would misreport what was actually found.
        worst_open_severity = next(
            (level for level in reversed(SEVERITY_ORDER) if security_by_severity.get(level, 0)), "none"
        )
        # A security tool that was selected but failed/missing means the security subscore is
        # unmeasured, never "clean" -- and `degraded` names every non-ok tool so consumers can see
        # which signals this summary is missing.
        security_runs = [t for t in self.tools if t.get("name") in _SECURITY_TOOL_NAMES]
        security_measured = bool(security_runs) and all(t.get("status") == "ok" for t in security_runs)
        # `outdated` excluded: it is fail-open-by-design and networked, so its failure already
        # reads as an unmeasured (null) subscore -- listing it here would park a permanent false
        # "summary is partial" flag on every airgapped deployment.
        degraded = sorted(t["name"] for t in self.tools if t.get("status") != "ok" and t.get("name") != "outdated")
        health = health_score(
            security_by_severity=security_by_severity,
            security_measured=security_measured,
            maintainability_count=maintainability_count,
            metrics=self.metrics,
            ac_verification=self.ac_verification,
            ac_execution=self.ac_execution,
        )
        return {
            "health_score": health["score"],
            "health_subscores": health["subscores"],
            "health_weights_used": health["weights_used"],
            "health_score_version": HEALTH_SCORE_VERSION,
            "degraded": degraded,
            "by_severity": by_severity,
            "by_category": dict(sorted(by_category.items())),
            "deduped_count": self.deduped_count,
            "gating_count": gating,
            "severity_floor": severity_floor,
            "measures": {
                "security": {"worst_open_severity": worst_open_severity, "by_severity": security_by_severity},
                "duplication_percent": (self.metrics.get("duplication") or {}).get("percent"),
                "mean_ccn": (self.metrics.get("complexity") or {}).get("mean_ccn"),
                "coverage_line_rate": (self.metrics.get("coverage") or {}).get("line_rate"),
                # Measured by e2e_nodes against the LIVE app and merged into metrics by
                # metrics_compute_node -- never by a scan tool here (this module's contract is
                # offline, no running app). None/absent on non-UI repos and runs whose e2e never
                # produced a score; the frontend hides the chips on absence.
                "lighthouse_performance": (self.metrics.get("lighthouse") or {}).get("performance"),
                "accessibility_score": (self.metrics.get("lighthouse") or {}).get("accessibility"),
            },
        }

    def to_dashboard_dict(self, *, severity_floor: str = SECURITY_SEVERITY_FLOOR, introduced_ids: frozenset[str] | None = None) -> dict[str, Any]:
        findings = [
            _dashboard_finding(
                f,
                is_gating(
                    f,
                    severity_floor=severity_floor,
                    introduced_ids=introduced_ids,
                    direct_dependencies=self.direct_dependencies,
                ),
            )
            for f in self.findings
        ]
        metrics = _public_metrics(self.metrics)
        body: dict[str, Any] = {"findings": findings, "metrics": metrics}
        # INSIDE the hash: static AC verification is pure worktree analysis, so an unchanged repo
        # must hash identically with it present.
        if self.ac_verification is not None:
            body["ac_verification"] = self.ac_verification
        # OUTSIDE the hash: lighthouse scores are timing-noisy live-app measurements merged in by
        # metrics_compute_node, not worktree analysis -- hashing them would break the "unchanged
        # repo hashes identically" contract the module docstring promises (same reasoning as
        # ac_execution below, except lighthouse must stay inside `metrics` because the delta
        # engine digs metrics.lighthouse.* -- so the HASH excludes it rather than the report).
        # `outdated` is excluded for the same reason from the other direction: an upstream registry
        # publishing a release changes it with zero repo change.
        hash_body = {**body, "metrics": {k: v for k, v in metrics.items() if k not in ("lighthouse", "outdated")}}
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "content_hash": content_hash(hash_body),
            "repo": self.repo,
            "summary": self.summary(severity_floor=severity_floor, introduced_ids=introduced_ids),
            **body,
            "tools": list(self.tools),
        }
        # OUTSIDE the hash, and added after it is computed: running a suite twice can legitimately
        # give different answers (that is what the flake metric measures), so including execution
        # would break the module's documented determinism contract on the first flaky test.
        if self.ac_execution is not None:
            report["ac_execution"] = self.ac_execution
        return report


# Which `summary()["measures"]` keys a given scan profile's own tools actually measure. A
# partial-profile scan (quality-remediation's own scan runs only scc/lizard/jscpd;
# security-remediation's runs only semgrep/trivy/gitleaks/osv-scanner) must not blank the OTHER
# loop's measures just because this scan's tool set doesn't touch them -- merge_measures keeps the
# prior value for any key not in this set. A profile absent from this mapping (namely "full", the
# baseline/metrics-report scan) measures everything a SCANNER can report, so merge_measures is a
# scanner-key no-op for it -- but NOT a full no-op: the _E2E_SOURCED_MEASURES below come from
# e2e's live-app run rather than any scan tool, are deliberately NOT listed in any profile here
# (listing them would blank them with fresh Nones), and are prior-preserved on every profile.
PROFILE_MEASURES: dict[str, frozenset[str]] = {
    "quality": frozenset({"duplication_percent", "mean_ccn"}),
    "security": frozenset({"security"}),
}


# Measures NO scan profile computes: lighthouse comes from e2e_nodes' live-app run, merged into
# metrics only by metrics_compute_node. Deliberately absent from every PROFILE_MEASURES set AND
# preserved from the prior summary on every merge (including "full"): a "full" scan measures
# everything a SCANNER can report, but these two come from outside the scanner set entirely, so
# any post-metrics summary writer (the per-commit background refresh, a re-entrant remediation
# re-scan) would otherwise overwrite real scores with None and hide the chips it just showed
# (2026-08-24 audit).
_E2E_SOURCED_MEASURES = frozenset({"lighthouse_performance", "accessibility_score"})


def merge_measures(prior_summary: dict[str, Any] | None, new_summary: dict[str, Any], profile: str) -> dict[str, Any]:
    """Merges a partial-profile scan's `measures` onto the prior summary's (the previous latest,
    or the baseline) -- see PROFILE_MEASURES. Every other field of `new_summary` (health_score,
    gating_count, by_severity, ...) is this scan's own and is returned untouched; only `measures`
    keys the profile's own tools didn't compute fall back to the prior value, so quality-remediation's
    scan can't zero out security's chip mid-run (or security-remediation's blank quality's).

    _E2E_SOURCED_MEASURES fall back to the prior value whenever this scan didn't carry them --
    on EVERY profile, "full" included, since no scanner ever measures them (see the constant's
    own comment above).
    """
    if prior_summary is None:
        return new_summary
    measured = PROFILE_MEASURES.get(profile)
    prior_measures = prior_summary.get("measures") or {}
    new_measures = dict(new_summary.get("measures") or {})
    for key, prior_value in prior_measures.items():
        scanner_gap = measured is not None and key not in measured and key in new_measures
        e2e_gap = key in _E2E_SOURCED_MEASURES and new_measures.get(key) is None
        if scanner_gap or e2e_gap:
            new_measures[key] = prior_value
    if new_measures == (new_summary.get("measures") or {}):
        return new_summary
    return {**new_summary, "measures": new_measures}


def _dashboard_finding(finding: Finding, gating: bool) -> dict[str, Any]:
    """No `tool`, no `sources`: the dashboard shows the issue, not who found it."""
    return {
        "id": finding.finding_key,
        "category": finding.category,
        "severity": finding.severity,
        "severity_source": finding.severity_source,
        "gating": gating,
        "title": finding.title or finding.rule_id,
        "description": finding.message,
        "location": {"path": finding.file, "start_line": finding.line, "end_line": finding.end_line},
        "rule_id": finding.rule_id or None,
        "cve": finding.cve,
        "aliases": list(finding.aliases),
        "cwe": finding.cwe,
        "package": finding.package,
        "confidence": "corroborated" if len(finding.sources) > 1 else "single",
        "occurrences": finding.occurrences,
    }


def _public_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Strips the `_`-prefixed join scratch (`_by_path`, `_per_file`) that the parsers hand to
    `_assemble_metrics` but that has no business in a committed artifact."""
    return {
        section: {k: v for k, v in values.items() if not k.startswith("_")}
        for section, values in sorted(metrics.items())
        if isinstance(values, dict)
    }


def content_hash(body: dict[str, Any]) -> str:
    """Over findings + metrics only. `generated_at` and per-tool durations are excluded on purpose,
    so an unchanged repo hashes identically on a re-run and the dashboard can say "nothing moved"."""
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assemble_metrics(fragments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merges parser fragments and computes the one cross-tool metric: hotspots, which need churn
    (git) joined against complexity (lizard). Neither tool can produce it alone."""
    metrics: dict[str, Any] = {}
    for fragment in fragments:
        for section, values in fragment.items():
            metrics.setdefault(section, {}).update(values)

    churn = metrics.get("churn") or {}
    per_file = churn.pop("_per_file", None)
    ccn_by_path = (metrics.get("complexity") or {}).pop("_by_path", {})
    if per_file:
        hotspots = sorted(
            (
                {
                    "path": path,
                    "commits": entry["commits"],
                    "lines_changed": entry["lines_changed"],
                    "ccn": ccn_by_path.get(path, 0),
                    # Churn x complexity: the file everyone keeps editing *and* nobody can read.
                    "hotspot_score": round(entry["lines_changed"] * max(ccn_by_path.get(path, 0), 1) / 100.0, 2),
                }
                for path, entry in per_file.items()
            ),
            key=lambda h: (-h["hotspot_score"], h["path"]),
        )
        churn["hotspots"] = hotspots[:20]
    return metrics


# --- delta ------------------------------------------------------------------------------------


# Declared, never inferred: more lines of code is not a regression, more duplication is.
_METRIC_DIRECTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("health_score", ("summary", "health_score"), "higher_is_better"),
    ("duplication_percent", ("metrics", "duplication", "percent"), "lower_is_better"),
    ("mean_ccn", ("metrics", "complexity", "mean_ccn"), "lower_is_better"),
    ("functions_over_threshold", ("metrics", "complexity", "functions_over_threshold"), "lower_is_better"),
    ("total_loc", ("metrics", "size", "total_loc"), "neutral"),
    ("coverage_line_rate", ("metrics", "coverage", "line_rate"), "higher_is_better"),
    ("coverage_branch_rate", ("metrics", "coverage", "branch_rate"), "higher_is_better"),
    # Dependency bloat. "neutral" on purpose: adding packages is how features get built, so a
    # framework install is not a regression -- but "this run added 47 components" is exactly the
    # kind of change that should be visible in a review rather than discovered later.
    ("sbom_component_count", ("metrics", "sbom", "component_count"), "neutral"),
    # Lighthouse, measured live by e2e_nodes and merged in by metrics_compute_node (see
    # summary()'s measures comment). _dig returns None when absent, and the delta engine already
    # skips None-on-either-side metrics, so non-UI repos never report a lighthouse regression.
    ("lighthouse_performance", ("metrics", "lighthouse", "performance"), "higher_is_better"),
    ("accessibility_score", ("metrics", "lighthouse", "accessibility"), "higher_is_better"),
)


def _dig(doc: dict[str, Any], path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def diff_scans(baseline: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any] | None:
    """Set arithmetic over stable, line-independent finding ids.

    Returns None when there is no baseline -- a fabricated zero-delta would read as "we changed
    nothing" when the truth is "we never measured".
    """
    if not baseline or baseline.get("baseline") is None and "findings" not in baseline:
        return None

    baseline_findings = {f["id"]: f for f in baseline.get("findings") or []}
    current_findings = {f["id"]: f for f in current.get("findings") or []}

    fixed = [_delta_entry(baseline_findings[i]) for i in sorted(baseline_findings.keys() - current_findings.keys())]
    introduced = [_delta_entry(current_findings[i]) for i in sorted(current_findings.keys() - baseline_findings.keys())]
    persisted = sorted(baseline_findings.keys() & current_findings.keys())
    severity_changed = [
        {"id": i, "from": baseline_findings[i]["severity"], "to": current_findings[i]["severity"]}
        for i in persisted
        if baseline_findings[i]["severity"] != current_findings[i]["severity"]
    ]

    net_change = {level: 0 for level in SEVERITY_ORDER}
    for finding in current_findings.values():
        net_change[finding["severity"]] = net_change.get(finding["severity"], 0) + 1
    for finding in baseline_findings.values():
        net_change[finding["severity"]] = net_change.get(finding["severity"], 0) - 1

    return {
        "baseline_commit": (baseline.get("repo") or {}).get("commit"),
        "current_commit": (current.get("repo") or {}).get("commit"),
        "findings": {
            "fixed": fixed,
            "introduced": introduced,
            "persisted": persisted,
            "severity_changed": severity_changed,
            "net_change": net_change,
        },
        "metrics": _metric_deltas(baseline, current),
        "caveats": {
            "db_drift": _db_versions(baseline) != _db_versions(current),
            "baseline_schema_version": baseline.get("schema_version"),
            # A renamed file reads as fixed + introduced: the stable id includes the path.
            # ponytail: accepted; upgrade path is `git log --follow` rename detection if it proves noisy.
            "renames_not_tracked": True,
        },
    }


def delta_summary(delta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Small, frontend-ready rollup of `diff_scans`' output. metrics_nodes.py used to read a
    `summary` key diff_scans has never produced -- dead since day one, so the UI never got a
    delta at all. None in, None out: no baseline still means no delta, never a fabricated one."""
    if delta is None:
        return None
    findings = delta.get("findings") or {}
    return {
        "fixed_count": len(findings.get("fixed") or []),
        "introduced_count": len(findings.get("introduced") or []),
        "severity_changed": len(findings.get("severity_changed") or []),
        "net_change": findings.get("net_change"),
        "metrics": delta.get("metrics"),
        "baseline_commit": delta.get("baseline_commit"),
    }


def _delta_entry(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": finding["id"],
        "severity": finding["severity"],
        "category": finding["category"],
        "title": finding.get("title", ""),
    }


def _metric_deltas(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    deltas: dict[str, Any] = {}
    for name, path, polarity in _METRIC_DIRECTIONS:
        before, after = _dig(baseline, path), _dig(current, path)
        if before is None or after is None:
            continue
        change = round(after - before, 2)
        if polarity == "neutral" or change == 0:
            direction = "neutral" if change == 0 or polarity == "neutral" else "improved"
        elif polarity == "higher_is_better":
            direction = "improved" if change > 0 else "regressed"
        else:
            direction = "improved" if change < 0 else "regressed"
        deltas[name] = {"from": before, "to": after, "delta": change, "direction": direction}
    return deltas


def _db_versions(report: dict[str, Any]) -> dict[str, Any]:
    return {t.get("name"): t.get("db_version") for t in report.get("tools") or [] if t.get("db_version")}


# ------------------------------------------------------------------------------------------------
# Tool table
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    license: str
    permissive: bool
    command: str
    output_path: str
    parse: Callable[[str], ParseResult]
    version_command: str


# Every command is offline by construction: no `--config auto`, no DB update, no registry fetch.
# The databases are baked into the sandbox image at build time -- see agent/sandbox-image/Dockerfile.
# ONE deliberate exception: `outdated` (bottom of the tuple) asks the live registries which
# packages have newer releases -- staleness cannot be measured offline. It is strictly fail-open,
# excluded from content_hash, and NOT in any profile: only metrics_compute_node opts into it.
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "scc", "MIT", True,
        "scc --format json . > agent-work/scc.json",
        "agent-work/scc.json", parse_scc, "scc --version",
    ),
    ToolSpec(
        "lizard", "MIT", True,
        "lizard --csv . > agent-work/lizard.csv",
        "agent-work/lizard.csv", parse_lizard, "lizard --version",
    ),
    ToolSpec(
        "jscpd", "MIT", True,
        # The ignore list is load-bearing: without it jscpd counts generated/lock/artifact files
        # as clones (observed live: 37.6% "duplication" made of wrangler d.ts, pnpm-lock.yaml,
        # drizzle migration snapshots, and the pipeline's OWN repo-scan-baseline.json) -- noise
        # that would deadlock the 3% audit-exit gate on every run while measuring zero authored
        # code. Duplication is a signal about code humans/models WROTE.
        # Tests are excluded too: repeated render/assert scaffolding is idiomatic there, and a
        # tiny greenfield app whose tree is mostly tests deadlocked the 3% quality gate on test
        # boilerplate alone (observed live, headless sc1: every clone pair was a *.test.tsx).
        f"jscpd . --threshold {MAX_DUPLICATION_PERCENT} --reporters json --output agent-work/jscpd --silent "
        "--format 'typescript,tsx,javascript,jsx,c-sharp,python' "
        '--ignore "**/node_modules/**,**/.git/**,**/dist/**,**/build/**,**/out/**,**/.next/**,'
        '**/coverage/**,**/*.min.js,**/*.d.ts,**/migrations/**,'
        '**/*.test.*,**/*.spec.*,**/tests/**,**/__tests__/**,**/*Tests.cs,**/*.Tests/**,'
        '**/.ai-dev-workflow/**,**/agent-work/**,**/drizzle/meta/**,**/.wrangler/**,**/*.snap"',
        "agent-work/jscpd/jscpd-report.json", parse_jscpd, "jscpd --version",
    ),
    ToolSpec(
        "gitleaks", "MIT", True,
        # --no-git: working-tree only. A full-history sweep is a separate, slower concern and is
        # flagged rather than silently folded in, matching security_nodes.py's existing decision.
        "gitleaks detect --report-format json --report-path agent-work/gitleaks.json --no-git --exit-code 0",
        "agent-work/gitleaks.json", parse_gitleaks, "gitleaks version",
    ),
    ToolSpec(
        "trivy", "Apache-2.0", True,
        "trivy fs --offline-scan --skip-db-update --skip-java-db-update "
        "--scanners vuln,misconfig,license,secret --format json --output agent-work/trivy.json .",
        "agent-work/trivy.json", parse_trivy, "trivy --version",
    ),
    ToolSpec(
        "osv-scanner", "Apache-2.0", True,
        f"osv-scanner scan source --recursive --offline-vulnerabilities --local-db-path {OSV_DB_DIR} "
        "--format json --output agent-work/osv.json .",
        "agent-work/osv.json", parse_osv, "osv-scanner --version",
    ),
    ToolSpec(
        # The one non-permissive dependency, kept deliberately and recorded as such in `tools[]`.
        "semgrep", "LGPL-2.1", False,
        f"semgrep scan --config {SEMGREP_RULES_DIR} --metrics=off --sarif --output agent-work/semgrep.sarif .",
        "agent-work/semgrep.sarif", parse_semgrep, "semgrep --version",
    ),
    ToolSpec(
        "git-churn", "n/a", True,
        f"git log --no-merges --numstat --format=C%x7C%H%x7C%an%x7C%aI "
        f"--since={CHURN_WINDOW_DAYS}.days.ago > agent-work/git-churn.txt",
        "agent-work/git-churn.txt", parse_git_churn, "git --version",
    ),
    ToolSpec(
        "checkov", "Apache-2.0", True,
        "checkov --directory . --compact --quiet --output json > agent-work/checkov.json",
        "agent-work/checkov.json", parse_checkov, "checkov --version",
    ),
    ToolSpec(
        "interrogate", "MIT", True,
        # --fail-under 0: this pipeline owns the gating decision (DOC_COVERAGE_MIN_PERCENT above),
        # never interrogate's own exit code.
        "interrogate --fail-under 0 -v . > agent-work/interrogate.txt",
        "agent-work/interrogate.txt", parse_interrogate, "interrogate --version",
    ),
    ToolSpec(
        # "n/a" license, like git-churn above: this invokes the .NET SDK's own Roslyn compiler, not
        # a separate vetted dependency.
        "dotnet-docs", "n/a", True,
        "mkdir -p agent-work && dotnet build --no-incremental "
        "/p:GenerateDocumentationFile=true /p:TreatWarningsAsErrors=false /p:WarningLevel=9999 "
        "> agent-work/dotnet-docs.txt",
        "agent-work/dotnet-docs.txt", parse_dotnet_docs, "dotnet --version",
    ),
    ToolSpec(
        "syft", "Apache-2.0", True,
        "syft dir:. -o cyclonedx-json=agent-work/sbom.json",
        "agent-work/sbom.json", parse_syft, "syft version",
    ),
    ToolSpec(
        # The one networked tool -- see the exception note above the tuple. Every probe is
        # `|| true`-guarded and appends into one marker-sectioned file; _run_one judges success on
        # that file, not on exit codes (`npm outdated` exits 1 whenever anything IS outdated).
        # dotnet needs a restore first (`project.assets.json`) -- doing it here rather than relying
        # on the concurrently-running dotnet-docs build, whose ordering under the semaphore is
        # undefined.
        # `find | grep -q` for the dotnet guard, NOT `ls glob glob`: under sh an unmatched glob
        # stays literal and `ls` exits 2 even when the OTHER operands exist, so an `ls *.sln
        # */*.csproj` guard required every shape to match at once -- no generated monorepo does,
        # and 6 of 8 stacks silently got zero NuGet staleness. The `### marker` header is echoed
        # unconditionally per attempted ecosystem; parse_outdated only counts a section as
        # MEASURED when the probe's own output parses (a bare marker = the probe failed = null).
        "outdated", "n/a", True,
        # The leading comment line keeps the file non-empty on a repo with NO ecosystems at all:
        # _run_one reads an empty output file as status=failed, which would park `outdated` in
        # summary.degraded forever on such repos (fail-open means null subscore, not a red flag).
        "mkdir -p agent-work && echo '# aidw outdated probe' > agent-work/outdated.txt && "
        "{ for d in . apps/*; do [ -f \"$d/package.json\" ] && { echo \"### npm $d\"; (cd \"$d\" && npm outdated --json 2>/dev/null); echo; } >> agent-work/outdated.txt || true; done; } ; "
        "{ find . -maxdepth 3 \\( -name '*.sln' -o -name '*.csproj' \\) -not -path '*/obj/*' -not -path '*/node_modules/*' 2>/dev/null | grep -q . && { dotnet restore >/dev/null 2>&1 || true; echo '### dotnet' >> agent-work/outdated.txt; dotnet list package --outdated >> agent-work/outdated.txt 2>/dev/null || true; } || true; } ; "
        "{ for v in .venv apps/*/.venv; do [ -x \"$v/bin/pip\" ] && { echo \"### pypi $v\"; \"$v/bin/pip\" list --outdated --format=json 2>/dev/null; echo; } >> agent-work/outdated.txt || true; done; } ; true",
        "agent-work/outdated.txt", parse_outdated, "sh -c 'echo probe-ok'",
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}

PROFILES: dict[str, tuple[str, ...]] = {
    "quality": ("scc", "lizard", "jscpd", "interrogate", "dotnet-docs"),
    "security": ("semgrep", "trivy", "gitleaks", "osv-scanner", "checkov"),
    # `outdated` is deliberately NOT in "full": start_background_refresh runs "full" after every
    # commit_all, and a networked registry probe per code commit would both hammer the registry
    # and make the streamed health score flicker as the dependencies subscore blinks in and out.
    "full": tuple(tool.name for tool in TOOLS if tool.name != "outdated"),
    # The Eval layer runs no scanner tools at all -- it reads the ledger, the test files and the
    # suites' own output. Deliberately its own profile and NOT part of "full": see run_repo_scan's
    # `include_eval` docstring for why adding it to an existing profile would run the test suite on
    # every commit.
    "eval": (),
}

# Tools whose only output is measurement, skipped entirely when a gate caller passes
# include_metrics=False.
METRIC_ONLY_TOOLS = frozenset({"scc", "git-churn", "dotnet-docs", "syft", "outdated"})


def select_tools(profile: str, tools: Sequence[str] | None, include_metrics: bool) -> list[ToolSpec]:
    names = list(tools) if tools else list(PROFILES.get(profile, PROFILES["full"]))
    if not include_metrics:
        names = [n for n in names if n not in METRIC_ONLY_TOOLS]
    unknown = [n for n in names if n not in TOOLS_BY_NAME]
    if unknown:
        raise ValueError(f"unknown tool(s): {unknown}")
    return [TOOLS_BY_NAME[n] for n in names]


# ------------------------------------------------------------------------------------------------
# Sandbox-I/O half
# ------------------------------------------------------------------------------------------------


async def _drop_uninstallable_licences(
    provider: Any, thread_id: str, findings: list[Finding]
) -> tuple[list[Finding] | None, frozenset[str]]:
    """Remove licence findings for packages the lockfile marks unusable on this platform.

    Returns None when nothing changed (no licence findings, no lockfile, nothing excluded), so the
    caller can leave the list untouched. Reads the target platform from the sandbox rather than
    assuming linux/x64 -- the scan runs wherever the container runs.
    """
    direct: set[str] = set()
    licence_pkgs = {
        (f.package or {}).get("name")
        for f in findings
        if f.category == "license" and (f.package or {}).get("name")
    }
    if not licence_pkgs:
        return None, frozenset()

    from . import repo_files

    probe = await provider.exec_in_sandbox(thread_id, "uname -s; uname -m")
    lines = [line.strip().lower() for line in (probe.stdout or "").splitlines() if line.strip()]
    target_os = {"linux": "linux", "darwin": "darwin"}.get(lines[0] if lines else "", "linux")
    target_arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        lines[1] if len(lines) > 1 else "", "x64"
    )

    excluded: set[str] = set()
    listing = await provider.exec_in_sandbox(
        thread_id,
        "git ls-files '*package-lock.json' && git ls-files --others --exclude-standard '*package-lock.json'",
    )
    for path in {line.strip() for line in (listing.stdout or "").splitlines() if line.strip()}:
        lock_text = await repo_files.read_repo_file(provider, thread_id, path)
        if lock_text:
            excluded |= uninstallable_lock_packages(lock_text, target_os, target_arch)
            direct.update(direct_dependency_names(lock_text))

    if not (excluded & licence_pkgs):
        return None, frozenset(direct)
    kept = [
        f for f in findings
        if not (f.category == "license" and (f.package or {}).get("name") in excluded)
    ]
    logger.info(
        "repo_scan: dropped %d licence finding(s) for packages not installable on %s/%s",
        len(findings) - len(kept), target_os, target_arch,
    )
    return kept, frozenset(direct)


async def run_repo_scan(
    provider: Any,
    thread_id: str,
    *,
    profile: str = "full",
    tools: Sequence[str] | None = None,
    include_metrics: bool = True,
    report_path: str | None = None,
    include_eval: bool = False,
) -> ScanReport:
    """Runs the selected tools in the sandbox and returns one deduplicated report.

    A tool that is missing, crashes, or emits unparseable output degrades the report -- it never
    raises. The dashboard needs to be able to say "this signal was unavailable" rather than
    silently showing zero findings, which reads identically to a clean repo.

    `include_eval` defaults to **False**, and that default is load-bearing rather than cautious:
    `start_background_refresh` runs `profile="full"` after every `commit_all`, and its contract says
    it "never runs the test suite". Making eval part of any existing profile would fire N suite runs
    per commit, in the background, concurrently with the pipeline's own test runs and app boots.
    Only a caller that explicitly wants the Eval layer opts in.
    """
    selected = select_tools(profile, tools, include_metrics)
    await provider.exec_in_sandbox(thread_id, "mkdir -p agent-work agent-work/jscpd")

    findings: list[Finding] = []
    fragments: list[dict[str, Any]] = []
    tool_runs: list[dict[str, Any]] = []

    # Each tool is an independent sandbox exec writing its own agent-work/ output file, so they
    # run concurrently -- wall clock drops from sum-of-tools to roughly the longest tool, which is
    # what makes per-stage background refresh scans affordable. gather() preserves call order, so
    # findings accumulate in `selected` order exactly as the sequential loop did.
    # ponytail: fixed concurrency of 3 (semgrep/trivy/osv are CPU+memory heavy in one container);
    # make it an env knob if a bigger sandbox shows headroom.
    semaphore = asyncio.Semaphore(3)

    async def _bounded(spec: ToolSpec) -> tuple[dict[str, Any], list[Finding], dict[str, Any]]:
        async with semaphore:
            return await _run_one(provider, thread_id, spec)

    for run, tool_findings, fragment in await asyncio.gather(*(_bounded(spec) for spec in selected)):
        tool_runs.append(run)
        findings.extend(tool_findings)
        if fragment:
            fragments.append(fragment)

    # Drop licence findings for optional native packages the lockfile itself says cannot install on
    # this target. See uninstallable_lock_packages: without this, a counter app with no image code
    # reported LGPL obligations from `@img/sharp-win32-arm64` on a linux/x64 container.
    dropped_platform_licences, direct_dependencies = await _drop_uninstallable_licences(
        provider, thread_id, findings
    )
    if dropped_platform_licences:
        findings = dropped_platform_licences

    eval_result: dict[str, Any] = {}
    if include_eval:
        from . import ac_eval

        try:
            eval_result = await ac_eval.evaluate(provider, thread_id)
        except Exception:  # noqa: BLE001 -- same rule as the tools: degrade, never raise
            logger.exception("repo_scan: eval layer failed for thread %s", thread_id)
            eval_result = {
                "ac_verification": ac_eval.not_evaluated("eval_raised"),
                "ac_execution": ac_eval.not_evaluated("eval_raised"),
            }

    deduped, collapsed = dedupe(findings)
    report = ScanReport(
        direct_dependencies=direct_dependencies or None,
        ac_verification=eval_result.get("ac_verification"),
        ac_execution=eval_result.get("ac_execution"),
        findings=tuple(deduped),
        metrics=_assemble_metrics(fragments),
        tools=tuple(sorted(tool_runs, key=lambda t: t["name"])),
        repo=await _repo_facts(provider, thread_id),
        deduped_count=collapsed,
    )

    if any(run["name"] == "syft" and run["status"] == "ok" for run in tool_runs):
        # Full SBOM, not the small summary parse_syft hands to `metrics` -- persisted as its own
        # artifact for the same reason the dashboard report is: too large to round-trip through the
        # sandbox's ephemeral agent-work/ on every caller that wants it.
        from . import repo_files

        sbom_raw = await repo_files.read_repo_file(provider, thread_id, "agent-work/sbom.json")
        if sbom_raw:
            await repo_files.write_repo_file(provider, thread_id, SBOM_PATH, sbom_raw)

    if report_path is not None:
        from . import repo_files  # local import: keeps the pure half importable without langchain

        await repo_files.write_repo_file(
            provider, thread_id, report_path,
            json.dumps(report.to_dashboard_dict(), indent=2, default=str) + "\n",
        )
    return report


async def _run_one(provider: Any, thread_id: str, spec: ToolSpec) -> tuple[dict[str, Any], list[Finding], dict[str, Any]]:
    started = time.monotonic()
    version_result = await provider.exec_in_sandbox(thread_id, f"LC_ALL=C {spec.version_command} 2>&1")
    version_output = (version_result.stdout or "").strip()

    run: dict[str, Any] = {
        "name": spec.name,
        "license": spec.license,
        "permissive": spec.permissive,
        "version": version_output.splitlines()[0].strip() if version_output else None,
        "db_version": _extract_db_version(version_output),
        "status": "ok",
        "exit_code": None,
        "duration_ms": 0,
    }

    if not version_result.ok:
        # The binary is not on PATH -- a sandbox image problem, not a repo problem. Say so.
        run.update(status="missing", version=None, duration_ms=_elapsed_ms(started))
        logger.warning("repo_scan: tool %s is not available in the sandbox", spec.name)
        return run, [], {}

    result = await provider.exec_in_sandbox(thread_id, f"LC_ALL=C {spec.command} 2>&1")
    run["exit_code"] = result.returncode

    from . import repo_files

    raw = await repo_files.read_repo_file(provider, thread_id, spec.output_path)
    if raw is None or not raw.strip():
        # Non-zero exit is normal for most of these tools (findings present), so the report file --
        # not the exit code -- is what decides success.
        run.update(status="failed", duration_ms=_elapsed_ms(started))
        logger.warning("repo_scan: tool %s produced no readable output at %s", spec.name, spec.output_path)
        return run, [], {}

    try:
        tool_findings, fragment = spec.parse(raw)
    except Exception:  # noqa: BLE001 -- one malformed report must not lose the other seven tools
        logger.warning("repo_scan: parser for %s failed", spec.name, exc_info=True)
        run.update(status="failed", duration_ms=_elapsed_ms(started))
        return run, [], {}

    run["duration_ms"] = _elapsed_ms(started)
    return run, tool_findings, fragment


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _extract_db_version(version_output: str) -> str | None:
    """trivy prints its baked vulnerability DB's UpdatedAt alongside its own version. Recording it
    is what lets a delta distinguish "the code changed" from "the database changed"."""
    match = _TRIVY_DB_RE.search(version_output)
    return match.group(1).strip() if match else None


# Background baseline-scan tasks keyed by thread_id (same in-process pattern as
# git_ops._PUSH_TOKENS). scaffold_finalize kicks the scan so it overlaps the tech-stack ->
# brownfield LLM chain instead of serializing ~100s behind it; repo_scan_baseline_node awaits it.
_BACKGROUND_SCANS: dict[str, "asyncio.Task[Any]"] = {}


def start_background_scan(thread_id: str, provider: Any, *, chat_provider: str, run_id: str = "unknown") -> None:
    """`chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is required, no
    default -- threaded straight through to the background task's own _scan_with_coverage call;
    not resolved in here. `run_id` (Phase E known-bugs fix) is threaded the same way, defaulting
    to "unknown"; its one caller (scaffold_finalize_node) has a real one in scope."""
    if thread_id in _BACKGROUND_SCANS:
        return
    _BACKGROUND_SCANS[thread_id] = asyncio.create_task(
        _scan_with_coverage(
            provider, thread_id, chat_provider=chat_provider,
            timeout_seconds=REPO_SCAN_COVERAGE_TIMEOUT_SECONDS, run_id=run_id,
        )
    )


# Display-only refresh scans keyed by thread_id: kicked after every code-writing commit
# (git_ops.commit_all), collected non-blocking at the next node boundary so the metrics bar
# tracks the code as it churns instead of going stale between the four gate scan points.
_BACKGROUND_REFRESH: dict[str, "asyncio.Task[Any]"] = {}


def start_background_refresh(thread_id: str, provider: Any) -> None:
    task = _BACKGROUND_REFRESH.get(thread_id)
    if task is not None and not task.done():
        return  # one in flight is enough -- the next code commit re-kicks
    # No report_path and no coverage run: this never touches committed artifacts and never runs
    # the test suite -- it exists purely to stream a fresher summary to the metrics bar.
    _BACKGROUND_REFRESH[thread_id] = asyncio.create_task(run_repo_scan(provider, thread_id, profile="full"))


def pop_finished_refresh(thread_id: str) -> "ScanReport | None":
    """The finished refresh scan for this thread, or None if none is pending/done. Consumes the
    task; a crashed scan logs and returns None (display-only, never worth failing a node over)."""
    task = _BACKGROUND_REFRESH.get(thread_id)
    if task is None or not task.done():
        return None
    del _BACKGROUND_REFRESH[thread_id]
    if task.cancelled():
        return None
    if task.exception() is not None:
        logger.warning("repo_scan: background refresh scan failed for thread_id=%s", thread_id, exc_info=task.exception())
        return None
    return task.result()


def pop_background_scan(thread_id: str) -> "asyncio.Task[Any] | None":
    return _BACKGROUND_SCANS.pop(thread_id, None)


async def _scan_with_coverage(
    provider: Any, thread_id: str, *, chat_provider: str, timeout_seconds: int | None = None,
    run_id: str = "unknown",
) -> tuple["ScanReport", dict[str, Any]]:
    """The baseline scan and coverage measurement, run concurrently within a single task so
    both finish before `repo_scan_baseline_node` awaits it -- the report is NOT written to
    BASELINE_PATH here, deliberately: the node writes it once, after merging coverage in, so the
    committed file and the streamed summary never disagree about whether coverage is present.

    `chat_provider` (this run's own pinned `state["provider"]`, Ruling 4) is required,
    keyword-only, no default -- threaded straight through to measure_coverage; not resolved in
    here. Callers include a fire-and-forget background task (start_background_scan) started from
    scaffold_finalize_node, so this value is captured at task-creation time, same as every other
    argument a background task closes over. `run_id` (Phase E known-bugs fix) is threaded the same
    way and captured the same way, defaulting to "unknown" -- both real roots
    (scaffold_finalize_node via start_background_scan, and repo_scan_baseline_node's own direct
    fallback calls below) have a real `state.get("run_id", "unknown")` in scope and now pass it.

    The coverage half is independently guarded: it runs during the tech-stack/brownfield LLM-overlap
    window, where a sandbox hiccup (e.g. exec_in_sandbox raising) is more likely than usual. Losing
    the ALREADY-COMPLETED scan over a coverage crash would force repo_scan_baseline_node's fallback
    to redo scan+coverage inline -- exactly the critical-path cost this task overlaps away.
    """
    from .gates.test_coverage_gate import measure_coverage  # local: keeps the pure half import-light

    # Scanners and the coverage test run touch disjoint outputs -- run them concurrently.
    async def _guarded_coverage() -> tuple[Any, Any, Any, str, Any]:
        try:
            return await measure_coverage(provider, thread_id, chat_provider=chat_provider, timeout_seconds=timeout_seconds, run_id=run_id)
        except Exception:  # noqa: BLE001 -- the scan must not be lost over a coverage crash
            logger.warning("repo_scan: coverage measurement crashed; keeping the completed scan", exc_info=True)
            return None, None, [], "runner_error", []

    report, (line_rate, branch_rate, _gaps, reason, _entry_reports) = await asyncio.gather(
        run_repo_scan(provider, thread_id, profile="full"), _guarded_coverage()
    )

    coverage: dict[str, Any] = {"line_rate": line_rate, "branch_rate": branch_rate}
    if line_rate is None:
        coverage["reason"] = reason
    return report, coverage


def _summary_from_stored(stored: dict[str, Any]) -> dict[str, Any]:
    """Old baseline files predate the `measures` block and only carry whatever summary shape was
    current when they were written. Reconstructs enough of a ScanReport from the stored dashboard
    dict's own findings/metrics to rerun the CURRENT `summary()` code, so a pre-change baseline
    feeds the new UI without a re-scan. `tool`/`raw_severity`/`message` are unused by summary()
    and is_gating() and are fabricated as empty -- the dashboard dict never carried them anyway
    (see `_dashboard_finding`)."""
    findings = tuple(
        Finding(
            finding_key=f.get("id", ""), tool="", rule_id=f.get("rule_id") or "",
            severity=f.get("severity", "info"), raw_severity="",
            file=(f.get("location") or {}).get("path", "unknown"), line=None, message="",
            category=f.get("category", "sast"),
        )
        for f in stored.get("findings") or []
        if isinstance(f, dict)
    )
    report = ScanReport(
        findings=findings, metrics=stored.get("metrics") or {}, tools=(), repo=stored.get("repo") or {}, deduped_count=0
    )
    recomputed = report.summary()
    # Prefer the STORED summary block wherever it exists: recomputing the health score here would
    # stamp today's formula (and health_score_version) onto a baseline scored under a different
    # one, giving the delta engine and the UI two different "baseline" numbers for the same file.
    # Only `measures` (the reason this function exists -- old files predate that block) is taken
    # from the recomputation, and only when the stored file doesn't already carry it.
    stored_summary = stored.get("summary")
    if isinstance(stored_summary, dict) and stored_summary.get("health_score") is not None:
        return {"measures": recomputed["measures"], **stored_summary}
    return recomputed


async def repo_scan_baseline_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Measures the repository as it arrived, before P1 writes anything.

    Idempotent on file existence, and that is a correctness requirement rather than an
    optimization. Every stage routes `needs_clarification -> END` and re-entry traverses the graph
    from the top again (which is why stages carry an `already_approved` short-circuit), so this
    node is re-entered many times per run -- once per clarification round on requirements, spec,
    plan, and everything after. Re-baselining on any of those would silently zero out the very
    improvement the metrics-report delta exists to report, and it would fail *quietly*: the number would just
    come out small.

    Deliberately keyed on the committed file, not on graph state: compile_graph() uses
    InMemorySaver(), so state does not survive a process restart but the file does. Re-baselining
    is a manual act -- delete `.ai-dev-workflow/repo-scan-baseline.json`.
    """
    thread_id = config["configurable"]["thread_id"]
    from . import git_ops, repo_files
    from .sandbox import registry as sandbox_registry
    from .sandbox.factory import get_sandbox_provider

    # `repo_scan` is a plain LastValue channel (no reducer): every return REPLACES the whole dict.
    # This node re-enters on every clarification round, so each branch must spread the prior dict
    # and re-emit baseline_summary -- dropping it here would blank the frontend metrics bar mid-run.
    prior = dict(state.get("repo_scan") or {})

    if sandbox_registry.get(thread_id) is None:
        return {"repo_scan": {**prior, "baseline": None, "reason": "no_sandbox"}}

    provider = get_sandbox_provider()
    background = pop_background_scan(thread_id)
    existing = await repo_files.read_repo_file(provider, thread_id, BASELINE_PATH)
    if existing is not None and existing.strip() and background is None:
        logger.info("repo_scan: baseline already present, not re-measuring")
        summary = prior.get("baseline_summary")
        baseline_coverage = prior.get("baseline_coverage")
        if summary is None:
            try:
                stored = json.loads(existing)
            except json.JSONDecodeError:
                stored = None
            if stored is not None:
                summary = _summary_from_stored(stored)
                baseline_coverage = (stored.get("metrics") or {}).get("coverage") or {
                    "line_rate": None, "reason": "not measured in stored baseline",
                }
        return {"repo_scan": {**prior, "baseline": "existing", "baseline_summary": summary,
                              "baseline_coverage": baseline_coverage}}

    # Overlap: scaffold_finalize kicked the scan (and coverage measurement) as a background task
    # ~2 LLM stages ago (both are data-independent of tech-stack/brownfield). Await it here; fall
    # back to an inline scan+coverage run when absent (process restart mid-run). Behavior note:
    # overlapped, the baseline usually EXCLUDES the tech-stack conventions writes (previously it
    # deterministically included them) -- racily, and accepted; the git-index lock in git_ops
    # covers the commit race.
    if background is not None:
        try:
            report, coverage = await background
        except Exception:  # noqa: BLE001 -- background failure falls back to a fresh inline run
            logger.warning("background repo scan failed; re-running inline", exc_info=True)
            report, coverage = await _scan_with_coverage(
                provider, thread_id, chat_provider=state["provider"], timeout_seconds=REPO_SCAN_COVERAGE_TIMEOUT_SECONDS,
                run_id=state.get("run_id", "unknown"),
            )
    else:
        report, coverage = await _scan_with_coverage(
            provider, thread_id, chat_provider=state["provider"], timeout_seconds=REPO_SCAN_COVERAGE_TIMEOUT_SECONDS,
            run_id=state.get("run_id", "unknown"),
        )
    report = replace(report, metrics={**report.metrics, "coverage": coverage})
    dashboard = report.to_dashboard_dict()
    await repo_files.write_repo_file(
        provider, thread_id, BASELINE_PATH, json.dumps(dashboard, indent=2, default=str) + "\n"
    )
    # Snapshot the SBOM alongside the baseline scan. SBOM_PATH is overwritten by every later scan,
    # so without this there is no "before" to compare against and `supply_chain_diff` has nothing to
    # do -- the run could report 816 components without being able to say which of them it added.
    baseline_sbom = await repo_files.read_repo_file(provider, thread_id, SBOM_PATH)
    committed = [BASELINE_PATH]
    if baseline_sbom:
        await repo_files.write_repo_file(provider, thread_id, SBOM_BASELINE_PATH, baseline_sbom)
        committed.append(SBOM_BASELINE_PATH)

    await repo_files.append_ledger_entry(
        provider, thread_id,
        {"stage": "repo_scan", "node": "baseline", "finding_count": len(report.findings),
         "commit": report.repo.get("commit")},
    )
    await git_ops.commit_paths(provider, thread_id, committed, "ai-dev-workflow: repo-scan baseline")
    return {"repo_scan": {**prior, "baseline": "written", "commit": report.repo.get("commit"),
                          "baseline_summary": dashboard["summary"], "baseline_coverage": coverage}}


async def _repo_facts(provider: Any, thread_id: str) -> dict[str, Any]:
    commit = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD 2>/dev/null")
    branch = await provider.exec_in_sandbox(thread_id, "git rev-parse --abbrev-ref HEAD 2>/dev/null")
    return {
        "commit": commit.stdout.strip() if commit.ok else None,
        "branch": branch.stdout.strip() if branch.ok else None,
    }


# ------------------------------------------------------------------------------------------------
# Self-check
# ------------------------------------------------------------------------------------------------


def _vuln(tool: str, advisory: str, pkg: str, severity: str, aliases: tuple[str, ...] = (), source: str = "native") -> Finding:
    return Finding(
        finding_key=stable_id("vulnerability", f"{advisory}:{pkg}", "package-lock.json"),
        tool=tool, rule_id=advisory, severity=severity, raw_severity=severity.upper(),
        file="package-lock.json", line=None, message=f"{advisory} in {pkg}",
        category="vulnerability", title=advisory,
        cve=advisory if _CVE_RE.match(advisory) else None,
        aliases=(advisory,) + aliases,
        package={"ecosystem": "npm", "name": pkg, "version": "1.0.0"},
        severity_source=source, sources=(tool,),
    )


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.repo_scan`
    """Self-check for the pure half."""

    # --- CVSS v3.1 base score, at the band boundaries -----------------------------------------
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == 0.0
    assert cvss3_base_score("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N") == 1.8
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N") == 6.1  # scope-changed path
    assert cvss3_base_score("not-a-vector") is None
    assert severity_from_score(9.0) == "critical" and severity_from_score(8.9) == "high"
    assert severity_from_score(7.0) == "high" and severity_from_score(6.9) == "medium"
    assert severity_from_score(4.0) == "medium" and severity_from_score(3.9) == "low"
    assert severity_from_score(None) is None

    assert normalize_tier("CRITICAL") == "critical"
    assert normalize_tier("UNKNOWN") is None, "UNKNOWN must not masquerade as a measurement"

    # --- dedup: the whole reason osv-scanner runs alongside trivy ------------------------------
    trivy_side = _vuln("trivy", "CVE-2024-1234", "lodash", "high")
    osv_side = _vuln("osv-scanner", "GHSA-aaaa-bbbb-cccc", "lodash", "medium", aliases=("CVE-2024-1234",))
    merged, collapsed = dedupe([trivy_side, osv_side])
    assert collapsed == 1 and len(merged) == 1, merged
    only = merged[0]
    assert only.cve == "CVE-2024-1234", only
    assert "GHSA-aaaa-bbbb-cccc" in only.aliases, only
    assert only.severity == "high", "merge must keep the worst severity, not the last one seen"
    assert set(only.sources) == {"trivy", "osv-scanner"} and only.occurrences == 2
    assert _dashboard_finding(only, True)["confidence"] == "corroborated"

    # Two genuinely different advisories on the same package must NOT collapse.
    other = _vuln("trivy", "CVE-2024-9999", "lodash", "low")
    assert len(dedupe([trivy_side, osv_side, other])[0]) == 2

    # ...and the same advisory on a different package must not collapse either.
    elsewhere = _vuln("trivy", "CVE-2024-1234", "express", "high")
    assert len(dedupe([trivy_side, elsewhere])[0]) == 2

    # Canonical id is deterministic regardless of which tool was seen first.
    assert dedupe([osv_side, trivy_side])[0][0].finding_key == only.finding_key
    assert canonical_advisory_id(["GHSA-z", "CVE-2024-1", "OSV-3"]) == "CVE-2024-1"
    assert canonical_advisory_id(["GHSA-z", "OSV-3"]) == "GHSA-z"
    assert canonical_advisory_id([]) == "unknown"

    # --- dedup: secrets, same line from two tools ---------------------------------------------
    gitleaks_hit, _ = parse_gitleaks(
        json.dumps([{"File": "./src/config.ts", "StartLine": 12, "RuleID": "aws-key", "Description": "AWS key"}])
    )
    trivy_hit, _ = parse_trivy(
        json.dumps({"Results": [{"Target": "src/config.ts", "Class": "secret",
                                 "Secrets": [{"RuleID": "aws-access-key-id", "StartLine": 12, "Title": "AWS creds"}]}]})
    )
    secrets, collapsed = dedupe(gitleaks_hit + trivy_hit)
    assert collapsed == 1 and len(secrets) == 1, secrets
    assert secrets[0].severity == "critical"
    # A corroborated finding still counts as one real issue -- never zero.
    assert is_gating(secrets[0], severity_floor="medium", introduced_ids=None)

    # --- stable id survives line drift ---------------------------------------------------------
    moved, _ = parse_gitleaks(
        json.dumps([{"File": "./src/config.ts", "StartLine": 400, "RuleID": "aws-key", "Description": "AWS key"}])
    )
    assert moved[0].finding_key == gitleaks_hit[0].finding_key, "id must not include the line number"

    # --- parsers -------------------------------------------------------------------------------
    _, scc_metrics = parse_scc(json.dumps([
        {"Name": "Go", "Count": 2, "Code": 100, "Comment": 10, "Lines": 130, "Complexity": 7},
        {"Name": "TypeScript", "Count": 5, "Code": 300, "Comment": 20, "Lines": 400, "Complexity": 21},
    ]))
    assert scc_metrics["size"]["total_loc"] == 530 and scc_metrics["size"]["files"] == 7
    assert [lang["name"] for lang in scc_metrics["size"]["languages"]] == ["TypeScript", "Go"]

    lizard_csv = (
        "5,3,20,1,7,fine@1-7@./src/a.py,./src/a.py,fine,fine(),1,7\n"
        "80,22,400,4,90,messy@1-90@./src/b.py,./src/b.py,messy,messy(),1,90\n"
        "120,40,900,6,150,awful@1-150@./src/b.py,./src/b.py,awful,awful(),1,150\n"
    )
    lizard_findings, lizard_metrics = parse_lizard(lizard_csv)
    assert lizard_metrics["complexity"]["max_ccn"] == 40
    assert lizard_metrics["complexity"]["functions_over_threshold"] == 2
    assert [f.severity for f in lizard_findings] == ["high", "medium"], lizard_findings
    assert lizard_metrics["complexity"]["_by_path"]["src/b.py"] == 40

    jscpd_findings, jscpd_metrics = parse_jscpd(json.dumps({
        "statistics": {"total": {"percentage": 9.5, "duplicatedLines": 190}},
        "duplicates": [{"firstFile": {"name": "src/a.ts", "start": 3}, "secondFile": {"name": "src/b.ts"}, "lines": 40}],
    }))
    assert jscpd_metrics["duplication"]["percent"] == 9.5 and len(jscpd_findings) == 1
    assert parse_jscpd(json.dumps({"statistics": {"total": {"percentage": 1.0}}, "duplicates": []}))[0] == []

    osv_findings, _ = parse_osv(json.dumps({"results": [{"source": {"path": "poetry.lock"}, "packages": [{
        "package": {"name": "requests", "version": "2.0.0", "ecosystem": "PyPI"},
        "vulnerabilities": [
            {"id": "GHSA-1", "aliases": ["CVE-2020-1"], "summary": "bad",
             "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]},
            {"id": "GHSA-2", "aliases": [], "summary": "worse", "database_specific": {"severity": "LOW"}},
            {"id": "GHSA-3", "aliases": [], "summary": "unrated"},
        ]}]}]}))
    assert [f.severity for f in osv_findings] == ["critical", "low", "medium"], osv_findings
    assert [f.severity_source for f in osv_findings] == ["derived", "native", "defaulted"], osv_findings
    assert osv_findings[0].cve == "CVE-2020-1"

    trivy_vulns, _ = parse_trivy(json.dumps({"Results": [{"Target": "package-lock.json", "Type": "npm",
        "Vulnerabilities": [{"VulnerabilityID": "CVE-2021-1", "PkgName": "left-pad", "InstalledVersion": "1.0.0",
                             "FixedVersion": "1.0.1", "Severity": "HIGH", "Title": "t", "CweIDs": ["CWE-79"]},
                            {"VulnerabilityID": "CVE-2021-2", "PkgName": "left-pad", "Severity": "UNKNOWN", "Title": "u"}]}]}))
    assert trivy_vulns[0].severity == "high" and trivy_vulns[0].severity_source == "native"
    assert trivy_vulns[0].package["fixed_version"] == "1.0.1" and trivy_vulns[0].cwe == "CWE-79"
    assert trivy_vulns[1].severity == "medium" and trivy_vulns[1].severity_source == "defaulted"

    _, churn = parse_git_churn(
        "C|abc|Ada|2026-01-01T00:00:00Z\n10\t2\tsrc/b.py\n1\t0\tsrc/a.py\n"
        "C|def|Ada|2026-01-02T00:00:00Z\n30\t5\tsrc/b.py\n"
        "C|ghi|Grace|2026-01-03T00:00:00Z\n-\t-\tassets/logo.png\n"
    )
    assert churn["churn"]["commits"] == 3 and churn["churn"]["files_touched"] == 3
    assert churn["churn"]["_per_file"]["src/b.py"]["lines_changed"] == 47
    assert churn["churn"]["ownership"]["bus_factor_files"] == 3
    assert "Ada" not in json.dumps(_public_metrics(churn)), "author names must not reach the artifact"

    # Every parser tolerates garbage rather than taking the scan down with it.
    for parse in (
        parse_scc, parse_lizard, parse_jscpd, parse_gitleaks, parse_trivy, parse_osv, parse_semgrep,
        parse_git_churn, parse_checkov, parse_interrogate, parse_dotnet_docs, parse_syft,
    ):
        assert parse("}{ not json") == ([], {}) or parse("}{ not json")[0] == []

    # --- checkov: list-of-frameworks shape, misconfig category reuses SECURITY_CATEGORIES ---------
    checkov_findings, checkov_metrics = parse_checkov(json.dumps([
        {"check_type": "terraform", "results": {
            "failed_checks": [{"check_id": "CKV_AWS_1", "check_name": "S3 bucket is not public",
                                "file_path": "/main.tf", "file_line_range": [10, 14], "severity": "HIGH"}],
            "passed_checks": [{"check_id": "CKV_AWS_2"}],
        }},
        {"check_type": "dockerfile", "results": {"failed_checks": [], "passed_checks": []}},
    ]))
    assert checkov_metrics["iac"] == {"failed_checks": 1, "passed_checks": 1}
    assert checkov_findings[0].category == "misconfig" and checkov_findings[0].severity == "high"
    assert checkov_findings[0].line == 10 and checkov_findings[0].end_line == 14
    assert is_gating(checkov_findings[0], severity_floor="medium", introduced_ids=None), (
        "checkov must gate through the existing misconfig category with no new gate code"
    )
    assert parse_checkov(json.dumps({"results": {"failed_checks": [], "passed_checks": []}})) == ([], {"iac": {"failed_checks": 0, "passed_checks": 0}})

    # --- interrogate: documented RESULT line, and the fallback when it's missing -------------------
    under, under_metrics = parse_interrogate("some table\nRESULT: FAILED (minimum: 0.0%, actual: 12.5%)\n")
    assert under_metrics["documentation"]["python_docstring_coverage_percent"] == 12.5
    assert len(under) == 1 and under[0].category == "maintainability"
    over, over_metrics = parse_interrogate("RESULT: PASSED (minimum: 0.0%, actual: 92.3%)\n")
    assert over_metrics["documentation"]["python_docstring_coverage_percent"] == 92.3
    assert over == [], "above threshold must not gate"
    fallback, fallback_metrics = parse_interrogate("| TOTAL | 10 | 2 | 8 | 80.0% |\n")
    assert fallback_metrics["documentation"]["python_docstring_coverage_percent"] == 80.0, (
        "must fall back to the last bare percentage when the RESULT line is absent"
    )

    # --- dotnet-docs: CS1591 count, and "no project" must not read as "fully documented" -----------
    _, dotnet_metrics = parse_dotnet_docs(
        "Foo.cs(10,5): warning CS1591: Missing XML comment for publicly visible type\n"
        "Bar.cs(20,5): warning CS1591: Missing XML comment for publicly visible type\n"
    )
    assert dotnet_metrics["documentation"]["dotnet_undocumented_public_members"] == 2
    assert parse_dotnet_docs("MSBUILD : error MSB1003: Specify a project or solution file.") == ([], {})

    # --- syft: SBOM summarized to a handful of numbers, never expanded into `metrics` --------------
    _, syft_metrics = parse_syft(json.dumps({"components": [
        {"purl": "pkg:npm/lodash@4.17.21"}, {"purl": "pkg:npm/left-pad@1.0.0"}, {"purl": "pkg:pypi/requests@2.0.0"},
    ]}))
    assert syft_metrics["sbom"]["component_count"] == 3
    assert syft_metrics["sbom"]["ecosystems"] == {"npm": 2, "pypi": 1}
    assert parse_syft("}{ not json") == ([], {})
    # No dependency graph must be distinguishable from "everything is direct".
    assert syft_metrics["sbom"]["ancestry"] == "no_dependency_graph", syft_metrics["sbom"]

    # --- SBOM ancestry: "you chose this" vs "your framework chose this", as a fact -----------------
    sbom_doc = {
        "metadata": {"component": {"bom-ref": "root"}},
        "components": [
            {"bom-ref": "next", "purl": "pkg:npm/next@15.4.9", "licenses": [{"license": {"id": "MIT"}}]},
            {"bom-ref": "styled-jsx", "purl": "pkg:npm/styled-jsx@5.1.6", "licenses": [{"license": {"name": "Apache-2.0"}}]},
            {"bom-ref": "deep", "purl": "pkg:npm/deep@1.0.0"},
        ],
        "dependencies": [
            {"ref": "root", "dependsOn": ["next"]},
            {"ref": "next", "dependsOn": ["styled-jsx"]},
            {"ref": "styled-jsx", "dependsOn": ["deep"]},
        ],
    }
    ancestry = sbom_ancestry(sbom_doc)
    # `next` is in the root's own dependsOn -- chosen. The other two arrived through it.
    assert ancestry["direct"] == ["next"], ancestry
    assert ancestry["direct_count"] == 1 and ancestry["transitive_count"] == 2, ancestry
    # Licences come from the components themselves -- both `license.id` and `license.name` forms.
    assert ancestry["licences_by_ref"]["styled-jsx"] == ["Apache-2.0"], ancestry["licences_by_ref"]
    assert ancestry["with_licence"] == 2, ancestry

    # With no identifiable root, "depended on by nothing" is the fallback definition of direct.
    rootless = sbom_ancestry({**sbom_doc, "metadata": {}})
    assert rootless["direct"] == ["next"], rootless

    # An absent graph returns {} -- NOT a report that every component is direct.
    assert sbom_ancestry({"components": sbom_doc["components"]}) == {}
    assert sbom_ancestry({"components": [], "dependencies": []}) == {}

    # A graph too sparse to attribute is also {}. This is the REAL shape syft produced on this
    # pipeline's branch (816 components, 20 dependency entries): without the guard it reported
    # "773 direct, 43 transitive", which describes syft's linking, not the project's choices.
    sparse = {
        "components": [{"bom-ref": f"c{i}"} for i in range(100)],
        "dependencies": [{"ref": "c0", "dependsOn": ["c1"]}, {"ref": "c1", "dependsOn": ["c2"]}],
    }
    assert sbom_ancestry(sparse) == {}, "a graph covering 3% of components must not yield an ancestry split"
    _, sparse_metrics = parse_syft(json.dumps(sparse))
    assert sparse_metrics["sbom"]["ancestry"] == "no_dependency_graph", sparse_metrics["sbom"]

    # --- direct vs transitive, from the lockfile's own root entry ---------------------------------
    # Shape and contents taken from a real generated app's package-lock.json.
    root_lock = json.dumps({"packages": {
        "": {"name": "web",
             "dependencies": {"next": "16.3.1", "react": "19.2.8"},
             "devDependencies": {"vitest": "4.1.11"}},
        "node_modules/next": {"version": "16.3.1"},
        "node_modules/@img/sharp-libvips-linux-x64": {"optional": True, "license": "LGPL-3.0-or-later"},
    }})
    direct = direct_dependency_names(root_lock)
    assert direct == {"next", "react", "vitest"}, direct
    assert direct_dependency_names("}{ not json") == set()
    assert direct_dependency_names("{}") == set()

    # A licence on a package the framework dragged in is advisory -- nothing here can change it.
    inherited = Finding(
        finding_key="lic-t", tool="trivy", rule_id="LGPL-3.0-or-later", severity="high",
        raw_severity="HIGH", file="apps/web/package-lock.json", line=None,
        message="licence LGPL-3.0-or-later on package @img/sharp-libvips-linux-x64",
        category="license", package={"name": "@img/sharp-libvips-linux-x64"},
    )
    assert not is_gating(inherited, severity_floor="medium", introduced_ids=None,
                         direct_dependencies=frozenset(direct))
    # A licence on a package THIS project chose gates: it is a decision someone here can unmake.
    # The old path heuristic could not tell these two apart -- both live in package-lock.json.
    chosen = replace(inherited, finding_key="lic-d", package={"name": "next"})
    assert is_gating(chosen, severity_floor="medium", introduced_ids=None,
                     direct_dependencies=frozenset(direct))
    # With no lockfile facts available, everything in a lock file stays advisory (older behaviour).
    assert not is_gating(chosen, severity_floor="medium", introduced_ids=None, direct_dependencies=None)

    # --- platform-excluded licences: npm records every variant, installs one ------------------
    lock = json.dumps({"packages": {
        "": {"name": "web"},
        "node_modules/next": {"version": "16.3.1"},
        "node_modules/@img/sharp-win32-arm64": {"optional": True, "os": ["win32"], "cpu": ["arm64"], "license": "Apache-2.0"},
        "node_modules/@img/sharp-libvips-darwin-x64": {"optional": True, "os": ["darwin"], "cpu": ["x64"], "license": "LGPL-3.0-or-later"},
        "node_modules/@img/sharp-libvips-linux-x64": {"optional": True, "os": ["linux"], "cpu": ["x64"], "license": "LGPL-3.0-or-later"},
        "node_modules/@img/sharp-wasm32": {"optional": True, "cpu": ["wasm32"], "license": "Apache-2.0"},
    }})
    excluded = uninstallable_lock_packages(lock, "linux", "x64")
    # Cannot install here -> its licence is not a fact about this artifact.
    assert "@img/sharp-win32-arm64" in excluded, excluded
    assert "@img/sharp-libvips-darwin-x64" in excluded, excluded
    assert "@img/sharp-wasm32" in excluded, excluded
    # The variant that DOES install must be kept -- this check must not hide a real obligation.
    assert "@img/sharp-libvips-linux-x64" not in excluded, excluded
    # Non-optional packages are never excluded, whatever their name.
    assert "next" not in excluded and "" not in excluded, excluded
    # On a darwin/arm64 target the exclusions invert, which is the point of reading the constraints.
    mac = uninstallable_lock_packages(lock, "darwin", "x64")
    assert "@img/sharp-libvips-darwin-x64" not in mac and "@img/sharp-libvips-linux-x64" in mac, mac
    # Malformed or absent lockfile: no exclusions, never a crash.
    assert uninstallable_lock_packages("}{ not json", "linux", "x64") == set()
    assert uninstallable_lock_packages("{}", "linux", "x64") == set()

    # --- supply-chain diff: works WITHOUT the dependency graph, which is why it is the half of
    # --- "put the SBOM to work" that actually ships on this stack.
    base_sbom = {"components": [
        {"purl": "pkg:npm/next@15.4.6"}, {"purl": "pkg:npm/react@19.0.0"}, {"purl": "pkg:npm/gone@1.0.0"},
    ]}
    curr_sbom = {"components": [
        {"purl": "pkg:npm/next@15.4.9"}, {"purl": "pkg:npm/react@19.0.0"}, {"purl": "pkg:npm/added@2.0.0"},
    ]}
    chain = supply_chain_diff(base_sbom, curr_sbom)
    assert chain["added"] == ["npm/added"], chain
    assert chain["removed"] == ["npm/gone"], chain
    # An upgrade is an upgrade, not a remove+add pair -- that is why identity excludes the version.
    assert chain["version_changed"] == ["npm/next: 15.4.6 -> 15.4.9"], chain
    assert chain["net_change"] == 0, chain
    # No baseline means no diff, never a fabricated one (same rule as delta_summary).
    assert supply_chain_diff(None, curr_sbom) is None
    # A component with NO purl is skipped -- it has no package identity. This is what stops syft's
    # catalogued build output (404 of 803 entries on a real branch, each named by an absolute file
    # path) from being reported as added dependencies.
    assert sbom_component_purls({"components": [{"name": "custom", "version": "1.2"}]}) == {}
    dll_noise = {"components": [
        {"name": "/workspace/repo/apps/api.Tests/bin/Debug/net10.0/Api.dll", "version": "1.0"},
        {"purl": "pkg:npm/next@15.4.9"},
    ]}
    assert sbom_component_purls(dll_noise) == {"npm/next": "15.4.9"}, sbom_component_purls(dll_noise)

    # Just past the threshold, attribution resumes: 6 of 10 components appear in the graph.
    dense = {
        "components": [{"bom-ref": f"d{i}"} for i in range(10)],
        "dependencies": [{"ref": f"d{i}", "dependsOn": [f"d{i + 1}"]} for i in range(5)],
    }
    dense_ancestry = sbom_ancestry(dense)
    assert dense_ancestry, "6/10 coverage is above MIN_GRAPH_COVERAGE and must be attributed"
    assert dense_ancestry["transitive_count"] == 5, dense_ancestry

    _, graphed = parse_syft(json.dumps(sbom_doc))
    assert graphed["sbom"]["direct_count"] == 1, graphed["sbom"]
    assert graphed["sbom"]["transitive_count"] == 2, graphed["sbom"]
    assert "ancestry" not in graphed["sbom"], "a measured graph must not also report no_dependency_graph"
    # The big per-ref maps stay OUT of the hashed metrics fragment.
    assert "licences_by_ref" not in graphed["sbom"] and "direct" not in graphed["sbom"], graphed["sbom"]

    # --- doc metrics from two languages merge into one `documentation` section, no key collision --
    merged_docs = _assemble_metrics([under_metrics, dotnet_metrics])
    assert merged_docs["documentation"]["python_docstring_coverage_percent"] == 12.5
    assert merged_docs["documentation"]["dotnet_undocumented_public_members"] == 2

    # --- hotspot join: needs churn AND complexity ---------------------------------------------
    metrics = _assemble_metrics([lizard_metrics, churn, jscpd_metrics, scc_metrics])
    assert metrics["churn"]["hotspots"][0]["path"] == "src/b.py"
    assert metrics["churn"]["hotspots"][0]["ccn"] == 40
    assert "_per_file" not in metrics["churn"] and "_by_path" not in metrics["complexity"]

    # --- report shape --------------------------------------------------------------------------
    report = ScanReport(
        findings=tuple(sort_findings(list(secrets) + list(merged) + lizard_findings)),
        metrics=metrics, tools=({"name": "trivy", "db_version": "2026-08-01"},),
        repo={"commit": "aaa", "branch": "main"}, deduped_count=2,
    )
    dashboard = report.to_dashboard_dict()
    assert json.loads(json.dumps(dashboard, default=str))  # must round-trip
    for key in ("schema_version", "generated_at", "content_hash", "repo", "summary", "findings", "metrics", "tools"):
        assert key in dashboard, key
    serialized = json.dumps(dashboard["findings"])
    assert '"tool"' not in serialized and '"sources"' not in serialized, "dashboard must not attribute tools"
    # ...but the gate path, which reads Finding.to_dict() straight off ScanReport.findings, must.
    assert '"tool"' in json.dumps([f.to_dict() for f in report.findings]), "gate payload must attribute tools"
    assert dashboard["summary"]["by_severity"]["critical"] == 1
    assert dashboard["summary"]["health_score"] < 100

    # --- health score v2 -----------------------------------------------------------------------
    assert abs(sum(HEALTH_WEIGHTS.values()) - 1.0) < 1e-9, "nominal weights must sum to 1.0"
    # The README's weights table documents these defaults -- keep them in lockstep (the assert is
    # skipped when an env override is actually set, since then HEALTH_WEIGHTS is deliberately off
    # the documented defaults).
    if not any(os.environ.get(f"HEALTH_WEIGHT_{name.upper()}") for name in HEALTH_WEIGHTS):
        assert HEALTH_WEIGHTS == {
            "security": 0.40, "coverage": 0.12, "dependencies": 0.12, "ac_verification": 0.10,
            "accessibility": 0.07, "complexity": 0.06, "performance": 0.05,
            "duplication": 0.04, "maintainability": 0.04,
        }, "README.md 'The health score' documents different defaults -- update both together"
    # This fixture's trivy entry has no status:"ok", so security is UNMEASURED -> None, and its
    # weight redistributes over the measured legs (weights_used re-sums to 1.0).
    summary_v2 = dashboard["summary"]
    assert summary_v2["health_score_version"] == HEALTH_SCORE_VERSION
    assert summary_v2["health_subscores"]["security"] is None, "failed/absent security tools must read unmeasured, not clean"
    assert "security" not in summary_v2["health_weights_used"]
    assert abs(sum(summary_v2["health_weights_used"].values()) - 1.0) < 1e-3, summary_v2["health_weights_used"]
    # A clean security run scores 100; three criticals zero the leg (and cap the composite at 60).
    clean = health_score(
        security_by_severity={lvl: 0 for lvl in SEVERITY_ORDER}, security_measured=True,
        maintainability_count=0, metrics={},
    )
    assert clean["subscores"]["security"] == 100.0 and clean["score"] == 100, clean
    three_crit = health_score(
        security_by_severity={**{lvl: 0 for lvl in SEVERITY_ORDER}, "critical": 3},
        security_measured=True, maintainability_count=0, metrics={},
    )
    assert three_crit["subscores"]["security"] == 0.0, three_crit
    # Nothing measured at all -> score None, never a fabricated number.
    nothing = health_score(
        security_by_severity={lvl: 0 for lvl in SEVERITY_ORDER}, security_measured=False,
        maintainability_count=0, metrics={},
    )
    # maintainability's count part is always computable, so at least that leg is present -- but a
    # weights table zeroed by env override must not divide by zero either.
    assert nothing["score"] is not None and nothing["subscores"]["security"] is None
    # Coverage is the raw measurement, not distance-to-the-95%-gate: 95% must NOT read as 100.
    covered = health_score(
        security_by_severity={lvl: 0 for lvl in SEVERITY_ORDER}, security_measured=True,
        maintainability_count=0, metrics={"coverage": {"line_rate": 95.0, "branch_rate": 95.0}},
    )
    assert covered["subscores"]["coverage"] == 95.0, covered["subscores"]
    # AC leg: flaky counts half.
    ac = health_score(
        security_by_severity={lvl: 0 for lvl in SEVERITY_ORDER}, security_measured=True,
        maintainability_count=0, metrics={},
        ac_verification={"total": 4}, ac_execution={"status": "ok", "solidly_verified": 2, "flaky": ["AC-1.1"]},
    )
    assert ac["subscores"]["ac_verification"] == 62.5, ac["subscores"]
    # Every curve constant the README's derivation column documents, pinned (the weights assert
    # below covers only the weights -- without these, a constant could drift from the doc while
    # every self-check stayed green).
    _zero = {lvl: 0 for lvl in SEVERITY_ORDER}

    def _leg(**kwargs: Any) -> dict[str, Any]:
        return health_score(
            security_by_severity=_zero, security_measured=True, maintainability_count=0, **kwargs
        )["subscores"]

    assert health_score(
        security_by_severity={**_zero, "high": 1, "medium": 1, "low": 1},
        security_measured=True, maintainability_count=0, metrics={},
    )["subscores"]["security"] == 79.0  # 100 - 15 - 5 - 1
    assert _leg(metrics={"coverage": {"line_rate": 100.0, "branch_rate": 0.0}})["coverage"] == 75.0  # 0.75/0.25 blend
    assert _leg(metrics={"outdated": {"total": 4}})["dependencies"] == 80.0  # 100 - 5*4
    assert _leg(metrics={"complexity": {"mean_ccn": 7.0, "max_ccn": 25.0}})["complexity"] == 80.0  # 0.7*80 + 0.3*80
    assert _leg(metrics={"duplication": {"percent": 3.0}})["duplication"] == 91.0  # 100 - 3*pct
    assert health_score(
        security_by_severity=_zero, security_measured=True, maintainability_count=4, metrics={},
    )["subscores"]["maintainability"] == 88.0  # 100 - 3*count

    # --- outdated leg: parser + hash exclusion -------------------------------------------------
    _, outdated_metrics = parse_outdated(
        "# aidw outdated probe\n"
        "### npm .\n{\"react\": {\"current\": \"18.0.0\", \"latest\": \"19.0.0\"}}\n"
        "### dotnet\nProject `Api` has the following updates to its packages\n"
        "   Top-level Package      Requested   Resolved   Latest\n"
        "   > Microsoft.NET.Test.Sdk  17.0.0  17.0.0  17.9.0\n"
        "Project `Api.Tests` has the following updates to its packages\n"
        "   Top-level Package      Requested   Resolved   Latest\n"
        "   > Microsoft.NET.Test.Sdk  17.0.0  17.0.0  17.9.0\n"
        "### pypi .venv\n[{\"name\": \"flask\"}]\n"
    )
    # The dotnet row appears in TWO projects but is ONE stale package -- deduped by name.
    assert outdated_metrics["outdated"]["total"] == 3, outdated_metrics
    assert outdated_metrics["outdated"] == {"npm": 1, "nuget": 1, "pypi": 1, "total": 3, "checked": ["npm", "dotnet", "pypi"]}
    assert parse_outdated("garbage with no markers") == ([], {}), "no sections must read unmeasured, not zero"
    # A bare marker whose probe produced nothing = FAILED probe = unmeasured, never a perfect 0.
    assert parse_outdated("### npm .\n") == ([], {}), "a failed probe must not score dependency health 100"
    assert parse_outdated("### dotnet\nMSB1003: Specify a project file.\n") == ([], {})
    # npm's own failure report rides stdout as {"error": {...}} -- a failed probe, not 1 package.
    assert parse_outdated('### npm .\n{"error": {"code": "ENOTFOUND"}}\n') == ([], {})
    # An empty-but-valid answer IS measured: everything current -> 0 outdated, subscore 100.
    ok_empty = parse_outdated("### npm .\n{}\n")[1]
    assert ok_empty["outdated"]["total"] == 0 and ok_empty["outdated"]["checked"] == ["npm"], ok_empty
    with_outdated = ScanReport(
        findings=report.findings, metrics={**metrics, "outdated": outdated_metrics["outdated"]},
        tools=report.tools, repo=report.repo, deduped_count=report.deduped_count,
    )
    assert with_outdated.to_dashboard_dict()["content_hash"] == dashboard["content_hash"], (
        "a registry publish (outdated delta) must never change the content hash"
    )

    # --- _summary_from_stored must keep the stored score, not restamp today's formula ----------
    stored_v1 = {"summary": {"health_score": 61}, "findings": [], "metrics": {}}
    restored = _summary_from_stored(stored_v1)
    assert restored["health_score"] == 61 and "health_score_version" not in restored, restored
    assert "measures" in restored

    # --- measures: security must not see quality's findings ------------------------------------
    measures = dashboard["summary"]["measures"]
    assert measures["security"]["worst_open_severity"] == "critical", measures
    # The two lizard findings are "high"/"medium" but category=maintainability (quality, not
    # security) -- counting them here is the live bug this block fixes.
    assert measures["security"]["by_severity"]["high"] == 1, "quality's high must not count as security"
    assert measures["security"]["by_severity"]["critical"] == 1
    assert measures["duplication_percent"] == metrics["duplication"]["percent"]
    assert measures["mean_ccn"] == metrics["complexity"]["mean_ccn"]
    assert measures["coverage_line_rate"] is None, "no coverage measured in this fixture"

    # Zero-state: quality-only findings must report "none", not a fabricated severity.
    quality_only = ScanReport(findings=tuple(lizard_findings), metrics=metrics, tools=(), repo={}, deduped_count=0)
    qs_measures = quality_only.summary()["measures"]
    assert qs_measures["security"]["worst_open_severity"] == "none"
    assert qs_measures["security"]["by_severity"] == {level: 0 for level in SEVERITY_ORDER}

    # --- merge_measures: a partial-profile scan must not blank the OTHER loop's measures --------
    prior_for_merge = {
        "measures": {
            "security": {"worst_open_severity": "high", "by_severity": {**{lvl: 0 for lvl in SEVERITY_ORDER}, "high": 1}},
            "duplication_percent": 4.2,
            "mean_ccn": 6.5,
            "coverage_line_rate": 71.0,
        }
    }
    quality_only_summary = {
        "measures": {
            "security": {"worst_open_severity": "none", "by_severity": {lvl: 0 for lvl in SEVERITY_ORDER}},
            "duplication_percent": 1.0,
            "mean_ccn": 3.0,
            "coverage_line_rate": None,
        }
    }
    merged_quality = merge_measures(prior_for_merge, quality_only_summary, "quality")
    assert merged_quality["measures"]["security"] == prior_for_merge["measures"]["security"], "quality-remediation's scan must not blank security's measures"
    assert merged_quality["measures"]["duplication_percent"] == 1.0, "quality-remediation's own duplication must win"
    assert merged_quality["measures"]["mean_ccn"] == 3.0, "quality-remediation's own ccn must win"

    security_only_summary = {
        "measures": {
            "security": {"worst_open_severity": "critical", "by_severity": {**{lvl: 0 for lvl in SEVERITY_ORDER}, "critical": 1}},
            "duplication_percent": None,
            "mean_ccn": None,
            "coverage_line_rate": None,
        }
    }
    merged_security = merge_measures(prior_for_merge, security_only_summary, "security")
    assert merged_security["measures"]["security"] == security_only_summary["measures"]["security"], "security-remediation's own findings must win"
    assert merged_security["measures"]["duplication_percent"] == 4.2, "security-remediation's scan must not blank duplication"
    assert merged_security["measures"]["mean_ccn"] == 6.5, "security-remediation's scan must not blank ccn"
    assert merged_security["measures"]["coverage_line_rate"] == 71.0, "security-remediation's scan must not blank coverage"

    # No prior summary yet (first scan ever this run) -> nothing to merge, new summary passes through.
    assert merge_measures(None, quality_only_summary, "quality") == quality_only_summary
    # "full" (baseline/metrics-report) measures everything itself -- merge_measures is a no-op.
    assert merge_measures(prior_for_merge, quality_only_summary, "full") == quality_only_summary

    # Trivy's NONE/NEGLIGIBLE severities normalize to "info" -- on a security category (here,
    # vulnerability) that must surface as "info", never clamped up to "low" or hidden as "none".
    info_only = ScanReport(findings=(_vuln("trivy", "CVE-2024-0", "x", "info"),), metrics={}, tools=(), repo={}, deduped_count=0)
    info_measures = info_only.summary()["measures"]
    assert info_measures["security"]["worst_open_severity"] == "info", info_measures
    assert info_measures["security"]["by_severity"]["info"] == 1

    # Sorting is a total order, so the hash is stable regardless of input order.
    shuffled = ScanReport(
        findings=tuple(sort_findings(list(reversed(report.findings)))),
        metrics=metrics, tools=report.tools, repo=report.repo, deduped_count=2,
    )
    assert shuffled.to_dashboard_dict()["content_hash"] == dashboard["content_hash"]

    # A coverage-acquisition failure must not break "unchanged repo hashes identically": the
    # contract is that test_coverage_gate.measure_coverage only ever returns a stable reason CODE
    # (never raw, volatile subprocess stdout/stderr) into metrics.coverage.reason -- two identical
    # repos that fail coverage acquisition the same way must hash the same.
    failed_coverage_metrics = {**metrics, "coverage": {"line_rate": None, "branch_rate": None, "reason": "runner_error"}}
    run_a = ScanReport(findings=report.findings, metrics=failed_coverage_metrics, tools=report.tools, repo=report.repo, deduped_count=2)
    run_b = ScanReport(findings=report.findings, metrics=dict(failed_coverage_metrics), tools=report.tools, repo=report.repo, deduped_count=2)
    assert run_a.to_dashboard_dict()["content_hash"] == run_b.to_dashboard_dict()["content_hash"], (
        "identical repo + identical stable failure reason must hash identically"
    )

    # --- the Eval layer's two halves sit on opposite sides of the hash ---------------------------
    # This is the whole reason ac_verification and ac_execution are separate fields. A flaky test
    # makes execution differ between two runs over an IDENTICAL worktree; if that moved the hash,
    # the module's documented "unchanged worktree hashes identically" contract would be false the
    # first time a test flaked.
    base_kwargs = dict(findings=report.findings, metrics=metrics, tools=report.tools, repo=report.repo, deduped_count=2)
    verification = {"total": 4, "linked": 3, "unverified": ["US-0003.2"], "levels": {"unit": 2, "integration": 1, "e2e": 1}}
    exec_run_1 = {"status": "evaluated", "passing": 3, "failing": 0, "flaky": []}
    exec_run_2 = {"status": "evaluated", "passing": 2, "failing": 1, "flaky": ["US-0002.1"]}
    eval_a = ScanReport(**base_kwargs, ac_verification=verification, ac_execution=exec_run_1)
    eval_b = ScanReport(**base_kwargs, ac_verification=verification, ac_execution=exec_run_2)
    dash_a, dash_b = eval_a.to_dashboard_dict(), eval_b.to_dashboard_dict()
    assert dash_a["content_hash"] == dash_b["content_hash"], (
        "ac_execution must be EXCLUDED from content_hash -- a flaky suite would otherwise make an "
        "unchanged worktree hash differently on every run"
    )
    assert dash_a["ac_execution"] != dash_b["ac_execution"], "both reports must still carry their own execution result"

    # ...while the static half IS hashed: a repo that gained a test is a changed repo.
    more_verified = {**verification, "linked": 4, "unverified": []}
    eval_c = ScanReport(**base_kwargs, ac_verification=more_verified, ac_execution=exec_run_1)
    assert eval_c.to_dashboard_dict()["content_hash"] != dash_a["content_hash"], (
        "ac_verification must be INSIDE content_hash -- linking a new test changes the repo's content"
    )

    # Absent eval (the default) must not change the hash of a report that never had it, or every
    # existing baseline on disk would appear to have moved.
    assert ScanReport(**base_kwargs).to_dashboard_dict()["content_hash"] == dashboard["content_hash"]
    assert "ac_execution" not in ScanReport(**base_kwargs).to_dashboard_dict()

    # --- gating ---------------------------------------------------------------------------------
    low_vuln = _vuln("trivy", "CVE-2024-5", "x", "low")
    assert not is_gating(low_vuln, severity_floor="medium", introduced_ids=None)
    assert is_gating(_vuln("trivy", "CVE-2024-6", "x", "high"), severity_floor="medium", introduced_ids=None)
    quality = lizard_findings[0]
    assert is_gating(quality, severity_floor="medium", introduced_ids=None), "greenfield: everything gates"
    assert not is_gating(quality, severity_floor="medium", introduced_ids=frozenset()), "pre-existing debt must not gate"
    assert is_gating(quality, severity_floor="medium", introduced_ids=frozenset({quality.finding_key}))

    # known_gap_ids (Ruling 8): a finding remediation already explained in `known_gaps` never
    # gates -- security or quality, same as remediation's own gate draws no category line there --
    # but an untouched, uncovered finding still does. A fix that silently disabled gating
    # altogether would be as bad as the bug it replaces, so both halves are asserted.
    high_vuln = _vuln("trivy", "CVE-2024-6", "x", "high")
    assert is_gating(high_vuln, severity_floor="medium", introduced_ids=None), "sanity: gates before known_gap_ids"
    assert not is_gating(
        high_vuln, severity_floor="medium", introduced_ids=None, known_gap_ids=frozenset({high_vuln.finding_key})
    ), "a security finding named in known_gaps must not gate"
    assert is_gating(
        high_vuln, severity_floor="medium", introduced_ids=None, known_gap_ids=frozenset({"unrelated-finding-id"})
    ), "a finding NOT covered by known_gaps must still gate"
    assert not is_gating(
        quality, severity_floor="medium", introduced_ids=frozenset({quality.finding_key}),
        known_gap_ids=frozenset({quality.finding_key}),
    ), "known_gap_ids excludes quality findings too"

    # Wired through ScanReport.summary()'s own gating_count, not just the pure is_gating() call.
    gap_report = ScanReport(findings=(high_vuln, quality), metrics={}, tools=(), repo={}, deduped_count=0)
    all_gating = gap_report.summary(introduced_ids=frozenset({quality.finding_key}))
    assert all_gating["gating_count"] == 2
    one_excused = gap_report.summary(
        introduced_ids=frozenset({quality.finding_key}), known_gap_ids=frozenset({high_vuln.finding_key})
    )
    assert one_excused["gating_count"] == 1, "summary() must drop a known_gap_ids finding from gating_count"

    # --- delta ------------------------------------------------------------------------------------
    baseline = {
        "schema_version": 1, "repo": {"commit": "aaa"},
        "summary": {"health_score": 61},
        "findings": [
            {"id": "keep", "severity": "high", "category": "vulnerability", "title": "kept"},
            {"id": "gone", "severity": "critical", "category": "secret", "title": "fixed"},
            {"id": "worse", "severity": "low", "category": "sast", "title": "escalated"},
        ],
        "metrics": {"duplication": {"percent": 8.4}, "size": {"total_loc": 12000}, "coverage": {"line_rate": 70.0}},
        "tools": [{"name": "trivy", "db_version": "2026-08-01"}],
    }
    current = {
        "schema_version": 1, "repo": {"commit": "bbb"},
        "summary": {"health_score": 78},
        "findings": [
            {"id": "keep", "severity": "high", "category": "vulnerability", "title": "kept"},
            {"id": "worse", "severity": "high", "category": "sast", "title": "escalated"},
            {"id": "new", "severity": "medium", "category": "misconfig", "title": "introduced"},
        ],
        "metrics": {"duplication": {"percent": 2.1}, "size": {"total_loc": 14100}, "coverage": {"line_rate": 85.0}},
        "tools": [{"name": "trivy", "db_version": "2026-08-01"}],
    }
    delta = diff_scans(baseline, current)
    assert [f["id"] for f in delta["findings"]["fixed"]] == ["gone"]
    assert [f["id"] for f in delta["findings"]["introduced"]] == ["new"]
    assert delta["findings"]["persisted"] == ["keep", "worse"]
    assert delta["findings"]["severity_changed"] == [{"id": "worse", "from": "low", "to": "high"}]
    # An escalation is not a fix and not an introduction -- it must appear in exactly one bucket.
    assert "worse" not in {f["id"] for f in delta["findings"]["fixed"] + delta["findings"]["introduced"]}
    assert delta["findings"]["net_change"]["critical"] == -1
    assert delta["metrics"]["health_score"]["direction"] == "improved"
    assert delta["metrics"]["duplication_percent"]["direction"] == "improved"
    assert delta["metrics"]["total_loc"]["direction"] == "neutral", "more code is not a regression"
    assert delta["metrics"]["coverage_line_rate"]["direction"] == "improved"
    assert delta["caveats"]["db_drift"] is False
    assert diff_scans({**baseline, "tools": [{"name": "trivy", "db_version": "2026-01-01"}]}, current)["caveats"]["db_drift"]
    assert diff_scans(None, current) is None, "no baseline must mean no delta, never a zero delta"
    assert diff_scans({"baseline": None, "reason": "greenfield"}, current) is None

    # A regression is reported as one, not spun.
    regressed = diff_scans(current, baseline)
    assert regressed["metrics"]["health_score"]["direction"] == "regressed"
    assert regressed["metrics"]["duplication_percent"]["direction"] == "regressed"
    assert regressed["metrics"]["coverage_line_rate"]["direction"] == "regressed"

    # --- delta_summary: the fix for the dead metrics_nodes.py `.get("summary")` read -------------
    dsum = delta_summary(delta)
    assert dsum["fixed_count"] == 1 and dsum["introduced_count"] == 1
    assert dsum["severity_changed"] == 1, "severity_changed is a COUNT, not the list"
    assert dsum["net_change"] == delta["findings"]["net_change"]
    assert dsum["metrics"]["coverage_line_rate"]["direction"] == "improved"
    assert dsum["baseline_commit"] == "aaa"
    assert delta_summary(None) is None, "no delta must mean no delta_summary, never a fabricated one"

    # --- old-baseline recompute: pre-`measures` stored files must still produce measures --------
    recomputed = _summary_from_stored(current)
    assert "measures" in recomputed
    # current's three findings (vulnerability/sast/misconfig) are all SECURITY_CATEGORIES.
    assert recomputed["measures"]["security"]["worst_open_severity"] == "high"
    assert recomputed["measures"]["duplication_percent"] == current["metrics"]["duplication"]["percent"]
    assert recomputed["measures"]["coverage_line_rate"] == 85.0

    # --- profiles ---------------------------------------------------------------------------------
    assert [t.name for t in select_tools("quality", None, True)] == ["scc", "lizard", "jscpd", "interrogate", "dotnet-docs"]
    assert "trivy" not in [t.name for t in select_tools("quality", None, True)]
    assert [t.name for t in select_tools("security", None, True)] == ["semgrep", "trivy", "gitleaks", "osv-scanner", "checkov"]
    assert [t.name for t in select_tools("full", ["jscpd"], True)] == ["jscpd"], "explicit tools= must win"
    assert "scc" not in [t.name for t in select_tools("full", None, False)]
    assert "git-churn" not in [t.name for t in select_tools("full", None, False)]
    assert "dotnet-docs" not in [t.name for t in select_tools("full", None, False)]
    assert "syft" not in [t.name for t in select_tools("full", None, False)]
    assert "syft" in [t.name for t in select_tools("full", None, True)], "syft is full-profile-only, like git-churn"
    try:
        select_tools("full", ["nope"], True)
        raise AssertionError("unknown tool must raise")
    except ValueError:
        pass

    # Licence bar: semgrep is the one declared exception, and it must be declared.
    non_permissive = [t.name for t in TOOLS if not t.permissive]
    assert non_permissive == ["semgrep"], non_permissive
    assert all("--config auto" not in t.command for t in TOOLS), "no network rule fetch"
    assert "--skip-db-update" in TOOLS_BY_NAME["trivy"].command
    assert "--offline-vulnerabilities" in TOOLS_BY_NAME["osv-scanner"].command

    # Non-application paths never gate. Each of these actually gated a real run.
    assert is_non_application_path("agent-work/gitleaks.json")          # 48 of that run's 68
    assert is_non_application_path("apps/web/.playwright-browsers/chromium-1234/x/main.js")
    assert is_non_application_path("apps/api.Tests/bin/Debug/net10.0/Api.Tests.deps.json")
    assert is_non_application_path("apps/api.Tests/obj/project.assets.json")
    assert is_non_application_path("apps/web/node_modules/left-pad/index.js")
    assert is_non_application_path(".ai-dev-workflow/history/x-report.json")
    # Real application code still gates -- this must not become a blanket amnesty.
    assert not is_non_application_path("apps/web/src/app/page.tsx")
    assert not is_non_application_path("apps/api/Program.cs")
    assert not is_non_application_path("apps/web/package.json")
    assert not is_non_application_path("src/binary_search.py")   # 'bin' must not match inside a word
    assert not is_non_application_path("apps/api/Services/Outbox.cs")  # nor 'out'
    assert not is_non_application_path(None)

    _noisy = Finding(
        finding_key="k1", tool="checkov", rule_id="CKV_SECRET_6", severity="high",
        raw_severity="HIGH", file="agent-work/gitleaks.json", line=29,
        message="Base64 High Entropy String", category="misconfig",
        title="Base64 High Entropy String", severity_source="native", sources=("checkov",),
    )
    assert not is_gating(_noisy, severity_floor="medium", introduced_ids=None)
    _real = replace(_noisy, finding_key="k2", file="apps/web/src/app/page.tsx")
    assert is_gating(_real, severity_floor="medium", introduced_ids=None)

    # Advisory (portability/i18n) rules report but never block -- 7 of one run's gating findings
    # were a single semgrep i18n rule firing on untranslated JSX text in a counter demo.
    assert is_advisory_rule(
        "opt.aidw.semgrep-rules.typescript.react.portability.i18next.jsx-not-internationalized"
    )
    assert not is_advisory_rule("python.lang.security.audit.dangerous-subprocess-use")
    assert not is_advisory_rule(None)
    _i18n = replace(_real, finding_key="k3", category="sast",
                    rule_id="opt.aidw.semgrep-rules.typescript.react.portability.i18next.jsx-not-internationalized")
    assert not is_gating(_i18n, severity_floor="medium", introduced_ids=None)
    # A REAL security rule in the same file still gates -- this is not a blanket sast exemption.
    _sec = replace(_real, finding_key="k4", category="sast", rule_id="typescript.react.security.audit.react-dangerouslysetinnerhtml")
    assert is_gating(_sec, severity_floor="medium", introduced_ids=None)

    # Transitive licence obligations are advisory; a licence on the manifest still gates.
    assert is_transitive_dependency_file("apps/web/package-lock.json")
    assert is_transitive_dependency_file("poetry.lock") and is_transitive_dependency_file("go.sum")
    assert not is_transitive_dependency_file("apps/web/package.json")
    _lic = replace(_noisy, finding_key="k5", category="license", rule_id="LGPL-3.0-or-later",
                   file="apps/web/package-lock.json", severity="high")
    assert not is_gating(_lic, severity_floor="medium", introduced_ids=None)
    assert is_gating(replace(_lic, finding_key="k6", file="apps/web/package.json"),
                     severity_floor="medium", introduced_ids=None)
    # The generic npm dependency lint is advisory (6 identical copies gated one run).
    assert is_advisory_rule("opt.aidw.semgrep-rules.json.npm.security.package-dependencies-check")
    # Quality findings respect the severity floor, exactly as security ones do.
    _low_quality = replace(_noisy, finding_key="k7", category="maintainability",
                           rule_id="docstring-coverage-under-threshold", severity="low",
                           file="apps/api/Program.cs")
    assert not is_gating(_low_quality, severity_floor="medium", introduced_ids=None)
    assert is_gating(replace(_low_quality, finding_key="k8", severity="high"),
                     severity_floor="medium", introduced_ids=None)

    print("repo_scan self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
