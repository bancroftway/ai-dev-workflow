"""Deterministic enforcement that a stage actually invoked the skills its prompt requires.

Why this exists: naming a skill in a prompt is a request, not a guarantee. Several stages here are
built around a specific methodology (test-driven-development's RED-before-GREEN for ac-to-tests,
writing-plans for the plan another agent must execute, receiving-code-review for the audits), and
a stage that silently skips it produces work that looks plausible and isn't. That failure is
invisible in the output -- which is exactly the class of bug that made this pipeline's earlier
failures so expensive to find.

Evidence, not self-report: the model also reports `StageReport.skills_invoked`, but that field is
telemetry. A model that skipped a skill will cheerfully claim it used one. The authority here is
the Copilot session's OWN `skill.invoked` events, written by the runtime inside the sandbox at
~/.copilot/session-state/<session-id>/events.jsonl, correlated to this exact stage via
copilot_chat_model.get_session_id().

Fails OPEN when it cannot verify (no sandbox, no session id, unreadable log): an infrastructure
gap must not masquerade as a methodology violation. It only fails CLOSED on positive evidence that
a required skill is absent from a log it could actually read.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass

from .. import config as workflow_config
from ..copilot_chat_model import get_session_id
from ..sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

_SESSION_STATE_DIR = "/home/vscode/.copilot/session-state"


@dataclass(frozen=True)
class SkillCheckOutcome:
    passed: bool
    required: list[str]
    invoked: list[str]
    missing: list[str]
    verified: bool  # False when the log could not be read at all (fail-open path)


async def invoked_skills(provider: SandboxProvider, thread_id: str, stage: str, role: str = "draft") -> list[str] | None:
    """Skill names this stage's own Copilot session invoked, or None if unverifiable."""
    session_id = get_session_id(thread_id, stage, role)
    if not session_id:
        return None
    # The session log lives OUTSIDE the repo, so read it directly rather than via repo_files
    # (which resolves repo-relative paths).
    path = f"{_SESSION_STATE_DIR}/{shlex.quote(session_id)}/events.jsonl"
    result = await provider.exec_in_sandbox(thread_id, f"cat {path} 2>/dev/null")
    if not result.ok or not (result.stdout or "").strip():
        return None

    names: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or '"skill.invoked"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "skill.invoked":
            continue
        name = (event.get("data") or {}).get("name")
        if isinstance(name, str) and name not in names:
            names.append(name)
    return names


async def check_required_skills(
    provider: SandboxProvider, thread_id: str, stage: str, role: str = "draft"
) -> SkillCheckOutcome:
    required = list(workflow_config.REQUIRED_SKILLS_BY_STAGE.get(stage, []))
    if not required:
        return SkillCheckOutcome(passed=True, required=[], invoked=[], missing=[], verified=True)

    invoked = await invoked_skills(provider, thread_id, stage, role)
    if invoked is None:
        logger.info("skill gate: cannot verify invocations for stage=%s (no readable session log)", stage)
        return SkillCheckOutcome(passed=True, required=required, invoked=[], missing=[], verified=False)

    missing = [skill for skill in required if skill not in invoked]
    if missing:
        logger.info("skill gate: stage=%s missing required skills %s (invoked: %s)", stage, missing, invoked)
    return SkillCheckOutcome(passed=not missing, required=required, invoked=invoked, missing=missing, verified=True)


def feedback_for(outcome: SkillCheckOutcome) -> str:
    return (
        f"You did not invoke {outcome.missing} this turn. This stage is built around "
        f"{'that methodology' if len(outcome.missing) == 1 else 'those methodologies'}, and the "
        "prompt requires it -- skipping it produces work that looks finished but skipped the "
        "discipline it depends on. Invoke it with your `skill` tool, follow it, and redo this "
        f"turn's work under it. (Skills actually invoked: {outcome.invoked or 'none'}.)"
    )


def _demo() -> None:
    """Self-check for the pure half; the log read itself needs a sandbox."""
    ok = SkillCheckOutcome(passed=True, required=["a"], invoked=["a"], missing=[], verified=True)
    bad = SkillCheckOutcome(passed=False, required=["a", "b"], invoked=["a"], missing=["b"], verified=True)
    assert ok.passed and not bad.passed
    assert "['b']" in feedback_for(bad)
    assert "a" in feedback_for(bad)  # names what WAS invoked, so the model can see the gap
    # Every stage that requires skills must name skills the vendored packs actually ship.
    known = {
        "brainstorming", "writing-plans", "test-driven-development", "executing-plans",
        "requesting-code-review", "receiving-code-review", "verification-before-completion",
        "finishing-a-development-branch", "systematic-debugging", "dispatching-parallel-agents",
        "subagent-driven-development",
    }
    for stage, skills in workflow_config.REQUIRED_SKILLS_BY_STAGE.items():
        for skill in skills:
            assert skill in known, f"{stage} requires unknown skill {skill!r}"
    print("skill_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
