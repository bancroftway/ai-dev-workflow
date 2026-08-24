"""Deterministic enforcement that a stage actually invoked the skills its prompt requires.

Why this exists: naming a skill in a prompt is a request, not a guarantee. Several stages here are
built around a specific methodology (test-driven-development's RED-before-GREEN for ac-to-tests,
writing-plans for the plan another agent must execute, receiving-code-review for the audits), and
a stage that silently skips it produces work that looks plausible and isn't. That failure is
invisible in the output -- which is exactly the class of bug that made this pipeline's earlier
failures so expensive to find.

Evidence, not self-report: the model also reports `StageReport.skills_invoked`, but that field is
telemetry. A model that skipped a skill will cheerfully claim it used one. The authority here is
`invoked_skills()` below, which calls `get_session_id()` then `read_skill_invocations()` through
the `chat_model` provider dispatch -- and those mean different things per provider. Claude's
implementation reads that session's own real CLI transcript inside the sandbox
(~/.claude/projects/.../<session-id>.jsonl) and actually works. Copilot's implementation
unconditionally returns None: the old SDK-server session log this gate used to read
(~/.copilot/session-state/<session-id>/events.jsonl, written by a persistent `copilot --server`
process) was retired along with that process, and no CLI-exec equivalent exists yet -- so under
the Copilot provider this gate cannot verify anything today. That is the fail-open contract below
doing its job, not an oversight -- and it is WHY the deployment default flipped to Claude
(user decision 2026-08-24, Phase E audit M-3): the default posture should be the provider whose
gate actually enforces.

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
from ..claude_chat_model import normalize_skill_name
from ..sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)


# Required-name prefixes beyond a plain Skill invocation. "agent:<name>" is a Task-tool subagent
# launch (see claude_chat_model.read_skill_invocations' naming scheme). Plugin slash commands need
# NO prefix: the CLI unifies commands into the Skill tool, so they arrive as plain skill names.
_KNOWN_PREFIXES = ("agent:",)


def _requirement_phrase(name: str) -> str:
    """How to tell the model to satisfy one required entry, matched to its invocation kind."""
    if name.startswith("agent:"):
        return f"launch the `{name.removeprefix('agent:')}` agent with your Agent/Task (subagent) tool"
    return f"invoke `{name}` with your Skill tool"


@dataclass(frozen=True)
class SkillCheckOutcome:
    passed: bool
    required: list[str]
    invoked: list[str]
    missing: list[str]
    verified: bool  # False when the log could not be read at all (fail-open path)


async def invoked_skills(
    provider: SandboxProvider | None, thread_id: str, stage: str, role: str = "draft", *, chat_provider: str
) -> list[str] | None:
    """Skill names this stage's own session invoked, or None if unverifiable.

    `provider` may be None (no sandbox registered for this thread) -- that is just another
    unverifiable case, same None as a missing session id, so skills_record can run on every path
    and still always emit a record (the provider-evidence requirement, 2026-08-24).

    `chat_provider` (required, keyword-only, no default -- Ruling 4,
    docs/superpowers/plans/part-4-org-settings-tasks.md) is this THREAD's own pinned
    "claude"/"copilot" provider, threaded in from the caller -- not resolved in here, and not to be
    confused with `provider` above (the pre-existing SandboxProvider connection object). Required
    because get_session_id/read_skill_invocations dispatch on it now: this function is not itself a
    graph node, but it operates on one specific thread's session, which has its own pinned
    provider, so it is not exempt from threading that value through just because it isn't a graph
    node (this was a real gap in this plan's own first draft, corrected as part of Ruling 4).
    """
    if provider is None:
        return None
    session_id = get_session_id(thread_id, stage, role, provider=chat_provider)
    if not session_id:
        return None
    return await read_skill_invocations(provider, thread_id, session_id, active_provider=chat_provider)


# Roles whose sessions count towards a stage's required skills. A stage runs a DRAFT session and,
# where it has an audit pass, a separate AUDIT session -- and several required skills belong to the
# audit by nature: `requesting-code-review` and `verification-before-completion` are what a review
# pass invokes, not what a first draft does. Checking only the draft therefore reported a skill
# missing that had genuinely been used in the other session. Observed live: minimal-code-to-green's
# draft invoked seven skills while `verification-before-completion` was reported missing, and real
# runs do create `plan:audit` and `minimal-code-to-green:audit` sessions.
_ROLES_CHECKED = ("draft", "audit")


async def check_required_skills(
    provider: SandboxProvider | None,
    thread_id: str,
    stage: str,
    roles: tuple[str, ...] = _ROLES_CHECKED,
    *,
    chat_provider: str,
    prior_invoked: list[str] | None = None,
) -> SkillCheckOutcome:
    """Union of every checked role's `skill.invoked` events for this stage.

    `chat_provider` (required, keyword-only, no default -- Ruling 4): this thread's own pinned
    provider, threaded straight through to invoked_skills -- see that function's own docstring.

    `prior_invoked` is the stage's previously PERSISTED invoked list (stage["skills"]["invoked"],
    passed in by make_verify_node). It exists because a failed skill check closes the draft
    session (make_verify_node's reset), and the fresh session's transcript no longer contains what
    lap 1 genuinely invoked -- without this union, one missed skill retroactively "un-invokes"
    every other requirement and the redraft must re-run all of them or fail again (2026-08-24 plan
    audit, session-reset amplification). The persisted list is itself transcript evidence from an
    earlier lap, never self-report, so unioning it keeps the "evidence, not self-report" contract.

    `verified` stays False only when NO role produced a readable log: one absent session (a stage
    with no audit pass) alongside one readable session is a complete answer, not an unverifiable one.
    """
    required = list(workflow_config.REQUIRED_SKILLS_BY_STAGE.get(stage, []))
    if not required:
        return SkillCheckOutcome(passed=True, required=[], invoked=[], missing=[], verified=True)

    invoked: list[str] = [s for s in (prior_invoked or []) if isinstance(s, str)]
    any_readable = False
    for role in roles:
        role_skills = await invoked_skills(provider, thread_id, stage, role, chat_provider=chat_provider)
        if role_skills is None:
            continue
        any_readable = True
        for skill in role_skills:
            if skill not in invoked:
                invoked.append(skill)

    if not any_readable:
        # Was logger.info. Under the Copilot provider this fires for every
        # skill-checked stage, every run, permanently (read_skill_invocations() unconditionally
        # returns None there -- see its own docstring) -- a silent, PERMANENT non-enforcement, not
        # a transient hiccup, and info buried it. Nothing here distinguishes that from a
        # genuinely one-off "couldn't read this one session's log" case (e.g. Claude, if its
        # transcript-path assumption ever drifts against a real container) -- both land in this
        # same branch with identical information -- so the bump applies uniformly rather than
        # guessing which one happened.
        logger.warning("skill gate: cannot verify invocations for stage=%s (no readable session log)", stage)
        # invoked keeps whatever prior_invoked carried: fail-open means "cannot verify THIS lap",
        # not "prior transcript evidence stopped existing" -- returning [] here made the
        # verification report and feedback contradict the persisted record (2026-08-24 audit).
        return SkillCheckOutcome(passed=True, required=required, invoked=invoked, missing=[], verified=False)

    missing = [skill for skill in required if skill not in invoked]
    if missing:
        logger.info("skill gate: stage=%s missing required skills %s (invoked across %s: %s)",
                    stage, missing, list(roles), invoked)
    return SkillCheckOutcome(passed=not missing, required=required, invoked=invoked, missing=missing, verified=True)


async def skills_record(
    provider: SandboxProvider | None,
    thread_id: str,
    stage: str,
    self_reported: list[str] | None = None,
    *,
    chat_provider: str,
    prior_invoked: list[str] | None = None,
) -> dict[str, Any]:
    """The stage's skill evidence, for persistence into state.json -- on the PASS path too.

    Previously only failures stored anything (the pass path returned early), so a healthy run left no
    trace that any skill had been used and `grep skill_gate` on a green log returned nothing. That is
    the whole reason "we force GHCP to report skills" looked unimplemented.

    `chat_provider` (required, keyword-only, no default -- Ruling 4): this thread's own pinned
    provider, threaded straight through to invoked_skills -- see that function's own docstring. It
    is ALSO persisted into the record itself (`provider` key, user requirement 2026-08-24): every
    stage's evidence must say which coding-agent provider produced it, not leave that to be
    reconstructed from run-level state later. `provider` (the SandboxProvider) may be None -- the
    record then persists as unverified but still carries the provider name.

    `prior_invoked` is the stage's PREVIOUS persisted record's `invoked` list, unioned in (and
    reflected in `missing`/`unsubstantiated`). Load-bearing, not telemetry polish (2026-08-24
    audit HIGH finding): a failed skill check closes the draft session, and the redraft's fresh
    transcript no longer contains lap 1's invocations -- without this union the draft node's own
    record overwrite destroys exactly the evidence make_verify_node's prior_invoked union was
    added to preserve, re-creating the lap-oscillation this whole mechanism exists to prevent.
    Prior entries are themselves transcript evidence from an earlier lap, never self-report.

    Self-reported names are normalized (claude_chat_model.normalize_skill_name) before comparison:
    a model that ran /code-review and honestly reports "/code-review" or
    "code-review:code-review" must not be flagged as fabricating just for the spelling. Claims
    that normalize to nothing (whitespace, a bare "/", a trailing-colon fragment) are dropped
    rather than kept as empty-string fabrication-signal noise.

    `unsubstantiated` is the interesting field: a skill the model CLAIMED but never invoked. The event
    log cannot be forged, so a non-empty list here is a fabrication signal of exactly the kind that
    has been this pipeline's most expensive failure mode. Recorded, not gated -- we should learn how
    often it fires before blocking on it.
    """
    required = list(workflow_config.REQUIRED_SKILLS_BY_STAGE.get(stage, []))
    invoked: list[str] = [s for s in (prior_invoked or []) if isinstance(s, str) and s]
    any_readable = False
    for role in _ROLES_CHECKED:
        role_skills = await invoked_skills(provider, thread_id, stage, role, chat_provider=chat_provider)
        if role_skills is None:
            continue
        any_readable = True
        for skill in role_skills:
            if skill not in invoked:
                invoked.append(skill)
    claimed = [n for n in (normalize_skill_name(str(s)) for s in (self_reported or [])) if n]
    # An "agent:x" transcript entry substantiates a bare "x" claim too -- the self-report schemas
    # ask for skill names, not this gate's internal prefix vocabulary.
    invoked_bare = {name.split(":", 1)[-1] for name in invoked}
    return {
        "provider": chat_provider,
        "required": required,
        "invoked": invoked,
        "self_reported": claimed,
        "unsubstantiated": [s for s in claimed if s not in invoked and s not in invoked_bare],
        "missing": [s for s in required if s not in invoked],
        # False means NO role produced a readable log. Kept distinct from `missing: []` on purpose:
        # "no evidence" must never read as "enforced".
        "verified": any_readable,
    }


def feedback_for(outcome: SkillCheckOutcome) -> str:
    actions = "; ".join(_requirement_phrase(name) for name in outcome.missing)
    return (
        f"You did not invoke {outcome.missing} this turn. This stage is built around "
        f"{'that methodology' if len(outcome.missing) == 1 else 'those methodologies'}, and the "
        "prompt requires it -- skipping it produces work that looks finished but skipped the "
        f"discipline it depends on. To satisfy the requirement: {actions}; follow what it says, "
        f"and redo this turn's work under it. (Actually invoked: {outcome.invoked or 'none'}.)"
    )


def _demo() -> None:
    """Self-check for the pure half; the log read itself needs a sandbox."""
    import asyncio

    ok = SkillCheckOutcome(passed=True, required=["a"], invoked=["a"], missing=[], verified=True)
    bad = SkillCheckOutcome(passed=False, required=["a", "b"], invoked=["a"], missing=["b"], verified=True)
    assert ok.passed and not bad.passed
    assert "['b']" in feedback_for(bad)
    assert "a" in feedback_for(bad)  # names what WAS invoked, so the model can see the gap
    # Per-kind feedback wording: an agent requirement names the subagent tool, a skill the Skill tool.
    agent_bad = SkillCheckOutcome(
        passed=False, required=["agent:code-simplifier"], invoked=[], missing=["agent:code-simplifier"], verified=True
    )
    assert "subagent" in feedback_for(agent_bad)
    assert "Skill tool" in feedback_for(bad)
    # Every stage that requires skills must name skills the vendored packs (or the CLI's own
    # bundled skills: code-review, security-review, simplify) actually ship. "agent:" entries name
    # subagents launched via the Task tool.
    known = {
        "brainstorming", "writing-plans", "test-driven-development", "executing-plans",
        "requesting-code-review", "receiving-code-review", "verification-before-completion",
        "finishing-a-development-branch", "systematic-debugging", "dispatching-parallel-agents",
        "subagent-driven-development", "ponytail", "code-review", "security-review", "simplify",
        "agent:code-simplifier", "frontend-design",
    }
    for stage, skills in workflow_config.REQUIRED_SKILLS_BY_STAGE.items():
        for skill in skills:
            assert skill in known, f"{stage} requires unknown skill {skill!r}"
            if ":" in skill:
                assert skill.startswith(_KNOWN_PREFIXES), f"{stage} requires unknown prefix in {skill!r}"

    # skills_record must always carry the provider, even fully unverifiable (no sandbox at all) --
    # the always-present provider-evidence requirement (2026-08-24). The None provider short-
    # circuits in invoked_skills, so no fake sandbox is needed here.
    record = asyncio.run(skills_record(None, "thread-x", "plan", ["/writing-plans"], chat_provider="claude"))
    assert record["provider"] == "claude", f"provider missing from evidence record: {record}"
    assert record["verified"] is False and record["invoked"] == []
    assert record["self_reported"] == ["writing-plans"], f"self-report not normalized: {record}"

    # prior_invoked union: a lap-1 transcript's evidence survives the session reset a lap-2 miss
    # triggers -- required entries already substantiated must not come back as missing.
    outcome = asyncio.run(
        check_required_skills(None, "thread-x", "plan", chat_provider="claude", prior_invoked=["writing-plans"])
    )
    # No readable session (None provider) -- fail-open branch still wins over prior_invoked, and
    # the outcome keeps the prior evidence rather than reporting "invoked: none" against it.
    assert outcome.verified is False and outcome.passed is True
    assert outcome.invoked == ["writing-plans"], f"fail-open dropped prior evidence: {outcome}"
    # The same union inside skills_record: a redraft's fresh record must never shrink `invoked`
    # below what earlier laps proved (the 2026-08-24 audit's HIGH finding -- the draft node's
    # record overwrite used to destroy exactly the evidence the verify-node union depended on).
    record2 = asyncio.run(
        skills_record(None, "thread-x", "plan", chat_provider="claude", prior_invoked=["writing-plans"])
    )
    assert record2["invoked"] == ["writing-plans"] and record2["missing"] == [], f"prior union lost: {record2}"
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
