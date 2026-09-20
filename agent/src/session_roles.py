"""Session-cache ROLE KEY convention: the ONE place that knows the shape a per-lap chat session's
role string is built in, and the ONE place every caller must go through to either build one or
recognize one -- construction (`lap_role`/`lap_role_keys`) and recognition (`role_matches`) of the
exact same string, so the two sides can never drift apart again.

A leaf module (stdlib only, no project imports) on purpose: `chat_model.py` imports both
`claude_chat_model.py` and `copilot_chat_model.py`, so neither of THOSE can import `chat_model.py`
back without a cycle -- which is exactly why `_required_skills_env_prefix` used to be duplicated,
by hand, in both provider files (same reasoning documented on `_map_tool_names` in both). Being
leaf-level lets `claude_chat_model.py` and `copilot_chat_model.py` both depend on this module
DIRECTLY, so their copies of this recognition logic are the same function call, not two
hand-typed copies of the same regex-shaped idea. `chat_model.py` re-exports every name here
unchanged, so `chat_model.lap_role(...)`, `chat_model.lap_role_keys(...)` (graph.py, gates/
skill_gate.py, gates/diagram_gate.py) keep working exactly as before -- only where the
implementation lives has moved.

Root-caused three separate times, same shape each time -- a caller compared a role against the
bare base-role LABEL ("draft"/"audit") while the actual session in play is keyed
`<base_role>-<run_id>-<cycle>` (a fresh session per redraft lap, so `--resume` never replays a
prior lap's whole transcript into the next one):
  1. 2026-09-18, gates/skill_gate.py's session-id lookups (income-investor runs e865062d/c9c293ea).
  2. 2026-09-18, graph.py's specification/plan full-file-read proof, diagram_gate.py's plan
     equivalent (session 6244ef47) -- both fixed by routing construction through `lap_role`.
  3. 2026-09-19, claude_chat_model.py's/copilot_chat_model.py's `_required_skills_env_prefix`,
     which silently disarmed the skill-enforcement Stop hook on every real turn since the day it
     shipped (session 5905ba13) -- its own unit test asserted the bare literal and never caught
     it, because the RECOGNITION side (a hand-written `role != "draft"`) had no shared function to
     go through at all; `role_matches` below closes that gap the same way `lap_role` already
     closed the construction side.

NOT a candidate for every `role`-shaped parameter in this codebase -- see this module's own
callers list in chat_model.py for the two categories confirmed to need it, and note what's
deliberately excluded: `StageSpec.session_options(state, role)` and
`model_config.get_model_name(stage, role, provider)` both take a `role` that is, by their own
calling CONVENTION, always the bare literal ("draft"/"audit"/"fix") -- a model-tier or tool-set
selector, never a per-lap session key -- at every real call site (confirmed by inspection,
2026-09-19). Routing an always-bare parameter through `role_matches` would not fix a bug (there
isn't one there) and would blur the distinction this module exists to make precise.
"""

from __future__ import annotations


def lap_role(base_role: str, run_id: str, cycle: int) -> str:
    """The session-cache ROLE KEY for one lap of a looping node/turn: `<base_role>-<run_id>-<cycle>`
    (e.g. `draft-5a2485e5-0`, `audit-5a2485e5-0`, `fix-5a2485e5-2`). Every node that runs the same
    stage/role more than once per run (graph.py's draft/audit/verify-fix nodes, e2e_fix,
    test-hardening fix, readme, stack_runner's ac-test-run, rebuild's fix node) keys its ChatModel
    with this so `--resume` never replays a prior lap's whole transcript into the next one.
    """
    return f"{base_role}-{run_id}-{cycle}"


def lap_role_keys(run_id: str, cycle: int) -> tuple[str, str]:
    """(draft, audit) keys for one stage lap -- `lap_role` for the two roles every StageSpec with
    an audit pass constructs. graph.py's `_lap_role_keys(state, stage_key)` is the state-reading
    wrapper; a deterministic_verify (which gets run_id and the lap counter, not state) calls
    `lap_role("audit", run_id, lap)` directly."""
    return lap_role("draft", run_id, cycle), lap_role("audit", run_id, cycle)


def role_matches(role: str, base_role: str) -> bool:
    """Whether `role` is logically `base_role` -- true both for the bare label itself (a
    self-check fixture, or a genuinely single-shot caller with no lap concept) and for the real
    per-lap form `lap_role(base_role, run_id, cycle)` this module builds. The read-side
    counterpart to `lap_role`: every caller that RECOGNIZES a role rather than constructs one
    (the Stop-hook activation switch in claude_chat_model.py/copilot_chat_model.py, the warning
    guard in gates/skill_gate.py's `invoked_skills`) must call this, never write its own
    `role == base_role` or `role.startswith(f"{base_role}-")` -- see this module's own docstring
    for the incident that class of hand-written check produced.

    A per-lap key is always `f"{base_role}-{run_id}-{cycle}"`, so `role.startswith(f"{base_role}-")`
    is sufficient once the exact-match case is also covered -- `run_id`/`cycle` are never empty in
    a real key, so this cannot false-match a different base_role sharing a prefix (e.g. "draft"
    vs. a hypothetical "draft2": "draft2-r-0" does not start with "draft-").
    """
    return role == base_role or role.startswith(f"{base_role}-")


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.session_roles` (pure, no sandbox/DB needed)."""
    assert lap_role("draft", "5a2485e5", 0) == "draft-5a2485e5-0"
    assert lap_role("fix", "r", 2) == "fix-r-2"
    assert lap_role_keys("5a2485e5", 0) == ("draft-5a2485e5-0", "audit-5a2485e5-0")

    # role_matches: bare label, real per-lap form, and the negative cases that must NOT match --
    # including the exact "draft" vs "draft2"-style prefix collision this shape could false-hit
    # if it only checked `startswith` without the run_id/cycle suffix guaranteeing a "-" boundary.
    assert role_matches("draft", "draft")
    assert role_matches("draft-73bc09ec-0", "draft")
    assert not role_matches("audit-73bc09ec-0", "draft")
    assert not role_matches("draft2-r-0", "draft"), "a different base_role sharing a prefix must not match"
    assert not role_matches("", "draft")

    print("session_roles self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.session_roles
    _demo()
