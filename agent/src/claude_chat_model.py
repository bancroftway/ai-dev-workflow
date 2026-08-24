"""LangChain chat-model adapter backed by the Claude Code CLI, run as a per-turn subprocess exec
inside the sandbox (cli_agent_exec.py's module docstring covers the shared runner both provider
modules build on).

Unlike copilot_chat_model.py's CopilotChatModel, which holds a live SDK session -- a persistent
TCP-connected object this process keeps open across turns -- there is no client or connection
object here: the CLI itself is stateless between invocations, and the whole of a session's state
is the CLI's own `session_id` string, threaded back in via `--resume` on every turn after the
first. That is why the eviction functions below (close_thread_session, forget_thread_sessions,
close_session) are pure dict pops rather than the async disconnect/client-close dance Copilot's
equivalents perform -- there is nothing live to close.

Full-authority mode is achieved differently too: Copilot pauses for a permission-request callback
unless one auto-approves every call; the Claude CLI has no such callback, so write access is
controlled entirely by --permission-mode, derived from `agent_mode` on every turn.

This module deliberately imports nothing from the `copilot` package. The kwarg vocabulary below
matches CopilotChatModel's field NAMES (so a caller can build either provider's model from the
same stage/role wiring without a provider-specific branch), but not its SDK-specific field TYPES
-- copilot_chat_model.py is itself slated to drop its own `from copilot import ...` lines once it
is rewritten onto this same CLI-exec shape, so this module reaching into that package for types
would only add a dependency everything else here is in the process of removing.

Update (task-4-report.md, Part 2 Task 4): `claude --help`, checked against this pipeline's own
pinned CLI version (2.1.126 -- the exact `ARG CLAUDE_CODE_CLI_VERSION` agent/sandbox-image/
Dockerfile pins, not an arbitrary local install), documents a third `--output-format` value beyond
the plain "text"/"json" this module used until now: "stream-json" ("realtime streaming"), gated
further by `--include-partial-messages`/`--include-hook-events` (both "only works with
--output-format=stream-json"). A real, disclosed, minimal verification call against this exact
pinned version (one turn, `--model haiku`, prompt "Use the Bash tool to run exactly this command:
echo hello-verify. Then stop, do nothing else.", run in an isolated scratch directory outside this
repo, `--permission-mode bypassPermissions --no-session-persistence`) confirmed three things
`--help`'s own text does not state: (1) `-p --output-format stream-json` fails outright ("Error:
When using --print, --output-format=stream-json requires --verbose") unless `--verbose` is also
passed -- an undocumented required companion flag, caught for free by a first attempt that failed
client-side before anything was actually spent; (2) with `--verbose` added, the CLI emits genuine
multi-line NDJSON to stdout -- the same "many small JSON objects, last one result-shaped" contract
this module already knew Copilot's (confusingly-also-named) `--output-format json` has (see
copilot_chat_model.py), not the single terminal object plain `--output-format json` gives Claude;
(3) the real captured assistant-message tool-call line is the same envelope this module's own
read_skill_invocations below already treats as CONFIRMED REAL from a real on-disk transcript
(`{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ..., "name": ...,
"input": ...}]}}`), plus a matching `{"type": "user", "message": {"content": [{"type":
"tool_result", "tool_use_id": ..., "content": ..., "is_error": ...}]}}` line -- two
independently-derived pieces of evidence in this exact codebase now agree on one real shape, not
one inference standing alone. Full transcript, real cost (a few cents against the operator's own
`claude.ai` team-subscription seat, not a separate API key -- see task-4-report.md), and credential
disclosure: task-4-report.md. This is why `_agenerate_inner` below now parses stdout as NDJSON
(`_parse_claude_jsonl`) and reads the terminal line's fields (is_error/result/session_id/usage/
total_cost_usd -- same key names as before, just read from `events[-1]` instead of the one parsed
object) instead of `json.loads(result.stdout)` directly. Unlike Copilot's Task 3 (where the
multi-line shape was already the design from day one and only ADDING intermediate-line handling
was in scope), switching Claude's own output format to get tool-call granularity necessarily
changes how the final line is parsed too -- the old single-object code would otherwise crash
outright (`json.JSONDecodeError: Extra data`) against genuinely multi-line stdout.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import shlex
import uuid
from typing import Any, Literal

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, PrivateAttr

from . import config
from . import run_event_store
from . import run_event_stream
from . import telemetry
from .cli_agent_exec import _SCRATCH_DIR, ResumeState, classify_resume, run_turn, write_scratch_file
from .redaction import redact_text, redact_value
from .run_events import RunEvent, RunEventType
from .sandbox import SandboxProvider, SandboxSession, get_sandbox_provider

logger = logging.getLogger(__name__)

# Keyed "{thread_id}:{stage}:{role}", exactly like copilot_chat_model._sessions -- a single
# LangGraph thread runs multiple stages, each with a draft and an audit role, and each of those
# (stage, role) pairs is its own independent Claude CLI conversation (its own --resume chain). The
# value is the CLI's own session_id string, nothing more -- there is no client or connection object
# to key alongside it the way Copilot needs (see this module's own docstring for why).
_session_ids: dict[str, str] = {}

# Phase E audit C-2: per-session-key resume-continuity classification, keyed identically to
# _session_ids ("{thread_id}:{stage}:{role}") -- see cli_agent_exec.classify_resume for the
# tri-state RULE and the real killed-turn experiment it is built from. Absent key means "no
# --resume has ever been attempted for this key yet", which is deliberately distinct from
# "unknown" (attempted, and the outcome could not be confirmed) -- a fresh first turn makes no
# continuity claim at all, so there is nothing to classify.
_resume_states: dict[str, ResumeState] = {}

# CLI error text that positively indicates a rejected resume, checked case-insensitively.
# INFERENCE, not confirmed real: the one real killed-turn experiment this module's classify_resume
# call is built from (fix-e3a-report.md) resumed CLEANLY -- it never triggered an actual
# rejection, so no real Claude CLI rejection message has been captured anywhere in this codebase.
# These are a defensible guess at plausible phrasing, not a verified fact; classify_resume's own
# conservative default (UNKNOWN, not REJECTED, when no marker matches) means a wrong guess here
# only under-detects REJECTED into UNKNOWN, it never mis-labels an unrelated error as a resume
# rejection.
_RESUME_REJECTED_MARKERS: tuple[str, ...] = (
    "no conversation found",
    "session not found",
    "no such session",
    "invalid session",
    "unable to resume",
)

# Copilot and Claude ship disjoint tool vocabularies, so a caller's available_tools/excluded_tools
# list -- written once, in Copilot's own "builtin:<name>" vocabulary (config.
# READ_ONLY_AVAILABLE_TOOLS) -- needs translating rather than passing through unchanged. Two
# Copilot names collapse onto Claude's single "Edit" tool (apply_patch and edit are the same
# operation under different Copilot-internal names); builtin:task_complete and builtin:ask_user are
# Copilot-native turn-control concepts with no Claude CLI tool at all.
_TOOL_NAME_MAP: dict[str, str] = {
    "builtin:view": "Read",
    "builtin:grep": "Grep",
    "builtin:glob": "Glob",
    "builtin:edit": "Edit",
    "builtin:create": "Write",
    "builtin:apply_patch": "Edit",
    "builtin:bash": "Bash",
    "builtin:skill": "Skill",
}

# Claude Code's project-transcript directory name is every non-alphanumeric character of the
# turn's cwd replaced with "-". Every turn in this pipeline execs with cwd /workspace/repo (sandbox
# providers always `exec -w /workspace/repo` -- see local_docker.py's WORKSPACE_DIR_IN_CONTAINER),
# so the slug is a fixed string here, not something to compute per-thread. Confirmed against a
# real local transcript during this plan's own prep AND, since then, byte-for-byte against a real
# Linux container run -- no longer the unverified guess task-2-brief.md originally flagged it as.
# read_skill_invocations still fails open (returns None) on any read failure regardless, the same
# "unverifiable" contract gates/skill_gate.py already relies on for Copilot -- kept as a defensive
# backstop, not because this path is expected to drift.
_CLAUDE_PROJECTS_DIR = "/home/vscode/.claude/projects/-workspace-repo"


# config.READ_ONLY_AVAILABLE_TOOLS permanently includes these two Copilot-native turn-control
# concepts (builtin:task_complete, builtin:ask_user), which have no Claude CLI tool at all and
# never will -- every caller that reuses that shared allowlist hits them on literally every turn
# under AGENT_PROVIDER=claude. _map_tool_names logs these at debug rather than warning so a
# genuinely unexpected unmapped name -- a caller typo, a new Copilot tool this map hasn't caught
# up to -- still stands out instead of drowning in expected, permanent noise.
_KNOWN_PERMANENTLY_UNMAPPED = {"builtin:task_complete", "builtin:ask_user"}


def _map_tool_names(names: list[str]) -> list[str]:
    """Translate a Copilot-vocabulary tool list to Claude CLI tool names, de-duped, in order.

    Unmapped entries are dropped rather than raised or silently kept -- passing an unrecognized
    name straight through to --tools/--disallowedTools would either be rejected by the CLI or
    silently ignored by it, and a caller reusing a read-only allowlist across both providers needs
    to know its allowlist just got narrower on this one, not fail outright over a tool that was
    never a real Claude concept to begin with. Logged at warning for a genuinely unexpected name,
    at debug for the two names in _KNOWN_PERMANENTLY_UNMAPPED above (expected on every turn, not a
    caller mistake).
    """
    mapped: list[str] = []
    unmapped: list[str] = []
    unexpected: list[str] = []
    for name in names:
        claude_name = _TOOL_NAME_MAP.get(name)
        if claude_name is None:
            unmapped.append(name)
            if name not in _KNOWN_PERMANENTLY_UNMAPPED:
                unexpected.append(name)
        elif claude_name not in mapped:
            mapped.append(claude_name)
    if unexpected:
        logger.warning("no Claude tool-name mapping for %s -- dropped from this turn's tool list", unexpected)
    elif unmapped:
        logger.debug("no Claude tool-name mapping for %s -- dropped from this turn's tool list", unmapped)
    return mapped


# Extensions for the real attachment mimeTypes RequirementsView.tsx's useAttachments config can
# actually produce (accept: "image/*,application/pdf,.doc,.docx,.txt,.md" -- part-3-attachments-
# research-notes.md section 3/6) -- not a general mimeType->extension table, just enough for what
# this pipeline's one real attachment source sends today. An unrecognized mimeType falls back to
# ".bin" in _prepare_attachment below rather than growing this table speculatively.
_ATTACHMENT_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def _prepare_attachment(item: dict[str, Any], index: int, scratch_prefix: str) -> tuple[str, bytes, str] | None:
    """Decide whether one non-text AG-UI InputContent dict is a real, decodable attachment, and if
    so, its scratch-file path, decoded bytes, and the prompt-text line that will reference it.

    Only handles `source.type == "data"` (base64 inline) -- confirmed (part-3-attachments-
    research-notes.md section 1/3) as the only shape RequirementsView.tsx's useAttachments
    actually produces today, since no onUpload backend is configured. A `source.type == "url"`
    part, the theoretical `type == "binary"` union member, or an undecodable/malformed payload
    all return None -- dropped with a warning by the caller, same as every non-text part was
    before this function existed, not guessed at.

    Pure (no I/O) on purpose: the one caller (_messages_to_prompt) does the actual scratch-file
    write, so this half stays unit-testable in _demo() without a live sandbox, matching this
    module's and cli_agent_exec.py's existing "pure half only" self-check convention.

    The returned label tells the model to read the file rather than embedding the bytes in the
    prompt text itself -- confirmed empirically (task-13) against the real pinned `claude` CLI
    that this is what actually works: `--file` requires a CLAUDE_CODE_SESSION_ACCESS_TOKEN this
    pipeline has no reason to provision ("Error: Session token required for file downloads"), and
    an inline base64 image block over `--input-format stream-json` reaches the model with no
    visual content at all (its own transcript showed it reasoning "I need... a file path" and
    asking for one) -- but a real local path mentioned in plain prompt text is exactly what
    Claude Code's own built-in Read tool (already unrestricted for this stage/role, see
    config.READ_ONLY_AVAILABLE_TOOLS's "builtin:view" -> "Read" mapping) already knows how to open,
    images included, with zero new CLI flags and zero new credentials.
    """
    source = item.get("source")
    if not isinstance(source, dict) or source.get("type") != "data":
        return None
    value = source.get("value")
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None

    ext = _ATTACHMENT_EXT_BY_MIME.get(source.get("mimeType", ""), ".bin")
    path = f"{scratch_prefix}.attach{index}{ext}"
    metadata = item.get("metadata")
    filename = metadata.get("filename") if isinstance(metadata, dict) else None
    kind = item.get("type") if isinstance(item.get("type"), str) else "file"
    described = f'{kind} "{filename}"' if filename else kind
    label = f"[Attached {described} -- read the file at {path} to see its contents.]"
    return path, raw, label


async def _messages_to_prompt(
    provider: SandboxProvider, thread_id: str, scratch_prefix: str, messages: list[BaseMessage]
) -> str:
    """Flatten a LangChain message list into a single Claude CLI prompt string.

    Mirrors copilot_chat_model._messages_to_prompt's text-handling exactly (SystemMessage gets an
    "Instructions:" prefix, everything else passes through verbatim, parts joined with a blank
    line). Unlike that function (which still drops multimodal content -- no confirmed CLI
    mechanism exists for Copilot, Ruling 9), a real non-text attachment part here is decoded and
    written to its own scratch file (sharing scratch_prefix with the turn's prompt/mcp-config
    files, so run_turn's own `rm -f {scratch_prefix}*` cleanup catches it too -- no separate
    cleanup needed), then referenced by path in the returned prompt text -- see
    _prepare_attachment's docstring for why a path reference, not a CLI flag, is what actually
    works. Only a part _prepare_attachment recognizes gets this treatment; anything else (a
    `source.type == "url"` part, the theoretical `binary` union member, an undecodable payload) is
    still dropped with a warning exactly as before.
    """
    parts: list[str] = []
    attachment_index = 0
    for message in messages:
        content = message.content
        if isinstance(content, list):
            text_parts: list[str] = []
            dropped = 0
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                    continue
                attachment_index += 1
                prepared = _prepare_attachment(item, attachment_index, scratch_prefix) if isinstance(item, dict) else None
                if prepared is None:
                    dropped += 1
                    continue
                path, raw, label = prepared
                await write_scratch_file(provider, thread_id, path, raw)
                text_parts.append(label)
            if dropped:
                logger.warning(
                    "dropped %d non-text content part(s) -- not a decodable data-sourced "
                    "attachment (url-sourced or malformed)",
                    dropped,
                )
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if isinstance(message, SystemMessage):
            parts.append(f"Instructions:\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def _parse_claude_jsonl(stdout: str) -> list[dict[str, Any]]:
    """Parse `claude -p --output-format stream-json --verbose`'s NDJSON stdout into a list of
    per-line event dicts, one dict per line.

    Mirrors copilot_chat_model._parse_copilot_jsonl's defensive per-line parsing exactly (each
    line parsed independently, so one malformed/non-JSON line cannot crash the whole turn -- it is
    simply skipped; only dict-shaped lines are kept). Does NOT need that function's other half
    (its defensive "scan every line for session_id" trick, worked around there because Copilot's
    real casing turned out broken): task-4-report.md confirmed real that Claude's own session_id
    appears, under a consistent key name, directly on the terminal `type: "result"` line -- the
    same line every existing caller already treats as authoritative for is_error/result/usage/
    total_cost_usd. The caller (_agenerate_inner) is what decides `events[-1]` is that line; this
    function only parses.
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
    return events


