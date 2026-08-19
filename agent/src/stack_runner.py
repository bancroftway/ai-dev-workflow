"""The one way this pipeline asks GHCP to run something in the repo and report back.

Why this module exists: for 25+ headless runs, Python guessed the stack-specific shell command
for a generated app (`dotnet test` at the repo root, a canned vitest invocation, an LLM-guessed
build command run blindly) and the guess kept being wrong -- MSB1003, zero instrumented lines,
Playwright specs pulled into a unit run. The repo layout is knowable only by looking at it, so
the looking is delegated to a GHCP session with real tools, and Python keeps the part that must
stay deterministic: parsing artifacts, checking freshness, applying thresholds.

The reporting contract (proven by spike before this module existed):

    Tool(name="report_stage_output", parameters=<the stage's JSON schema>, is_terminal=True)

A schema-VALID call ends the agent's turn -- it is the stage's only exit door. An INVALID call is
rejected by _make_report_tool's handler, which runs in THIS process, and returns the exact
Pydantic errors as the tool result; per the SDK's own contract a failed terminal call leaves the
agent loop running, so the model reads the field-level feedback and calls again. No re-prompting,
no parsing prose. Note that a terminal tool call means there may be NO final assistant text at
all, which is why nothing here uses copilot_chat_model.ainvoke_structured.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from copilot import Tool
from copilot.tools import ToolResult

from . import model_config, repo_files
from .copilot_chat_model import get_chat_model_for_thread
from .prompt_loader import load_prompt_pair, render_prompt
from .sandbox import registry as sandbox_registry
from .schemas import StageReport

logger = logging.getLogger(__name__)

ReportT = TypeVar("ReportT", bound=StageReport)

REPORT_TOOL_NAME = "report_stage_output"

# Appended to every prompt this module sends. Belt-and-braces next to the tool's own schema
# enforcement: the tool boundary is what actually guarantees shape, but nudging the model away
# from fenced/decorated payloads costs nothing and cuts the reject-retry round trips.
WELL_FORMED_JSON_RULES = """
Reporting rules (strict):
- Report ONLY by calling the `report_stage_output` tool. Your work is not complete until that
  call succeeds -- do not finish with a prose answer instead.
- Pass real JSON values: double-quoted keys and strings, no trailing commas, no code fences, no
  commentary inside the arguments, booleans as true/false (never "true"), numbers unquoted.
- Every required field must be present. If something failed, set success=false and put the real
  reason in `error` -- never report success for work you did not actually complete.
- Report only what you actually observed. Do not report a file path you did not create, or a
  command result you did not see.
""".strip()


# (thread_id, stage_key) -> the last validated report from that session's reporting tool.
#
# Deliberately module-level rather than a per-call local: copilot_chat_model caches one Copilot
# session per (thread, stage, role), and `tools` are only registered on the CREATE call, so the
# handler closure from the FIRST invocation is the one that stays live for every later invocation
# on that session. A per-call dict therefore looked empty on every reuse -- observed live on the
# rebuild node, which runs once per fix lap against one cached session. Keying the shared store by
# session identity makes the reused handler and the current caller agree on where the report goes.
_STASHES: dict[tuple[str, str], Any] = {}


def _make_report_tool(schema: type[ReportT], stash_key: tuple[str, str]) -> Tool:
    """The validating exit door. Handler runs client-side (SDK round-trips the call to us over
    HandlePendingToolCall), so this Pydantic validation is genuinely OUR check, not the model's
    self-assessment."""

    def handler(invocation: Any) -> ToolResult:
        try:
            _STASHES[stash_key] = schema.model_validate(invocation.arguments)
        except ValidationError as exc:
            logger.info("report_stage_output rejected: %s validation error(s)", exc.error_count())
            return ToolResult(
                text_result_for_llm=(
                    "Your report did NOT match the required schema and was rejected. Fix exactly "
                    f"these problems and call `{REPORT_TOOL_NAME}` again:\n{exc}"
                ),
                result_type="failure",
                error="schema validation failed",
            )
        return ToolResult(text_result_for_llm="Report accepted.", result_type="success")

    return Tool(
        name=REPORT_TOOL_NAME,
        description=(
            "Report your final results for this stage. Your work is not complete until this call "
            "succeeds. If the arguments do not match the schema you will get the validation "
            "errors back and must call again with them fixed."
        ),
        parameters=schema.model_json_schema(),
        handler=handler,
        is_terminal=True,
    )


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
    """Run one GHCP session that does real work in the sandbox and reports through `schema`.

    Always returns a report -- never raises for model misbehavior. If the session ends without a
    valid call (or there is no sandbox at all), a success=False report is synthesized so the
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

    stash_key = (thread_id, stage_key)
    _STASHES.pop(stash_key, None)  # never read a previous lap's report
    # The report tool must be reachable when an allowlist is in play, or the model has no exit
    # door at all. Source-qualified ("custom:") per copilot._mode.ToolSet's own convention.
    tools_allowlist = [*available_tools, f"custom:{REPORT_TOOL_NAME}"] if available_tools else None

    try:
        model = get_chat_model_for_thread(
            thread_id,
            stage_key,
            "draft",
            model_name=model_name or model_config.get_model_name(stage_key, "draft") or model_config.get_model_name("stack-run", "draft"),
            sandbox=sandbox,
            agent_mode="autopilot",
            available_tools=tools_allowlist,
            tools=[_make_report_tool(schema, stash_key)],
        )
        system, template = load_prompt_pair(prompt_name)
        prompt = render_prompt(template, **render_values)
        messages = [SystemMessage(content=system), HumanMessage(content=f"{prompt}\n\n{WELL_FORMED_JSON_RULES}")]

        # A turn can end with prose instead of the required tool call (observed live: the coverage
        # session answered in text and never reported). The tool is the ONLY accepted exit, so a
        # silent turn gets one explicit nudge before we give up -- cheaper than failing a whole
        # gate cycle, and the model still has its full session context to report from.
        for attempt in range(2):
            await model.ainvoke(messages)
            if stash_key in _STASHES:
                break
            if attempt == 0:
                logger.info("stack_runner: %s ended without a report -- nudging once", stage_key)
                messages = [
                    HumanMessage(
                        content=(
                            f"You ended your turn without calling `{REPORT_TOOL_NAME}`, so none of "
                            "your work has been recorded. Call it NOW with the results of what you "
                            "just did. Report only what you actually observed; if the work could "
                            "not be completed, call it with success=false and the real reason in "
                            "`error`."
                        )
                    )
                ]
    except Exception as exc:  # noqa: BLE001 -- a failed stage must not kill the run
        logger.warning("stack_runner session failed for stage_key=%s", stage_key, exc_info=True)
        return await _ledger(
            thread_id,
            stage_key,
            schema(success=False, ready_for_next_stage=False, error=f"session error: {exc}"),
        )

    report = _STASHES.pop(stash_key, None)
    if report is None:
        return await _ledger(
            thread_id,
            stage_key,
            schema(
                success=False,
                ready_for_next_stage=False,
                error=f"session ended without a valid {REPORT_TOOL_NAME} call, even after a nudge",
            ),
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
