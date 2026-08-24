"""LangChain chat-model adapter backed by the GitHub Copilot CLI, run as a per-turn subprocess exec
inside the sandbox (cli_agent_exec.py's module docstring covers the shared runner both provider
modules build on).

This replaces the previous SDK-based implementation, which held a persistent TCP-connected
CopilotClient/CopilotSession pair open for the life of a LangGraph thread (session.send(), a
SessionEventType event stream, on_permission_request callbacks). None of that exists anymore: like
claude_chat_model.py, the CLI itself is stateless between invocations, and the whole of a session's
state is the CLI's own `session_id` string, threaded back in via `--session-id` on every turn after
the first (see _session_ids below). Full-authority mode (BR-6) is likewise no longer a
create_session(on_permission_request=PermissionHandler.approve_all) callback -- it is the
unconditional `--no-ask-user` flag _agenerate_inner passes on every turn.

This module deliberately imports nothing from the `copilot` package anymore, for the same reason
claude_chat_model.py never did: the SDK types (CopilotClient, CopilotSession, MCPServerConfig,
PreToolUseHandler, ...) only ever modeled the now-retired persistent session, and
part-1-provider-unification-tasks/progress.md's own task-dependency table requires this module's
`from copilot import ...` lines gone before a later task removes the `github-copilot-sdk` pip
dependency entirely.

UNVERIFIED against real output, stated plainly rather than papered over (task-3-brief.md): GitHub's
own reference docs confirm `copilot -p --output-format=json` emits JSONL (one JSON object per line,
unlike Claude's single terminal object), but no real invocation's actual per-line content was
sampled before writing this module -- no `copilot` binary is installed or authenticated in this dev
environment to sample one against (confirmed empirically: `which copilot` finds nothing here).
_parse_copilot_jsonl/_agenerate_inner below therefore treat the LAST parsed line as the
result-shaped summary event (by analogy with Claude's one-object shape) for
result/is_error/usage/total_cost_usd, but scan EVERY parsed line for a `session_id` field rather
than assuming its position, per the brief's explicit instruction -- the highest-risk unverified
guess in this whole module. A completely unparseable stream fails loud (RuntimeError); an
unexpected-but-parseable shape degrades to falsy defaults plus a logged warning rather than
crashing. Fixing this parser against a real container's real output is Task 12's (final
verification) named job, not a defect being hidden here.

Update (task-3-report.md, Part 2 Task 3): in a later dev environment, `which copilot` still finds
nothing (no persistent PATH install here either), but `npx --yes @github/copilot` fetches and runs
a real, authenticatable CLI on demand -- combined with a `gh auth token` credential, this produced
the first real output from this CLI seen anywhere in this project's history (task-3-report.md has
the full transcript and circumstances). It confirms the non-final-line envelope shape (a
dot-namespaced `type` string, a `data` dict, `id`/`timestamp`/`parentId`, optional `ephemeral`) but
hit a real quota_exceeded error before the model ever invoked a tool, so a real tool-call-shaped
line is still unconfirmed -- see _translate_intermediate_events' own docstring below. It also
surfaced a concrete, real gap in the session_id scan just below: the real terminal line's own
session identifier is camelCase `sessionId`, not the snake_case `session_id` this module scans
for, so real multi-turn `--session-id` continuity is very likely broken today. That is final-line
parsing, out of THIS task's scope (task-3-brief.md: "the existing final-result parsing... is
untouched") -- flagged here rather than silently carried forward uncorrected.

Update (Phase E known-bugs fix, this task): the two gaps flagged directly above are now fixed.
_parse_copilot_jsonl checks "sessionId" first (snake_case kept only as a cheap, non-contractual
fallback), so --session-id continuity now works against the real capture instead of silently never
firing. Separately, the real capture's `usage` object ({"premiumRequests", "totalApiDurationMs",
"sessionDurationMs", "codeChanges"}) shares no keys with the input_tokens/output_tokens this module
used to read with a fabricated `0` default -- _extract_usage (defined below _parse_copilot_jsonl)
now reads them with no fabricated default, so "not reported" comes back None rather than a
misleading "measured zero"; premiumRequests (the one real cost-adjacent number this CLI version
actually reports) is surfaced under its own name instead. Both fixes are proven against the same
real captured sample in _demo(), not a fixture invented to agree with the code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import uuid
from typing import Any, Literal

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from . import config
from . import run_event_store
from . import run_event_stream
from . import telemetry
from .cli_agent_exec import (
    _RESUME_REJECTED_MARKERS,
    _SCRATCH_DIR,
    ResumeState,
    SessionCache,
    classify_resume,
    flatten_messages_to_prompt,
    run_turn,
    write_scratch_file,
)
from .redaction import redact_text, redact_value
from .run_events import RunEvent, RunEventType
from .sandbox import SandboxProvider, SandboxSession, get_sandbox_provider

logger = logging.getLogger(__name__)

# Shared session-id + resume-state cache (cli_agent_exec.SessionCache -- see its docstring for the
# keying, eviction, and Phase E audit C-2 tri-state rules both providers used to duplicate here).
# This module's own instance, never shared with claude_chat_model's: evicting one provider's
# sessions must not touch the other's. The Copilot side of the resume classification is
# QUOTA-BLOCKED (module docstring: no authenticated `copilot` binary in this environment) -- the
# RULE is identical to Claude's (same shared classify_resume call), but every real-world trigger of
# it here is inference, never confirmed against a real killed-then-resumed Copilot session. The two
# dict aliases below are the SAME objects as the cache's own (not copies) -- chat_model._demo and
# this module's _demo reach them by these names.
_session_cache = SessionCache("Copilot")
_session_ids = _session_cache.session_ids
_resume_states = _session_cache.resume_states


def _messages_to_prompt(messages: list[BaseMessage]) -> str:
    """Flatten a LangChain message list into a single Copilot CLI prompt string.

    The text-flatten core is cli_agent_exec.flatten_messages_to_prompt, shared with
    claude_chat_model._messages_to_prompt (SystemMessage gets an "Instructions:" prefix, everything
    else passes through verbatim, parts joined with a blank line). Unlike Claude's wrapper, this
    one drops ALL multimodal content instead of translating any of it to an attachment. The old SDK
    version translated image_url parts into a Copilot Attachment over the live session
    (_content_part_to_attachment, now deleted along with the rest of the SDK plumbing); the
    verified Copilot CLI flags table (task-3-brief.md) has no attachment/file flag to translate one
    into over `-p`'s stdin-string interface, and no stage in this pipeline currently sends Copilot
    an image. Not attempted here as a ponytail-style deliberate cut, not an oversight -- same
    upgrade path as Claude's: mirror write_scratch_file for binary payloads and pass the result via
    a flag per part, if a real Copilot CLI flag for it is ever confirmed.
    """
    return flatten_messages_to_prompt(
        messages,
        "dropped %d non-text content part(s) -- CopilotChatModel has no multimodal "
        "support over the CLI",
    )


def _parse_copilot_jsonl(stdout: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse `copilot -p --output-format json`'s JSONL stdout into (events, session_id).

    Pure and side-effect-free so _demo can exercise the genuinely uncertain part of this module
    (see the module docstring) without a live sandbox. Each line is parsed independently so one
    malformed/non-JSON line cannot crash the whole turn -- it is simply skipped, the same defensive
    per-line handling gates/skill_gate.py already uses reading Copilot's other JSONL log
    (session-state/events.jsonl). Only dict-shaped lines are kept, so every caller can treat every
    element of `events` as a dict without a repeated isinstance check.

    session_id is the one field scanned across EVERY line rather than assumed to live only on the
    last one -- the brief's explicit defensive instruction, because this is the highest-risk
    unverified guess in this whole module. The LAST occurrence found wins, on the assumption a
    later event is at least as authoritative as an earlier one if more than one line happens to
    carry it.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed_line = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed_line, dict):
            events.append(parsed_line)

    session_id: str | None = None
    for event in events:
        # Real capture (2026-08-23, task-3-report.md): the CLI's own terminal line names this
        # field camelCase "sessionId", checked first since it is the one confirmed-real key.
        # "session_id" stays as a cheap fallback -- never observed, not contractual, but free to
        # keep in case some line/future CLI version reports the snake_case spelling instead.
        candidate = event.get("sessionId") or event.get("session_id")
        if isinstance(candidate, str) and candidate:
            session_id = candidate
    return events, session_id


def _extract_usage(final: dict[str, Any], model_name: str | None) -> dict[str, Any]:
    """Build the `_last_usage`-shaped dict from a turn's final result-shaped JSONL line.

    Pure, so _demo can assert this against the real captured sample without a live sandbox (same
    "pure half only" scoping as _parse_copilot_jsonl above) -- _agenerate_inner just calls this and
    assigns the result to self._last_usage.

    Real capture (2026-08-23, task-3-report.md): the terminal line's `usage` object is
    {"premiumRequests": 0, "totalApiDurationMs": ..., "sessionDurationMs": ..., "codeChanges":
    {...}} -- it shares NO keys with input_tokens/output_tokens, and there is no total_cost_usd
    anywhere on the line. Reading those with a fabricated `0`/None default (the old code) made
    Copilot's token/cost tracking silently report "measured zero" for every real turn, which is a
    different claim than the true one: this CLI version does not report a token count or a dollar
    cost at all. `.get()` with no fabricated default is used instead, so a genuinely absent number
    comes back None -- the same "never fabricate a 0" principle claude_chat_model.py's own
    _last_usage comment states. Old snake_case keys are kept as the read path (not renamed) in case
    a future CLI version reports them; premiumRequests -- GitHub Copilot's own billing unit, the one
    real cost-adjacent number this version does report -- is surfaced under its own honest name
    rather than mislabeled as a token count or a dollar cost, which it is neither.
    """
    usage = final.get("usage") or {}
    return {
        "model": model_name or "default",
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cost": final.get("total_cost_usd"),
        "premium_requests": usage.get("premiumRequests"),
    }


def _translate_intermediate_events(
    intermediate_events: list[dict[str, Any]],
    *,
    run_id: str,
    session_id: str,
    stage: str,
    node: str,
) -> list[RunEvent]:
    """Translate Copilot's intermediate (non-final) parsed JSONL lines into Task-1-shaped
    RunEvents, tool-call granularity where a line represents one (task-3-brief.md). `_agenerate_inner`
    passes `events[:-1]` here -- every parsed line EXCEPT the last, which stays the result-shaped
    summary line handled separately and left untouched by this task.

    Not every intermediate line becomes a RunEvent: only ones this function can positively identify
    as tool-call-shaped do (task-3's explicit instruction -- "not every line necessarily is one").
    Everything else (session/status bookkeeping, plain assistant text, ...) is silently skipped,
    the same defensive-skip spirit _parse_copilot_jsonl already applies to a malformed line.

    CONFIRMED REAL (task-3-report.md: a real, authenticated `copilot -p --output-format json`
    invocation, captured 2026-08-23 via `npx --yes @github/copilot` + a `gh auth token` credential
    -- the first real output from this CLI seen anywhere in this project's history): every
    intermediate line carries a dot-namespaced `type` string ("session.mcp_server_status_changed",
    "user.message", "assistant.turn_start", "model.call_start", "assistant.turn_end",
    "assistant.idle", ...), an event-specific `data` dict (sometimes empty), an `id`, a `timestamp`,
    and a `parentId`; some additionally carry `ephemeral: true` for transient UI-status noise. That
    real capture hit a real quota_exceeded error before the model ever invoked a tool, so it
    contains ZERO real tool-call-shaped lines -- there is no confirmed-real example of one anywhere
    yet (module docstring). The `namespace == "tool"` check below is therefore a defensible
    INFERENCE from the one confirmed-real naming convention above (a tool call plausibly lives
    under its own single-word "tool" domain, exactly the way session/user/assistant/model each get
    their own) -- NOT a confirmed-real shape. Task 14's whole-Part sweep against a real Copilot run
    is what actually proves or disproves this guess; flagged here rather than papered over, the
    same standard this module already holds `_parse_copilot_jsonl` to.

    Phase E audit finding 3 (capture half), same INFERENCE caveat as the "tool" namespace guess
    above (this provider is quota-blocked -- module docstring -- so nothing here is confirmed
    against real narration content): an `assistant.message_delta` line -- the exact type string
    this module's own docstring already names as one of the real confirmed `type` values this CLI
    emits, just never captured carrying real content (the one real capture hit quota first) --
    becomes a RunEventType.REASONING event when `data` carries a plausible text field (`text` or
    `content`, the same defensive multi-spelling-scan spirit `_parse_copilot_jsonl`'s own
    session_id lookup already uses for an unconfirmed key name). Any other shape for that line --
    no `data`, no text-shaped field inside it, an empty string -- is silently skipped rather than
    fabricated: fail-soft on a surprise shape is the same standing contract every guess in this
    module already follows.

    Phase E audit finding 6 (capture half): every real captured envelope line -- tool-shaped or
    not -- carries a real `timestamp` sibling field (module docstring: "an `id`, a `timestamp`,
    and a `parentId`"). Folded into every event's payload as `envelope_ts` when present, never
    fabricated when the line happens to lack one -- named distinctly from `RunEvent.ts` (the
    dataclass field DB-assigns on append) so nothing downstream conflates the CLI's own reported
    timestamp with the store's insertion timestamp; they answer different questions.
    """
    translated: list[RunEvent] = []
    for raw_event in intermediate_events:
        event_type = raw_event.get("type")
        if not isinstance(event_type, str):
            continue
        namespace, _, verb = event_type.partition(".")
        raw_data = raw_event.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        ts = raw_event.get("timestamp")

        if namespace == "tool":
            # Defensive key-name scan (same spirit as _parse_copilot_jsonl's own session_id scan
            # just above): which key actually carries the tool's name is exactly as unconfirmed as
            # the "tool.*" namespace guess itself, so try the plausible spellings rather than
            # committing to one -- `verb` (e.g. "call_start" in "tool.call_start") is the
            # last-resort fallback so a tool call with no recognizable name field still yields a
            # non-empty summary.
            tool_name = data.get("name") or data.get("toolName") or data.get("tool_name") or verb or "unknown"
            payload = dict(data)  # copy -- never mutate the caller's own parsed event in place
            if ts is not None:
                payload["envelope_ts"] = ts
            translated.append(
                RunEvent(
                    run_id=run_id,
                    session_id=session_id,
                    type=RunEventType.TOOL_CALL,
                    stage=stage,
                    node=node,
                    # Task 5 (Part 2 run-visibility): this is real captured tool-call content
                    # reaching a human-facing event log for the first time -- redact_text/
                    # redact_value (redaction.py, shared with telemetry.py's own long-standing
                    # command scrub) scrub it here, before this RunEvent ever reaches
                    # run_event_store.append_event/run_event_stream.emit_live.
                    summary=redact_text(f"tool call: {tool_name}"),
                    payload=redact_value(payload),
                )
            )
            continue

        if event_type == "assistant.message_delta":
            text = data.get("text") or data.get("content")
            if not isinstance(text, str) or not text:
                continue  # surprise shape, or genuinely nothing to narrate -- never fabricate
            head = text[:160]
            if len(text) > 160:
                head += "..."
            payload = {"text": text}
            if ts is not None:
                payload["envelope_ts"] = ts
            translated.append(
                RunEvent(
                    run_id=run_id,
                    session_id=session_id,
                    type=RunEventType.REASONING,
                    stage=stage,
                    node=node,
                    summary=redact_text(head),
                    payload=redact_value(payload),
                )
            )
    return translated


def _build_copilot_wrapper_script(argv: list[str], prompt_path: str, timeout_seconds: int) -> str:
    """Build the text of the tiny wrapper script that feeds `-p` its value via shell expansion.

    Pure string construction, no I/O -- so _demo can assert its exact shape without a live sandbox
    (same "pure half only" scoping as _parse_copilot_jsonl above). The caller writes this text to a
    scratch file via write_scratch_file and runs it as `sh <path>`; see that call site in
    _agenerate_inner for the full "why" this exists at all (real CLI has no stdin-fed prompt mode)
    and why it is a wrapper FILE rather than a directly-embedded command-substitution fragment
    (avoids a double-quote-nesting hazard in azure_aci.py's own re-embedding of the command
    string). This function only owns the string's shape, not that reasoning.

    argv[0] must be "copilot" (the caller's own hardcoded first element, never touched again after
    construction) -- everything in argv[1:] is flags only; `-p`'s value is never one of them.
    """
    assert argv and argv[0] == "copilot", f"expected argv[0] == 'copilot', got argv={argv!r}"
    # Double-quoted command substitution: the shell fixes where this quoted argument starts and
    # ends from THIS literal text, before it ever runs `cat` -- the prompt file's bytes only
    # appear afterward, already-parsed, dropped into that one argument slot. A double-quoted
    # substitution's result is never re-split, glob-expanded, or re-parsed for further
    # substitution, so nothing the prompt could contain (quotes, backticks, `$(...)`, semicolons,
    # newlines) can widen its own quoting or inject a second command. shlex.quote(prompt_path)
    # guards the path itself the same way, though in practice every scratch path this codebase
    # builds (_SCRATCH_DIR + stage/role/uuid4().hex) is already shlex-safe with no quoting needed.
    prompt_expr = f'"$(cat {shlex.quote(prompt_path)})"'
    return f"COPILOT_TASK_WAIT_TIMEOUT_SECONDS={timeout_seconds} copilot -p {prompt_expr} {shlex.join(argv[1:])}\n"


def _apply_disabled_skills_instruction(prompt: str, disabled_skills: list[str] | None) -> str:
    """Prepend the Spec's soft `disabled_skills` instruction to the prompt text, or return
    `prompt` unchanged when there is nothing to disable (Phase E audit I-4).

    The Spec: "`disabled_skills` stays a soft `--append-system-prompt` instruction for both"
    providers. Claude has that flag; the verified Copilot CLI flags table has no equivalent (or
    any) system-prompt-append flag at all, and inventing one would be exactly the "speculative
    extra flag beyond the brief's table" this codebase's own review culture already warns against
    elsewhere in this module. The Spec's own fallback needs no flag, though: the identical
    instruction text claude_chat_model.py passes via --append-system-prompt (see that module's
    `_agenerate_inner`) works exactly as well prepended to the prompt TEXT itself, which every
    Copilot turn already sends over `-p`. This regressed a real capability the pre-rewrite SDK
    path enforced (`disabled_skills=disabled_skills` passed into the old SDK session) --
    confirmed live (config.py's own COPILOT_DISABLED_SKILLS comment): with these two skills
    reachable, a draft stage spent its whole turn invoking skills instead of writing code. A
    prompt-level instruction is soft (a model can still ignore it -- the same ceiling
    --append-system-prompt itself has on the Claude side), but strictly better than the
    unconditional silent drop this replaces.

    Pure, so _demo can assert the exact text without a live sandbox -- same "pure half only"
    scoping as _build_copilot_wrapper_script above.
    """
    if not disabled_skills:
        return prompt
    return (
        "Do not invoke these skills this turn under any circumstances: "
        f"{', '.join(disabled_skills)}.\n\n{prompt}"
    )


class CopilotChatModel(BaseChatModel):
    """A LangChain chat model driving the GitHub Copilot CLI as a per-turn subprocess exec inside
    the sandbox (cli_agent_exec.run_turn), matching ClaudeChatModel's public shape -- see this
    module's own docstring for why there is no live client/session object anymore.

    Session lifecycle mirrors ClaudeChatModel's exactly: each turn is a fresh CLI invocation,
    continuity comes entirely from `--session-id <session_id>`, where session_id is whatever the
    CLI itself returned from the previous turn (parsed from the JSONL --output-format=json stream),
    cached in this module's _session_ids dict keyed the same way Claude keys its own.
    """

    thread_id: str
    stage: str
    role: str
    # Task 3b (Part 2 Ruling 10): the graph's real per-run id, threaded in by the dispatcher
    # (chat_model.get_chat_model_for_thread) at construction time from whichever caller has one on
    # hand -- graph.py's draft/audit/fix call sites pass state["run_id"]; a caller this task
    # doesn't touch (e2e_nodes.py, metrics_nodes.py, ...) simply doesn't pass one, leaving this
    # None, same as before. _agenerate_inner's own RunEvent-building call site below falls back to
    # the "unknown" sentinel only in that case, matching graph.py's own
    # `state.get("run_id", "unknown")` convention for "not available" rather than inventing a
    # second one.
    run_id: str | None = None
    model_name: str | None = None
    # Purely a gating flag now, not a live-connection selector: unlike the old SDK version (which
    # spawned a local child process when this was None), every turn always execs through
    # cli_agent_exec.run_turn, which requires a sandbox already registered for thread_id regardless
    # of this field's value -- claude_chat_model.sandbox now documents the identical situation.
    # Kept only to gate the sandbox-filesystem-only bits below (--plugin-dir, disabled_skills): a
    # locally-spawned (no-sandbox) process has no such content to point at.
    sandbox: SandboxSession | None = None

    # Agent Plugin / write-access controls -- same kwarg vocabulary as ClaudeChatModel (Part A of
    # the plugin plan). available_tools/excluded_tools entries are source-qualified
    # ("builtin:<name>") per copilot._mode.ToolSet's original vocabulary, and that vocabulary is
    # already --available-tools/--excluded-tools' own native shape -- no translation table needed
    # here, unlike claude_chat_model._map_tool_names.
    agent_mode: Literal["interactive", "plan", "autopilot", "shell"] = "plan"
    available_tools: list[str] | None = None
    excluded_tools: list[str] | None = None
    # Never translated into a flag -- see the logger.warning at its one use site in
    # _agenerate_inner for why (no CLI equivalent for this SDK-only hook).
    pre_tool_use_hook: Any | None = None
    mcp_servers: dict[str, Any] | None = None

    # Custom agents (same fields as ClaudeChatModel). Only `agent` (a name string) has a verified
    # CLI flag (--agent=<name>); custom_agents (inline ad-hoc definitions) does not -- see the
    # logger.warning at its use site in _agenerate_inner.
    custom_agents: list[dict] | None = None
    agent: str | None = None

    # Copilot SDK terminal-tool objects. Never translated into a flag -- see the logger.warning at
    # its one use site in _agenerate_inner; structured_output.ainvoke_structured is this
    # provider's structured-output mechanism instead.
    tools: list[Any] | None = None

    # Per-stage override of config.COPILOT_DISABLED_SKILLS, reused as-is -- same plugin-marketplace
    # content the CLI loads via --plugin-dir, so the same two mandate-style skills need silencing
    # here too. Unlike claude_chat_model (which has --append-system-prompt to work around this),
    # the verified Copilot CLI flags table has no equivalent flag at all -- see the logger.warning
    # at its use site.
    disabled_skills: list[str] | None = None

    # Same core shape as claude_chat_model.ClaudeChatModel._last_usage (model/input_tokens/
    # output_tokens/cost), for the provider-agnostic OTEL span attributes _agenerate sets below,
    # plus one Copilot-only key (premium_requests) -- see _extract_usage's own docstring for the
    # real 2026-08-23 capture this is built from. reasoning_tokens/cache_read_tokens/
    # cache_write_tokens are NOT included -- unlike the old SDK's ASSISTANT_USAGE event, this CLI's
    # real usage shape reports none of them, and a fabricated 0 would read as "measured zero"
    # instead of "not reported" to anything that later reads this dict.
    _last_usage: dict[str, Any] | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "github-copilot"

    @property
    def _session_key(self) -> str:
        return f"{self.thread_id}:{self.stage}:{self.role}"

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Provider-agnostic wrapping (span naming, attributes, the _last_usage reset/read) --
        # identical to claude_chat_model.ClaudeChatModel._agenerate, it only touches
        # self.thread_id/stage/role/model_name and self._agenerate_inner.
        with telemetry.tracer.start_as_current_span(
            f"llm/{self.stage}/{self.role}",
            attributes={
                "thread_id": self.thread_id,
                "stage": self.stage,
                "role": self.role,
                "gen_ai.request.model": self.model_name or "default",
            },
        ) as llm_span:
            self._last_usage = None  # never attach a previous call's numbers to this span
            result = await self._agenerate_inner(messages, stop=stop, run_manager=run_manager, **kwargs)
            if self._last_usage is not None:
                # input_tokens/output_tokens are None, not 0, when this CLI version's real usage
                # shape doesn't report them (_extract_usage's own docstring) -- set_attribute
                # rejects a None value (logs an error, does not raise), so it is only called when
                # there is a real number to attach; an unreported count means no attribute at all,
                # never a fabricated 0 span value.
                if self._last_usage["input_tokens"] is not None:
                    llm_span.set_attribute("gen_ai.usage.input_tokens", self._last_usage["input_tokens"])
                if self._last_usage["output_tokens"] is not None:
                    llm_span.set_attribute("gen_ai.usage.output_tokens", self._last_usage["output_tokens"])
                llm_span.set_attribute("gen_ai.response.model", self._last_usage["model"])
            return result

    async def _agenerate_inner(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        provider = get_sandbox_provider()
        prompt = _messages_to_prompt(messages)
        session_id = _session_ids.get(self._session_key)

        # `-p`'s own value is deliberately NOT put in argv here (unlike Claude's stdin-fed
        # `["claude", "-p", ...]` -- see claude_chat_model.py, confirmed working). Verified against
        # the real CLI, v1.0.79 (task-12-report.md "BUG A"): `-p`/`--prompt` has no stdin-fed mode
        # at all -- it always requires the prompt as `-p`'s own next argv token, no matter what's
        # piped in. 4 independent real-CLI probes confirmed this: bare `-p` followed by more flags
        # -> "Invalid command format ... prompt was not quoted"; `-p` with no following token ->
        # "argument missing"; `-p` with an explicit quoted value -> proceeds past parsing straight
        # to the auth check; `-p -` -> "-" taken as a literal 1-char prompt, not a stdin marker.
        # cli_agent_exec.run_turn's stdin redirection (`< scratch_prefix`) is correct for and
        # required by Claude, but is simply not a mechanism this CLI ever reads its prompt from.
        # `-p`'s real value is supplied via the shell-wrapper construction right before `command`
        # is built below, near the end of this function -- see the comment there for the full
        # "why" (both why shell expansion instead of argv/stdin, and why a wrapper script file).
        argv = ["copilot", "--output-format", "json"]
        # --session-id, not the flags-table-documented --resume: --resume alone opens an
        # interactive picker that needs a TTY a headless sandbox exec never has, whereas
        # --session-id resumes the exact id with no prompt (task-3-brief.md's explicit preference).
        if session_id:
            argv += ["--session-id", session_id]

        mode = self.agent_mode
        if mode == "shell":
            # Only interactive|plan|autopilot are documented for --mode in the verified flags
            # table -- "shell" is a value this codebase's own Literal defines for other purposes
            # (see claude_chat_model's --permission-mode mapping), not a confirmed Copilot CLI
            # value. Falling back rather than guessing the CLI accepts an unlisted value.
            logger.info(
                "agent_mode='shell' has no verified --mode value for the Copilot CLI -- falling "
                "back to 'autopilot' for this turn (stage=%s role=%s)",
                self.stage,
                self.role,
            )
            mode = "autopilot"
        argv += ["--mode", mode]

        # Full-authority mode (BR-6): unconditional, unlike the old SDK's
        # on_permission_request=PermissionHandler.approve_all callback -- there is no per-call-site
        # opt-out because no stage in this pipeline ever wants an interactive pause here; the only
        # pauses in this system are the Gates implemented as LangGraph interrupts.
        argv.append("--no-ask-user")

        # Always included, not conditional on secrets actually being in play this turn -- the
        # brief is unconditional here ("--secret-env-vars always includes whatever
        # secret_env_names() returns"), and sorted() keeps the built command deterministic/testable
        # rather than varying with set iteration order.
        argv += ["--secret-env-vars", ",".join(sorted(secret_env_names()))]

        # Copilot's own vocabulary ("builtin:<name>", config.READ_ONLY_AVAILABLE_TOOLS) already IS
        # --available-tools/--excluded-tools' native shape -- passed straight through, no mapping
        # table needed here (contrast claude_chat_model._map_tool_names). Allowlist wins over
        # blocklist when both are set, same precedent as the old SDK version and Claude's CLI
        # mapping (Phase A0's spike: blocklisting is incomplete, an allowlist is the only reliable
        # read-only boundary).
        if self.available_tools:
            argv += ["--available-tools", ",".join(self.available_tools)]
        elif self.excluded_tools:
            argv += ["--excluded-tools", ",".join(self.excluded_tools)]

        if self.pre_tool_use_hook is not None:
            logger.warning(
                "CopilotChatModel.pre_tool_use_hook is set but Layer 1 write-scope enforcement has "
                "no CLI equivalent to translate it into -- Layer 2's git-diff gate "
                "(gates/write_scope_gate.py) is authoritative regardless, so this turn proceeds "
                "without it"
            )
        if self.tools:
            logger.warning(
                "CopilotChatModel.tools is set but there is no CLI terminal-tool mechanism to "
                "translate it into -- structured_output.ainvoke_structured's JSON-schema-prompting "
                "retry loop is this provider's structured-output mechanism instead; this turn "
                "ignores it"
            )
        if self.custom_agents:
            logger.warning(
                "CopilotChatModel.custom_agents is set but the verified Copilot CLI flags table "
                "(task-3-brief.md) has only --agent=<name> to select a predefined agent (already "
                "covered by self.agent below), no flag for inline custom-agent definitions -- this "
                "turn ignores it"
            )

        # [] (not None) in the no-sandbox branch on purpose -- iterated directly below to emit one
        # --plugin-dir per entry.
        plugin_directories: list[str] = config.COPILOT_PLUGIN_DIRECTORIES if self.sandbox is not None else []
        for directory in plugin_directories:
            argv += ["--plugin-dir", directory]

        # Same gating as the old SDK version's _get_session: skills only exist because
        # --plugin-dir loaded them, so with no plugin dirs there is nothing to disable regardless
        # of what disabled_skills says.
        disabled_skills = (
            (self.disabled_skills if self.disabled_skills is not None else config.COPILOT_DISABLED_SKILLS)
            if plugin_directories
            else None
        )
        # Phase E audit I-4: see _apply_disabled_skills_instruction's own docstring for why this
        # is a prompt-text prepend rather than a CLI flag, and why that is the Spec-required
        # fallback rather than a workaround.
        prompt = _apply_disabled_skills_instruction(prompt, disabled_skills)

        if self.model_name:
            argv += ["--model", self.model_name]
        if self.agent:
            argv += ["--agent", self.agent]

        # Every scratch file for this turn (the prompt, and the mcp-config file below when
        # present) shares this prefix -- run_turn's own cleanup (`rm -f {scratch_prefix}*`) removes
        # all of them together, so nothing here needs its own cleanup step.
        scratch_prefix = f"{_SCRATCH_DIR}/copilot-{self.stage}-{self.role}-{uuid.uuid4().hex}"

        if self.mcp_servers:
            # Judgment call, unconfirmed against a live CLI -- same class of gap as
            # claude_chat_model's own --mcp-config guess (see that module's comment at its
            # equivalent line). {"mcpServers": {...}} is a near-universal MCP config convention
            # (Claude Code, VS Code, Claude Desktop all use it), and the flags table's own
            # "--additional-mcp-config=<json or @file>" syntax documents the "@file" reference form
            # as real, so this writes that wrapper shape to a scratch file rather than guessing at
            # a Copilot-specific key name. Each per-server dict is passed through unreshaped. If
            # this guess is wrong, the CLI rejects it and the is_error check below fails this turn
            # loud, not silently.
            mcp_config_path = f"{scratch_prefix}.mcp.json"
            await write_scratch_file(
                provider, self.thread_id, mcp_config_path, json.dumps({"mcpServers": self.mcp_servers})
            )
            argv += ["--additional-mcp-config", f"@{mcp_config_path}"]

        # COPILOT_TASK_WAIT_TIMEOUT_SECONDS (task-3-brief.md): Copilot's own internal task-wait
        # timeout defaults to 600s and, left unset, can return early on a legitimately long
        # backgrounded shell command the CLI itself spawned -- well inside our own outer timeout
        # below. Scoped to just this one command via a leading POSIX shell assignment (`VAR=val
        # cmd`), not a new run_turn parameter -- cli_agent_exec.py is Task 1's reviewed-clean
        # shared file, out of this task's scope to touch, and every exec here already runs through
        # `sh -c` (_build_startup_command), where a leading env assignment is ordinary syntax.
        #
        # Bug A fix (task-12-report.md / task-12b-report.md): `-p` gets its value from shell
        # expansion, not argv or stdin -- see the comment on `argv = ["copilot", ...]` above for
        # why stdin (this shared runner's normal mechanism, and Claude's own, confirmed-working
        # one) cannot work for this CLI at all. `scratch_prefix` IS the prompt file's real path:
        # run_turn (cli_agent_exec.py) writes the prompt there as its own first step, before it
        # ever launches `command` -- its internal `prompt_path = scratch_prefix` is the exact same
        # string passed as `scratch_prefix` a few lines below, so by the time anything here
        # actually reads that path, the prompt is already on disk (write-then-launch is
        # run_turn's own ordering, not re-derived here).
        #
        # _build_copilot_wrapper_script's own docstring covers the double-quoted-$(cat...) safety
        # argument (why arbitrary prompt content -- quotes, backticks, $(), semicolons, newlines --
        # cannot escape this construction). The remaining choice made here, at the call site, is
        # WHERE that expansion lives: in a small wrapper SCRIPT FILE (written via
        # write_scratch_file, the same base64-safe primitive already used for the prompt itself and
        # the MCP config above) rather than spliced directly into `command`.
        #
        # That choice is deliberate, not decorative: `command` does not end its short life as one
        # opaque token. cli_agent_exec._build_startup_command embeds it into a larger sh_script and
        # shlex.quote()'s THAT once -- fine, single-quoting survives arbitrary content -- but
        # azure_aci.py's exec_in_sandbox (this provider's OTHER SandboxProvider) re-embeds the
        # resulting string a SECOND time via plain Python f-string interpolation into its own
        # double-quoted `/bin/sh -c "cd ... && {command}"` (see that module's own docstring,
        # already flagged there as unverified to preserve shell operators). A `-p "$(cat ...)"`
        # fragment spliced straight into `command` would put two guaranteed literal `"` characters
        # inside that outer double-quoted string on EVERY Copilot turn, not just some rare edge
        # case -- closing it early and breaking the exec. Routing the expansion through a wrapper
        # file sidesteps this entirely: the file's bytes travel via base64 like every other scratch
        # file here, so `command` itself ends up as just `sh <plain-path>` -- no shell
        # metacharacters left for any provider's own re-embedding to mishandle. Verified against the
        # real LocalDockerProvider container below (task-12b-report.md); AzureContainerInstance
        # itself was not re-verified live (no ACI target in this environment, matching Task 12's
        # own stated limitation). Precisely stated (fix-round-1 correction -- the original wording
        # here overstated this): this shape adds NO NEW dependence on that provider's pre-existing
        # quoting fragility, it does not make Copilot-on-ACI independent of a gap the whole
        # pipeline already has. exec_in_sandbox's shared startup_command already carries other
        # metacharacters (`'`, `;`, `&`, redirects) through that same re-embedding for BOTH
        # providers today, regardless of this fix. What this shape actually buys is narrower and
        # real: `command` itself now carries zero shell metacharacters instead of two guaranteed
        # `"` characters, which is strictly safer for every SandboxProvider -- Claude's included,
        # and any future one -- not a workaround scoped only to the one gap named above.
        #
        # Two known fidelity gaps versus the old (broken) stdin path, neither a correctness risk
        # for this pipeline's real prompts, both worth stating plainly rather than leaving implicit:
        # - POSIX command substitution always strips trailing newlines from `$(cat ...)`'s output --
        #   irrelevant here since _messages_to_prompt never appends one and a stripped trailing
        #   newline has no semantic effect on an LLM prompt.
        # - A NUL byte in prompt content would be silently truncated (argv strings are
        #   NUL-terminated in the shell/kernel, unlike a stdin byte stream) -- not a realistic risk
        #   for LLM conversation text, and not currently checked for.
        # ponytail: `$(cat ...)` becomes a single argv word at exec time, capped by the container
        # kernel's MAX_ARG_STRLEN (128 KiB per argv element on Linux) -- measured, distant for this
        # pipeline's real prompts (largest static prompt template is ~17KB,
        # src/prompts/ac_to_tests_draft.md; every other interpolated block is already
        # hard-truncated well under it -- e.g. preflight_nodes.py's [:4000]/[:20000], graph.py's
        # [:8000], several [-4000:]/[-3000:] truncations elsewhere), unlike the old stdin path this
        # replaces, which had no such ceiling at all. No real workaround exists short of shrinking
        # the prompt further or a CLI-side file-based prompt flag (which does not exist today) -- a
        # `read` builtin would still land the prompt in one argv-sized shell word to hand `-p`, so
        # it would hit the identical ceiling, not raise it; that is not an upgrade path, just a
        # different way to write the same failure.
        wrapper_path = f"{scratch_prefix}.cmd.sh"
        wrapper_script = _build_copilot_wrapper_script(argv, scratch_prefix, config.CLI_AGENT_TURN_TIMEOUT_SECONDS)
        await write_scratch_file(provider, self.thread_id, wrapper_path, wrapper_script)
        command = f"sh {shlex.quote(wrapper_path)}"
        try:
            result = await run_turn(
                provider,
                self.thread_id,
                command,
                prompt,
                scratch_prefix,
                timeout_seconds=config.CLI_AGENT_TURN_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            # Phase E audit C-2: same shape as claude_chat_model's identical guard -- see this
            # module's own _RESUME_REJECTED_MARKERS comment for why the classification RULE is
            # shared but every trigger of it here is inference (quota-blocked, no real killed-then-
            # resumed Copilot session was or could be captured for this task). Marked "unknown",
            # never dropped outright -- the real Claude-side experiment (cli_agent_exec.
            # classify_resume's own docstring) showed a killed session can resume cleanly, and there
            # is no reason to assume Copilot's CLI is strictly worse at this than Claude's; keeping
            # the id lets the next turn's own real --session-id attempt decide for real.
            if session_id:
                _resume_states[self._session_key] = "unknown"
                logger.warning(
                    "Copilot turn for %r was killed mid-turn -- resume continuity for cached "
                    "session %r is now UNKNOWN (kept, not dropped). The next turn's own "
                    "--session-id attempt will classify the real outcome.",
                    self._session_key, session_id,
                )
            raise TimeoutError(
                f"{exc} -- resume continuity for session {session_id!r} at "
                f"{self._session_key!r} is now UNKNOWN (killed mid-turn)"
            ) from exc

        events, new_session_id = _parse_copilot_jsonl(result.stdout)
        if not events:
            # Phase E audit C-2: a resume the CLI rejects outright, before ever invoking the model,
            # would plausibly produce no parseable stdout at all -- same reasoning as
            # claude_chat_model's identical guard. No real captured example of an actual Copilot
            # resume rejection exists (quota-blocked); classify defensively from the real text on
            # hand rather than claim more than is known.
            if session_id:
                combined_text = f"{result.stdout} {result.stderr}"
                rejected = any(marker in combined_text.lower() for marker in _RESUME_REJECTED_MARKERS)
                _session_cache.record_resume_state(self._session_key, "rejected" if rejected else "unknown")
            raise RuntimeError(
                f"Copilot CLI turn for {self._session_key!r} produced no parseable JSONL lines "
                f"under --output-format json (resume_state="
                f"{_resume_states.get(self._session_key)!r}): stdout={result.stdout!r}\n"
                f"stderr={result.stderr!r}"
            )

        # Task 3 (Part 2 run-visibility) + Phase E audit finding 5: every intermediate JSONL line
        # this turn produced (all of `events` except the final result-shaped line, handled below)
        # is translated, then the whole batch is appended in ONE round trip
        # (run_event_store.append_events -- was a per-event append_event/emit_live loop, N
        # sequential DB round-trips for a chatty turn) before still emitting each one live
        # individually: emit_live is an in-process custom-event dispatch, not a DB write. See
        # _translate_intermediate_events' own docstring for exactly what is/isn't confirmed real
        # about which lines qualify. Both calls fail soft internally (their own docstrings) -- no
        # extra try/except needed here.
        #
        # Task 3b (Part 2 Ruling 10) fix: self.run_id is now the graph's real per-run id, set by
        # chat_model.get_chat_model_for_thread at construction time from whatever the caller has on
        # hand (graph.py's draft/audit/fix sites pass state["run_id"]). Falls back to the "unknown"
        # sentinel only for a caller this task doesn't touch (e2e_nodes.py, metrics_nodes.py, ...)
        # that hasn't been wired up to pass one yet -- matches graph.py's OWN sentinel for the
        # identical "not available" case (`state.get("run_id", "unknown")`) rather than inventing a
        # second one. Previously always "unknown" unconditionally (task-3-report.md); see that
        # report for the concrete cost this left: events not retrievable via
        # run_event_store.list_events(run_id) for a specific run.
        translated_events = _translate_intermediate_events(
            events[:-1],
            run_id=self.run_id or "unknown",
            session_id=self.thread_id,
            stage=self.stage,
            node=self.role,
        )
        if translated_events:
            translated_events = await run_event_store.append_events(translated_events)
            for translated_event in translated_events:
                await run_event_stream.emit_live(translated_event)

        # Best-effort guess, NOT confirmed against real output (module docstring): the LAST line is
        # the result-shaped summary event, by analogy with Claude's single terminal JSON object
        # carrying result/is_error/usage/total_cost_usd together. Every element of `events` is
        # guaranteed dict-shaped by _parse_copilot_jsonl, so no isinstance check is needed here.
        final = events[-1]
        if not ({"result", "is_error", "usage"} & final.keys()):
            logger.warning(
                "Copilot CLI turn for %r: the last JSONL line has none of result/is_error/usage -- "
                "the 'final line is the result-shaped summary event' guess (module docstring) may "
                "not hold for this CLI version; got keys=%s",
                self._session_key,
                sorted(final.keys()),
            )

        is_error = bool(final.get("is_error"))

        # Phase E audit C-2: classify THIS turn's resume continuity from what was actually
        # observed -- None when no --session-id was requested this turn. See
        # cli_agent_exec.classify_resume's own docstring for the tri-state rule. Computed before
        # the is_error raise below so the raised message can carry it.
        resume_state = classify_resume(
            session_id, new_session_id, is_error, str(final.get("result") or ""), _RESUME_REJECTED_MARKERS,
        )
        if resume_state is not None:
            _session_cache.record_resume_state(self._session_key, resume_state)
            if resume_state == "unknown":
                logger.warning(
                    "resume continuity for session %r at %r is UNKNOWN this turn (requested=%r "
                    "returned=%r is_error=%s) -- never assumed continuous without a matching id",
                    session_id, self._session_key, session_id, new_session_id, is_error,
                )

        if is_error:
            raise RuntimeError(
                f"Copilot CLI turn for {self._session_key!r} reported an error "
                f"(stop_reason={final.get('stop_reason')!r}, resume_state={resume_state!r}): "
                f"{final.get('result')!r}"
            )

        if new_session_id:
            _session_cache.cache_session_id(self._session_key, new_session_id, resume_state)

        self._last_usage = _extract_usage(final, self.model_name)

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=final.get("result", "")))])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop=stop, run_manager=None, **kwargs))


def get_chat_model_for_thread(
    thread_id: str,
    stage: str,
    role: str,
    *,
    run_id: str | None = None,
    model_name: str | None = None,
    sandbox: SandboxSession | None = None,
    agent_mode: Literal["interactive", "plan", "autopilot", "shell"] = "plan",
    available_tools: list[str] | None = None,
    excluded_tools: list[str] | None = None,
    pre_tool_use_hook: Any | None = None,
    mcp_servers: dict[str, Any] | None = None,
    custom_agents: list[dict] | None = None,
    agent: str | None = None,
    tools: list[Any] | None = None,
    disabled_skills: list[str] | None = None,
) -> CopilotChatModel:
    """Return the chat model for the given LangGraph thread's (stage, role) Copilot session.

    Every other kwarg name and default is unchanged from the pre-rewrite SDK version -- every call
    site in this codebase (graph.py, e2e_nodes.py, metrics_nodes.py, preflight_nodes.py,
    test_hardening_nodes.py, rebuild.py) depends on this surface staying stable; only
    _agenerate_inner's insides changed. (The old SDK version's github_token kwarg is gone: nothing
    read it since the CLI-exec rewrite -- the sandbox's own COPILOT_GITHUB_TOKEN env var, see
    secret_env_names below, is what the copilot CLI actually authenticates from.)

    run_id (Task 3b, Part 2 Ruling 10): optional, defaults to None -- see CopilotChatModel.run_id's
    own comment for who passes a real value and why a caller that doesn't is not a regression.
    """
    return CopilotChatModel(
        thread_id=thread_id,
        stage=stage,
        role=role,
        run_id=run_id,
        model_name=model_name,
        sandbox=sandbox,
        agent_mode=agent_mode,
        available_tools=available_tools,
        excluded_tools=excluded_tools,
        pre_tool_use_hook=pre_tool_use_hook,
        mcp_servers=mcp_servers,
        custom_agents=custom_agents,
        agent=agent,
        tools=tools,
        disabled_skills=disabled_skills,
    )


async def close_thread_session(thread_id: str) -> None:
    """Evict every cached Copilot session id for a thread (call on graph run completion/error).

    No live connection to close anymore -- see this module's own docstring for why. Declared async
    only for call-site parity with every caller here (run_headless.py, exit_nodes.py, graph.py)
    that already awaits this function; eviction itself is a pure dict pop
    (cli_agent_exec.SessionCache), not a network call that can fail or hang against an already-dead
    sandbox.
    """
    _session_cache.forget_thread_sessions(thread_id)


def forget_thread_sessions(thread_id: str) -> None:
    """Drop cached Copilot session ids for a thread whose sandbox is already gone.

    Sync and network-free, same as claude_chat_model.forget_thread_sessions -- there is no live
    client/connection to close gracefully anymore, only a session_id string to forget. Called from
    sandbox.registry.pop() (already wired there today), the one choke point every
    container-destruction path routes through (both providers' terminate(), the idle reaper, and
    the DELETE /{thread_id} endpoint).
    """
    _session_cache.forget_thread_sessions(thread_id)


async def close_session(thread_id: str, stage: str, role: str) -> None:
    """Drop one (thread, stage, role) Copilot session id so the next call starts fresh (omits
    --session-id), the same recovery mechanism as claude_chat_model.close_session for a stage whose
    session history now contains a fabricated claim. Also drops any cached resume-continuity
    classification for the same key (SessionCache.close_session's docstring covers why).

    Async for the same call-site-parity reason as close_thread_session; nothing here awaits either.
    """
    _session_cache.close_session(thread_id, stage, role)


def get_session_id(thread_id: str, stage: str, role: str) -> str | None:
    """The Copilot session id backing one (thread, stage, role), or None if none was created yet.

    Exists so a gate can verify what a stage's session actually did -- gates/skill_gate.py already
    calls this today. read_skill_invocations below is where that verification logic is moving to
    (task-3-brief.md), not this function.
    """
    return _session_cache.get_session_id(thread_id, stage, role)


def get_resume_state(thread_id: str, stage: str, role: str) -> ResumeState | None:
    """The last-observed resume-continuity classification for one (thread, stage, role), or None
    if no --session-id resume has ever been attempted for this key yet.

    Same purpose as claude_chat_model.get_resume_state -- see that function's own docstring. This
    provider's classification is INFERENCE (module docstring: quota-blocked, no real killed-then-
    resumed Copilot session was captured for this task), but the accessor and eviction shape are
    identical so a caller (chat_model.get_resume_state) can dispatch to either provider without a
    provider-specific branch.
    """
    return _session_cache.get_resume_state(thread_id, stage, role)


async def read_skill_invocations(provider: SandboxProvider, thread_id: str, session_id: str) -> list[str] | None:
    """Skill names this Copilot session actually invoked, or None if unverifiable.

    Currently always None. The old SDK-server implementation read
    `/home/vscode/.copilot/session-state/<session_id>/events.jsonl`, written by the persistent
    `copilot --server` process this module no longer starts (see this module's own docstring).
    Per task-3-brief.md: do not assume a bare `copilot -p` invocation writes an equivalent log --
    that would be inventing a verified fact this session has no way to check (no `copilot` binary
    is installed or authenticated in this dev environment; see the module docstring). Until someone
    confirms a real headless-CLI equivalent exists and wires it up here, this fails open
    unconditionally, matching gates/skill_gate.py's own contract: an infrastructure gap must never
    masquerade as "no skills were invoked" (that would fail CLOSED on a stage that actually did the
    work). This is a genuine, reportable capability gap, not a bug hidden behind a passing return
    value -- skill_gate.py's check_required_skills already treats verified=False (which a None here
    produces, transitively, once a later task wires this in) as "cannot enforce," not "enforcement
    passed."
    """
    return None


def secret_env_names() -> set[str]:
    """Names to redact from this turn's own shell output via --secret-env-vars (see
    _agenerate_inner) -- a masking/redaction list, NOT a declaration of what actually authenticates
    the CLI (fix-round-1 correction: the docstring here previously conflated the two). If the
    wrapped shell command a turn runs happens to echo one of these names' value, the CLI scrubs it
    from its own output; nothing here sets or reads any of these values itself.

    COPILOT_GITHUB_TOKEN is the one that matters today -- sandbox/provider.py's provision()
    docstring and local_docker.py/azure_aci.py are what actually write the real secret there.
    COPILOT_SDK_AUTH_TOKEN/COPILOT_CONNECTION_TOKEN gated the now-retired `copilot --server`
    process (per entrypoint.sh's own history) and are never set by anything anymore; GITHUB_TOKEN
    is listed defensively even though this codebase deliberately no longer sets it for Copilot's
    sandbox env (task-12b fix-round-1: a plain GITHUB_TOKEN is also read ambiently by `gh`/git/npm
    inside the sandbox, which is exactly what COPILOT_GITHUB_TOKEN avoids). All four are harmless
    to list even when unset -- redacting a name that never appears in the output is a no-op, not an
    error.

    Re-exported as the same `secret_env_names()` symbol via chat_model.py for both providers, but
    NOT the same contract: claude_chat_model.py's function of this name means something else
    entirely (env var names the sandbox must already have set for that CLI to authenticate, not a
    redaction list -- see that module's own docstring). A reader who only ever looks at one
    provider's version should not assume the other works the same way. The asymmetry runs deeper
    than naming, too: the Claude CLI has no `--secret-env-vars` equivalent at all, so there is
    currently no redaction mechanism for anything a Claude turn's own shell output might echo --
    a real, previously-undocumented gap.
    """
    return {"COPILOT_SDK_AUTH_TOKEN", "COPILOT_CONNECTION_TOKEN", "COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN"}


def _demo() -> None:
    """Self-check for the JSONL parser's defensive scanning (session_id found on ANY line, not
    just the assumed-final one, and now the real camelCase "sessionId" key -- Phase E known-bugs
    fix), the Bug A wrapper-script shape (task-12b), _translate_intermediate_events (Task 3, Part 2
    -- both against a real captured shape and a clearly-labeled synthetic one, see that function's
    own docstring), _extract_usage's honest-absence usage parsing (Phase E known-bugs fix, same
    real + synthetic samples), and the session-cache eviction path -- the live CLI-exec path needs
    a sandbox, see cli_agent_exec.py's and claude_chat_model.py's own demos for the same "pure half
    only" scoping.
    """
    # Bug A fix (task-12b): the wrapper script must feed `-p` an explicit, double-quoted
    # command-substitution value -- never bare/stdin-fed (the real CLI has no such mode -- see
    # _agenerate_inner's own comment on `argv = ["copilot", ...]`) and never the raw prompt text
    # inlined (would re-blow cli_agent_exec._EXEC_CMD_BUDGET for a large prompt). Exact-match, not
    # a substring check, so any accidental reordering/re-quoting/missing-newline regression fails
    # loud here rather than only against a live container.
    wrapper = _build_copilot_wrapper_script(
        ["copilot", "--output-format", "json", "--mode", "plan"], "/tmp/aidw-agent/fake-prompt", 1800
    )
    assert wrapper == (
        'COPILOT_TASK_WAIT_TIMEOUT_SECONDS=1800 copilot -p "$(cat /tmp/aidw-agent/fake-prompt)" '
        "--output-format json --mode plan\n"
    ), f"unexpected wrapper script shape: {wrapper!r}"

    # A prompt path containing shell-special characters must come out shlex-quoted, not raw --
    # this is what actually makes the $(cat ...) construction safe (see the function's own
    # docstring); built from the same stdlib call under test so this assertion tracks shlex's own
    # behavior rather than hand-duplicating its quoting rules.
    dangerous_path = "/tmp/aidw-agent/a b'c\"d"
    wrapper_special = _build_copilot_wrapper_script(["copilot"], dangerous_path, 60)
    assert f"$(cat {shlex.quote(dangerous_path)})" in wrapper_special, (
        f"prompt path with shell-special characters must be shlex-quoted: {wrapper_special!r}"
    )

    # Defensive scan: session_id shows up on an EARLY line, not the (more likely, per this
    # module's own guess) final line -- must still be found. This is exactly the defensive
    # behavior task-3-brief.md calls for, since the real per-line shape is unverified.
    stdout_session_id_early = (
        '{"type": "delta", "session_id": "sess-early", "text": "..."}\n'
        '{"type": "delta", "text": "more..."}\n'
        '{"result": "done", "usage": {"input_tokens": 3, "output_tokens": 2}}\n'
    )
    events, session_id = _parse_copilot_jsonl(stdout_session_id_early)
    assert session_id == "sess-early", f"defensive scan should find session_id on an early line, got {session_id!r}"
    assert len(events) == 3, f"expected 3 parsed JSONL events, got {len(events)}"
    assert events[-1]["result"] == "done", "last line should be treated as the result-shaped event"

    # A garbage/non-JSON line, and a JSON line that isn't an object, must not crash the parser --
    # both are skipped, not fatal.
    events, session_id = _parse_copilot_jsonl('not json at all\n[1, 2, 3]\n{"result": "ok", "session_id": "s1"}\n')
    assert len(events) == 1, f"malformed/non-dict lines should be skipped, got {events}"
    assert session_id == "s1"

    # Fully empty/unparseable stdout: the parser itself just reports an empty events list and
    # never raises -- _agenerate_inner is what turns that into a RuntimeError.
    events, session_id = _parse_copilot_jsonl("not json\n\n   \n")
    assert events == [] and session_id is None

    # --- Task 3 (Part 2 run-visibility): _translate_intermediate_events self-check ---
    #
    # Part A -- REAL captured shape (task-3-report.md has the full transcript and circumstances):
    # a real, authenticated `copilot -p --output-format json` invocation, run via `npx --yes
    # @github/copilot` + a `gh auth token` credential on 2026-08-23 -- the first real output from
    # this CLI seen anywhere in this project's history (the module docstring's original "no
    # copilot binary is installed or authenticated" was accurate for the environment part-1's
    # task-3/task-12 reports were written in, not this one). It hit a real, genuine 402
    # quota_exceeded error before the model ever invoked a tool, so it proves the real non-tool-call
    # envelope shape (dot-namespaced `type`, a `data` dict, `id`/`timestamp`/`parentId`, optional
    # `ephemeral`) but contains ZERO real tool-call-shaped lines. `id`/`timestamp`/`parentId`/
    # `sessionId` values below are copied verbatim (random UUIDs/timestamps, nothing sensitive);
    # `data` payloads are trimmed of this-machine-local specifics (personal MCP server names,
    # absolute file paths, an unrelated project's content) that carried no test signal for what is
    # being checked here -- the KEYS and STRUCTURE are exactly as captured.
    real_shape_jsonl = (
        '{"type": "session.tools_updated", "data": {"model": "claude-sonnet-5"}, "ephemeral": true, '
        '"id": "31813b9e-70ef-410f-ba5b-eb4134ab24ec", "timestamp": "2026-08-23T18:50:42.305Z", '
        '"parentId": "ee6716ea-9e8c-4cbc-8faf-5d34a6c27f97"}\n'
        '{"type": "user.message", "data": {"content": "reply with exactly: hello"}, '
        '"id": "c91b0d13-6432-4b27-b545-bdda158e214f", "timestamp": "2026-08-23T18:50:42.315Z", '
        '"parentId": "ee6716ea-9e8c-4cbc-8faf-5d34a6c27f97"}\n'
        '{"type": "assistant.turn_end", "data": {"turnId": "0"}, '
        '"id": "8304d712-7080-44ec-be6a-d997488b70f4", "timestamp": "2026-08-23T18:50:42.725Z", '
        '"parentId": "c61e3b31-b4da-4369-bf57-4dc925acfe18"}\n'
        '{"type": "assistant.idle", "data": {}, "ephemeral": true, '
        '"id": "d5289a42-7a0f-43b5-98c0-5594626f1911", "timestamp": "2026-08-23T18:50:42.735Z", '
        '"parentId": "a55990ad-7426-4d4e-90ad-8bd2dd519077"}\n'
        '{"type": "result", "timestamp": "2026-08-23T18:50:42.775Z", '
        '"sessionId": "8d6fbad9-d488-4558-90bd-a8e8bffac2ab", "exitCode": 1, '
        '"usage": {"premiumRequests": 0, "totalApiDurationMs": 0, "sessionDurationMs": 5559, '
        '"codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": []}}}\n'
    )
    real_events, real_session_id = _parse_copilot_jsonl(real_shape_jsonl)
    assert len(real_events) == 5, f"expected 5 parsed real-shape events, got {len(real_events)}"
    # Phase E known-bugs fix (this task) -- proof, not a tripwire: the real terminal line's session
    # identifier is camelCase "sessionId", not the snake_case "session_id" _parse_copilot_jsonl
    # used to scan for exclusively -- against genuinely real output, new_session_id used to come
    # back None every turn, silently breaking --session-id multi-turn continuity. This used to
    # assert `real_session_id is None` (a known-bug tripwire, pinned so a future fix would trip it
    # and have to update the comment); now flipped into a proof the fix actually works against the
    # real captured sample, the same flip sessions_api.py did for project_id in an earlier task.
    assert real_session_id == "8d6fbad9-d488-4558-90bd-a8e8bffac2ab", (
        f"expected the real camelCase 'sessionId' to be found by the defensive scan, got {real_session_id!r}"
    )
    # The actual point of Part A: fed through the translation function, real captured non-tool-call
    # lines must yield ZERO RunEvents -- proves "not every line is a tool call" against genuinely
    # real data, not just a synthetic fixture built to already agree with the code under test.
    real_translated = _translate_intermediate_events(
        real_events[:-1], run_id="run-real", session_id="thread-real", stage="specification", node="draft",
    )
    assert real_translated == [], f"real non-tool-call lines must translate to nothing, got {real_translated}"

    # Phase E known-bugs fix (this task) -- _extract_usage against the SAME real captured sample's
    # final ("result") line: the real usage object has no input_tokens/output_tokens and the real
    # line has no total_cost_usd at all, so both must come back None (honest absence), never a
    # fabricated 0 -- and premiumRequests (a real, actually-reported 0 in this capture, not a
    # synthesized default) must surface under its own premium_requests key.
    real_usage = _extract_usage(real_events[-1], "claude-sonnet-5")
    assert real_usage == {
        "model": "claude-sonnet-5",
        "input_tokens": None,
        "output_tokens": None,
        "cost": None,
        "premium_requests": 0,
    }, f"real usage sample must parse honestly (no fabricated 0s for unreported tokens/cost): {real_usage}"

    # Part B -- SYNTHETIC, explicitly NOT confirmed real (module docstring / task-3-report.md): no
    # real tool-call-shaped line has ever been captured (Part A's real turn failed on quota before
    # the model could invoke one). This fixture is this session's own best-effort, clearly-labeled
    # GUESS at what one might look like, built only by extending the one real, confirmed naming
    # convention from Part A (a dot-namespaced "<domain>.<verb>" `type`, sharing session/user/
    # assistant/model's own single-word-domain shape) to a plausible "tool" domain -- never treat
    # this as ground truth.
    synthetic_jsonl = (
        '{"type": "tool.call_start", "data": {"name": "str_replace_editor", "input": {"path": "a.py"}}, '
        '"id": "syn-1", "timestamp": "2026-01-01T00:00:00.000Z", "parentId": "syn-0"}\n'
        '{"type": "assistant.message_delta", "data": {"text": "..."}, "ephemeral": true, '
        '"id": "syn-2", "timestamp": "2026-01-01T00:00:00.100Z", "parentId": "syn-0"}\n'
        '{"type": "tool.call_end", "data": {"toolName": "bash", "exitCode": 0}, '
        '"id": "syn-3", "timestamp": "2026-01-01T00:00:00.200Z", "parentId": "syn-0"}\n'
        '{"result": "done", "is_error": false, "usage": {"input_tokens": 5, "output_tokens": 3}}\n'
    )
    synthetic_events, _ = _parse_copilot_jsonl(synthetic_jsonl)
    assert len(synthetic_events) == 4
    # This fixture's final line uses the OLD hypothetical shape ({"input_tokens": 5,
    # "output_tokens": 3}, no premiumRequests/total_cost_usd) -- proves _extract_usage's kept-cheap
    # snake_case fallback still works if some future CLI version reports it, and that a genuinely
    # absent field (premium_requests here) comes back None rather than a fabricated 0.
    synthetic_usage = _extract_usage(synthetic_events[-1], None)
    assert synthetic_usage == {
        "model": "default",
        "input_tokens": 5,
        "output_tokens": 3,
        "cost": None,
        "premium_requests": None,
    }, f"old-shape input_tokens/output_tokens keys must still work as a fallback: {synthetic_usage}"
    synthetic_translated = _translate_intermediate_events(
        synthetic_events[:-1],  # drop the final result-shaped line, exactly like _agenerate_inner does
        run_id="run-1", session_id="thread-1", stage="specification", node="draft",
    )
    # Phase E audit finding 3 (capture half): the assistant.message_delta line, previously
    # skipped entirely, now yields a REASONING event of its own -- so all 3 of the 3 intermediate
    # lines translate, not 2. "Not every line is a tool call" still holds (skip-if-unrecognized is
    # still the rule); it's just no longer "everything that isn't a tool call is dropped."
    assert len(synthetic_translated) == 3, (
        f"expected 2 tool-call + 1 REASONING RunEvent, got {len(synthetic_translated)}"
    )
    tool_start, reasoning_event, tool_end = synthetic_translated
    assert tool_start.type is RunEventType.TOOL_CALL and tool_end.type is RunEventType.TOOL_CALL
    assert reasoning_event.type is RunEventType.REASONING
    assert all(e.run_id == "run-1" and e.session_id == "thread-1" for e in synthetic_translated)
    assert all(e.stage == "specification" and e.node == "draft" for e in synthetic_translated)
    # Phase E audit finding 6 (capture half): every one of these synthetic lines carries a real-
    # shaped "timestamp" sibling field (matching the module's own confirmed-real envelope shape);
    # each translated payload must carry it forward as envelope_ts, never fabricating one where
    # absent.
    assert tool_start.payload == {
        "name": "str_replace_editor", "input": {"path": "a.py"}, "envelope_ts": "2026-01-01T00:00:00.000Z",
    }
    assert tool_start.summary == "tool call: str_replace_editor"
    assert reasoning_event.payload == {"text": "...", "envelope_ts": "2026-01-01T00:00:00.100Z"}
    assert reasoning_event.summary == "..."
    assert tool_end.payload == {
        "toolName": "bash", "exitCode": 0, "envelope_ts": "2026-01-01T00:00:00.200Z",
    }
    assert tool_end.summary == "tool call: bash"
    # seq/ts are never pre-set by the translator -- append_event (run_event_store.py) fills those
    # in on actual persistence, the same contract as every other RunEvent built in this codebase.
    # (RunEvent.ts, the dataclass field, is distinct from the payload's own envelope_ts above.)
    assert all(e.seq is None and e.ts is None for e in synthetic_translated)

    # A surprise shape for assistant.message_delta -- no recognizable text field, or an explicitly
    # empty one -- must be silently skipped, never fabricated (module's own fail-soft contract).
    surprise_jsonl = (
        json.dumps({"type": "assistant.message_delta", "data": {"unexpected": "shape"}, "timestamp": "t"}) + "\n"
        + json.dumps({"type": "assistant.message_delta", "data": {"text": ""}, "timestamp": "t"}) + "\n"
    )
    surprise_events, _ = _parse_copilot_jsonl(surprise_jsonl)
    assert _translate_intermediate_events(
        surprise_events, run_id="r", session_id="s", stage="st", node="n"
    ) == [], "an unrecognized or empty assistant.message_delta shape must yield no event, not a guess"

    # No intermediate lines at all (a turn whose only JSONL line is the final result) must yield
    # an empty list, not an error -- _agenerate_inner's events[:-1] is [] in that case.
    assert _translate_intermediate_events([], run_id="r", session_id="s", stage="st", node="n") == []

    # --- Task 3b (Part 2 Ruling 10): run_id threads through the constructor into self.run_id, and
    # _agenerate_inner's own fallback expression (`self.run_id or "unknown"`) no longer collapses a
    # real value down to the placeholder. _agenerate_inner itself needs a live sandbox+CLI exec
    # (module docstring's "pure half only" scoping), so this exercises the exact expression at its
    # call site directly rather than the whole turn.
    real_run_id_model = get_chat_model_for_thread("t", "s", "r", run_id="run-real-123")
    assert real_run_id_model.run_id == "run-real-123", "run_id did not thread through the constructor"
    assert (real_run_id_model.run_id or "unknown") == "run-real-123", (
        "a real run_id must not fall back to the 'unknown' sentinel"
    )
    no_run_id_model = get_chat_model_for_thread("t", "s", "r")
    assert no_run_id_model.run_id is None, "omitting run_id must leave it None, not a silently-injected default"
    assert (no_run_id_model.run_id or "unknown") == "unknown", (
        "omitting run_id must still fall back to the pre-existing 'unknown' sentinel"
    )

    # Session-cache eviction: one thread's dead sandbox must not evict another thread's live
    # sessions (mirrors claude_chat_model._demo's doomed/survivor shape).
    doomed, survivor = "thread-doomed", "thread-survivor"
    for thread in (doomed, survivor):
        for suffix in ("specification:draft", "plan:audit"):
            _session_ids[f"{thread}:{suffix}"] = f"sess-{thread}-{suffix}"
            # Phase E audit C-2: _resume_states mirrors _session_ids' own eviction shape.
            _resume_states[f"{thread}:{suffix}"] = "unknown"

    forget_thread_sessions(doomed)
    assert not [k for k in _session_ids if k.startswith(f"{doomed}:")], "doomed sessions survived"
    assert len([k for k in _session_ids if k.startswith(f"{survivor}:")]) == 2, "evicted the wrong thread"
    assert not [k for k in _resume_states if k.startswith(f"{doomed}:")], "doomed resume_states survived"
    assert len([k for k in _resume_states if k.startswith(f"{survivor}:")]) == 2, (
        "forget_thread_sessions evicted the wrong thread's resume_states"
    )

    forget_thread_sessions("never-existed")  # must not raise

    asyncio.run(close_session(survivor, "specification", "draft"))
    assert get_session_id(survivor, "specification", "draft") is None, "close_session did not evict"
    assert get_session_id(survivor, "plan", "audit") is not None, "close_session evicted the wrong key"
    assert get_resume_state(survivor, "specification", "draft") is None, "close_session did not evict resume_state"
    assert get_resume_state(survivor, "plan", "audit") == "unknown", "close_session evicted the wrong resume_state key"

    asyncio.run(close_thread_session(survivor))
    assert not [k for k in _session_ids if k.startswith(f"{survivor}:")], "close_thread_session did not evict"
    assert not [k for k in _resume_states if k.startswith(f"{survivor}:")], (
        "close_thread_session did not evict resume_states"
    )
    assert get_resume_state("thread-never-seen", "x", "y") is None, "an unseen key must report None, not raise"

    # Phase E review (Important 1), pinned directly: a REJECTED classification must drop the
    # cached session id outright -- proven against the real function _agenerate_inner actually
    # calls, not a re-derived tautology.
    pin_key = "pin-thread:pin-stage:draft"
    _session_ids[pin_key] = "dead-session-id"
    _session_cache.record_resume_state(pin_key, "rejected")
    assert pin_key not in _session_ids, "a REJECTED classification must drop the cached session id"
    assert _resume_states[pin_key] == "rejected"

    _session_ids[pin_key] = "still-good-session-id"
    for harmless_state in ("unknown", "resumed"):
        _session_cache.record_resume_state(pin_key, harmless_state)
        assert _session_ids[pin_key] == "still-good-session-id", (
            f"{harmless_state!r} must NOT drop the cached session id, only 'rejected' does"
        )
    _resume_states.pop(pin_key, None)
    _session_ids.pop(pin_key, None)

    # Phase E review residual, pinned directly: rejected -> pop -> the very next turn requests no
    # resume (classify_resume(None, ...) returns None by contract) -> _session_cache.cache_session_id must still
    # clear the stale "rejected" verdict when it caches the fresh, unrelated new id, even though
    # record_resume_state itself never ran this turn (resume_state is None, not "rejected").
    _session_ids[pin_key] = "dead-session-id"
    _session_cache.record_resume_state(pin_key, "rejected")
    assert pin_key not in _session_ids and _resume_states[pin_key] == "rejected"  # pre-condition

    fresh_new_id = "brand-new-unrelated-session-id"
    _session_cache.cache_session_id(pin_key, fresh_new_id, None)  # None: no resume was requested this turn
    assert _session_ids[pin_key] == fresh_new_id
    assert pin_key not in _resume_states, (
        "a stale 'rejected' verdict from the OLD id must not survive to mislabel the fresh new id"
    )
    _resume_states.pop(pin_key, None)
    _session_ids.pop(pin_key, None)

    # Phase E audit I-4: _apply_disabled_skills_instruction, pure and directly testable (the
    # actual _agenerate_inner call site needs a live sandbox -- see this module's own "pure half
    # only" scoping convention).
    assert _apply_disabled_skills_instruction("do the work", None) == "do the work", (
        "no disabled_skills -- prompt must pass through completely unchanged"
    )
    assert _apply_disabled_skills_instruction("do the work", []) == "do the work", (
        "an empty disabled_skills list is the same as None -- nothing to disable"
    )
    injected = _apply_disabled_skills_instruction("do the work", ["using-superpowers", "brainstorming"])
    assert injected == (
        "Do not invoke these skills this turn under any circumstances: "
        "using-superpowers, brainstorming.\n\ndo the work"
    ), f"unexpected instruction text/placement: {injected!r}"
    assert injected.endswith("do the work"), "the original prompt content must survive verbatim, just prefixed"

    assert secret_env_names() == {
        "COPILOT_SDK_AUTH_TOKEN",
        "COPILOT_CONNECTION_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GITHUB_TOKEN",
    }

    # read_skill_invocations must fail open unconditionally (module docstring) -- None args are
    # safe here because the function never touches them, by contract.
    assert asyncio.run(read_skill_invocations(None, "thread", "session")) is None, "must always fail open"

    # --- Task 5 (Part 2 run-visibility): redaction at the actual capture path ---
    #
    # The concrete proof task-5-brief.md asks for: a payload containing a long base64-shaped
    # fake-secret-looking token, run through this module's own _translate_intermediate_events, must
    # not leak that token into either sink a translated RunEvent actually reaches -- the JSON text
    # run_event_store.append_event would INSERT (mirrored here via json.dumps; append_event itself
    # needs a live DB pool this module's self-check deliberately never opens -- module docstring)
    # and the dict run_event_stream.emit_live actually dispatches live (_json_safe_payload is pure
    # and side-effect-free, called directly here for the same "no live graph run needed" reason).
    fake_secret = "ghp_" + "x1Y2z3" * 8  # 52 chars, base64-ish -- comfortably over the 40-char floor
    secret_line = json.dumps({
        "type": "tool.call_start",
        "data": {"name": "bash", "input": {"command": f"curl -H Authorization:Bearer_{fake_secret}"}},
        "id": "sec-1", "timestamp": "2026-01-01T00:00:00.000Z", "parentId": "sec-0",
    }) + "\n"
    secret_events, _ = _parse_copilot_jsonl(secret_line)
    secret_translated = _translate_intermediate_events(
        secret_events, run_id="run-secret", session_id="thread-secret", stage="specification", node="draft",
    )
    assert len(secret_translated) == 1, secret_translated
    secret_event = secret_translated[0]
    assert fake_secret not in (secret_event.summary or ""), "token leaked into summary"
    stored_json = json.dumps(secret_event.payload)
    assert fake_secret not in stored_json, f"token leaked into the JSON the DB would store: {stored_json}"
    assert "<redacted>" in stored_json, "payload was not actually scrubbed, just happened to omit the field"
    emitted_json = json.dumps(run_event_stream._json_safe_payload(secret_event))
    assert fake_secret not in emitted_json, f"token leaked into the dict emit_live dispatches live: {emitted_json}"

    # Phase E audit finding 3's own redaction requirement, Copilot side: a secret-shaped string
    # inside the model's own (inferred) narration must be scrubbed exactly like one inside a tool
    # call's input/output is.
    secret_delta_line = json.dumps({
        "type": "assistant.message_delta",
        "data": {"text": f"the token is {fake_secret}, using it now"},
        "timestamp": "2026-01-01T00:00:00.000Z",
    }) + "\n"
    secret_reasoning_events, _ = _parse_copilot_jsonl(secret_delta_line)
    secret_reasoning_translated = _translate_intermediate_events(
        secret_reasoning_events, run_id="run-secret", session_id="thread-secret", stage="specification", node="draft",
    )
    assert len(secret_reasoning_translated) == 1, secret_reasoning_translated
    secret_reasoning_event = secret_reasoning_translated[0]
    assert secret_reasoning_event.type is RunEventType.REASONING
    assert fake_secret not in (secret_reasoning_event.summary or ""), "token leaked into REASONING summary"
    reasoning_stored_json = json.dumps(secret_reasoning_event.payload)
    assert fake_secret not in reasoning_stored_json, f"token leaked into REASONING payload: {reasoning_stored_json}"
    assert "<redacted>" in reasoning_stored_json, "REASONING payload was not actually scrubbed"

    print("copilot_chat_model self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.copilot_chat_model` loads
    # this file as "__main__", so a direct `_demo()` call would import this module a second time as
    # a non-package import -- splitting this module's own module-level `_session_ids` dict across
    # two sys.modules entries and silently breaking both the eviction self-check above and
    # sandbox.registry.pop's real production wiring, which imports this module by its package name
    # (`from ..copilot_chat_model import forget_thread_sessions`). Re-dispatching through
    # `from src.copilot_chat_model import` ensures there is only one copy of this module in
    # sys.modules. This convention is unconditional across this codebase (see cli_agent_exec.py,
    # claude_chat_model.py).
    from src.copilot_chat_model import _demo as _packaged_demo

    _packaged_demo()