def _translate_intermediate_events(
    intermediate_events: list[dict[str, Any]],
    *,
    run_id: str,
    session_id: str,
    stage: str,
    node: str,
) -> list[RunEvent]:
    """Translate Claude's intermediate (non-final) parsed NDJSON lines into Task-1-shaped
    RunEvents, tool-call granularity (task-4-brief.md, mirroring task-3-brief.md's Copilot
    instruction). `_agenerate_inner` passes `events[:-1]` here -- every parsed line except the
    last, which stays the result-shaped summary line handled separately.

    CONFIRMED REAL (task-4-report.md: one real, disclosed, minimal `claude -p --output-format
    stream-json --verbose` turn against this pipeline's own pinned CLI version, 2.1.126): an
    assistant-role line whose `message.content` list contains a block shaped `{"type": "tool_use",
    "id": ..., "name": ..., "input": ...}` for each tool the model invokes -- the exact same
    envelope this module's own read_skill_invocations below already treats as confirmed real from
    a real on-disk transcript, not a fresh guess. Every other line shape observed in that same real
    capture (`type: "system"` init/hook lines, `type: "rate_limit_event"`) has no `message.content`
    list at all and is silently skipped by the `isinstance` guards below, the same defensive-skip
    spirit `_parse_claude_jsonl` already applies to a malformed line.

    Unlike copilot_chat_model._translate_intermediate_events (whose `tool.call_start`/
    `tool.call_end` become two independent, uncorrelated RunEvents -- flagged in task-3's own
    review as a consumer gotcha, not a deliberate design choice), Claude's real shape carries a
    genuine correlating id: a `tool_use` block's `id` is echoed back on the matching `{"type":
    "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ..., "content": ...,
    "is_error": ...}]}}` line once the tool finishes. This function uses that id to fold the result
    into the SAME RunEvent as its call (one full call+result per tool invocation, not two
    fragments) -- a real improvement the Copilot side has no id to make, not scope creep. A
    tool_use with no matching tool_result yet (unusual for a completed turn, but not impossible if
    a turn errors out mid-call) still yields a RunEvent, just without a "result"/"is_error" payload
    key -- fails soft, never drops the call itself.

    Phase E audit finding 3 (capture half): `type: "thinking"`/`type: "text"` content blocks --
    previously deliberately NOT translated (see this module's git history for the original
    reasoning: matching Task 3's then-current Copilot scope, not a broader granularity) -- now
    become one RunEventType.REASONING event PER BLOCK. One event per contiguous block, never
    per-token/per-line, is already what this CLI's real captured shape gives for free: every real
    sample seen in this module (including the fresh one behind classify_resume's own docstring)
    delivers a "thinking" or "text" block as one COMPLETE block on its own assistant-role line, not
    as fragmentary deltas across many lines -- this pipeline never passes
    `--include-partial-messages`, so there is no sub-block streaming to coalesce here. `summary`
    is a short head of the text (never the full text -- that stays in `payload` only); both are
    redacted through the same redact_text/redact_value calls the TOOL_CALL path below already
    uses, since agent narration is exactly as capable of echoing a secret-shaped string as a tool
    call's own input/output is.

    Phase E audit finding 6 (capture half): the real captured `tool_result` line carries a real
    `"timestamp"` field (module's own `_demo()` sample); the matching `tool_use` line does not.
    That timestamp lives on the tool_result's OUTER envelope (the line), not inside the
    `tool_result` content block itself -- `results_by_tool_use_id` below now keeps both. Folded
    into the TOOL_CALL payload as `result_ts` when present, never fabricating a start time or a
    duration -- this makes a real per-call span derivable later (once a real start time is ALSO
    captured) without inventing one now.
    """
    _REASONING_HEAD_CHARS = 160
    results_by_tool_use_id: dict[str, dict[str, Any]] = {}
    for raw_event in intermediate_events:
        message = raw_event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    results_by_tool_use_id[tool_use_id] = {
                        "content": block.get("content"),
                        "is_error": block.get("is_error"),
                        # Real (finding 6): the timestamp is on raw_event, not the block itself.
                        "timestamp": raw_event.get("timestamp"),
                    }

    translated: list[RunEvent] = []
    for raw_event in intermediate_events:
        message = raw_event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type in ("thinking", "text"):
                text = block.get("thinking") if block_type == "thinking" else block.get("text")
                text = text if isinstance(text, str) else ""
                if not text:
                    continue  # nothing to narrate (e.g. an empty text block) -- no event for it
                head = text[:_REASONING_HEAD_CHARS]
                if len(text) > _REASONING_HEAD_CHARS:
                    head += "..."
                translated.append(
                    RunEvent(
                        run_id=run_id,
                        session_id=session_id,
                        type=RunEventType.REASONING,
                        stage=stage,
                        node=node,
                        summary=redact_text(head),
                        payload=redact_value({"kind": block_type, "text": text}),
                    )
                )
                continue

            if block_type != "tool_use":
                continue
            name = block.get("name") or "unknown"
            payload: dict[str, Any] = {"name": name, "input": block.get("input")}
            tool_use_id = block.get("id")
            result_block = results_by_tool_use_id.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if result_block is not None:
                payload["result"] = result_block["content"]
                payload["is_error"] = result_block["is_error"]
                if result_block["timestamp"] is not None:
                    payload["result_ts"] = result_block["timestamp"]
            translated.append(
                RunEvent(
                    run_id=run_id,
                    session_id=session_id,
                    type=RunEventType.TOOL_CALL,
                    stage=stage,
                    node=node,
                    # Task 5 (Part 2 run-visibility): real captured tool-call content (a Bash
                    # command, its output, ...) reaching a human-facing event log for the first
                    # time -- scrub it here, before this RunEvent reaches run_event_store.
                    # append_event/run_event_stream.emit_live. Same shared helper as
                    # copilot_chat_model.py's identical call site and telemetry.py's own
                    # long-standing command scrub (redaction.py).
                    summary=redact_text(f"tool call: {name}"),
                    payload=redact_value(payload),
                )
            )
    return translated


