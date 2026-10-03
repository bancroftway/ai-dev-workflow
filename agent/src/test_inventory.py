"""Whole-repo test inventory for the Tests tab and the code-gen prompt.

Every test in the tree, grouped under the approved Specification's stories and acceptance criteria
(the Specification tab's own layout and `change` stamps), each test stamped with what this run did
to it relative to a baseline commit: new / modified / deleted / unchanged. Built deterministically
from git and the files themselves, never from the model's own claim about what it wrote (the
ac-to-tests `test_files` list is model-declared and covers only this run's work).

Built by the r_ac_to_tests rebuild node (right after ac-to-tests, in every code-gen mode -- its
verify is "off" in YOLO) and again by metrics_compute at the end of the run, since later stages edit
tests too. Stored whole as GraphState.test_inventory; the frontend paints it as-is.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections import Counter
from typing import Any

from . import repo_files, workflow_persistence
from .gates.ac_residue_checks import TEST_DECL_CANDIDATE_ERE, _TEST_FILE_LISTING, _TEST_PATH_FILTER, test_declarations
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Header line the declaration scan prints before each file's grep output. grep's own lines always
# start with a line number or "--", so this can never collide with file content.
_FILE_MARK = "@@FILE@@ "
_GREP_LINE_RE = re.compile(r"^(\d+)[:-](.*)$")

Test = dict[str, Any]  # {path, name, change, ac_ids, line}


def _parse_decl_scan(stdout: str) -> dict[str, list[tuple[int, str]]]:
    """{path: [(line number, text)]} from the per-file `grep -n -B2` scan. Every listed file gets an
    entry, even one with no candidate lines, so "scanned but nothing recognised" stays visible."""
    files: dict[str, list[tuple[int, str]]] = {}
    current: list[tuple[int, str]] | None = None
    for line in stdout.splitlines():
        if line.startswith(_FILE_MARK):
            current = files.setdefault(line[len(_FILE_MARK):], [])
            continue
        match = _GREP_LINE_RE.match(line)
        if current is not None and match:
            current.append((int(match.group(1)), match.group(2)))
    return files


def _parse_status(stdout: str) -> dict[str, str]:
    """{path: 'A'|'M'|'D'} from the sectioned status listing (untracked counts as added)."""
    status: dict[str, str] = {}
    section = ""
    for line in stdout.splitlines():
        if line.startswith("@@"):
            section = "A" if line[2:] == "U" else line[2:]
        elif line.strip() and section:
            status[line.strip()] = section
    return status


def _tests_with_bodies(content: str) -> list[tuple[str, list[str], str, int]]:
    """(name, ac_ids, normalised body, line) per test in a whole file. The body runs from the
    declaration line (with the name itself blanked out -- a one-line test's whole body sits on it)
    up to the next declaration."""
    lines = content.splitlines()
    decls = test_declarations(list(enumerate(lines, start=1)))
    out = []
    for i, (line_no, name, ids) in enumerate(decls):
        end = decls[i + 1][0] - 1 if i + 1 < len(decls) else len(lines)
        body = "\n".join([lines[line_no - 1].replace(name, "", 1), *lines[line_no:end]])
        out.append((name, ids, re.sub(r"\s+", " ", body).strip(), line_no))
    return out


def diff_file_tests(path: str, old: str, new: str) -> list[Test]:
    """Per-test change for one file present at both ends. Pure.

    Matched by (name, occurrence) first: same body -> unchanged, different -> modified. Then the
    leftovers pair up in order when they name the same non-empty AC-id set -- a reworded criterion's
    test usually gets a reworded title, and that is an update, not a delete plus a new test.
    ponytail: a test moved to another file still reads as deleted + new; pair across files if
    reviewers find that noisy."""

    def keyed(items: list[tuple[str, list[str], str, int]]) -> dict[tuple[str, int], tuple[list[str], str, int]]:
        seen: Counter[str] = Counter()
        out = {}
        for name, ids, body, line in items:
            out[(name, seen[name])] = (ids, body, line)
            seen[name] += 1
        return out

    before, after = keyed(_tests_with_bodies(old)), keyed(_tests_with_bodies(new))
    result: list[Test] = []
    for key, (ids, body, line) in after.items():
        if key in before:
            change = "unchanged" if before[key][1] == body else "modified"
            result.append({"path": path, "name": key[0], "change": change, "ac_ids": ids, "line": line})
    old_only = [(k, v) for k, v in before.items() if k not in after]
    for key, (ids, _body, line) in ((k, v) for k, v in after.items() if k not in before):
        partner = next((i for i, (_k, v) in enumerate(old_only) if ids and v[0] == ids), None)
        change = "new" if partner is None else "modified"
        if partner is not None:
            old_only.pop(partner)
        result.append({"path": path, "name": key[0], "change": change, "ac_ids": ids, "line": line})
    for key, (ids, _body, line) in old_only:
        result.append({"path": path, "name": key[0], "change": "deleted", "ac_ids": ids, "line": line})
    return result


def _story_card(
    story_id: str, title: str, change: str, deferred: bool, retired: bool, criteria: list[dict[str, Any]]
) -> dict[str, Any]:
    quiet = change in ("unchanged", "") and not retired and all(
        c["change"] == "unchanged" and all(t["change"] == "unchanged" for t in c["tests"]) for c in criteria
    )
    return {
        "id": story_id, "title": title, "change": change or "unchanged", "deferred": deferred,
        "retired": retired, "collapsed": quiet, "criteria": criteria,
    }


def group_by_spec(tests: list[Test], spec_doc: dict[str, Any]) -> dict[str, Any]:
    """Stories -> criteria -> tests, in the approved Specification's own order and with its own
    `change` stamps (so a criterion's badge here matches the Specification tab). Pure.

    Criteria retired THIS round are shown crossed out with the tests they lost; an earlier retired
    criterion is shown only while some test still names it. A test naming several criteria is
    listed under each; one naming none the Specification knows goes to `unattributed`."""

    def under(ac_id: str) -> list[dict[str, str]]:
        hits = [t for t in tests if ac_id in t["ac_ids"]]
        hits.sort(key=lambda t: (t["change"] == "deleted", t["path"], t["line"]))
        return [{"path": t["path"], "name": t["name"], "change": t["change"]} for t in hits]

    retired_now = set(spec_doc.get("retired_ac_ids") or [])
    retired_stories_now = set(spec_doc.get("retired_us_ids") or [])
    retired_acs: dict[str, list[dict[str, Any]]] = {}
    for ac in spec_doc.get("retired_acceptance_criteria") or []:
        ac_id = ac.get("id") or ""
        tests_here = under(ac_id)
        if ac_id in retired_now or ac.get("parent_us_id") in retired_stories_now or tests_here:
            retired_acs.setdefault(ac.get("parent_us_id") or "", []).append({
                "id": ac_id, "description": ac.get("description", ""), "change": "deleted",
                "deferred": False, "retired": True, "tests": tests_here,
            })

    known: set[str] = {ac.get("id") or "" for ac in spec_doc.get("retired_acceptance_criteria") or []}
    stories = []
    for story in spec_doc.get("user_stories") or []:
        criteria = []
        for ac in story.get("acceptance_criteria") or []:
            known.add(ac["id"])
            criteria.append({
                "id": ac["id"], "description": ac.get("description", ""), "change": ac.get("change") or "unchanged",
                "deferred": bool(ac.get("deferred") or story.get("deferred")), "retired": False, "tests": under(ac["id"]),
            })
        criteria += retired_acs.pop(story["id"], [])
        stories.append(_story_card(
            story["id"], story.get("title", ""), story.get("change") or "unchanged", bool(story.get("deferred")), False, criteria
        ))
    retired_titles = {s.get("id"): s.get("title", "") for s in spec_doc.get("retired_user_stories") or []}
    for story_id, criteria in retired_acs.items():
        stories.append(_story_card(story_id, retired_titles.get(story_id, story_id), "deleted", False, True, criteria))

    unattributed = sorted(
        (t for t in tests if not set(t["ac_ids"]) & known),
        key=lambda t: (t["change"] == "deleted", t["path"], t["line"]),
    )
    return {
        "stories": stories,
        "unattributed": [{"path": t["path"], "name": t["name"], "change": t["change"]} for t in unattributed],
        "unattributed_collapsed": all(t["change"] == "unchanged" for t in unattributed),
    }


async def build_test_inventory(
    provider: SandboxProvider, thread_id: str, baseline_commit: str | None, *, stage_key: str, as_of_label: str
) -> dict[str, Any] | None:
    """The Tests tab's view model, or None when there is no baseline to diff against or the scan
    itself failed (the caller keeps whatever inventory it already had).

    Two execs cover every unchanged file -- a status listing and one in-sandbox loop that greps each
    test file for declaration candidates -- so the uncapped scan costs O(1) round trips, not one
    per file. Only added-at-both-ends (modified) and deleted files are read whole."""
    if not baseline_commit:
        return None
    base = shlex.quote(baseline_commit)
    status_cmd = "".join(
        f"echo @@{s}; git diff --name-only --no-renames --diff-filter={s} {base} -- . | {_TEST_PATH_FILTER}; "
        for s in ("A", "M", "D")
    ) + f"echo @@U; git ls-files --others --exclude-standard | {_TEST_PATH_FILTER}; true"
    status_result = await provider.exec_in_sandbox(thread_id, status_cmd)
    scan_result = await provider.exec_in_sandbox(
        thread_id,
        f"{_TEST_FILE_LISTING} | while IFS= read -r f; do printf '{_FILE_MARK}%s\\n' \"$f\"; "
        f"grep -n -i -E -B2 -- {shlex.quote(TEST_DECL_CANDIDATE_ERE)} \"$f\" 2>/dev/null; done; true",
    )
    if not (status_result.ok and scan_result.ok):
        logger.warning("test inventory: scan failed for thread %s", thread_id)
        return None
    status = _parse_status(status_result.stdout or "")
    scanned = _parse_decl_scan(scan_result.stdout or "")

    tests: list[Test] = []
    without_tests: list[str] = []
    for path, lines in sorted(scanned.items()):
        if status.get(path) == "M":
            continue
        decls = test_declarations(lines)
        if not decls:
            without_tests.append(path)
        change = "new" if status.get(path) == "A" else "unchanged"
        tests += [{"path": path, "name": n, "change": change, "ac_ids": ids, "line": ln} for ln, n, ids in decls]
    for path, kind in sorted(status.items()):
        if kind not in ("M", "D"):
            continue
        repo_files.validate_repo_relative_path(path)
        old = await provider.exec_in_sandbox(thread_id, f"git show {base}:{shlex.quote(path)}")
        if kind == "D":
            tests += [
                {"path": path, "name": n, "change": "deleted", "ac_ids": ids, "line": ln}
                for n, ids, _body, ln in _tests_with_bodies(old.stdout or "")
            ]
            continue
        new = await repo_files.read_repo_file(provider, thread_id, path)
        file_tests = diff_file_tests(path, old.stdout or "", new or "")
        if not file_tests:
            without_tests.append(path)
        tests += file_tests

    spec_doc: dict[str, Any] = {}
    raw_spec = await repo_files.read_repo_file(provider, thread_id, workflow_persistence.SPECIFICATION_APPROVED_PATH)
    if raw_spec:
        try:
            spec_doc = json.loads(raw_spec)
        except json.JSONDecodeError:
            logger.warning("test inventory: approved specification unparseable for thread %s", thread_id)
    counts = Counter(t["change"] for t in tests)
    return {
        "stage_key": stage_key,
        "as_of_label": as_of_label,
        "counts": {k: counts.get(k, 0) for k in ("new", "modified", "deleted", "unchanged")},
        "files_scanned": len(scanned),
        "files_without_recognised_tests": sorted(without_tests),
        **group_by_spec(tests, spec_doc),
    }


def changes_summary(inventory: dict[str, Any] | None) -> str | None:
    """Plain-text list of this run's new / updated / deleted tests (unchanged ones as a count), for
    the code-gen prompt; None when there is no inventory or nothing changed. Each test once, even
    when it is listed under several criteria."""
    if not inventory:
        return None
    by_change: dict[str, dict[tuple[str, str], set[str]]] = {"new": {}, "modified": {}, "deleted": {}}
    for story in inventory.get("stories") or []:
        for crit in story.get("criteria") or []:
            for t in crit.get("tests") or []:
                if t["change"] in by_change:
                    by_change[t["change"]].setdefault((t["path"], t["name"]), set()).add(crit["id"])
    for t in inventory.get("unattributed") or []:
        if t["change"] in by_change:
            by_change[t["change"]].setdefault((t["path"], t["name"]), set())
    if not any(by_change.values()):
        return None
    lines = []
    for change, heading in (("new", "New"), ("modified", "Updated"), ("deleted", "Deleted")):
        if by_change[change]:
            lines.append(f"{heading} ({len(by_change[change])}):")
            for (path, name), ids in sorted(by_change[change].items()):
                lines.append(f"- {path} :: {name}" + (f" ({', '.join(sorted(ids))})" if ids else ""))
    lines.append(f"Unchanged: {(inventory.get('counts') or {}).get('unchanged', 0)}")
    return "\n".join(lines)


def _demo() -> None:
    """`cd agent && uv run python -m src.test_inventory` -- fake sandbox, no network."""
    import asyncio

    # --- diff_file_tests: the four states, reword pairing, duplicate titles ------------------------
    old = (
        "test('[US-0001.1] adds a note', () => { expect(add()).toBe(1); });\n"
        "test('[US-0001.2] old wording', () => { expect(x).toBe(1); });\n"
        "test('[US-0002.1] retired behaviour', () => { expect(y).toBe(2); });\n"
        "test('dup', () => { a(); });\n"
        "test('dup', () => { b(); });\n"
    )
    new = (
        "test('[US-0001.1] adds a note', () => { expect(add()).toBe(1); });\n"
        "test('[US-0001.2] new wording', () => { expect(x).toBe(9); });\n"
        "test('dup', () => { a(); });\n"
        "test('dup', () => { c(); });\n"
        "test('[US-0003.1] brand new', () => { expect(z).toBe(3); });\n"
    )
    got = {(t["name"], t["change"]) for t in diff_file_tests("a.test.ts", old, new)}
    assert got == {
        ("[US-0001.1] adds a note", "unchanged"),
        ("[US-0001.2] new wording", "modified"),  # reworded title, same AC set -> paired
        ("dup", "unchanged"), ("dup", "modified"),  # second occurrence's body changed
        ("[US-0003.1] brand new", "new"),
        ("[US-0002.1] retired behaviour", "deleted"),
    }, got

    # --- group_by_spec: Spec order + stamps, retired criteria, unattributed ------------------------
    spec = {
        "user_stories": [{
            "id": "US-0001", "title": "Notes", "change": "modified",
            "acceptance_criteria": [
                {"id": "US-0001.1", "description": "add", "change": "unchanged"},
                {"id": "US-0001.2", "description": "reworded", "change": "modified"},
            ],
        }, {
            "id": "US-0004", "title": "Quiet", "change": "unchanged",
            "acceptance_criteria": [{"id": "US-0004.1", "description": "q", "change": "unchanged"}],
        }],
        "retired_ac_ids": ["US-0002.1"], "retired_us_ids": ["US-0002"],
        "retired_user_stories": [{"id": "US-0002", "title": "Old story"}],
        "retired_acceptance_criteria": [
            {"id": "US-0002.1", "description": "gone", "parent_us_id": "US-0002"},
            {"id": "US-0009.1", "description": "long gone, nothing names it", "parent_us_id": "US-0009"},
        ],
    }
    tests = diff_file_tests("a.test.ts", old, new) + [
        {"path": "b.test.ts", "name": "[US-0004.1] q", "change": "unchanged", "ac_ids": ["US-0004.1"], "line": 1},
        {"path": "h.test.ts", "name": "formats date", "change": "unchanged", "ac_ids": [], "line": 1},
    ]
    view = group_by_spec(tests, spec)
    assert [s["id"] for s in view["stories"]] == ["US-0001", "US-0004", "US-0002"], view["stories"]
    notes, quiet, retired = view["stories"]
    assert [c["id"] for c in notes["criteria"]] == ["US-0001.1", "US-0001.2"] and not notes["collapsed"]
    assert notes["criteria"][1]["tests"] == [{"path": "a.test.ts", "name": "[US-0001.2] new wording", "change": "modified"}]
    assert quiet["collapsed"] and not retired["collapsed"]
    assert retired["retired"] and retired["title"] == "Old story" and retired["criteria"][0]["tests"][0]["change"] == "deleted"
    # US-0003.1 is in no story of this Specification -- unattributed, same as a test naming nothing.
    assert {t["name"] for t in view["unattributed"]} == {"dup", "formats date", "[US-0003.1] brand new"}
    assert view["unattributed_collapsed"] is False, "a changed unattributed test keeps the card open"

    # --- build_test_inventory against a fake sandbox -----------------------------------------------
    class _R:
        def __init__(self, stdout: str = "", ok: bool = True) -> None:
            self.ok, self.stdout, self.stderr, self.returncode = ok, stdout, "", 0 if ok else 1

    files_now = {
        "a.test.ts": new,
        "b.test.ts": "test('[US-0004.1] q', () => {});\n",
        "h.test.ts": "export const helper = 1;\n",
        "Api.Tests/NotesTests.cs": '[Fact(DisplayName = "[US-0004.1] api")]\npublic void Api() { }\n',
    }
    files_base = {"a.test.ts": old, "gone.test.ts": "test('[US-0002.1] other', () => {});\n"}

    class _Fake:
        async def exec_in_sandbox(self, _thread_id: str, command: str) -> _R:
            if command.startswith("echo @@A"):
                return _R("@@A\nApi.Tests/NotesTests.cs\n@@M\na.test.ts\n@@D\ngone.test.ts\n@@U\nb.test.ts\n")
            if _FILE_MARK in command:
                out = []
                for path, content in files_now.items():
                    out.append(_FILE_MARK + path)
                    out += [f"{i}:{line}" for i, line in enumerate(content.splitlines(), start=1)]
                return _R("\n".join(out))
            if command.startswith("git show "):
                path = shlex.split(command)[2].split(":", 1)[1]
                return _R(files_base.get(path, ""))
            if command.startswith("cat "):
                path = shlex.split(command)[1]
                if path == workflow_persistence.SPECIFICATION_APPROVED_PATH:
                    return _R(json.dumps(spec))
                return _R(files_now[path]) if path in files_now else _R("", ok=False)
            return _R()

    inv = asyncio.run(build_test_inventory(_Fake(), "t", "base", stage_key="ac-to-tests", as_of_label="After tests"))  # type: ignore[arg-type]
    assert inv is not None
    assert inv["files_scanned"] == 4 and inv["files_without_recognised_tests"] == ["h.test.ts"], inv
    quiet_tests = {(t["name"], t["change"]) for t in inv["stories"][1]["criteria"][0]["tests"]}
    assert quiet_tests == {("[US-0004.1] q", "new"), ("[US-0004.1] api", "new")}, quiet_tests
    assert ("[US-0002.1] other", "deleted") in {
        (t["name"], t["change"]) for t in inv["stories"][2]["criteria"][0]["tests"]
    }, inv["stories"][2]
    assert inv["counts"] == {"new": 3, "modified": 2, "deleted": 2, "unchanged": 2}, inv["counts"]
    assert asyncio.run(build_test_inventory(_Fake(), "t", None, stage_key="x", as_of_label="y")) is None  # type: ignore[arg-type]

    summary = changes_summary(inv) or ""
    assert "Deleted (2):" in summary and "- gone.test.ts :: [US-0002.1] other (US-0002.1)" in summary, summary
    assert "Unchanged: 2" in summary and changes_summary(None) is None
    print("test_inventory self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
