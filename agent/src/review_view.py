"""The open human review -- a pending LangGraph interrupt -- as a ready-to-render view model, and
the mapping from a reviewer's action to the exact resume value graph.make_gate_node expects.

Why this exists (root-caused 2026-10-01): the review used to reach the UI ONLY as an AG-UI
interrupt event on a live run stream (CopilotKit useInterrupt). The interrupt itself is durable in
the checkpoint, but a reloaded page after an agent restart (or any dropped stream) never got it
back, so a session paused at its Specification gate lost its Approve button for good. The review
is server state now: sessions_api's GET /sessions/{id}/review reads the checkpoint's pending
interrupt and builds this model, POST resolves it server-side (run_activity.start_resume). The
frontend paints `title`/`body`/`actions` and posts an action id; it holds no copy and no rule.

Pure: the caller passes the snapshot's interrupts plus two live facts (sandbox registered, a run
driving the thread). `cd agent && uv run python -m src.review_view`.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .pipeline_layout import PIPELINE
from .prompt_loader import load_prompt

TECH_STACK = "tech-stack"
# Gates whose change requests belong in the requirements document (user ruling 2026-08-31): no
# Reject box -- the Requirements tab's Submit resolves the open gate with the revised document.
# Value: what this stage's content is derived from, for the card's copy.
SOURCE_OF_TRUTH_STAGES: dict[str, str] = {
    "specification": "this specification — and every plan, test, and line of code after it — is derived from that document alone",
    "plan": "this plan is derived from the approved Specification, which is itself derived from that document alone",
}
RESUBMIT_ACTION = "resubmit_requirements"

REVIEW_TEXT: dict[str, Any] = {
    "ready": "is ready for your review.",
    "source_of_truth": [
        " Your ", ("Requirements document is the single source of truth", True), ": {derivation}. Nothing you "
        "want will make it into the product unless it's written there. To change anything here, don't comment "
        "— edit the document on the Requirements tab and resubmit; {redrafted} redrafted from it, and every "
        "question it answers is traced back to your wording.",
    ],
    "redrafted": {"specification": "the specification is", "plan": "the specification and this plan are"},
    "reject_placeholder": "What should change before this is approved? (required to reject)",
    "reject_hint": "Add feedback below to explain what should change",
    "tech_stack_subtitle": "Pick a starting stack or review what was detected — the text below is fully "
    "editable either way, and whatever you submit becomes the stack of record.",
    "verification_title": "Your last submission didn’t pass verification (attempt {attempts} of {max_attempts}). "
    "Edit the text below and submit again.",
    "check_status": {"failed": "Failed", "infra": "Couldn’t check", "advisory": "Advisory"},
    "requirements_note": {
        TECH_STACK: "Finish the Tech Stack tab first, then resubmit.",
        "specification": "The Specification is awaiting review — submitting here revises the requirements and "
        "redrafts it from the updated document.",
        "plan": "The Plan is awaiting review — submitting here revises the requirements and redrafts the "
        "Specification first, then the Plan.",
        None: "A review is waiting — approve or reject it first, then edit and resubmit.",
    },
    "busy": "Applying a decision on this review…",
    # The page's own sandbox boot reprovisions on load; a tab left open across an agent restart
    # has nothing that will, hence the reload hint.
    "no_sandbox": "Waiting for this session's dev-tool sandbox to connect — if this doesn't clear in a few "
    "seconds, reload the page.",
    "this_stage": "this stage",
}

_ACTIONS: dict[str, dict[str, Any]] = {
    "approve": {"label": "Approve", "style": "primary", "needs_text": False},
    "reject": {"label": "Reject", "style": "danger", "needs_text": True, "hint": REVIEW_TEXT["reject_hint"]},
    "submit": {"label": "Submit", "style": "primary", "needs_text": True},
    "retry": {"label": "Acknowledge & retry", "style": "primary", "needs_text": False},
}

CLOSED: dict[str, Any] = {"open": False, "id": None, "stage": None, "kind": None, "requirements": None}


class ReviewActionError(ValueError):
    """An action the open review does not offer, or one missing its required text -> HTTP 400."""


def _label(stage: str | None) -> str:
    spec = PIPELINE.stage(stage) if stage else None
    return spec.label if spec else REVIEW_TEXT["this_stage"]


def _segments(parts: list[Any], **fill: str) -> list[dict[str, Any]]:
    return [
        {"text": p[0], "bold": True} if isinstance(p, tuple) else {"text": p.format(**fill), "bold": False}
        for p in parts
    ]


def _action(action_id: str, enabled: bool) -> dict[str, Any]:
    a = _ACTIONS[action_id]
    return {"id": action_id, "label": a["label"], "style": a["style"], "needs_text": a["needs_text"],
            "hint": a.get("hint"), "enabled": enabled}


def _verification(v: Any) -> dict[str, Any] | None:
    """Tech-stack's reopened gate: the after-submit verdict on the last submission
    (graph._build_tech_stack_interrupt_extra), as rows. None when there's nothing to report."""
    if not isinstance(v, dict) or v.get("passed"):
        return None
    spec = PIPELINE.stage(TECH_STACK)
    names = {c.id: c.label for c in (spec.gate.checks if spec and spec.gate else ())}
    words = REVIEW_TEXT["check_status"]
    items = [
        {"key": c.get("id"), "tone": "fail" if c.get("status") == "failed" else "warn",
         "label": f"{words[c['status']]}: {names.get(c.get('id'), c.get('id'))}", "detail": c.get("detail") or None}
        for c in (v.get("checks") or []) if isinstance(c, dict) and c.get("status") in words
    ]
    return {
        "title": REVIEW_TEXT["verification_title"].format(attempts=v.get("attempts", 0), max_attempts=v.get("max_attempts", 0)),
        "items": items,
        "feedback": None if items else (v.get("feedback") or None),
    }