class ClaudeChatModel(BaseChatModel):
    """A LangChain chat model driving the Claude Code CLI as a per-turn subprocess exec inside the
    sandbox (cli_agent_exec.run_turn), matching CopilotChatModel's public shape so a caller can
    build either provider's model from the same stage/role wiring without a provider-specific
    branch.

    Session lifecycle is the opposite of Copilot's: this process holds no session object open.
    Each turn is a fresh CLI invocation; continuity comes entirely from `--resume <session_id>`,
    where session_id is whatever the CLI itself returned from the previous turn (parsed from
    --output-format stream-json's terminal line, Task 4 -- previously plain --output-format json's
    one terminal object; same field name either way), cached in this module's _session_ids dict
    keyed the same way Copilot keys _sessions.
    """

    thread_id: str
    stage: str
    role: str
    # Task 3b (Part 2 Ruling 10): same field, same dispatcher-set-at-construction-time shape as
    # CopilotChatModel.run_id (see that class's own comment) -- kept in sync purely so both
    # provider constructors stay on one shape. Not read anywhere in this module yet: Claude's
    # _agenerate_inner builds no RunEvents of its own (that's Task 4, still pending); this just
    # makes the real value reachable here already, for whenever that task needs it.
    run_id: str | None = None
    model_name: str | None = None
    # Structural parity with CopilotChatModel.sandbox -- but unlike Copilot (whose SDK can start a
    # local child process when this is None), every Claude turn always execs through
    # cli_agent_exec.run_turn, which requires a sandbox already registered for thread_id regardless
    # of this field. sandbox is kept here only to gate the sandbox-filesystem-only bits below
    # (--plugin-dir, disabled_skills) the same way Copilot gates plugin_directories -- it does not
    # select between two different connection strategies the way it does on the Copilot side.
    sandbox: SandboxSession | None = None

    # Agent Plugin / write-access controls -- same kwarg vocabulary as CopilotChatModel (Part A of
    # the plugin plan), translated onto this CLI's own flags in _agenerate_inner. available_tools/
    # excluded_tools and disabled_skills accept the same Copilot-vocabulary values a caller already
    # has on hand (config.READ_ONLY_AVAILABLE_TOOLS, config.COPILOT_DISABLED_SKILLS) so a call site
    # does not need to know which provider is active to build them.
    agent_mode: Literal["interactive", "plan", "autopilot", "shell"] = "plan"
    available_tools: list[str] | None = None
    excluded_tools: list[str] | None = None
    # Never translated into a flag -- see the logger.warning at its one use site in
    # _agenerate_inner for why (no CLI equivalent for this SDK-only hook).
    pre_tool_use_hook: Any | None = None
    mcp_servers: dict[str, Any] | None = None

    # Custom agents (same fields as CopilotChatModel).
    custom_agents: list[dict] | None = None
    agent: str | None = None

    # Copilot SDK terminal-tool objects. Never translated into a flag -- see the logger.warning at
    # its one use site in _agenerate_inner; response_schema below is this provider's structured-
    # output mechanism instead.
    tools: list[Any] | None = None

    # Per-stage override of config.COPILOT_DISABLED_SKILLS, reused as-is -- same plugin-marketplace
    # content both CLIs load via --plugin-dir, so the same two mandate-style skills need silencing
    # here too.
    disabled_skills: list[str] | None = None

    # Claude-only: the Copilot SDK has no schema-constrained-output primitive to mirror, so there
    # is nothing equivalent on CopilotChatModel. Maps to --json-schema; the structured-output
    # caller (src/stack_runner.py, Task 9) sets this instead of passing a tools=[...] terminal-tool
    # object.
    response_schema: type[BaseModel] | None = None

    # Same shape as CopilotChatModel._last_usage (model/input_tokens/output_tokens/cost), for the
    # provider-agnostic OTEL span attributes _agenerate sets below. reasoning_tokens/
    # cache_read_tokens/cache_write_tokens are NOT included -- unlike Copilot's SDK usage event,
    # `claude`'s own terminal result line does not report them (true of both plain
    # --output-format json and stream-json's terminal line -- same usage keys either way, task-4-
    # report.md), and a fabricated 0 would read as "measured zero" instead of "not reported" to
    # anything that later reads this dict.
    _last_usage: dict[str, Any] | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "claude-code"

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
        # Copied unchanged from copilot_chat_model.py -- this wrapping (span naming, attributes,
        # the _last_usage reset/read) is provider-agnostic, it only touches self.thread_id/stage/
        # role/model_name and self._agenerate_inner.
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
                llm_span.set_attribute("gen_ai.usage.input_tokens", self._last_usage["input_tokens"])
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
        session_id = _session_ids.get(self._session_key)

        # Task 4 (Part 2 run-visibility): "stream-json" (real, confirmed against this pipeline's
        # own pinned CLI version -- see this module's own docstring and task-4-report.md), not
        # plain "json", is what actually gives per-tool-call granularity -- "json" is a single
        # terminal object with no intermediate lines at all. --verbose is not optional here: a real
        # invocation (not --help's own text, which never mentions this) confirmed `-p
        # --output-format stream-json` fails outright without it ("Error: When using --print,
        # --output-format=stream-json requires --verbose").
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if session_id:
            argv += ["--resume", session_id]

        permission_mode = {
            "plan": "plan",
            "autopilot": "bypassPermissions",
            "shell": "bypassPermissions",
            "interactive": "default",
        }.get(self.agent_mode, "default")
        argv += ["--permission-mode", permission_mode]

        # available_tools (allowlist) wins over excluded_tools (blocklist) when both are set --
        # same allowlist-is-authoritative precedent as CopilotChatModel (Phase A0's spike found
        # blocklisting incomplete; an allowlist is the only reliable read-only boundary).
        if self.available_tools:
            mapped = _map_tool_names(self.available_tools)
            if mapped:
                argv += ["--tools", ",".join(mapped)]
        elif self.excluded_tools:
            mapped = _map_tool_names(self.excluded_tools)
            if mapped:
                argv += ["--disallowedTools", ",".join(mapped)]

        if self.pre_tool_use_hook is not None:
            logger.warning(
                "ClaudeChatModel.pre_tool_use_hook is set but Layer 1 write-scope enforcement has "
                "no CLI equivalent to translate it into -- Layer 2's git-diff gate "
                "(gates/write_scope_gate.py) is authoritative regardless, so this turn proceeds "
                "without it"
            )
        if self.tools:
            logger.warning(
                "ClaudeChatModel.tools is set but the Copilot SDK terminal-tool mechanism does not "
                "exist for the Claude CLI -- use response_schema instead (src/stack_runner.py, "
                "Task 9); this turn ignores it"
            )

        # [] (not None) in the no-sandbox branch on purpose, unlike Copilot's plugin_directories --
        # this list is iterated directly below to emit one --plugin-dir per entry.
        plugin_directories: list[str] = config.COPILOT_PLUGIN_DIRECTORIES if self.sandbox is not None else []
        for directory in plugin_directories:
            argv += ["--plugin-dir", directory]

        # Same gating as the old SDK version's copilot_chat_model._get_session: skills only exist
        # because --plugin-dir loaded them, so with no plugin dirs there is nothing to disable
        # regardless of what disabled_skills says.
        disabled_skills = (
            (self.disabled_skills if self.disabled_skills is not None else config.COPILOT_DISABLED_SKILLS)
            if plugin_directories
            else None
        )
        if disabled_skills:
            argv += [
                "--append-system-prompt",
                "Do not invoke these skills this turn under any circumstances: "
                f"{', '.join(disabled_skills)}.",
            ]

        if self.model_name:
            argv += ["--model", self.model_name]
        if self.custom_agents:
            argv += ["--agents", json.dumps(self.custom_agents)]
        if self.agent:
            argv += ["--agent", self.agent]

        # Every scratch file for this turn (the prompt, any attachment payloads below, and the
        # mcp-config file below when present) shares this prefix -- run_turn's own cleanup
        # (`rm -f {scratch_prefix}*`) removes all of them together, so nothing here needs its own
        # cleanup step.
        scratch_prefix = f"{_SCRATCH_DIR}/claude-{self.stage}-{self.role}-{uuid.uuid4().hex}"
        # Computed here (not up near provider/session_id above) because attachment parts, if any,
        # get written to their own scratch files sharing scratch_prefix -- see _prepare_attachment.
        prompt = await _messages_to_prompt(provider, self.thread_id, scratch_prefix, messages)

        if self.mcp_servers:
            # Judgment call, unconfirmed against the live CLI this session (unlike the flags table
            # in the task brief): {"mcpServers": {...}} is Claude Code's documented .mcp.json/
            # --mcp-config file shape, and each per-server dict is passed through unreshaped on the
            # assumption both CLIs consume the same command/args/env server-config fields -- the
            # same kind of reuse the --plugin-dir marketplace format above already relies on. If
            # this shape is wrong, the CLI rejects it and the is_error check below fails this turn
            # loud, not silently.
            mcp_config_path = f"{scratch_prefix}.mcp.json"
            await write_scratch_file(
                provider, self.thread_id, mcp_config_path, json.dumps({"mcpServers": self.mcp_servers})
            )
            argv += ["--mcp-config", mcp_config_path]

        if self.response_schema is not None:
            argv += ["--json-schema", json.dumps(self.response_schema.model_json_schema())]

        command = shlex.join(argv)
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
            # Phase E audit C-2: cli_agent_exec.run_turn's own SIGKILL-the-process-group path just
            # fired. Mark this session's resume continuity UNKNOWN -- never leave the prior
            # classification (if any) standing unqualified, and never silently drop the cached id
            # either. See classify_resume's own docstring for the real experiment this is based
            # on: one real Claude session, killed this exact way (`timeout -s KILL` mid-tool-call),
            # later `--resume`d CLEANLY (same session_id, no error). Unconditionally dropping the
            # id here would trade a real, sometimes-avoidable loss of accumulated context/budget
            # for a hypothetical corruption that this experiment did not actually observe. Keeping
            # it lets the next turn's own real --resume attempt -- and this same classification
            # logic, applied to THAT attempt's real outcome -- decide for real, which is what makes
            # this legible instead of either silently trusting or silently discarding.
            if session_id:
                _resume_states[self._session_key] = "unknown"
                logger.warning(
                    "Claude turn for %r was killed mid-turn -- resume continuity for cached "
                    "session %r is now UNKNOWN (kept, not dropped: a real experiment showed a "
                    "killed session can still resume cleanly). The next turn's own --resume "
                    "attempt will classify the real outcome.",
                    self._session_key, session_id,
                )
            raise TimeoutError(
                f"{exc} -- resume continuity for session {session_id!r} at "
                f"{self._session_key!r} is now UNKNOWN (killed mid-turn)"
            ) from exc

        # Task 4 (Part 2 run-visibility): stdout is now NDJSON (this module's own docstring/
        # task-4-report.md), not one terminal object -- `json.loads(result.stdout)` directly would
        # raise on genuinely multi-line output ("Extra data"). Every line is parsed independently
        # (one malformed line can't sink the whole turn); a totally empty/unparseable stdout still
        # fails loud, matching copilot_chat_model's identical guard for the identical situation.
        events = _parse_claude_jsonl(result.stdout)
        if not events:
            # Phase E audit C-2: a resume the CLI rejects outright, before ever invoking the model,
            # would plausibly look exactly like this -- empty/unparseable stdout, a message on
            # stderr only (this module's own docstring records exactly this shape of failure for a
            # different bad invocation: "Error: When using --print, --output-format=stream-json
            # requires --verbose" arrived on stderr with no stdout at all). No real captured
            # example of an actual resume rejection exists anywhere in this codebase yet -- this
            # task's one real killed-turn experiment resumed cleanly instead (classify_resume's own
            # docstring) -- so this classifies defensively from the real text actually on hand
            # rather than claim more than is known.
            if session_id:
                combined_text = f"{result.stdout} {result.stderr}"
                rejected = any(marker in combined_text.lower() for marker in _RESUME_REJECTED_MARKERS)
                _resume_states[self._session_key] = "rejected" if rejected else "unknown"
            raise RuntimeError(
                f"Claude CLI turn for {self._session_key!r} produced no parseable "
                f"--output-format stream-json lines (resume_state="
                f"{_resume_states.get(self._session_key)!r}): stdout={result.stdout!r}\n"
                f"stderr={result.stderr!r}"
            )

        # Best-effort guess, confirmed real for this pipeline's own pinned CLI version
        # (task-4-report.md): the LAST line is the result-shaped summary event, exactly the same
        # is_error/result/session_id/usage/total_cost_usd/stop_reason keys the old single-object
        # `--output-format json` response had -- only WHERE they live changed (events[-1] instead
        # of the one parsed object), not their names.
        final = events[-1]
        is_error = bool(final.get("is_error"))
        new_session_id = final.get("session_id")

        # Phase E audit C-2: classify THIS turn's resume continuity from what was actually
        # observed -- None when no --resume was requested this turn (a fresh session's first
        # turn makes no continuity claim). See classify_resume's own docstring for the tri-state
        # rule and the real experiment it is built from. Computed before the is_error raise below
        # so the raised message can carry it (Spec: "surface the state in exception messages").
        resume_state = classify_resume(
            session_id, new_session_id, is_error, str(final.get("result") or ""), _RESUME_REJECTED_MARKERS,
        )
        if resume_state is not None:
            _resume_states[self._session_key] = resume_state
            if resume_state == "unknown":
                logger.warning(
                    "resume continuity for session %r at %r is UNKNOWN this turn (requested=%r "
                    "returned=%r is_error=%s) -- never assumed continuous without a matching id",
                    session_id, self._session_key, session_id, new_session_id, is_error,
                )

        if is_error:
            raise RuntimeError(
                f"Claude CLI turn for {self._session_key!r} reported an error "
                f"(stop_reason={final.get('stop_reason')!r}, resume_state={resume_state!r}): "
                f"{final.get('result')!r}"
            )

        if new_session_id:
            _session_ids[self._session_key] = new_session_id

        # Task 4 (Part 2 run-visibility) + Phase E audit finding 5: every intermediate NDJSON line
        # this turn produced (all of `events` except the final result-shaped line, handled above)
        # is translated, then the whole batch is appended in ONE round trip
        # (run_event_store.append_events -- was a per-event append_event/emit_live loop, N
        # sequential DB round-trips for a chatty turn) before still emitting each one live
        # individually: emit_live is an in-process custom-event dispatch, not a DB write, so the
        # Spec's "batched... not one write per X" requirement is about the append_events call, not
        # about making the live stream batch too. Both calls fail soft internally (their own
        # docstrings) -- no extra try/except needed here. self.run_id already carries the graph's
        # real per-run id (Task 3b) for every caller that has one; the "unknown" fallback is only
        # for a caller that hasn't been wired up to pass one yet, same sentinel convention as
        # copilot_chat_model's identical call site.
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

        usage = final.get("usage") or {}
        self._last_usage = {
            # Not read from the CLI response like the 3 keys below -- `claude --output-format
            # stream-json`'s terminal line reports no model field at all, so the requested model
            # name is the best available label.
            "model": self.model_name or "default",
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cost": final.get("total_cost_usd"),
        }

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
    response_schema: type[BaseModel] | None = None,
) -> ClaudeChatModel:
    """Return the chat model for the given LangGraph thread's (stage, role) Claude session.

    Same stage/role keying rationale as copilot_chat_model.get_chat_model_for_thread (a single
    thread can have up to four persistent sessions open, one per (stage, role) pair, each on a
    possibly-different model) -- see this module's _session_ids docstring.

    No github_token-equivalent parameter here: the Claude CLI authenticates from the sandbox
    container's own environment (see secret_env_names below), not from anything this process
    passes in.

    run_id (Task 3b, Part 2 Ruling 10): optional, defaults to None -- see ClaudeChatModel.run_id's
    own comment for why this module accepts it but doesn't read it yet.
    """
    return ClaudeChatModel(
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
        response_schema=response_schema,
    )


