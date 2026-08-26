"""Per-stage, per-role, per-provider model configuration (SPECIFICATION.md Section 3.4).

Loaded once from agent/config/models.yaml -- editable without touching code, same principle as
agent/src/prompts/*.md. Replaces the old single global COPILOT_MODEL_NAME env var. Every stage now
nests one full config per AGENT_PROVIDER value (see models.yaml's own header comment); callers
pass which one they want rather than this module assuming Copilot, the only provider that existed
when this file was first written.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "models.yaml"

Stage = Literal[
    "tech-stack",
    "specification",
    "plan",
    "ac-to-tests",
    "minimal-code-to-green",
    "remediation",
    "adversarial-compliance",
    "metrics-exit",
    "brownfield-baseline",
    "rebuild",
    "e2e",
    "e2e-run",
    "coverage-run",
    "ac-test-run",
    "stack-run",
    "test-hardening-run",
    "test-hardening-flake-triage",
    "metrics-report",
    "readme",
    "session-title",
]
# "fix" is a WRITE-capable pass that closes what a stage's deterministic verify reported (see
# graph.make_verify_fix_node). Declared so a stage can give it a different model from its draft.
Role = Literal["draft", "audit", "extract", "fix"]


@lru_cache(maxsize=None)
def _load_config() -> dict[str, dict[str, dict[str, str | None]]]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_model_name(stage: Stage, role: Role, provider: str) -> str | None:
    # provider is an explicit, required parameter rather than this module calling
    # chat_model.get_provider() itself, for the same reason chat_model.py's own 7 dispatch
    # functions take an explicit provider argument instead of resolving it themselves (Ruling 4,
    # docs/superpowers/plans/part-4-org-settings-tasks.md): a stage's model choice must stay
    # pinned to the run's own GraphState.provider for the run's whole lifetime, not drift mid-run
    # if this function re-resolved the org's live setting instead of being told. Every real call
    # site already has a pinned provider in hand (state["provider"], or chat_model.get_provider()
    # called once by provisioning-time code with no state yet) and passes it straight through.
    stage_config = _load_config().get(stage, {}).get(provider, {})
    draft_model = stage_config.get("draft_model")
    if role in ("draft", "extract"):
        # "extract" deliberately shares draft_model rather than needing its own models.yaml entry
        # -- a one-shot structured-extraction pass is not a config-worthy decision distinct from
        # drafting, unlike audit (a genuinely separate model choice, hence its own warning below).
        return draft_model

    if role == "fix":
        # Explicit branch, because without it "fix" fell through to the audit lookup below: the
        # `fix_model` key was silently ignored (dead config) and every fix pass logged "No
        # audit_model configured", which is a misleading thing to print about a write pass.
        # Falls back to draft_model rather than audit_model -- fixing is a code-writing job, and
        # the audit tier is chosen to critique, sometimes on a cheaper model.
        return stage_config.get("fix_model") or draft_model

    audit_model = stage_config.get("audit_model")
    if audit_model is None:
        logger.warning(
            "No audit_model configured for stage %r provider %r in models.yaml; falling back "
            "to draft_model %r. The audit pass will run, but against the same model as the "
            "draft it's meant to critique.",
            stage,
            provider,
            draft_model,
        )
        return draft_model
    return audit_model


def _demo() -> None:
    """Self-check for the provider-keyed lookup added by Task 10 (part-1-provider-unification):
    real models.yaml content, not a hand-built fixture, so a nesting mistake in the YAML itself
    (wrong indentation collapsing copilot/claude together, a stage missing a provider block) fails
    this assert exactly like it would fail a live run.
    """
    # "specification" has an explicit audit_model on both providers -- values must resolve
    # correctly AND differ per provider, or a stage would silently run the wrong vendor's model.
    assert get_model_name("specification", "draft", "copilot") == "gpt-5.4-mini"
    assert get_model_name("specification", "draft", "claude") == "haiku"
    assert get_model_name("specification", "audit", "copilot") == "gemini-3.6-flash"
    assert get_model_name("specification", "audit", "claude") == "sonnet"

    # "ac-to-tests" joined the audited stages 2026-08-24 -- same shape as "specification" above.
    # Claude audit was opus (2026-08-24 user default) until the 2026-08-26 cheaper-models
    # directive dropped every opus leg to sonnet (two runs died on the 5h usage window).
    assert get_model_name("ac-to-tests", "draft", "copilot") == "gpt-5.3-codex"
    assert get_model_name("ac-to-tests", "draft", "claude") == "sonnet"
    assert get_model_name("ac-to-tests", "audit", "copilot") == "gemini-3.6-flash"
    assert get_model_name("ac-to-tests", "audit", "claude") == "sonnet"
    # ...and nothing on the Claude side asks for opus at all now -- the whole roster fits the
    # cheap tiers. A reintroduction should be a deliberate decision that updates this assert.
    config_all = _load_config()
    assert not any(
        v == "opus"
        for stage_cfg in config_all.values()
        for m in (stage_cfg.get("claude") or {}).values()
        for v in [m]
    ), "an opus leg reappeared in the claude roster -- deliberate? update this assert"

    # "tech-stack" has no audit_model on either provider -- audit must fall back to that SAME
    # provider's own draft_model, never leaking across to the other provider's value.
    assert get_model_name("tech-stack", "draft", "copilot") == "gpt-5.4-mini"
    assert get_model_name("tech-stack", "draft", "claude") == "haiku"
    assert get_model_name("tech-stack", "audit", "copilot") == get_model_name("tech-stack", "draft", "copilot")
    assert get_model_name("tech-stack", "audit", "claude") == get_model_name("tech-stack", "draft", "claude")
    assert get_model_name("tech-stack", "audit", "copilot") != get_model_name("tech-stack", "audit", "claude")

    # Phase E audit M-5: "e2e-run"/"test-hardening-run" are declared Stage values that genuinely
    # call get_model_name (via stack_runner.run_and_report's own fallback chain -- e2e_nodes.py and
    # test_hardening_nodes.py never pass an explicit model_name). get_model_name's return value
    # can't distinguish "key absent" from "key present with an explicit null" -- both resolve to
    # None -- so the only thing worth asserting here is that models.yaml actually DECLARES them
    # (an explicit, documented null) rather than leaving them silently missing from the file, which
    # is exactly what M-5 flagged. A future regression that deletes these blocks again fails this.
    config = _load_config()
    assert "e2e-run" in config, "e2e-run must have an explicit models.yaml block (Phase E audit M-5)"
    assert "test-hardening-run" in config, "test-hardening-run must have an explicit models.yaml block (Phase E audit M-5)"
    # W7: readme_write_node's write-capable pass -- an unlisted stage silently resolves to None
    # (the CLI picks its own default), so both providers must carry a real draft_model here.
    assert get_model_name("readme", "draft", "copilot"), "readme stage needs a copilot draft_model"
    assert get_model_name("readme", "draft", "claude"), "readme stage needs a claude draft_model"
    assert get_model_name("e2e-run", "draft", "copilot") is None
    assert get_model_name("test-hardening-run", "draft", "claude") is None

    print("model_config self-check: provider-keyed draft/audit lookups all resolved correctly")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.model_config` loads this file
    # as "__main__", so a direct `_demo()` call would import this module a second time as a
    # non-package import, splitting `_load_config`'s lru_cache across two sys.modules entries.
    # Re-dispatching through `from src.model_config import` ensures there is only one copy of this
    # module in sys.modules. This convention is unconditional across this codebase (see
    # cli_agent_exec.py, claude_chat_model.py, chat_model.py).
    from src.model_config import _demo as _packaged_demo

    _packaged_demo()
