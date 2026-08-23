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
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from .prompt_loader import load_prompt_pair, render_prompt

from . import git_ops, model_config, repo_files, run_failure, stack_runner, test_results
from .chat_model import get_chat_model_for_thread
from .infra_retry import call_with_infra_retry
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


# Where the TDD-red gate's suite run tees its console output (same convention as the AC gate's
# AC_TEST_OUTPUT_PATH; separate file so the two runs never clobber each other's evidence).
_RED_GATE_OUTPUT_PATH = "agent-work/red-gate-output.txt"


def red_gate_verdict(outcomes: dict[str, str]) -> tuple[bool, list[str], int]:
    """(all_red, passing test names, failed count) over runner-reported outcomes. Pure.

    Vacuous red is a FAIL: zero parsed outcomes means the suite never demonstrably ran, and "all
    zero tests are failing" must not open the gate."""
    passed = sorted(name for name, outcome in outcomes.items() if outcome == "pass")
    failed = sum(1 for outcome in outcomes.values() if outcome == "fail")
    return (not passed and failed > 0), passed, failed


async def _verify_all_red(thread_id: str) -> tuple[bool, str]:
    """Deterministic TDD-red gate: run the suite, parse the runners' own structured reports, and
    require zero passing tests (and at least one failing). The scaffold fix node is INSTRUCTED to
    keep tests failing at runtime; this is the check that stops an over-implemented scaffold --
    an accidental green here means a test that will never have its "watch it fail" moment."""
    from .gates.ac_coverage_gate import AcTestRunReport  # local: avoids import at module load

    provider = get_sandbox_provider()
    await provider.exec_in_sandbox(thread_id, f"rm -f {shlex.quote(_RED_GATE_OUTPUT_PATH)}")
    report = await stack_runner.run_and_report(
        thread_id,
        stage_key="red-gate",
        prompt_name="ac_test_run",
        schema=AcTestRunReport,
        output_path=_RED_GATE_OUTPUT_PATH,
    )
    outcomes: dict[str, str] = {}
    for artifact in report.result_artifacts or []:
        rel = test_results.repo_relative(artifact)
        contents = await repo_files.read_repo_file(provider, thread_id, rel) if rel else None
        if not contents:
            continue
        parsed = (
            test_results.parse_trx(contents)
            or test_results.parse_vitest_json(contents)
            or test_results.playwright_outcomes(contents)
        )
        outcomes = test_results.merge_outcomes(outcomes, parsed)

    all_red, passed, failed = red_gate_verdict(outcomes)
    if not outcomes:
        return False, (
            "TDD-red gate: could not verify a single test outcome -- the suite run produced no "
            f"parseable runner report ({report.error or 'no result_artifacts reported'}). Re-run "
            "the suite with a machine-readable reporter (.trx / vitest-json / playwright-json); "
            "the pipeline does not proceed until every test demonstrably FAILS."
        )
    if not all_red:
        names = ", ".join(passed[:10]) + (f", and {len(passed) - 10} more" if len(passed) > 10 else "")
        return False, (
            f"TDD-red gate: {len(passed)} test(s) PASSED after scaffolding ({failed} failed): "
            f"{names}. Scaffolding must not implement behavior -- strip those code paths back to "
            "NotImplementedException-style stubs so every test fails at runtime; a test that "
            "passes before the implementation stage proves nothing."
        )
    return True, f"TDD-red verified: 0 passed / {failed} failed."


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

        # TDD-red gate, scaffold placement only: a green build is necessary but NOT sufficient --
        # the suite must also RUN with zero passing tests before the implementation stage may
        # start. A red-gate violation re-enters the same bounded fix loop (the fix prompt gets the
        # passing test names via last_stderr_tail); at the cap the run ENDs with run_failure.
        red_detail = ""
        red_failed = False
        # Only while the implementation stage has NOT yet run: on a resumed thread with
        # minimal-code-to-green already approved, the suite is legitimately GREEN here -- observed
        # live (s04 run 7): the red gate on a resume stripped the finished implementation back to
        # stubs to satisfy all-red, mctg was hydrated-skipped, and test-hardening flagged the
        # wreckage as a stable regression.
        mctg_status = ((state.get("stages") or {}).get("minimal-code-to-green") or {}).get("status")
        if build_ok and spec.fix_scope == "scaffold_only" and mctg_status != "approved":
            red_ok, red_detail = await _verify_all_red(thread_id)
            if not red_ok:
                build_ok = False
                red_failed = True

        rb["status"] = "clean" if build_ok else "failed"
        rb["last_exit_ok"] = build_ok
        rb["last_stdout_tail"] = (report.stdout_tail or "")[-4000:]
        # A red-gate violation replaces the (green) build's stderr as the fix node's feedback --
        # the passing test names are the actionable part, not a clean compiler log.
        rb["last_stderr_tail"] = (red_detail if red_failed else (report.stderr_tail or report.error or ""))[-4000:]
        rebuild[spec.key] = rb

        ledger_entry: dict[str, Any] = {"stage": spec.key, "node": "rebuild", "ok": build_ok, "cycle": rb["fix_cycle_count"]}
        if red_detail:
            ledger_entry["red_gate"] = red_detail[:300]
        await repo_files.append_ledger_entry(provider, thread_id, ledger_entry)
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
            model_name=model_config.get_model_name("rebuild", "draft", state["provider"]) or model_config.get_model_name("plan", "draft", state["provider"]),
            sandbox=sandbox_registry.get(thread_id),
            available_tools=[
                "builtin:view", "builtin:grep", "builtin:glob", "builtin:bash",
                "builtin:edit", "builtin:create", "builtin:apply_patch", "builtin:skill",
            ],
            # Always autopilot -- a fix node's whole purpose requires write access.
            agent_mode="autopilot",
        )
        try:
            await call_with_infra_retry(
                lambda: model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)]),
                label=f"rebuild-{spec.key}:fix",
            )
        except (TimeoutError, RuntimeError) as exc:
            # A Copilot session failure that survived infra_retry's own backoff attempts. No new
            # separate counter here (unlike make_draft_node/make_verify_node): fix_node has no
            # routing decision of its own -- rebuild_node's NEXT build-check run is what decides
            # pass/fail, driven only by fix_cycle_count vs max_fix_cycles. Tagging last_stderr_tail
            # lets make_escalate_node's failure_type classification (run_failure.py) correctly
            # report infra_transient/quota_exhausted if this stage does eventually escalate,
            # instead of looking like a genuine build defect.
            logger.warning("rebuild fix infra-exhausted for %s -- counting the lap: %s", spec.key, exc)
            rb["last_stderr_tail"] = f"[infra failure, fix lap not attempted] {exc}"[-4000:]

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
        payload = await run_failure.record_run_failure_and_reset(
            thread_id, state.get("run_id"),
            payload=payload,
            detail_for_classification=f"{rb['last_stdout_tail']} {rb['last_stderr_tail']}",
        )
        rebuild = {key: dict(value) for key, value in (state.get("rebuild") or {}).items()}
        rebuild.setdefault(spec.key, default_rebuild_state())
        rebuild[spec.key]["fix_cycle_count"] = 0
        rebuild[spec.key]["cannot_verify"] = False
        return {"rebuild": rebuild, "run_failure": payload}

    return escalate_node


def _demo() -> None:
    """`cd agent && uv run python -m src.rebuild`."""
    # Vacuous red is a FAIL: no parsed outcomes must not open the gate.
    ok, passed, failed = red_gate_verdict({})
    assert not ok and passed == [] and failed == 0
    ok, _, failed = red_gate_verdict({"a": "fail", "b": "fail"})
    assert ok and failed == 2
    ok, passed, _ = red_gate_verdict({"a": "fail", "b": "pass"})
    assert not ok and passed == ["b"]
    print("rebuild red-gate self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
