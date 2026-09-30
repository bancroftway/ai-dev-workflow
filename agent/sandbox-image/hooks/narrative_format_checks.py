"""Pure, dependency-free (stdlib `re` only) User Story narrative-template check -- extracted from
`spec_ledger.py` (2026-09-29) specifically so the sandbox's own same-turn Stop hook
(`sandbox-image/hooks/check-narrative-format-stop.mjs`) can run the REAL check by shelling out to
`python3` on this ONE file, instead of a hand-ported JavaScript reimplementation drifting from it --
that hook's own header used to say "KEEP THESE TWO IN SYNC BY HAND ... there is no automated drift
guard for this one", the exact drift risk `coverage_parsing.py`/`test_quality_checks.py`/
`wireframe_linkage_checks.py` were each extracted to close.

Why a subprocess, not a straight import: `spec_ledger.py` (and everything upstream of it --
`graph.py`, `sandbox/provider.py`, this pipeline's DB drivers) lives entirely outside the sandbox
and is never copied into the image; even if it were, importing that module pulls in a dependency
graph with no business running inside a per-turn Stop hook. This module is the answer to
"duplicate the logic, or ship the real thing": everything below is provably pure (a list of dicts
in, a list of strings out, confirmed by this file's own self-check) and needs nothing beyond what
python3's standard library already provides, so it is the ONE file this pipeline ships into
`/opt/aidw-hooks/` and calls directly -- the real implementation, not a port of it.

`spec_ledger.py` imports `check_narrative_format` UNCHANGED (this is a pure code-move, not a
fork); its own self-check (which still exercises it via `spec_ledger.check_narrative_format`) is
the proof this extraction changed no behavior.

CLI mode (`python3 narrative_format_checks.py --check-hook`, stdin JSON: `{"user_stories":
[{"id"|"existing_us_id": ..., "narrative": "..."}, ...]}`, stdout JSON: `{"violations": [str,
...]}`, empty list = all pass) is what the Stop hook actually invokes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# The full template regex, one capture group isolating the role text (non-greedy, stops at the
# FIRST ", I want" rather than the last) so the deny-list check below reads off this same match
# instead of a second, separately-maintained pattern. "a/an/the" -- not just "a/an" -- since "As
# the administrator, I want..." is a perfectly ordinary, grammatically-fine stakeholder phrasing;
# the article itself was never the problem, only whether the noun after it names a real
# stakeholder (the deny-list's job) or a wrong connective (the ", so that" requirement below).
# IGNORECASE for "As a"/"As An"/"I Want"/"So That" case variants; DOTALL because `.` doesn't match a
# newline by default and nothing guarantees a model never embeds one inside a narrative string --
# without it, a narrative with a stray newline would false-positive as a template violation it
# doesn't actually have.
_NARRATIVE_RE = re.compile(r"^As (?:a|an|the) (.+?), I want .+, so that .+$", re.IGNORECASE | re.DOTALL)

# Generic non-stakeholder role text observed live (income-investor run 1352296c, lap 1: "As the
# system, I need...", "As the Portfolio Optimizer...") -- a curated internal quality rule, not an
# operator-tunable runtime knob (AGENTS.md's own test: no deploy operator would plausibly want to
# override this independent of a code change), same precedent as skill_gate.py's _SKILL_ALIASES
# living as a plain module constant rather than in config.py. Deliberately generic
# software-engineering nouns only -- catches "the system"/"the algorithm" reliably across any
# project, but NOT a project-specific component name used as a role ("the Portfolio Optimizer"):
# that residual case has no fixed word list to catch it and still needs audit's own judgment, which
# the evidence shows it already handles.
_NON_STAKEHOLDER_ROLE_WORDS = frozenset({
    "system", "application", "api", "backend", "database", "algorithm", "function", "service",
    "platform", "scheduler", "job", "engine", "module", "process",
})


def _normalize_role_text(text: str) -> str:
    """Whitespace/case-insensitive comparison key for the role-text deny-list check below --
    collapses formatting noise (extra spaces, capitalization) without fuzzy matching. A private,
    narrower copy of spec_ledger.py's own `_normalize_text` (not imported from there: spec_ledger.py
    imports THIS module, so the reverse import would be circular, and spec_ledger.py's version has
    other, unrelated callers this module has no business depending on)."""
    return " ".join(text.split()).strip().lower()


def check_narrative_format(user_stories: list[dict[str, Any]]) -> list[str]:
    """Every User Story's `narrative` must be 'As a <role>, I want <capability>, so that
    <benefit>' -- returns one actionable message per violation (empty list = all pass).

    Root-caused 2026-09-17 (income-investor run 1352296c): both specification prompts already
    state this template verbatim, yet 16 of lap 1's 19 story_changes were pure reformatting of
    violations audit had to catch by LLM judgment alone -- a fully mechanical rule with zero
    deterministic enforcement. Two checks, one regex: the structural 3-part shape (catches a
    dropped "that" directly), and a deny-list rejecting the specific generic-non-stakeholder
    pattern observed live ("the system", "the algorithm", ...). Honestly scoped: does not catch a
    project-specific component name used as a role -- see _NON_STAKEHOLDER_ROLE_WORDS' own
    docstring for why a fixed word list can't cover that case.
    """
    violations: list[str] = []
    for story in user_stories:
        narrative = story.get("narrative") or ""
        story_id = story.get("id") or story.get("existing_us_id") or "(no id)"
        match = _NARRATIVE_RE.match(narrative)
        if match is None:
            violations.append(
                f"{story_id}: narrative {narrative!r} does not match the required "
                "'As a <role>, I want <capability>, so that <benefit>' template (check for a "
                "missing 'that', or a role/capability/benefit segment that isn't actually present)."
            )
            continue
        role_text = _normalize_role_text(match.group(1)).removeprefix("a ").removeprefix("an ").strip()
        if role_text in _NON_STAKEHOLDER_ROLE_WORDS:
            violations.append(
                f"{story_id}: narrative {narrative!r} names '{match.group(1)}' as the role, which "
                "is not a real human or organizational stakeholder -- never the system itself, a "
                "module, a function, or a named system component. Name the actual person/role who "
                "wants this capability instead."
            )
    return violations


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.gates.narrative_format_checks`
    """Self-check: no sandbox, no DB -- every function here is pure.

    Also re-proves the sandbox-image STAGING COPY (sandbox-image/hooks/narrative_format_checks.py,
    what the Dockerfile actually COPYs into /opt/aidw-hooks/ -- see check-narrative-format-stop.mjs's
    own header for why a physical copy exists at all: Docker's build context there is
    agent/sandbox-image, which cannot COPY a path outside itself) is byte-identical to THIS file.
    Skipped, not failed, when the staged copy is absent -- this file is also imported standalone by
    spec_ledger.py in contexts (packaging, a checkout without sandbox-image/) where that path was
    never expected to exist."""
    staged_copy = Path(__file__).resolve().parents[2] / "sandbox-image" / "hooks" / "narrative_format_checks.py"
    if staged_copy.exists():
        assert staged_copy.read_bytes() == Path(__file__).read_bytes(), (
            f"{staged_copy} has drifted from this file -- re-run "
            "`cp ../src/gates/narrative_format_checks.py hooks/narrative_format_checks.py` "
            "from agent/sandbox-image and rebuild the sandbox image"
        )

    # Same three cases spec_ledger.py's own self-check used to exercise directly (root-caused
    # 2026-09-17, income-investor run 1352296c) -- now proven here, at the source of the logic.
    valid_story = {
        "id": "US-0001",
        "narrative": (
            "As an administrator, I want to configure the risk-free rate, so that Sharpe/Sortino "
            "calculations use a current value."
        ),
    }
    assert check_narrative_format([valid_story]) == [], "a correctly-shaped narrative must pass"

    wrong_role_and_connective = {
        "id": "US-0002",
        "narrative": "As the system, I need to refresh the universe so I can keep data current.",
    }
    violations = check_narrative_format([wrong_role_and_connective])
    assert len(violations) == 1 and "US-0002" in violations[0], violations

    missing_that = {
        "id": "US-0003",
        "narrative": "As a user, I want to export the data, so I can share it with my team.",
    }
    violations = check_narrative_format([missing_that])
    assert len(violations) == 1 and "US-0003" in violations[0], violations

    # Honestly-scoped boundary: a project-specific component name used as a role is NOT caught --
    # no fixed word list can cover an arbitrary project's own component names, see
    # _NON_STAKEHOLDER_ROLE_WORDS' own docstring. This documents the scope, not a bug.
    project_specific_role = {
        "id": "US-0004",
        "narrative": "As the Portfolio Optimizer, I want to reuse the Evaluator function, so that scoring stays consistent.",
    }
    assert check_narrative_format([project_specific_role]) == [], (
        "a project-specific component name as role is a documented gap, not caught by the deny-list"
    )

    assert check_narrative_format([]) == [], "no stories at all must never be a violation"

    print("narrative_format_checks self-check: all assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-hook":
        # The Stop hook's own entry point: JSON {"user_stories": [...]} on stdin, JSON result on
        # stdout. Deliberately the ONLY thing this branch does -- no sandbox access beyond what the
        # hook already read and handed over as data.
        payload = json.loads(sys.stdin.read())
        json.dump({"violations": check_narrative_format(payload.get("user_stories") or [])}, sys.stdout)
    else:
        _demo()
