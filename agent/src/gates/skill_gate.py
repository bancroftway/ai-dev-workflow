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

import logging
from dataclasses import dataclass
from typing import Any

from .. import config as workflow_config
from ..chat_model import get_session_id, read_skill_invocations
from ..sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillCheckOutcome:
    passed: bool
    required: list[str]
    invoked: list[str]
    missing: list[str]
    verified: bool  # False when the log could not be read at all (fail-open path)


async def invoked_skills(provider: SandboxProvider, thread_id: str, stage: str, role: str = "draft") -> list[str] | None:
    """Skill names this stage's own session invoked, or None if unverifiable."""
    session_id = get_session_id(thread_id, stage, role)
    if not session_id:
        return None
    return await read_skill_invocations(provider, thread_id, session_id)


# Roles whose sessions count towards a stage's required skills. A stage runs a DRAFT session and,
# where it has an audit pass, a separate AUDIT session -- and several required skills belong to the
# audit by nature: `requesting-code-review` and `verification-before-completion` are what a review
# pass invokes, not what a first draft does. Checking only the draft therefore reported a skill
# missing that had genuinely been used in the other session. Observed live: minimal-code-to-green's
# draft invoked seven skills while `verification-before-completion` was reported missing, and real
# runs do create `plan:audit` and `minimal-code-to-green:audit` sessions.
_ROLES_CHECKED = ("draft", "audit")


async def check_required_skills(
    provider: SandboxProvider, thread_id: str, stage: str, roles: tuple[str, ...] = _ROLES_CHECKED
) -> SkillCheckOutcome:
    """Union of every checked role's `skill.invoked` events for this stage.

    `verified` stays False only when NO role produced a readable log: one absent session (a stage
    with no audit pass) alongside one readable session is a complete answer, not an unverifiable one.
    """
    required = list(workflow_config.REQUIRED_SKILLS_BY_STAGE.get(stage, []))
    if not required:
        return SkillCheckOutcome(passed=True, required=[], invoked=[], missing=[], verified=True)

    invoked: list[str] = []
    any_readable = False
    for role in roles:
        role_skills = await invoked_skills(provider, thread_id, stage, role)
        if role_skills is None:
            continue
        any_readable = True
        for skill in role_skills:
            if skill not in invoked:
                invoked.append(skill)

    if not any_readable:
        logger.info("skill gate: cannot verify invocations for stage=%s (no readable session log)", stage)
        return SkillCheckOutcome(passed=True, required=required, invoked=[], missing=[], verified=False)

    missing = [skill for skill in required if skill not in invoked]
    if missing:
        logger.info("skill gate: stage=%s missing required skills %s (invoked across %s: %s)",
                    stage, missing, list(roles), invoked)
    return SkillCheckOutcome(passed=not missing, required=required, invoked=invoked, missing=missing, verified=True)


async def skills_record(
    provider: SandboxProvider, thread_id: str, stage: str, self_reported: list[str] | None = None
) -> dict[str, Any]:
    """The stage's skill evidence, for persistence into state.json -- on the PASS path too.

    Previously only failures stored anything (the pass path returned early), so a healthy run left no
    trace that any skill had been used and `grep skill_gate` on a green log returned nothing. That is
    the whole reason "we force GHCP to report skills" looked unimplemented.

    `unsubstantiated` is the interesting field: a skill the model CLAIMED but never invoked. The event
    log cannot be forged, so a non-empty list here is a fabrication signal of exactly the kind that
    has been this pipeline's most expensive failure mode. Recorded, not gated -- we should learn how
    often it fires before blocking on it.
    """
    required = list(workflow_config.REQUIRED_SKILLS_BY_STAGE.get(stage, []))
    invoked: list[str] = []
    any_readable = False
    for role in _ROLES_CHECKED:
        role_skills = await invoked_skills(provider, thread_id, stage, role)
        if role_skills is None:
            continue
        any_readable = True
        for skill in role_skills:
            if skill not in invoked:
                invoked.append(skill)
    claimed = [str(s) for s in (self_reported or [])]
    return {
        "required": required,
        "invoked": invoked,
        "self_reported": claimed,
        "unsubstantiated": [s for s in claimed if s not in invoked],
        "missing": [s for s in required if s not in invoked],
        # False means NO role produced a readable log. Kept distinct from `missing: []` on purpose:
        # "no evidence" must never read as "enforced".
        "verified": any_readable,
    }


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
    # A stage key that does not exist silently disables enforcement for it -- worse than a crash,
    # because everything still looks configured. Two stale keys ("exit", "adversarial-audit") got
    # in this way before this check existed.
    from ..graph import _STAGE_KEYS

    unknown_stages = [s for s in workflow_config.REQUIRED_SKILLS_BY_STAGE if s not in _STAGE_KEYS]
    assert not unknown_stages, f"REQUIRED_SKILLS_BY_STAGE names non-existent stages: {unknown_stages}"

    # This gate runs BEFORE deterministic_verify, so a stage with a required skill and no verify
    # laps turns one missed skill into an instant, unrecoverable run failure. metrics-exit was
    # exactly that: max_verify_cycles=0 (set when its deterministic_verify could only pass) plus a
    # required skill ended an otherwise-complete run on its last stage.
    from ..graph import _ALL_STAGE_SPECS

    caps = {spec.key: spec.max_verify_cycles for spec in _ALL_STAGE_SPECS}
    no_retry = [
        stage
        for stage in workflow_config.REQUIRED_SKILLS_BY_STAGE
        if caps.get(stage, 0) < 1
    ]
    assert not no_retry, (
        f"stages require a skill but have no verify laps to correct a miss: {no_retry} "
        "-- give them max_verify_cycles >= 1 or drop the requirement"
    )
    print("skill_gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