async def close_thread_session(thread_id: str) -> None:
    """Evict every cached Claude session id for a thread (call on graph run completion/error).

    Declared async only for call-site parity with copilot_chat_model.close_thread_session -- a
    future provider-agnostic dispatcher awaits whichever provider's version is active without
    needing to know which one that is. There is nothing to await here: unlike Copilot, a Claude
    session holds no client or connection object this process needs to close, so eviction is the
    same pure dict pop as forget_thread_sessions, not a network call that can fail or hang against
    an already-dead sandbox.
    """
    forget_thread_sessions(thread_id)


def forget_thread_sessions(thread_id: str) -> None:
    """Drop cached Claude session ids for a thread whose sandbox is already gone.

    Sync and network-free for the same reason close_thread_session is (see its docstring). Meant
    to be called from sandbox.registry.pop() once this provider is wired into it alongside
    copilot_chat_model.forget_thread_sessions -- the one choke point every container-destruction
    path routes through -- but that wiring is a later task's job, not this module's; registry.py
    is untouched here.
    """
    prefix = f"{thread_id}:"
    stale = [key for key in _session_ids if key.startswith(prefix)]
    for key in stale:
        _session_ids.pop(key, None)
        _resume_states.pop(key, None)
    if stale:
        logger.info("forgot %d Claude session id(s) for thread_id=%s (sandbox gone)", len(stale), thread_id)


