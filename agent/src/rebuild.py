"""R -- the reusable "clean & rebuild" node (plan's Part B, section R).

No LLM at all in the happy path: `make_rebuild_node` runs a stack-appropriate clean+build command
and gates on its exit code. Only on failure does an LLM-backed fix node run, and even then with a
fix_scope-restricted prompt (see RebuildSpec.fix_scope's docstring) rather than unrestricted access
-- R's gate is "does it build," never "do tests pass," and two different placements in the
pipeline need two very different answers to "what is the fix node allowed to touch."

Kept as its own module (not folded into graph.py) since RebuildSpec/RebuildState are a genuinely
different node shape from StageSpec's draft->audit->gate template -- graph.py's build_graph()
wires make_rebuild_node/make_fix_node in at each of R's several placements (after P4, after P6,
after quality-remediation, after security-remediation, after audit-cluster), each with a different fix_scope and next_node.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from .prompt_loader import load_prompt_pair, render_prompt
from langgraph.types import interrupt

from . import model_config, repo_files
from .copilot_chat_model import get_chat_model_for_thread
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

FixScope = Literal["scaffold_only", "full"]


class RebuildState(TypedDict):
    status: Literal["not_started", "clean", "failed", "fixing"]
    fix_cycle_count: int
    last_stdout_tail: str
    last_stderr_tail: str
    last_exit_ok: bool
    cannot_verify: bool  # sandbox missing at run time -- the build never ran, escalate not pass


def default_rebuild_state() -> RebuildState:
    return {"status": "not_started", "fix_cycle_count": 0, "last_stdout_tail": "", "last_stderr_tail": "", "last_exit_ok": False, "cannot_verify": False}


# Each fragment is guarded by a test for the config file preflight_nodes.apply_stack_conventions
# writes. That test, not a state flag, is what keeps a repo whose install failed (or whose own
# lint config this pipeline deliberately deferred to) from being asked to pass a check that was
# never set up -- the file's presence on disk is the only signal that is always true.
_ESLINT_FRAGMENT = (
    "if [ -f eslint.config.mjs ]; then npx --yes eslint . --max-warnings=0; "
    "else echo 'no ai-dev-workflow eslint config -- skipping lint'; fi"
)
# `-p . --strict` verified to be a legal combination: the CLI flag overrides tsconfig's own
# setting, so a repo that opted out of strict mode is still type-checked strictly here without
# this pipeline rewriting its tsconfig.json.
_TSC_FRAGMENT = "if [ -f tsconfig.json ]; then npx --yes tsc -p . --noEmit --strict; fi"
_RUFF_FRAGMENT = "if [ -f ruff.toml ]; then ruff check .; fi"
_MYPY_FRAGMENT = "if [ -f mypy.ini ]; then mypy .; fi"


def _resolve_build_command(tech_stack: dict[str, Any]) -> str:
    """Parameterized by the tech-stack detection stage's own reported fields, not hardcoded to
    one stack -- .NET gets a real clean+build, Node/TS gets its own build (or tsc) plus the lint
    and strict typecheck that make analyzer findings fatal, Python gets ruff + mypy, and absent
    any signal a no-op success (nothing to gate on is not the same as a build failure).

    The non-.NET checks exist for the same reason `-warnaserror` does: an LLM only reliably fixes
    what a deterministic tool refuses to accept. A lint warning that does not fail the build is
    advice, and advice gets reported as "done"."""
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if tech_stack.get("dotnet_detected"):
        return "dotnet clean && dotnet build -warnaserror"
    if "typescript" in languages or "javascript" in languages:
        return (
            "if [ -f package.json ] && node -e \"process.exit(require('./package.json').scripts?.build?0:1)\"; "
            "then npm run build; else npx --yes tsc --noEmit; fi"
            f" && {_TSC_FRAGMENT} && {_ESLINT_FRAGMENT}"
        )
    if "python" in languages:
        return f"python -m py_compile $(git ls-files '*.py') && {_RUFF_FRAGMENT} && {_MYPY_FRAGMENT}"
    return "echo 'no build-command mapping for this stack -- nothing to check' && true"


@dataclass(frozen=True)
class RebuildSpec:
    key: str
    max_fix_cycles: int
    fix_prompt_addendum: str
    fix_scope: FixScope
    next_node: str
    resolve_build_command: Callable[[dict[str, Any]], str] = _resolve_build_command


def make_rebuild_node(spec: RebuildSpec):
    async def rebuild_node(state: dict[str, Any], config) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rb = rebuild.get(spec.key, default_rebuild_state())

        if sandbox_registry.get(thread_id) is None:
            # No sandbox means the build never ran. Escalate rather than declare it clean
            # (route reads cannot_verify).
            rb["status"] = "failed"
            rb["last_exit_ok"] = False
            rb["cannot_verify"] = True
            rebuild[spec.key] = rb
            return {"rebuild": rebuild}

        provider = get_sandbox_provider()
        tech_stack = (state.get("stages", {}).get("tech-stack") or {}).get("approved_content") or {}
        command = spec.resolve_build_command(tech_stack)
        result = await provider.exec_in_sandbox(thread_id, command)

        rb["status"] = "clean" if result.ok else "failed"
        rb["last_exit_ok"] = result.ok
        rb["last_stdout_tail"] = (result.stdout or "")[-4000:]
        rb["last_stderr_tail"] = (result.stderr or "")[-4000:]
        rebuild[spec.key] = rb

        await repo_files.append_ledger_entry(
            provider, thread_id, {"stage": spec.key, "node": "rebuild", "ok": result.ok, "cycle": rb["fix_cycle_count"]}
        )
        if result.ok:
            # A green build is the checkpoint where the code-writing sessions' source changes
            # (codegen, fixes) become worth keeping -- the artifact-only commit sites never stage
            # source, so without this the pushed work branch would carry no code at all.
            await git_ops.commit_all(provider, thread_id, f"ai-dev-workflow: {spec.key} source changes (build green)")
        return {"rebuild": rebuild}

    return rebuild_node


def make_route_after_rebuild(spec: RebuildSpec) -> Callable[[dict[str, Any]], str]:
    def route(state: dict[str, Any]) -> str:
        rb = (state.get("rebuild") or {}).get(spec.key, default_rebuild_state())
        if rb.get("cannot_verify"):
            return "escalate"  # no sandbox -- the build never ran; a human must see it
        if rb["last_exit_ok"]:
            return "next"
        if rb["fix_cycle_count"] < spec.max_fix_cycles:
            return "fix"
        return "escalate"

    return route


_SCAFFOLD_ONLY_ADDENDUM = (
    "You may ONLY add minimal compile-enabling scaffolding: signatures, classes, and interfaces "
    "that don't yet exist, each throwing NotImplementedException (or the stack's equivalent) in "
    "every method body. Do NOT implement real behavior. Tests must remain failing at RUNTIME after "
    "your change -- only the COMPILER's complaints are your job here. If a test fails to compile "
    "because it references a symbol that doesn't exist yet, add the minimal stub; do not make the "
    "test pass."
)


def make_fix_node(spec: RebuildSpec):
    async def fix_node(state: dict[str, Any], config) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rb = rebuild.get(spec.key, default_rebuild_state())

        addendum = _SCAFFOLD_ONLY_ADDENDUM if spec.fix_scope == "scaffold_only" else spec.fix_prompt_addendum
        system, template = load_prompt_pair("rebuild_build_fix")
        prompt = render_prompt(
            template,
            addendum=addendum,
            stdout_tail=rb["last_stdout_tail"],
            stderr_tail=rb["last_stderr_tail"],
        )

        # Own session key per placement (rebuild-<key>:draft), not plan:draft -- sharing plan's key
        # returned its cached read-only session so this autopilot fixer silently couldn't write, and
        # bled plan's conversation across every R placement. No dedicated model_config entry, so
        # reuse plan's model explicitly.
        model = get_chat_model_for_thread(
            thread_id,
            f"rebuild-{spec.key}",
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name("plan", "draft"),
            sandbox=sandbox_registry.get(thread_id),
            agent_mode="autopilot",
        )
        await model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])

        rb["fix_cycle_count"] = rb["fix_cycle_count"] + 1
        rb["status"] = "fixing"
        rebuild[spec.key] = rb
        return {"rebuild": rebuild}

    return fix_node


def make_escalate_node(spec: RebuildSpec):
    async def escalate_node(state: dict[str, Any], config) -> dict[str, Any]:
        rb = (state.get("rebuild") or {}).get(spec.key, default_rebuild_state())
        # R never auto-approves past a failing build, at any placement -- a real human decision is
        # required once max_fix_cycles is exhausted.
        interrupt(
            {
                "stage": spec.key,
                "type": "cannot_verify" if rb.get("cannot_verify") else "rebuild_cap_exceeded",
                "stdout_tail": rb["last_stdout_tail"],
                "stderr_tail": rb["last_stderr_tail"],
            }
        )
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rebuild[spec.key]["fix_cycle_count"] = 0
        return {"rebuild": rebuild}

    return escalate_node
