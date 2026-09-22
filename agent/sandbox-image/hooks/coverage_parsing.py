"""Pure, minimal-dependency (`defusedxml` -- now baked into the sandbox image specifically for
this file, see Dockerfile's DEFUSEDXML_VERSION; a plain `dataclass`, not `pydantic`, needs no such
install) coverage-artifact parsing -- extracted from `test_coverage_gate.py` (2026-09-21) so the
sandbox's own same-turn Stop hook (sandbox-image/hooks/check-coverage-stop.mjs) can run the REAL
threshold check by shelling out to `python3` on this ONE file, instead of a hand-ported JavaScript
reimplementation drifting from it -- the same rationale as `test_quality_checks.py`'s own
extraction; see that module's docstring for the full argument.

Uses `defusedxml.ElementTree`, matching `test_coverage_gate.py`'s own choice exactly rather than
substituting stdlib `xml.etree` -- even though the XML this module parses is a coverage artifact
the SAME sandboxed test run just generated for itself (`_replay_coverage_contract`'s own
delete-then-rerun recipe), not input from outside this sandbox, a compromised test-toolchain
dependency (`pytest-cov`/`coverlet`/etc.) could still craft a malicious artifact, and the parser
duplicating in two different XML libraries would itself be exactly the kind of drift this
extraction pattern exists to prevent. `CoverageContractEntry` is a plain `dataclass` instead of
`pydantic.BaseModel` (manual validation in `_load_coverage_contract` replaces pydantic's
constructor-time checks one-for-one) since pydantic genuinely isn't installed in the sandbox and
adding it there would cost far more than this one small model is worth.

`test_coverage_gate.py` imports every name below UNCHANGED (this is a pure code-move for the
parsing/contract/merge logic, not a fork); its own self-check is the proof this extraction changed
no behavior. `CoverageEntry` (the pydantic model used for the coverage-run LLM's structured output
schema) intentionally stays in `test_coverage_gate.py` -- that one genuinely needs pydantic for
schema generation, and never runs inside the sandbox.

CLI mode (`python3 coverage_parsing.py --check-hook`, stdin: JSON `{"entries": [{"format":
"cobertura"|"istanbul-json-summary", "content": "<raw artifact text>"|null, "error":
"<optional pre-computed error, e.g. a non-zero replay exit code>"}, ...]}`, stdout: JSON
`{"line_rate": float|null, "branch_rate": float|null, "gaps": [{"file":..., "line_rate":...,
"branch_rate":...}, ...], "errors": [str, ...]}`) is what the Stop hook actually invokes.
`line_rate`/`branch_rate` are null when no entry parsed to anything usable -- the hook must fail
OPEN in that case (this is exactly "contract replay produced no usable artifact", the same
condition the real gate already re-discovers from, not a coverage shortfall to block on).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import defusedxml.ElementTree as ET

# Same env var name as test_coverage_gate.py's own config.MIN_COVERAGE_PERCENT read (AGENTS.md's
# own rule: two reads of the same env var, not a second knob) -- kept in sync by that identity,
# not by import, since config.py itself pulls in dependencies this sandbox-side module must not.
MIN_COVERAGE_PERCENT = float(os.environ.get("MIN_COVERAGE_PERCENT", "95.0"))

_CONTRACT_FORMATS = frozenset({"cobertura", "istanbul-json-summary"})

# Files no model authored and no test can meaningfully cover: compiler/source-generator output.
# Copied unchanged from test_coverage_gate.py -- see that module's own comment for the live
# incident (RegexGenerator.g.cs) that motivated it.
_GENERATED_FILE_RE = re.compile(
    r"(^|[/\\])(obj|bin|node_modules|\.next|dist|build)([/\\])"
    r"|\.(g|g\.i|generated|designer)\.(cs|ts|js)$"
    r"|\.d\.ts$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CoverageGap:
    file: str
    line_rate: float
    branch_rate: float


@dataclass(frozen=True)
class _Counts:
    """Covered/total counts, mergeable across stacks (rates are not -- a 10-line worker and a
    10k-line app would weigh equally)."""

    lines_covered: int
    lines_total: int
    branches_covered: int
    branches_total: int
    gaps: list[CoverageGap] = field(default_factory=list)


@dataclass(frozen=True)
class CoverageContractEntry:
    """One test root that was actually run with coverage -- the plain-dataclass twin of
    test_coverage_gate.py's pydantic `CoverageEntry`, used wherever validation doesn't need a
    schema (contract-file parsing, the sandbox-side hook)."""

    root: str
    command: str
    artifact: str
    format: str


def _parse_cobertura_counts(raw_xml: str) -> tuple[_Counts | None, str]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None, "artifact failed to parse as Cobertura XML"
    try:
        lc, lt = int(root.get("lines-covered", "0")), int(root.get("lines-valid", "0"))
        bc, bt = int(root.get("branches-covered", "0")), int(root.get("branches-valid", "0"))
    except ValueError:
        return None, "Cobertura root counters are not integers"
    if lt == 0:
        return None, "Cobertura artifact reports zero valid lines -- nothing was instrumented"
    gaps: list[CoverageGap] = []
    for cls in root.iter("class"):
        cls_line_rate = float(cls.get("line-rate", "1")) * 100
        covered_branches = total_branches = 0
        cls_lines_covered = cls_lines_total = 0
        uncovered_lines: list[str] = []
        seen_lines: set[str] = set()
        for line in cls.iter("line"):
            if (num := line.get("number")) is not None:
                if num in seen_lines:
                    continue
                seen_lines.add(num)
            cls_lines_total += 1
            try:
                if int(line.get("hits", "0") or "0") > 0:
                    cls_lines_covered += 1
            except ValueError:
                pass
            if (line.get("branch") or "").lower() != "true":
                continue
            match = re.search(r"\((\d+)/(\d+)\)", line.get("condition-coverage", ""))
            if not match:
                continue
            hit, total = int(match.group(1)), int(match.group(2))
            covered_branches += hit
            total_branches += total
            if hit < total and (number := line.get("number")) and number not in uncovered_lines:
                uncovered_lines.append(number)
        name = cls.get("filename", cls.get("name", "?"))
        if _GENERATED_FILE_RE.search(name):
            lc -= cls_lines_covered
            lt -= cls_lines_total
            bc -= covered_branches
            bt -= total_branches
            continue
        cls_branch_rate = (100.0 * covered_branches / total_branches) if total_branches else 100.0
        if cls_line_rate < MIN_COVERAGE_PERCENT or cls_branch_rate < MIN_COVERAGE_PERCENT:
            if uncovered_lines:
                name = f"{name} (partially-covered branch lines: {', '.join(uncovered_lines[:20])})"
            gaps.append(CoverageGap(file=name, line_rate=cls_line_rate, branch_rate=cls_branch_rate))
    if lt <= 0:
        return None, "every instrumented line is in generated code -- nothing authored was measured"
    return _Counts(lc, lt, max(bc, 0), max(bt, 0), gaps), ""


def _parse_istanbul_counts(raw: str) -> tuple[_Counts | None, str]:
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError:
        return None, "artifact failed to parse as istanbul json-summary"

    def _count(entry: dict, key: str, field_name: str) -> int:
        try:
            return int(entry.get(key, {}).get(field_name, 0))
        except (TypeError, ValueError):
            return 0

    total = summary.get("total", {})
    lt, lc = _count(total, "lines", "total"), _count(total, "lines", "covered")
    bt, bc = _count(total, "branches", "total"), _count(total, "branches", "covered")
    if lt == 0:
        return None, "istanbul summary reports zero total lines -- nothing was instrumented"

    def _pct(entry: dict, key: str, default: float) -> float:
        try:
            return float(entry.get(key, {}).get("pct", default))
        except (TypeError, ValueError):
            return default

    gaps: list[CoverageGap] = []
    for file_path, entry in summary.items():
        if file_path == "total" or not isinstance(entry, dict):
            continue
        file_line_rate = _pct(entry, "lines", 100)
        file_branch_rate = 100.0 if _count(entry, "branches", "total") == 0 else _pct(entry, "branches", 100)
        if file_line_rate < MIN_COVERAGE_PERCENT or file_branch_rate < MIN_COVERAGE_PERCENT:
            gaps.append(CoverageGap(file=file_path, line_rate=file_line_rate, branch_rate=file_branch_rate))
    return _Counts(lc, lt, bc, bt, gaps), ""


def _load_coverage_contract(raw: str | None) -> list[CoverageContractEntry]:
    """Validated entries from a committed coverage-commands.json, or [] when absent/unusable.
    Pure. Every entry must carry a non-empty command, a repo-relative artifact path and a known
    format -- anything else means the contract is not replayable and discovery must run. Manual
    validation replaces pydantic's constructor-time checks (no pydantic in the sandbox)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    entries: list[CoverageContractEntry] = []
    for item in (parsed or {}).get("entries") or []:
        if not isinstance(item, dict):
            return []
        command = item.get("command")
        artifact = item.get("artifact")
        fmt = item.get("format")
        root = item.get("root", "")
        if not isinstance(command, str) or not isinstance(artifact, str) or not isinstance(fmt, str) or not isinstance(root, str):
            return []
        if not command.strip() or not artifact.strip() or fmt not in _CONTRACT_FORMATS:
            return []
        if artifact.startswith("/") or ".." in artifact.split("/"):
            return []
        entries.append(CoverageContractEntry(root=root, command=command, artifact=artifact, format=fmt))
    return entries