async def close_session(thread_id: str, stage: str, role: str) -> None:
    """Drop one (thread, stage, role) Claude session id so the next call starts fresh (omits
    --resume), the same recovery mechanism as copilot_chat_model.close_session for a stage whose
    session history now contains a fabricated claim -- see that function's docstring for why a
    fresh session, not a retry in the same one, is what actually recovers from it.

    Also drops any cached resume-continuity classification (Phase E audit C-2) for the same key --
    a fresh session that hasn't attempted a resume yet has no continuity claim to report, so a
    stale "unknown"/"rejected" from the session this just replaced must not linger and be read as
    if it described the new one.

    Async for the same call-site-parity reason as close_thread_session; nothing here awaits either.
    """
    session_key = f"{thread_id}:{stage}:{role}"
    _session_ids.pop(session_key, None)
    _resume_states.pop(session_key, None)
    logger.info("closed Claude session %r so the next attempt starts fresh", session_key)


def get_session_id(thread_id: str, stage: str, role: str) -> str | None:
    """The Claude session id backing one (thread, stage, role), or None if none was created yet.

    Same purpose as copilot_chat_model.get_session_id: lets a gate verify what a stage's session
    actually did against its own transcript (read_skill_invocations below) rather than trusting
    the model's self-report.
    """
    return _session_ids.get(f"{thread_id}:{stage}:{role}")