def build_review(interrupts: Sequence[Any], *, sandbox_ready: bool, busy: bool) -> dict[str, Any]:
    """The open review for the FIRST pending interrupt (one gate is open at a time), or CLOSED.

    `blocked` (actions disabled) when a run is already driving the thread -- a resolve is being
    applied, or another tab got there first -- or when no sandbox is registered: gate_node only
    persists/signs an approval (and tech-stack only verifies a submission) with a sandbox, so
    resolving without one would approve unpersisted content. The page's own sandbox boot
    reprovisions it after an agent restart."""
    pending = next((i for i in interrupts if isinstance(getattr(i, "value", None), dict)), None)
    if pending is None:
        return dict(CLOSED)
    p: dict[str, Any] = pending.value
    stage = p.get("stage") if isinstance(p.get("stage"), str) else None
    label = _label(stage)
    blocked = REVIEW_TEXT["busy"] if busy else None if sandbox_ready else REVIEW_TEXT["no_sandbox"]
    ok = blocked is None
    view: dict[str, Any] = {
        "open": True, "id": getattr(pending, "id", None), "stage": stage, "stage_label": label,
        "card": True, "tone": "review", "title": None, "body": [], "details": None, "input": None,
        "blocked": blocked, "tech_stack": None,
        "requirements": {"resubmit_action": None, "note": REVIEW_TEXT["requirements_note"][None]},
    }

    if p.get("type"):
        # Escalation interrupt: none is raised today (escalations END the run with run_failure),
        # but a checkpoint from before that change may still hold one -- keep it resolvable.
        rest = {k: v for k, v in p.items() if k not in ("stage", "type", "draft", "feedback", "reason")}
        text = next((v for v in (p.get("feedback"), p.get("reason")) if isinstance(v, str) and v), None)
        return {**view, "kind": "escalation", "tone": "error",
                "title": f"{label}: {str(p['type']).replace('_', ' ')}",
                "body": [{"text": text, "bold": False}] if text else [],
                "details": json.dumps(rest, indent=2, default=str) if rest else None,
                "actions": [_action("retry", ok)]}

    if stage == TECH_STACK:
        # The Tech Stack tab renders this review itself (an editor, not a card).
        return {**view, "kind": "tech_stack", "card": False, "actions": [_action("submit", ok)],
                "requirements": {"resubmit_action": None, "note": REVIEW_TEXT["requirements_note"][TECH_STACK]},
                "tech_stack": {
                    "subtitle": REVIEW_TEXT["tech_stack_subtitle"],
                    "markdown": p.get("markdown") if isinstance(p.get("markdown"), str) else "",
                    # The canned-stack picker only when no real tech-stack.md existed.
                    "show_catalog": p.get("file_existed") is False,
                    "verification": _verification(p.get("verification")),
                }}

    body = [{"text": "The ", "bold": False}, {"text": label, "bold": True}, {"text": f" {REVIEW_TEXT['ready']}", "bold": False}]
    if stage in SOURCE_OF_TRUTH_STAGES:
        body += _segments(REVIEW_TEXT["source_of_truth"], derivation=SOURCE_OF_TRUTH_STAGES[stage],
                          redrafted=REVIEW_TEXT["redrafted"][stage])
        return {**view, "kind": "approval", "body": body, "actions": [_action("approve", ok)],
                "requirements": {"resubmit_action": RESUBMIT_ACTION if ok else None,
                                 "note": REVIEW_TEXT["requirements_note"][stage]}}
    return {**view, "kind": "approval", "body": body, "actions": [_action("approve", ok), _action("reject", ok)],
            "input": {"placeholder": REVIEW_TEXT["reject_placeholder"]}}


