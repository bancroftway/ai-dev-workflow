"""How this pipeline asks a coding agent to run something in the repo and report back.

Why this module exists: for 25+ headless runs, Python guessed the stack-specific shell command
for a generated app (`dotnet test` at the repo root, a canned vitest invocation, an LLM-guessed
build command run blindly) and the guess kept being wrong -- MSB1003, zero instrumented lines,
Playwright specs pulled into a unit run. The repo layout is knowable only by looking at it, so
the looking is delegated to a coding-agent session with real tools, and Python keeps the part
that must stay deterministic: parsing artifacts, checking freshness, applying thresholds.

The reporting contract: the session does its real work with its tools, then responds with a
final assistant message that is a single JSON object matching the stage's `schema`.
structured_output.ainvoke_structured (Task 4) is what actually enforces this -- it prompts for
that JSON, validates the response against the Pydantic schema, and on a mismatch feeds the model
back the exact validation error and asks again, up to its own retry budget, before raising. This
module's job stops at catching that raise and synthesizing the same success=False report shape it
already produces on every other failure path, so one bad stage session degrades the run instead
of crashing it.

This used to be a client-side terminal-tool call (`Tool(name="report_stage_output",
is_terminal=True)`, validated by a handler running in THIS process) that only worked because
Copilot ran as a persistent SDK session able to call back into that in-memory handler. Once
Copilot moved to per-turn CLI-exec there was no live process left for a `copilot -p` subprocess to
call back into, so that mechanism silently stopped working -- every call synthesized a
success=False report regardless of what the session actually did. ainvoke_structured's
prompt-and-validate approach needs nothing but `model.ainvoke()`, which both providers implement,
so it is now the one path for both.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from langchain_core.messages import HumanMessage, SystemMessage

from . import chat_model
from . import model_config, repo_files
from .chat_model import ainvoke_structured, get_chat_model_for_thread
from .prompt_loader import load_prompt_pair, render_prompt
from .sandbox import registry as sandbox_registry
from .schemas import StageReport

logger = logging.getLogger(__name__)

ReportT = TypeVar("ReportT", bound=StageReport)

# Appended to prompts the same way WELL_FORMED_JSON_RULES is. Its value is NOT the list -- the
# session's own skill.invoked events already give us that, and they cannot be forged. Its value is
# the DISAGREEMENT: a skill claimed here but absent from the log lands in the stage record's
# `unsubstantiated`, which is the fabrication signature that has cost this pipeline the most (one
# session reported "Added required Playwright e2e skeleton files (config + spec)" on four
# consecutive turns having made zero write calls).
SKILLS_REPORT_RULES = """
Also report `skills_invoked`: the exact names of the skills you invoked with your `skill` tool during
this turn, and only those. Do not list a skill you merely read about, intended to use, or believe
would have been appropriate. This is cross-checked against the session's own recorded skill
invocations, so a name you did not actually invoke is visible as an unsubstantiated claim -- an empty
list is a perfectly good answer, an inaccurate one is not.
"""

# Appended to every prompt this module sends. Belt-and-braces next to ainvoke_structured's own
# schema validation: that validate-and-retry loop is what actually guarantees shape, but nudging
# the model away from fenced/decorated payloads up front costs nothing and cuts the reject-retry
# round trips.
WELL_FORMED_JSON_RULES = """
Reporting rules (strict):
- Report ONLY by responding with the JSON object described in your instructions, as your final
  message. Your work is not complete until you send that JSON -- do not finish with a prose
  answer instead.
- Pass real JSON values: double-quoted keys and strings, no trailing commas, no code fences, no
  commentary before or after the JSON, booleans as true/false (never "true"), numbers unquoted.
- Every required field must be present. If something failed, set success=false and put the real
  reason in `error` -- never report success for work you did not actually complete.
- Report only what you actually observed. Do not report a file path you did not create, or a
  command result you did not see.
""".strip()


async def run_and_report(
    thread_id: str,
    *,
    stage_key: str,
    prompt_name: str,
    schema: type[ReportT],
    available_tools: list[str] | None = None,
    model_name: str | None = None,
    **render_values: str,
) -> ReportT:
    """Run one coding-agent session that does real work in the sandbox and reports through `schema`.

    Always returns a report -- never raises for model misbehavior. If the session ends without a
    valid report (or there is no sandbox at all), a success=False report is synthesized so the
    caller routes into its own existing failure path instead of the run dying. Every outcome,
    including the synthesized failure, is appended to the ledger before returning.
    """
    sandbox = sandbox_registry.get(thread_id)
    if sandbox is None:
        return await _ledger(
            thread_id,
            stage_key,
            schema(success=False, ready_for_next_stage=False, error="no sandbox available -- nothing was run"),
            sandboxed=False,
        )

    try:
        model = get_chat_model_for_thread(
            thread_id,
            stage_key,
            "draft",
            model_name=model_name or model_config.get_model_name(stage_key, "draft", chat_model.PROVIDER) or model_config.get_model_name("stack-run", "draft", chat_model.PROVIDER),
            sandbox=sandbox,
            agent_mode="autopilot",
            available_tools=available_tools,
        )
        system, template = load_prompt_pair(prompt_name)
        prompt = render_prompt(template, **render_values)
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=f"{prompt}\n\n{WELL_FORMED_JSON_RULES}\n{SKILLS_REPORT_RULES}"),
        ]
        # ainvoke_structured owns the "no report -> nudge -> give up" loop internally (its own
        # validate-and-retry, up to its own attempt budget) -- a turn ending in prose instead of
        # the required JSON (observed live: the coverage session answered in text and never
        # reported) is exactly the mismatch it retries on, so nothing here needs to duplicate that
        # loop. It either returns a valid `schema` instance or raises once its retries are spent,
        # which the except below turns into the same synthesized failure as any other session error.
        report = await ainvoke_structured(model, messages, schema)
    except Exception as exc:  # noqa: BLE001 -- a failed stage must not kill the run
        logger.warning("stack_runner session failed for stage_key=%s", stage_key, exc_info=True)
        return await _ledger(
            thread_id,
            stage_key,
            schema(success=False, ready_for_next_stage=False, error=f"session error: {exc}"),
        )
    return await _ledger(thread_id, stage_key, report)


async def _ledger(thread_id: str, stage_key: str, report: ReportT, *, sandboxed: bool = True) -> ReportT:
    """The choke point that makes "every stage reports" structurally true rather than a
    convention: nothing returns from run_and_report without passing through here."""
    if sandboxed and sandbox_registry.get(thread_id) is not None:
        from .sandbox.factory import get_sandbox_provider

        try:
            await repo_files.append_ledger_entry(
                get_sandbox_provider(),
                thread_id,
                {
                    "stage": stage_key,
                    "node": "stage_report",
                    "success": report.success,
                    "ready_for_next_stage": report.ready_for_next_stage,
                    "error": report.error,
                    "summary": report.summary,
                    "artifacts": report.artifacts,
                },
            )
        except Exception:  # noqa: BLE001 -- ledger write must never mask the report itself
            logger.warning("failed to ledger stage report for %s", stage_key, exc_info=True)
    return report