def get_resume_state(thread_id: str, stage: str, role: str) -> ResumeState | None:
    """The last-observed resume-continuity classification for one (thread, stage, role), or None
    if no --resume has ever been attempted for this key yet (a fresh key, or one just cleared by
    close_session/forget_thread_sessions).

    Phase E audit C-2: this is the accessor that makes resume continuity legible to a caller
    outside this module -- graph.py's make_verify_node reads it (via chat_model.get_resume_state)
    right where it already resets a stalled/fabricating draft session, so a human/log reading that
    reset can tell "this session's continuity was already suspect before the reset" apart from
    "this session looked fine and got reset anyway for an unrelated reason."
    """
    return _resume_states.get(f"{thread_id}:{stage}:{role}")


async def read_skill_invocations(provider: SandboxProvider, thread_id: str, session_id: str) -> list[str] | None:
    """Skill names this Claude session actually invoked, read from its own transcript, or None if
    unverifiable -- see _CLAUDE_PROJECTS_DIR's docstring for the fail-open contract this follows
    (an infrastructure gap here must never masquerade as "no skills were invoked").

    Transcript line shape per task-2-brief.md, confirmed there against a real transcript during
    this plan's own prep: an assistant-role JSONL entry (`type == "assistant"`) whose
    `message.content` contains a block shaped
    `{"type": "tool_use", "name": "Skill", "input": {"skill": "<name>"}}`. Implemented as
    specified, not re-derived from first principles.
    """
    path = shlex.quote(f"{_CLAUDE_PROJECTS_DIR}/{session_id}.jsonl")
    result = await provider.exec_in_sandbox(thread_id, f"cat {path} 2>/dev/null")
    if not result.ok or not (result.stdout or "").strip():
        return None

    names: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                name = (block.get("input") or {}).get("skill")
                if isinstance(name, str) and name not in names:
                    names.append(name)
    return names


def secret_env_names() -> set[str]:
    """Env var names the sandbox container must already have set for this provider's CLI to
    authenticate.

    Unlike Copilot (whose github_token is a constructor kwarg on CopilotChatModel, honored only in
    local/no-sandbox mode -- see that class's own docstring), the Claude CLI reads its own auth
    directly from the container's environment on every exec, and this module never spawns it any
    other way. There is deliberately no anthropic_api_key kwarg here to plumb through: whatever
    provisions the sandbox is the thing that must ensure this name is set there, and this function
    is how it discovers what name to set for this provider.

    Two names, not one (Phase E audit C-1: the subscription/CLAUDE_CODE_OAUTH_TOKEN billing mode) --
    but the sandbox still only ever sets exactly ONE of them for a given session, never both (see
    sandbox/local_docker.py's/azure_aci.py's own provision() for why even a real+empty pair is
    avoided, not just a real+real one). This function is a declaration of the two POSSIBLE names,
    not a claim that both are simultaneously set.

    Re-exported as the same `secret_env_names()` symbol via chat_model.py for both providers, but
    NOT the same contract: copilot_chat_model.py's function of this name means something else
    entirely (a --secret-env-vars masking/redaction list for that turn's own shell output, not an
    auth-source declaration -- see that module's own docstring). A reader who only ever looks at
    one provider's version should not assume the other works the same way.
    """
    return {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}