def merge_counts(counts_list: list[_Counts]) -> tuple[float, float, list[CoverageGap]]:
    """Line-weighted merge across every replayed entry -- same arithmetic as
    test_coverage_gate.py's own `_run_coverage_via_ghcp` aggregation, extracted so the hook and
    the host gate can never compute the aggregate two different ways."""
    lines_total = sum(c.lines_total for c in counts_list)
    lines_covered = sum(c.lines_covered for c in counts_list)
    branches_total = sum(c.branches_total for c in counts_list)
    branches_covered = sum(c.branches_covered for c in counts_list)
    line_rate = 100.0 * lines_covered / lines_total if lines_total else 0.0
    # No branch points anywhere is vacuously satisfied, not a 0% failure -- matches
    # test_coverage_gate.py's own aggregation exactly (the one place this module's logic is a
    # pure extraction, not a fork: this arithmetic must never read differently in two places).
    branch_rate = 100.0 * branches_covered / branches_total if branches_total else 100.0
    gaps = [g for c in counts_list for g in c.gaps]
    return line_rate, branch_rate, gaps


def evaluate_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The Stop hook's actual question: given what each replayed command's artifact contained
    (or the error that kept it from having one), is coverage over threshold? Returns
    line_rate/branch_rate as None when nothing usable parsed at all -- the hook's own contract is
    to fail OPEN on that (matches the real gate's re-discovery path), never to block on it."""
    counts: list[_Counts] = []
    errors: list[str] = []
    for entry in entries:
        pre_error = entry.get("error")
        if pre_error:
            errors.append(str(pre_error))
            continue
        content = entry.get("content")
        fmt = entry.get("format")
        if not content:
            errors.append(f"no artifact content for format={fmt!r}")
            continue
        parsed, parse_error = (
            _parse_cobertura_counts(content) if fmt == "cobertura" else _parse_istanbul_counts(content)
        )
        if parsed is None:
            errors.append(parse_error)
            continue
        counts.append(parsed)

    if not counts:
        return {"line_rate": None, "branch_rate": None, "gaps": [], "errors": errors}

    line_rate, branch_rate, gaps = merge_counts(counts)
    return {
        "line_rate": line_rate,
        "branch_rate": branch_rate,
        "gaps": [{"file": g.file, "line_rate": g.line_rate, "branch_rate": g.branch_rate} for g in gaps],
        "errors": errors,
    }


def _demo() -> None:  # pragma: no cover -- `cd agent && python -m src.gates.coverage_parsing`
    from pathlib import Path

    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "coverage_parsing.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            "sandbox-image/hooks/coverage_parsing.py has drifted from src/gates/coverage_parsing.py "
            "-- re-sync with: cp src/gates/coverage_parsing.py sandbox-image/hooks/coverage_parsing.py"
        )

    cobertura_ok = (
        '<coverage lines-covered="9" lines-valid="10" branches-covered="1" branches-valid="2">'
        '<packages><package><classes>'
        '<class filename="a.py" line-rate="0.9">'
        '<lines><line number="1" hits="1" branch="true" condition-coverage="50% (1/2)"/></lines>'
        "</class>"
        "</classes></package></packages></coverage>"
    )
    counts, err = _parse_cobertura_counts(cobertura_ok)
    assert counts is not None and err == ""
    assert counts.lines_covered == 9 and counts.lines_total == 10
    assert counts.branches_covered == 1 and counts.branches_total == 2
    assert len(counts.gaps) == 1 and counts.gaps[0].file.startswith("a.py")

    generated = cobertura_ok.replace('filename="a.py"', 'filename="dist/bundle.g.ts"')
    counts2, _ = _parse_cobertura_counts(generated)
    assert counts2 is not None and counts2.gaps == [], "generated-code classes must not appear as gaps"

    bad_xml_counts, bad_xml_err = _parse_cobertura_counts("<not-xml")
    assert bad_xml_counts is None and "Cobertura" in bad_xml_err

    istanbul_ok = json.dumps({
        "total": {"lines": {"total": 10, "covered": 10, "pct": 100}, "branches": {"total": 0, "covered": 0, "pct": 100}},
        "src/ok.ts": {"lines": {"total": 5, "covered": 5, "pct": 100}, "branches": {"total": 0, "covered": 0, "pct": 100}},
        "src/bad.ts": {"lines": {"total": 5, "covered": 2, "pct": 40}, "branches": {"total": 0, "covered": 0, "pct": 100}},
    })
    icounts, ierr = _parse_istanbul_counts(istanbul_ok)
    assert icounts is not None and ierr == ""
    assert icounts.lines_covered == 10 and icounts.lines_total == 10
    assert len(icounts.gaps) == 1 and icounts.gaps[0].file == "src/bad.ts"

    contract_raw = json.dumps({
        "entries": [
            {"root": "apps/web", "command": "npx vitest run --coverage", "artifact": "apps/web/coverage/coverage-summary.json", "format": "istanbul-json-summary"},
            {"root": "apps/api", "command": "pytest --cov=.", "artifact": "apps/api/coverage.xml", "format": "cobertura"},
        ]
    })
    entries = _load_coverage_contract(contract_raw)
    assert len(entries) == 2 and entries[0].format == "istanbul-json-summary" and entries[1].root == "apps/api"

    assert _load_coverage_contract(json.dumps({"entries": [{"root": "", "command": "", "artifact": "x", "format": "cobertura"}]})) == [], (
        "an empty command must invalidate the whole contract"
    )
    assert _load_coverage_contract(json.dumps({"entries": [{"root": "", "command": "x", "artifact": "../escape", "format": "cobertura"}]})) == [], (
        "an artifact path escaping the repo must invalidate the whole contract"
    )
    assert _load_coverage_contract(None) == [] and _load_coverage_contract("not json") == []

    line_rate, branch_rate, gaps = merge_counts([counts, icounts])
    assert round(line_rate, 2) == round(100.0 * (9 + 10) / (10 + 10), 2)
    assert len(gaps) == 2  # one from each entry's own gaps

    zero_branch_line_rate, zero_branch_rate, _ = merge_counts([_Counts(5, 5, 0, 0, [])])
    assert zero_branch_line_rate == 100.0 and zero_branch_rate == 100.0, (
        "an aggregate with zero branch points anywhere must read as vacuously satisfied (100.0), "
        "not a 0% failure -- this must match test_coverage_gate.py's own merge exactly"
    )

    cobertura_full = (
        '<coverage lines-covered="10" lines-valid="10" branches-covered="2" branches-valid="2">'
        '<packages><package><classes>'
        '<class filename="a.py" line-rate="1.0">'
        '<lines><line number="1" hits="1" branch="true" condition-coverage="100% (2/2)"/></lines>'
        "</class>"
        "</classes></package></packages></coverage>"
    )
    passing = evaluate_entries([{"format": "cobertura", "content": cobertura_full}])
    assert passing["line_rate"] == 100.0 and passing["branch_rate"] == 100.0 and passing["gaps"] == [], passing

    no_artifact = evaluate_entries([{"format": "cobertura", "content": None, "error": "no artifact at coverage.xml"}])
    assert no_artifact["line_rate"] is None and no_artifact["branch_rate"] is None
    assert no_artifact["errors"] == ["no artifact at coverage.xml"], "fail-open case: nothing usable parsed"

    print("coverage_parsing self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"entries": [...]} on stdin, JSON result on
        # stdout. Deliberately the ONLY thing this branch does -- no sandbox access beyond what
        # the hook already ran and handed over as data.
        payload = json.loads(sys.stdin.read())
        json.dump(evaluate_entries(payload.get("entries") or []), sys.stdout)
    else:
        _demo()
