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

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from .prompt_loader import load_prompt_pair, render_prompt

from . import git_ops, model_config, repo_files, stack_runner
from .copilot_chat_model import get_chat_model_for_thread
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .schemas import StageReport

FixScope = Literal["scaffold_only", "full"]


class BuildVerifyReport(StageReport):
    """What the build-verification agent must report (prompts/rebuild_verify.md)."""

    ok: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""


class RebuildState(TypedDict):
    status: Literal["not_started", "clean", "failed", "fixing"]
    fix_cycle_count: int
    last_stdout_tail: str
    last_stderr_tail: str
    last_exit_ok: bool
    cannot_verify: bool  # sandbox missing at run time -- the build never ran, escalate not pass


def default_rebuild_state() -> RebuildState:
    return {"status": "not_started", "fix_cycle_count": 0, "last_stdout_tail": "", "last_stderr_tail": "", "last_exit_ok": False, "cannot_verify": False}


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RebuildSpec:
    key: str
    max_fix_cycles: int
    fix_prompt_addendum: str
    fix_scope: FixScope
    next_node: str


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
        # Clear the sticky no-sandbox flag: it survives END-terminated runs in the checkpoint,
        # and the router checks it FIRST -- without this a healthy resubmit insta-fails.
        rb["cannot_verify"] = False

        # GHCP finds every buildable project and builds it from the right directory, then reports
        # through a schema-validated terminal tool. Replaces "an audit model guesses a build
        # command + root, Python runs `cd {root} && {command}` blindly" -- that guess was wrong on
        # every headless run (a greenfield monorepo has nothing buildable at the repo root, so
        # `dotnet build` died with MSB1003 in ~2s and this node silently escalated every time).
        report = await stack_runner.run_and_report(
            thread_id,
            stage_key="rebuild",
            prompt_name="rebuild_verify",
            schema=BuildVerifyReport,
            addendum=spec.fix_prompt_addendum or "",
        )
        build_ok = report.success and report.ok

        rb["status"] = "clean" if build_ok else "failed"
        rb["last_exit_ok"] = build_ok
        rb["last_stdout_tail"] = (report.stdout_tail or "")[-4000:]
        rb["last_stderr_tail"] = (report.stderr_tail or report.error or "")[-4000:]
        rebuild[spec.key] = rb

        await repo_files.append_ledger_entry(
            provider, thread_id, {"stage": spec.key, "node": "rebuild", "ok": build_ok, "cycle": rb["fix_cycle_count"]}
        )
        if build_ok:
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
        # bled plan's conversation across every R placement. Dedicated "rebuild" model entry
        # (falling back to plan's) -- the fixer needs codegen-tier capability regardless of how
        # cheap the drafting roster is.
        #
        # No custom_agents: confirmed live (twice -- ac-to-tests, then minimal-code-to-green) that
        # a session created with custom_agents silently loses part of the agent's own declared
        # `tools:` list, leaving a "fixer" that cannot edit anything and answers in prose instead.
        # available_tools IS honored, so the tool set is declared here.
        model = get_chat_model_for_thread(
            thread_id,
            f"rebuild-{spec.key}",
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name("rebuild", "draft") or model_config.get_model_name("plan", "draft"),
            sandbox=sandbox_registry.get(thread_id),
            available_tools=[
                "builtin:view", "builtin:grep", "builtin:glob", "builtin:bash",
                "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
            ],
            # Always autopilot -- a fix node's whole purpose requires write access.
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
        thread_id = config["configurable"]["thread_id"]
        rb = (state.get("rebuild") or {}).get(spec.key, default_rebuild_state())
        # R never auto-approves past a failing build -- and never pauses for a human either: the
        # run ENDs with run_failure set. Counters/flags are reset in the same return so the
        # checkpointed thread isn't poisoned for the next resubmission.
        payload = {
            "stage": spec.key,
            "type": "cannot_verify" if rb.get("cannot_verify") else "rebuild_cap_exceeded",
            "stdout_tail": rb["last_stdout_tail"],
            "stderr_tail": rb["last_stderr_tail"],
        }
        await git_ops.record_run_failure(thread_id, payload, state.get("run_id"))
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rebuild.setdefault(spec.key, default_rebuild_state())
        rebuild[spec.key]["fix_cycle_count"] = 0
        rebuild[spec.key]["cannot_verify"] = False
        return {"rebuild": rebuild, "run_failure": payload}

    return escalate_node