def resume_value(review: dict[str, Any], action_id: str, text: str | None) -> Any:
    """The exact value the pre-2026-10-01 frontend resolved the interrupt with for this action --
    graph.make_gate_node discriminates on it (approved / rejected[+revised_requirements] /
    tech-stack {"markdown"}), so these shapes are a contract, not a choice."""
    offered = {a["id"] for a in review.get("actions") or []}
    if (review.get("requirements") or {}).get("resubmit_action"):
        offered.add(review["requirements"]["resubmit_action"])
    if not review.get("open") or action_id not in offered:
        raise ReviewActionError(f"action {action_id!r} is not offered by this review (offered: {sorted(offered)})")
    text = text or ""
    if (action_id == RESUBMIT_ACTION or _ACTIONS.get(action_id, {}).get("needs_text")) and not text.strip():
        raise ReviewActionError(f"action {action_id!r} needs non-empty text")
    if action_id == "approve":
        return {"decision": "approved"}
    if action_id == "reject":
        return {"decision": "rejected", "feedback": text.strip()}
    if action_id == "submit":
        return {"markdown": text}
    if action_id == "retry":
        # Scalar on purpose: an empty dict is an empty resume MAP to LangGraph -- no value delivered.
        return "retry"
    # RESUBMIT_ACTION: Plan's restart-from-specification cascade is make_gate_node's own call
    # (it checks its stage key), so the same shape serves both gates.
    return {
        "decision": "rejected",
        "feedback": load_prompt(f"gate_resubmit_{review['stage']}_segment"),
        "revised_requirements": text.strip(),
    }