def _demo() -> None:
    """Self-check for the pure tool-name mapping and the session-cache eviction path (the live
    CLI-exec path needs a sandbox -- see cli_agent_exec.py's and copilot_chat_model.py's own demos
    for the same "pure half only" scoping).
    """
    # Known Copilot-vocabulary names translate and de-dupe; a fully-unknown name is dropped, not
    # silently kept or raised on.
    mapped = _map_tool_names(["builtin:view", "builtin:edit", "builtin:apply_patch", "builtin:task_complete"])
    assert mapped == ["Read", "Edit"], f"expected de-duped known names only, got {mapped}"
    all_known = _map_tool_names(
        ["builtin:grep", "builtin:glob", "builtin:bash", "builtin:skill", "builtin:create"]
    )
    assert all_known == ["Grep", "Glob", "Bash", "Skill", "Write"], f"unexpected mapping: {all_known}"
    assert _map_tool_names(["builtin:ask_user"]) == [], "fully-unknown list should map to empty, not raise"

    # Task 3b (Part 2 Ruling 10): run_id threads through the constructor same as CopilotChatModel's
    # (shape parity, even though nothing here reads it yet -- see ClaudeChatModel.run_id's comment).
    assert get_chat_model_for_thread("t", "s", "r", run_id="run-real-123").run_id == "run-real-123", (
        "run_id did not thread through the constructor"
    )
    assert get_chat_model_for_thread("t", "s", "r").run_id is None, "omitting run_id must leave it None"

    # _prepare_attachment (task-13): the pure decode/shape half of attachment forwarding -- the
    # actual write_scratch_file call needs a live sandbox, same "pure half only" scoping as
    # everything else in this self-check, but this is the one branch worth locking in given it's a
    # real parser over untrusted frontend-supplied dicts (AG-UI InputContent), not a one-liner.
    real_png = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake-but-decodable-bytes").decode("ascii")
    prepared = _prepare_attachment(
        {"type": "image", "source": {"type": "data", "value": real_png, "mimeType": "image/png"}},
        1,
        "/tmp/aidw-agent/scratch-abc",
    )
    assert prepared is not None, "well-formed data-sourced image attachment should not be dropped"
    path, raw, label = prepared
    assert path == "/tmp/aidw-agent/scratch-abc.attach1.png", f"unexpected scratch path: {path}"
    assert raw == base64.b64decode(real_png), "decoded bytes should match the original payload exactly"
    assert path in label and "image" in label, f"label should name the kind and reference the path: {label!r}"

    # Unrecognized mimeType falls back to .bin rather than raising or guessing an extension.
    unknown_mime = _prepare_attachment(
        {"type": "document", "source": {"type": "data", "value": real_png, "mimeType": "application/x-weird"}},
        2,
        "/tmp/aidw-agent/scratch-abc",
    )
    assert unknown_mime is not None and unknown_mime[0].endswith(".bin"), "unknown mimeType should fall back to .bin"

    # A filename in metadata is surfaced in the label for a more useful prompt reference.
    named = _prepare_attachment(
        {
            "type": "image",
            "source": {"type": "data", "value": real_png, "mimeType": "image/png"},
            "metadata": {"filename": "screenshot.png"},
        },
        3,
        "/tmp/aidw-agent/scratch-abc",
    )
    assert named is not None and "screenshot.png" in named[2], "filename metadata should appear in the label"

    # Shapes this pipeline's one real frontend source never actually produces today (url-sourced,
    # the theoretical "binary" union member) are dropped, not guessed at -- same as malformed
    # base64 and a part with no "source" key at all.
    assert _prepare_attachment(
        {"type": "image", "source": {"type": "url", "value": "https://example.com/x.png"}}, 4, "/tmp/x"
    ) is None, "url-sourced attachments are not handled -- no upload backend exists to have produced one"
    assert _prepare_attachment(
        {"type": "binary", "mimeType": "image/png", "data": real_png}, 4, "/tmp/x"
    ) is None, "the flat 'binary' InputContent variant (no nested source) is not handled"
    assert _prepare_attachment(
        {"type": "image", "source": {"type": "data", "value": "not-valid-base64!!!", "mimeType": "image/png"}},
        4,
        "/tmp/x",
    ) is None, "undecodable base64 should be dropped, not raise"

    # --- Task 4 (Part 2 run-visibility): _parse_claude_jsonl / _translate_intermediate_events ---
    #
    # REAL captured shape (task-4-report.md has the full transcript, cost, and credential
    # disclosure): one real, disclosed, minimal `claude -p --output-format stream-json --verbose`
    # invocation against this pipeline's own pinned CLI version (2.1.126), prompt "Use the Bash
    # tool to run exactly this command: echo hello-verify. Then stop, do nothing else.",
    # `--model haiku`. `session_id`/`uuid`/tool_use `id` values below are copied verbatim (random
    # UUIDs, nothing sensitive); the `system`/init line's tools/mcp_servers/slash_commands lists
    # and the `usage` sub-dicts on the two assistant lines are trimmed to short stand-ins (the real
    # ones are large and carry no test signal here) -- everything else, including the surprising
    # bits (an empty `"result":""` on a genuinely successful turn, because the prompt said "then
    # stop" and the model took it literally), is byte-for-byte what was captured.
    real_shape_jsonl = (
        '{"type":"system","subtype":"init","cwd":"/workspace/repo","session_id":'
        '"cf9ce8f4-8863-463f-acc9-82e19fab0f59","tools":["Bash","Read","Edit"],"mcp_servers":[],'
        '"model":"claude-haiku-4-5-20251001","permissionMode":"bypassPermissions",'
        '"apiKeySource":"none","claude_code_version":"2.1.126",'
        '"uuid":"1b6757b7-a59a-450e-b8e9-894e0ffa9a26"}\n'
        '{"type":"assistant","message":{"model":"claude-haiku-4-5-20251001",'
        '"id":"msg_011CeLGf4gHcp3LmA8QPVDpH","type":"message","role":"assistant","content":'
        '[{"type":"thinking","thinking":"The user is asking me to run echo hello-verify, then '
        'stop.","signature":"trimmed"}],"stop_reason":null,"usage":{"input_tokens":10,'
        '"output_tokens":8}},"parent_tool_use_id":null,'
        '"session_id":"cf9ce8f4-8863-463f-acc9-82e19fab0f59",'
        '"uuid":"962294a3-f4bd-4be7-b0da-374ab6cd32e4"}\n'
        '{"type":"assistant","message":{"model":"claude-haiku-4-5-20251001",'
        '"id":"msg_011CeLGf4gHcp3LmA8QPVDpH","type":"message","role":"assistant","content":'
        '[{"type":"tool_use","id":"toolu_01TPxxQyXkVt2ZFAUepbdmqt","name":"Bash","input":'
        '{"command":"echo hello-verify","description":"Run verification command"},"caller":'
        '{"type":"direct"}}],"stop_reason":null,"usage":{"input_tokens":10,"output_tokens":8}},'
        '"parent_tool_use_id":null,"session_id":"cf9ce8f4-8863-463f-acc9-82e19fab0f59",'
        '"uuid":"18f0b459-3747-4aec-ace1-b6de6d670b7a"}\n'
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1787520600,'
        '"rateLimitType":"five_hour","overageStatus":"rejected",'
        '"overageDisabledReason":"org_level_disabled","isUsingOverage":false},'
        '"uuid":"999757f8-aeef-476f-8e21-afbd1dec2950",'
        '"session_id":"cf9ce8f4-8863-463f-acc9-82e19fab0f59"}\n'
        '{"type":"user","message":{"role":"user","content":[{"tool_use_id":'
        '"toolu_01TPxxQyXkVt2ZFAUepbdmqt","type":"tool_result","content":"hello-verify",'
        '"is_error":false}]},"parent_tool_use_id":null,'
        '"session_id":"cf9ce8f4-8863-463f-acc9-82e19fab0f59",'
        '"uuid":"82033a77-9f5b-4943-b783-3aa47235acdd",'
        '"timestamp":"2026-08-23T19:57:39.403Z"}\n'
        '{"type":"result","subtype":"success","is_error":false,"duration_ms":8138,'
        '"num_turns":2,"result":"","stop_reason":"end_turn",'
        '"session_id":"cf9ce8f4-8863-463f-acc9-82e19fab0f59",'
        '"total_cost_usd":0.023036500000000005,'
        '"usage":{"input_tokens":18,"output_tokens":196}}\n'
    )
    real_events = _parse_claude_jsonl(real_shape_jsonl)
    assert len(real_events) == 6, f"expected 6 parsed real-shape events, got {len(real_events)}"

    # The terminal line's fields are read by _agenerate_inner exactly as before (same key names),
    # just from events[-1] instead of the one whole-stdout-parsed object -- asserted directly here
    # since that call site itself needs a live sandbox+CLI exec to exercise end to end.
    real_final = real_events[-1]
    assert real_final["is_error"] is False
    assert real_final["session_id"] == "cf9ce8f4-8863-463f-acc9-82e19fab0f59"
    assert real_final["total_cost_usd"] == 0.023036500000000005
    assert real_final["usage"] == {"input_tokens": 18, "output_tokens": 196}
    # A real, slightly funny edge case worth locking in: "result" can be genuinely EMPTY on a
    # successful (is_error=False) turn if the model produces no final text block (here, because
    # the prompt said "then stop" and it took that literally) -- AIMessage(content="") must not be
    # treated as a parse failure by anything downstream.
    assert real_final["result"] == "", "a successful turn can legitimately have an empty result"

    # The actual point: fed through the translator, the one real tool_use+tool_result pair becomes
    # exactly ONE fully-correlated RunEvent (name, input, AND result folded together via the real
    # tool_use_id/id correlation -- see _translate_intermediate_events' own docstring for why this
    # is possible for Claude but not for Copilot's own uncorrelated pair).
    #
    # Phase E audit finding 3 (capture half): this exact real capture ALSO contains a real
    # "thinking" block (line 2, unchanged since task-4) that was previously dropped on the floor --
    # it now yields its own REASONING event, so this proof runs against the same real sample
    # task-4 already captured, not a freshly-invented fixture built to agree with the new code.
    real_translated = _translate_intermediate_events(
        real_events[:-1], run_id="run-real", session_id="thread-real", stage="specification", node="draft",
    )
    assert len(real_translated) == 2, (
        f"expected 1 REASONING + 1 correlated tool-call RunEvent, got {len(real_translated)}"
    )
    real_reasoning, real_event = real_translated
    assert real_reasoning.type is RunEventType.REASONING
    assert real_reasoning.run_id == "run-real" and real_reasoning.session_id == "thread-real"
    assert real_reasoning.summary == "The user is asking me to run echo hello-verify, then stop."
    assert real_reasoning.payload == {
        "kind": "thinking",
        "text": "The user is asking me to run echo hello-verify, then stop.",
    }
    assert real_event.type is RunEventType.TOOL_CALL
    assert real_event.run_id == "run-real" and real_event.session_id == "thread-real"
    assert real_event.stage == "specification" and real_event.node == "draft"
    assert real_event.summary == "tool call: Bash"
    assert real_event.payload == {
        "name": "Bash",
        "input": {"command": "echo hello-verify", "description": "Run verification command"},
        "result": "hello-verify",
        "is_error": False,
        # Phase E audit finding 6 (capture half): the real tool_result line's own real timestamp,
        # never fabricated -- see _translate_intermediate_events' own docstring.
        "result_ts": "2026-08-23T19:57:39.403Z",
    }
    assert real_event.seq is None and real_event.ts is None, "append_event fills these in, not the translator"

    # Fails-soft branch, not present in the one real capture (that turn's single tool call WAS
    # cleanly paired) -- a tool_use with no matching tool_result yet must still yield a RunEvent,
    # just without "result"/"is_error" payload keys, rather than being dropped. Clearly a synthetic
    # addition for this one branch, not claimed as real.
    unpaired_jsonl = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use",'
        '"id":"toolu_SYNTHETIC_no_result","name":"Read","input":{"file_path":"a.py"}}]},'
        '"session_id":"s"}\n'
    )
    unpaired_events = _parse_claude_jsonl(unpaired_jsonl)
    unpaired_translated = _translate_intermediate_events(
        unpaired_events, run_id="r", session_id="s", stage="st", node="n"
    )
    assert len(unpaired_translated) == 1
    assert unpaired_translated[0].payload == {"name": "Read", "input": {"file_path": "a.py"}}, (
        "an unpaired tool_use must still produce a RunEvent, just without result/is_error keys"
    )

    # Phase E audit finding 3: synthetic coverage for branches the one real capture doesn't
    # exercise -- a "text" block (the real capture only has "thinking"), a block long enough to
    # need its summary head-truncated, and an empty block that must yield NO event at all (nothing
    # to narrate). Clearly labelled synthetic, not claimed as real.
    long_text = "word " * 40  # 200 chars, over _REASONING_HEAD_CHARS=160
    reasoning_jsonl = (
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}}) + "\n"
    )
    reasoning_events = _parse_claude_jsonl(reasoning_jsonl)
    reasoning_translated = _translate_intermediate_events(
        reasoning_events, run_id="r", session_id="s", stage="st", node="n"
    )
    assert len(reasoning_translated) == 1, "an empty thinking/text block must yield no event -- nothing to narrate"
    long_event = reasoning_translated[0]
    assert long_event.type is RunEventType.REASONING
    assert long_event.payload == {"kind": "text", "text": long_text}, "full text always in payload, never truncated"
    assert long_event.summary == long_text[:160] + "...", "summary should be a truncated head, not the full text"
    assert len(long_event.summary) < len(long_text), "summary must actually be shorter than the full text"

    # A garbage/non-JSON line, and a JSON line that isn't an object, must not crash the parser --
    # both are skipped, not fatal (mirrors copilot_chat_model._parse_copilot_jsonl's identical
    # guard).
    garbage_events = _parse_claude_jsonl('not json at all\n[1, 2, 3]\n{"type": "result", "is_error": false}\n')
    assert len(garbage_events) == 1, f"malformed/non-dict lines should be skipped, got {garbage_events}"

    # Fully empty/unparseable stdout: the parser itself just reports an empty list and never raises
    # -- _agenerate_inner is what turns that into a RuntimeError.
    assert _parse_claude_jsonl("not json\n\n   \n") == []

    # No intermediate lines at all (a turn whose only NDJSON line is the final result) must yield
    # an empty list, not an error -- _agenerate_inner's events[:-1] is [] in that case.
    assert _translate_intermediate_events([], run_id="r", session_id="s", stage="st", node="n") == []

    # Session-cache eviction: one thread's dead sandbox must not evict another thread's live
    # sessions (mirrors copilot_chat_model._demo's doomed/survivor shape -- same failure class,
    # simpler state here since there's no client/lock dict alongside the session-id cache).
    doomed, survivor = "thread-doomed", "thread-survivor"
    for thread in (doomed, survivor):
        for suffix in ("specification:draft", "plan:audit"):
            _session_ids[f"{thread}:{suffix}"] = f"sess-{thread}-{suffix}"
            # Phase E audit C-2: _resume_states must mirror _session_ids' own eviction shape --
            # seeded here so forget_thread_sessions/close_session/close_thread_session below are
            # proven to clear BOTH dicts, not just the session-id one.
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

    # --- Task 5 (Part 2 run-visibility): redaction at the actual capture path ---
    #
    # Same concrete proof as copilot_chat_model._demo()'s own Task 5 block (task-5-brief.md): a
    # long base64-shaped fake-secret-looking token, injected into BOTH a tool_use's `input` (the
    # command) and its paired tool_result's `content` (the output) -- task-5-brief's own example is
    # exactly "a Bash tool's command/output" -- must not survive _translate_intermediate_events into
    # either sink a translated RunEvent actually reaches: the JSON text run_event_store.append_event
    # would INSERT (mirrored via json.dumps; append_event itself needs a live DB pool this module's
    # self-check deliberately never opens) and the dict run_event_stream.emit_live actually
    # dispatches live (_json_safe_payload is pure/side-effect-free, called directly, no live graph
    # run needed).
    fake_secret = "ghp_" + "x1Y2z3" * 8  # 52 chars, base64-ish -- comfortably over the 40-char floor
    secret_use_line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "toolu_secret", "name": "Bash",
            "input": {"command": f"echo {fake_secret}"},
        }]},
    }) + "\n"
    secret_result_line = json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "toolu_secret",
            "content": f"token-in-output: {fake_secret}", "is_error": False,
        }]},
    }) + "\n"
    secret_events = _parse_claude_jsonl(secret_use_line + secret_result_line)
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

    # Phase E audit finding 3's own redaction requirement ("redacted with the same redact_text/
    # redact_value calls the TOOL_CALL path already uses on the same line") -- proof that a
    # secret-shaped string inside the model's own narration is scrubbed exactly like one inside a
    # tool call's input/output is, both in the summary head and the full payload text.
    secret_thinking_line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": f"the token is {fake_secret}, using it now"}]},
    }) + "\n"
    secret_reasoning_events = _parse_claude_jsonl(secret_thinking_line)
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

    print("claude_chat_model self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.claude_chat_model` loads this
    # file as "__main__", so a direct `_demo()` call would import this module a second time as a
    # non-package import -- splitting this module's own module-level `_session_ids` dict across two
    # sys.modules entries and silently breaking the eviction self-check above. Re-dispatching
    # through `from src.claude_chat_model import` ensures there is only one copy of this module in
    # sys.modules. This convention is unconditional across this codebase (see cli_agent_exec.py,
    # copilot_chat_model.py).
    from src.claude_chat_model import _demo as _packaged_demo

    _packaged_demo()
