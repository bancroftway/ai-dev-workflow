"""Shared suppression-ledger mechanics for P8 and P10: writing `.ai-dev-workflow/suppressions.md`
rows from deterministic Finding data (never from the model's free-text alone), and the
no-silent-suppression gate that rejects any suppression marker in the diff without a matching
ledger row.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

from .. import git_ops, repo_files
from ..sandbox.provider import SandboxProvider
from .sarif import Finding

SUPPRESSIONS_PATH = ".ai-dev-workflow/suppressions.md"

_HEADER = """# Suppressions

Each row is a justified suppression of a real scan finding, recorded here so the no-silent-
suppression gate can verify every suppression marker in the codebase traces back to a real,
justified decision -- never a bare pragma with no accountability trail.

| Ref | Stage | Tool | Rule | File | Severity | Justification |
|---|---|---|---|---|---|---|
"""

# Suppression-marker prefixes per tool family, each expected to carry a `ref:<hex>` token pointing
# at this ledger's own row -- e.g. `// nosemgrep: rule-id -- ref:ab12cd34ef56`.
_MARKER_PATTERNS = [
    re.compile(r"//\s*nosemgrep:[^\n]*"),
    re.compile(r"#\s*nosec[^\n]*"),
    re.compile(r"//\s*ReSharper disable[^\n]*"),
    re.compile(r"#pragma warning disable[^\n]*"),
    re.compile(r"//\s*jscpd:ignore[^\n]*"),
]
_REF_TOKEN_RE = re.compile(r"ref:([0-9a-f]{6,})")


@dataclass(frozen=True)
class SuppressionRow:
    ref: str
    stage: str
    tool: str
    rule_id: str
    file: str
    severity: str
    justification: str


def new_ref() -> str:
    return secrets.token_hex(6)


async def append_suppression(
    provider: SandboxProvider, thread_id: str, stage: str, finding: Finding, justification: str
) -> str:
    """Appends one suppression row, returns the `ref` token to embed in the actual suppression
    marker the fix node inserts."""
    ref = new_ref()
    existing = await repo_files.read_repo_file(provider, thread_id, SUPPRESSIONS_PATH)
    row = f"| {ref} | {stage} | {finding.tool} | {finding.rule_id} | {finding.file} | {finding.severity} | {justification} |\n"
    # read_repo_file strips the trailing newline, so a bare concat fuses this row onto the previous
    # row's line and load_suppression_refs then mis-reads the ref. Guarantee a newline boundary.
    base = existing if existing is not None else _HEADER
    if base and not base.endswith("\n"):
        base += "\n"
    await repo_files.write_repo_file(provider, thread_id, SUPPRESSIONS_PATH, base + row)
    return ref


async def load_suppression_refs(provider: SandboxProvider, thread_id: str) -> set[str]:
    raw = await repo_files.read_repo_file(provider, thread_id, SUPPRESSIONS_PATH)
    if raw is None:
        return set()
    refs: set[str] = set()
    for line in raw.splitlines():
        if line.startswith("| ") and not line.startswith("| Ref") and not line.startswith("|---"):
            parts = [p.strip() for p in line.strip("|").split("|")]
            if parts:
                refs.add(parts[0])
    return refs


@dataclass(frozen=True)
class NoSilentSuppressionResult:
    passed: bool
    bare_markers: list[str]
    dangling_refs: list[str]


async def check_no_silent_suppression(
    provider: SandboxProvider, thread_id: str, baseline_commit: str
) -> NoSilentSuppressionResult:
    """Diffs added lines since `baseline_commit`, scans for known suppression-marker prefixes, and
    fails if any marker lacks a `ref:<hex>` token, or if a `ref:` token doesn't match a real
    ledger row -- catches both a bare pragma with no accountability and a fabricated/typo'd ref.
    """
    result = await provider.exec_in_sandbox(thread_id, f"git diff {baseline_commit} -- .")
    diff_text = result.stdout or ""
    added_lines = [line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")]

    known_refs = await load_suppression_refs(provider, thread_id)
    bare_markers: list[str] = []
    dangling_refs: list[str] = []

    for line in added_lines:
        for pattern in _MARKER_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            marker_text = match.group(0)
            ref_match = _REF_TOKEN_RE.search(marker_text)
            if ref_match is None:
                bare_markers.append(marker_text.strip())
            elif ref_match.group(1) not in known_refs:
                dangling_refs.append(marker_text.strip())

    return NoSilentSuppressionResult(passed=not bare_markers and not dangling_refs, bare_markers=bare_markers, dangling_refs=dangling_refs)