def _demo() -> None:
    from langgraph.types import Interrupt

    assert build_review([], sandbox_ready=True, busy=False) == CLOSED

    # Specification gate (the stuck-session case): Approve only, source-of-truth copy, the
    # Requirements tab may resubmit.
    spec = build_review([Interrupt(value={"stage": "specification", "draft": {"x": 1}}, id="i-spec")], sandbox_ready=True, busy=False)
    assert spec["open"] and spec["id"] == "i-spec" and spec["kind"] == "approval" and spec["card"], spec
    assert [a["id"] for a in spec["actions"]] == ["approve"] and spec["actions"][0]["enabled"]
    assert spec["input"] is None and spec["blocked"] is None
    assert any(s["bold"] and "single source of truth" in s["text"] for s in spec["body"])
    assert "the specification is redrafted" in "".join(s["text"] for s in spec["body"])
    assert spec["requirements"]["resubmit_action"] == RESUBMIT_ACTION and "Specification" in spec["requirements"]["note"]
    assert '"x"' not in json.dumps(spec)  # the draft is the views' business, never the review's
    assert resume_value(spec, "approve", None) == {"decision": "approved"}
    plan = build_review([Interrupt(value={"stage": "plan", "draft": {}}, id="i-plan")], sandbox_ready=True, busy=False)
    rv = resume_value(plan, RESUBMIT_ACTION, "  revised doc  ")
    assert rv["decision"] == "rejected" and rv["revised_requirements"] == "revised doc" and "Plan" in rv["feedback"], rv
    assert "redraft the Specification" in resume_value(spec, RESUBMIT_ACTION, "x")["feedback"]
    for bad in (("reject", "why"), ("submit", "x"), (RESUBMIT_ACTION, "  ")):
        try:
            resume_value(spec, *bad)
            raise AssertionError(f"{bad} must be refused on a spec gate")
        except ReviewActionError:
            pass

    # Brownfield baseline gates: generic Approve/Reject, Reject needs feedback; no resubmit.
    bf = build_review([Interrupt(value={"stage": "brownfield-spec", "draft": {}}, id="i-bf")], sandbox_ready=True, busy=False)
    assert [a["id"] for a in bf["actions"]] == ["approve", "reject"] and bf["input"]["placeholder"]
    assert bf["actions"][1]["needs_text"] and bf["requirements"]["resubmit_action"] is None
    assert resume_value(bf, "reject", " too vague ") == {"decision": "rejected", "feedback": "too vague"}
    try:
        resume_value(bf, "reject", "")
        raise AssertionError("reject without feedback must be refused")
    except ReviewActionError:
        pass

    # Tech-stack gate re-opened after a failed after-submit verify: editor review, no card.
    iv = {"passed": False, "feedback": "f", "attempts": 1, "max_attempts": 3, "checks": [
        {"id": "nope-check", "status": "failed", "detail": "missing x"},
        {"id": "other", "status": "passed", "detail": None},
        {"id": "infra-check", "status": "infra", "detail": None},
    ]}
    ts = build_review([Interrupt(value={"stage": TECH_STACK, "draft": {}, "markdown": "# Stack", "file_existed": False,
                                        "verification": iv}, id="i-ts")], sandbox_ready=True, busy=False)
    assert ts["kind"] == "tech_stack" and ts["card"] is False and [a["id"] for a in ts["actions"]] == ["submit"]
    t = ts["tech_stack"]
    assert t["markdown"] == "# Stack" and t["show_catalog"] is True and t["subtitle"]
    assert [i["tone"] for i in t["verification"]["items"]] == ["fail", "warn"], t["verification"]
    assert t["verification"]["items"][0]["label"] == "Failed: nope-check" and "attempt 1 of 3" in t["verification"]["title"]
    assert t["verification"]["feedback"] is None
    assert ts["requirements"] == {"resubmit_action": None, "note": REVIEW_TEXT["requirements_note"][TECH_STACK]}
    assert resume_value(ts, "submit", "# Stack\n") == {"markdown": "# Stack\n"}  # raw text, as the editor had it
    first = build_review([Interrupt(value={"stage": TECH_STACK, "markdown": "m", "file_existed": True, "verification": None})],
                         sandbox_ready=True, busy=False)
    assert first["tech_stack"]["verification"] is None and first["tech_stack"]["show_catalog"] is False

    # Legacy escalation interrupt: error card, scalar "retry".
    esc = build_review([Interrupt(value={"stage": "ac-to-tests", "type": "verification_cap_exceeded", "feedback": "3 failed",
                                         "report": {"n": 1}, "draft": "huge"}, id="i-esc")], sandbox_ready=True, busy=False)
    assert esc["kind"] == "escalation" and esc["tone"] == "error" and esc["title"].endswith("verification cap exceeded")
    assert esc["body"] == [{"text": "3 failed", "bold": False}] and '"report"' in esc["details"] and "huge" not in esc["details"]
    assert resume_value(esc, "retry", None) == "retry"

    # Blocked: no sandbox / a run already driving -- actions disabled, resubmit withheld, reason given.
    for kw, reason in (({"sandbox_ready": False, "busy": False}, "no_sandbox"), ({"sandbox_ready": True, "busy": True}, "busy")):
        b = build_review([Interrupt(value={"stage": "specification", "draft": {}}, id="i")], **kw)
        assert b["open"] and b["blocked"] == REVIEW_TEXT[reason] and not b["actions"][0]["enabled"]
        assert b["requirements"]["resubmit_action"] is None
    # A closed review offers nothing.
    try:
        resume_value(CLOSED, "approve", None)
        raise AssertionError("closed review must refuse")
    except ReviewActionError:
        pass
    print("review_view self-check passed")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.review_view
    _demo()
