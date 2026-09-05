"""Shared CLI-subprocess runner for per-turn provider execution inside the sandbox.

Both providers (GitHub Copilot and Claude Code) become per-turn subprocess execs launched
inside the sandbox. This module factors out what's identical between them: scratch-file
writing, backgrounded turn launch, polling, timeout handling, and cleanup. Only the argv
construction and per-line/whole-output JSON parsing differ per provider (Tasks 2 and 3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from langchain_core.messages import BaseMessage, SystemMessage

from . import config
from . import run_event_store
from . import run_event_stream
from .run_events import RunEvent, RunEventType
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Keep each exec's command line well under Windows' ~32K CreateProcess cap (WinError 206).
_EXEC_CMD_BUDGET = 16000

# Scratch-file directory for all provider execs (shared, not provider-specific).
_SCRATCH_DIR = "/tmp/aidw-agent"

def _llm_io_log_path() -> Path:
    """Host-side sink for AIDW_LOG_LLM_IO: raw per-turn stdout (both providers' stream-json/JSONL
    already carries every tool call, including the literal path argument of every file write), so
    a Phase 0-style spike can grep what path the model actually asked to write to, without a
    per-tool-call parser this shared runner has no reason to own otherwise."""
    return Path(os.environ.get("AIDW_LOG_LLM_IO_PATH") or (Path(__file__).parent.parent / "agent-work" / "llm-io.jsonl"))


# A single completion-wait exec blocks (via the remote `timeout`/`tail --pid` below) for up to
# this long before returning to let the host loop re-check its own deadline and re-touch
# last_active (see local_docker.py's DEFAULT_IDLE_TIMEOUT_SECONDS=1800 -- this is comfortably
# under that with margin to spare). Replaces a fixed-interval host-side poll (2026-09-01: a
# runaway stack of those, each issuing its own `docker exec` every few seconds, hammered Docker
# Desktop's API into a VM reset). One exec call per chunk instead of one every few seconds cuts
# call volume ~60x on a long turn while still noticing completion within a second or two, since
# the remote wait itself is event-driven (`tail --pid`), not a sleep loop.
_ACTIVITY_CHUNK_SECONDS = 300.0

# Agent Narration Drawer feature: a MUCH shorter per-iteration chunk than the plain-completion-wait
# loop above uses, ONLY for a caller that passes classify_line to run_turn (both provider chat
# models, as of this feature). classify_line=None skips this branch and the exec-call cadence
# entirely, keeping _ACTIVITY_CHUNK_SECONDS's original ~18-calls-per-turn shape for anything that
# doesn't need live narration. Env-overridable so an operator can dial this down without a code
# change -- same override convention as AIDW_SANDBOX_IDLE_TIMEOUT (local_docker.py/azure_aci.py).
#
# ponytail: ~40x more host-side exec calls than the 300s cadence for a full-length turn (5400s/7s
# ~= 771 vs 5400s/300s ~= 18) -- a real, accepted trade-off (deliberately NOT the same failure shape
# as the 2026-09-01 Docker Desktop incident above, which was many OVERLAPPING independent pollers;
# this is one well-behaved loop at a fixed cadence), not a free lunch. If a real deployment's exec
# volume ever becomes a problem, raise AIDW_NARRATION_POLL_SECONDS first before touching this code.
_NARRATION_POLL_SECONDS = float(os.environ.get("AIDW_NARRATION_POLL_SECONDS", "7.0"))

# Per-poll tail-read cap: bounds one exec call's response size regardless of how much a turn wrote
# since the last poll. A burst bigger than this is simply consumed across more than one iteration
# (byte_offset only advances by what was actually read) -- see run_turn's DONE-but-hit-cap handling.
_MAX_TAIL_BYTES = 2 * 1024 * 1024

# Delimiter lines wrapping the base64 tail payload in a streaming poll's combined stdout, so the
# host can locate it by structure (index of these exact lines) rather than by substring-searching
# for DONE/DEAD/PENDING -- a base64 blob can coincidentally contain any of those three words.
_TAIL_MARKER_START = "===TAIL-B64-START==="
_TAIL_MARKER_END = "===TAIL-B64-END==="


def parse_jsonl_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line into a dict, or None if it's blank, malformed, or not object-shaped.

    Extracted from copilot_chat_model._parse_copilot_jsonl/claude_chat_model._parse_claude_jsonl's
    near-identical per-line bodies (Agent Narration Drawer feature) so run_turn's own incremental
    streaming below and both providers' whole-buffer batch parsers apply the EXACT SAME
    line-acceptance rule -- TurnResult.streamed_line_count is a plain index into the batch parsers'
    own `events` list, and that index is only meaningful if both paths agree on which lines count.
    """
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _split_tail_poll_output(stdout_text: str) -> tuple[str, str]:
    """Split one streaming poll iteration's combined stdout into (base64_tail_payload, status).

    status is DONE/DEAD/PENDING -- the LAST non-empty line, matched exactly, never a substring
    search (see _TAIL_MARKER_START/_TAIL_MARKER_END's own comment for why). base64_tail_payload is
    whatever sits between the two marker lines (normally a single line -- tr -d '\\n' guarantees
    the base64 command's own output carries no embedded newlines), joined defensively in case a
    future change ever splits it further. Missing markers entirely (e.g. the tail-read commands
    themselves failed) falls back to treating the whole output as status text -- the same leniency
    the original substring-search had, so a transient shell hiccup degrades to "no new bytes this
    poll," never a crash.
    """
    lines = stdout_text.split("\n")
    try:
        start_idx = lines.index(_TAIL_MARKER_START)
        end_idx = lines.index(_TAIL_MARKER_END, start_idx + 1)
    except ValueError:
        status = next((line.strip() for line in reversed(lines) if line.strip()), "")
        return "", status
    payload = "".join(lines[start_idx + 1 : end_idx])
    status = next((line.strip() for line in reversed(lines[end_idx + 1 :]) if line.strip()), "")
    return payload, status


@dataclass
class TurnResult:
    """Result of a backgrounded provider turn execution."""

    stdout: str
    stderr: str
    exit_code: int
    # Agent Narration Drawer feature: number of complete JSONL lines already streamed live during
    # the turn (0 when run_turn was called with classify_line=None). Callers slice their own
    # post-hoc parsed `events` list from this index (events[streamed_line_count:-1]) instead of
    # re-translating everything the streaming path already handled -- see run_turn's docstring.
    streamed_line_count: int = 0


class TurnTimeout(TimeoutError):
    """run_turn's timeout, carrying whatever the CLI had streamed to stdout before the kill.

    The stream-json `system/init` line -- and with it the session_id -- is emitted at turn START,
    so a killed turn's partial output still names the session a retry can `--resume`. Observed
    live (run d16959d3): three 40-minute minimal-code-to-green turns, each killed at the ceiling,
    each retried as a FRESH session because the id was only ever read from the final result line
    that a killed turn never produces -- 2.4 hours of work re-derived three times."""

    def __init__(self, message: str, partial_stdout: str = "") -> None:
        super().__init__(message)
        self.partial_stdout = partial_stdout


class TurnCrashed(TurnTimeout):
    """The backgrounded CLI process died (crash/OOM/signal) WITHOUT ever writing exit_path,
    detected via /proc/$PID/stat's zombie state instead of exhausting the full timeout_seconds
    budget waiting on a file that will never appear. Subclasses TurnTimeout (not a bare
    RuntimeError): both chat models' `except TimeoutError` handlers already do exactly the right
    thing for "the turn didn't finish" -- cache the partial stdout's session id for --resume,
    mark resume continuity unknown (never dropped), re-raise with a clearer message -- so this
    needs ZERO call-site changes in claude_chat_model.py/copilot_chat_model.py; only the message
    text (and how fast this fires vs. a real timeout) tells the two apart.
    """


# Phase E audit C-2 ("Resume-rejected is not a distinct signal ... collapsed exactly as the Spec
# forbade"): shared tri-state vocabulary for both claude_chat_model.py and copilot_chat_model.py,
# which each hold their own per-provider SessionCache instance (see that class below) -- the STATE
# lives with the callers, this module has no `--resume`/`--session-id` argv knowledge; the
# classification VOCABULARY and RULE are shared, the same split RunEventType (run_events.py) makes
# between "what the value means" and "who writes it."
ResumeState = Literal["resumed", "rejected", "unknown"]


def classify_resume(
    requested_id: str | None,
    returned_id: str | None,
    is_error: bool,
    error_text: str,
    rejected_markers: tuple[str, ...],
) -> ResumeState | None:
    """Classify one completed turn's resume continuity against what the CLI actually reported --
    never against what a caller merely hoped for.

    Returns None when `requested_id` is falsy: no `--resume`/`--session-id` was requested this
    turn, so there is no continuity claim to classify (a fresh session succeeding says nothing
    about a PRIOR session's fate -- callers must leave any earlier `_resume_states` entry for a
    different key untouched in that case, not overwrite it with a verdict about an unrelated turn).

    Real experiment (Phase E, fix-e3a-report.md -- Spec Verification 4, "the single highest-risk
    untested behavior in the whole shared runner"): one real Claude Code turn (haiku,
    `--output-format stream-json --verbose`, forced into a long `sleep 180` Bash tool call) was
    SIGKILLed mid-tool-call via `timeout -s KILL 8`, confirmed genuinely killed (exit 137, no
    terminal `result` line ever written). A real `claude --resume <that session_id>` attempt
    immediately after came back CLEAN: `is_error: false`, the terminal line's own `session_id`
    EQUAL to the requested id, model replied coherently. So a killed-mid-tool-call session is NOT
    automatically unresumable -- this function's job is to tell the three real outcomes apart
    after observing one, never to assume the worst (or the best) before that.

    - REJECTED: `is_error` and `error_text` positively matches one of `rejected_markers` -- a
      provider-specific set of substrings. Neither provider has a real captured example of an
      actual rejection message yet (this experiment's one real resume attempt succeeded instead)
      -- see each call site's own comment for why its marker tuple is labelled inference.
    - RESUMED: not `is_error` and `returned_id == requested_id` -- the terminal line's own report
      of which session it used matches what was asked for. This is what the real experiment above
      actually observed.
    - UNKNOWN: anything else that isn't "no resume requested" -- a different `returned_id` (a
      silent fresh start), a missing/unparseable one, or an `is_error` turn that matches none of
      the rejection markers. Conservative default: never claim RESUMED without the id match
      actually confirming it.
    """
    if not requested_id:
        return None
    if is_error and any(marker in error_text.lower() for marker in rejected_markers):
        return "rejected"
    if not is_error and returned_id == requested_id:
        return "resumed"
    return "unknown"


# CLI error text that positively indicates a rejected resume, checked case-insensitively.
# INFERENCE, not confirmed real: the one real killed-turn experiment classify_resume is built from
# (fix-e3a-report.md) resumed CLEANLY -- no real rejection message has been captured for EITHER
# provider's CLI anywhere in this codebase. These are a defensible guess at plausible phrasing;
# classify_resume's own conservative default (UNKNOWN, not REJECTED, when no marker matches) means
# a wrong guess here only under-detects REJECTED into UNKNOWN, it never mis-labels an unrelated
# error as a resume rejection.
_RESUME_REJECTED_MARKERS: tuple[str, ...] = (
    "no conversation found",
    "session not found",
    "no such session",
    "invalid session",
    "unable to resume",
)


class SessionCache:
    """Per-provider session-id + resume-state cache, keyed "{thread_id}:{stage}:{role}".

    A single LangGraph thread runs multiple stages, each with a draft and an audit role, and each
    of those (stage, role) pairs is its own independent CLI conversation (its own
    --resume/--session-id chain). The value is the CLI's own session_id string, nothing more --
    there is no client or connection object to key alongside it under the per-turn CLI-exec model.

    Both provider modules used to carry byte-identical copies of these dicts and methods; the
    STATE still lives with each provider (each module holds its own instance -- eviction of one
    provider's sessions must never touch the other's), only the mechanics are shared here.
    `provider_label` only affects log text.

    `resume_states` (Phase E audit C-2) mirrors `session_ids`' keying and eviction: an absent key
    means "no resume has ever been attempted for this key yet", deliberately distinct from
    "unknown" (attempted, outcome unconfirmed) -- see classify_resume above for the tri-state rule.
    """

    def __init__(self, provider_label: str) -> None:
        self.provider_label = provider_label
        self.session_ids: dict[str, str] = {}
        self.resume_states: dict[str, ResumeState] = {}

    def record_resume_state(self, session_key: str, resume_state: ResumeState) -> None:
        """Record this turn's resume classification for `session_key`, and drop the cached session
        id outright when it was positively REJECTED (Phase E review, Important 1) -- a dead id must
        never survive to be resumed again: infra_retry's own backoff would otherwise resume the
        SAME dead session two more times before the stage escalates infra_exhausted.
        """
        self.resume_states[session_key] = resume_state
        if resume_state == "rejected":
            self.session_ids.pop(session_key, None)

    def cache_session_id(self, session_key: str, new_session_id: str, resume_state: ResumeState | None) -> None:
        """Cache the CLI's returned session id for the next turn, and drop any resume-state verdict
        that no longer describes it (Phase E review residual).

        The invariant: a `resume_states` entry must never describe a session id that is no longer
        the one cached under this key. classify_resume only ever returns "resumed" when the
        returned id equals the id actually asked to resume -- the ONE case where the verdict just
        recorded is genuinely about the id being cached here. Every other case (None: no resume
        requested this turn, e.g. right after a REJECTED id was popped; "unknown" with a different
        returned id: a suspected silent fresh start) means the id being cached has no verdict of
        its own yet, so any stale entry must be cleared rather than left to mislabel it.
        """
        self.session_ids[session_key] = new_session_id
        if resume_state != "resumed":
            self.resume_states.pop(session_key, None)

    def forget_thread_sessions(self, thread_id: str) -> None:
        """Drop every cached session id (and resume verdict) for a thread whose sandbox is gone."""
        prefix = f"{thread_id}:"
        stale = [key for key in self.session_ids if key.startswith(prefix)]
        for key in stale:
            self.session_ids.pop(key, None)
            self.resume_states.pop(key, None)
        if stale:
            logger.info(
                "forgot %d %s session id(s) for thread_id=%s (sandbox gone)",
                len(stale), self.provider_label, thread_id,
            )

    def close_session(self, thread_id: str, stage: str, role: str) -> None:
        """Drop one (thread, stage, role) session id -- and any stale resume verdict -- so the next
        call starts fresh."""
        session_key = f"{thread_id}:{stage}:{role}"
        self.session_ids.pop(session_key, None)
        self.resume_states.pop(session_key, None)
        logger.info("closed %s session %r so the next attempt starts fresh", self.provider_label, session_key)

    def get_session_id(self, thread_id: str, stage: str, role: str) -> str | None:
        return self.session_ids.get(f"{thread_id}:{stage}:{role}")

    def get_resume_state(self, thread_id: str, stage: str, role: str) -> ResumeState | None:
        return self.resume_states.get(f"{thread_id}:{stage}:{role}")


def flatten_messages_to_prompt(messages: list[BaseMessage], drop_warning: str) -> str:
    """Flatten a LangChain message list into a single CLI prompt string -- the text core shared by
    both providers' _messages_to_prompt wrappers: a SystemMessage gets an "Instructions:" prefix,
    everything else passes through verbatim, list-shaped content keeps only its text-typed parts
    (anything else is dropped, warned once per message via `drop_warning` -- a logging format
    string with one %d slot for the dropped count), and messages are joined with a blank line.
    """
    parts: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            text_parts: list[str] = []
            dropped = 0
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                else:
                    dropped += 1
            if dropped:
                logger.warning(drop_warning, dropped)
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if isinstance(message, SystemMessage):
            parts.append(f"Instructions:\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


async def write_scratch_file(provider: SandboxProvider, thread_id: str, path: str, content: str | bytes) -> None:
    """Writes content to an arbitrary absolute path via chunked printf execs when needed.

    Base64-encodes `content` to avoid shell-quoting hazards for arbitrary content (quotes,
    backticks, `$`, newlines). Unlike repo_files.write_repo_file, this accepts arbitrary
    absolute paths (no validate_repo_relative_path call), since the file is NOT repo-relative.

    `content` may be `str` (the original, still the common case -- prompt text, .mcp.json) or
    raw `bytes` (task-13: decoded attachment payloads -- screenshots/documents forwarded to
    Claude). Bytes skip the `.encode("utf-8")` step rather than going through it, since arbitrary
    binary data (a PNG's raw bytes, say) is not valid UTF-8 in general and that round-trip would
    corrupt it; base64 itself is encoding-agnostic, so everything below this line is unchanged
    either way. Confirmed empirically (task-13) that this chunked echo/base64-d shell pattern
    round-trips arbitrary binary bytes losslessly through a real `docker exec` (sha256-verified
    before/after against a real container, not just assumed from the text-only cases already in
    production).
    """
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    parent_dir = path.rsplit("/", 1)[0] if "/" in path else ""
    quoted = shlex.quote(path)

    if len(encoded) <= _EXEC_CMD_BUDGET:
        parent_mkdir = f"mkdir -p {shlex.quote(parent_dir)} && " if parent_dir else ""
        commands = [f"{parent_mkdir}echo {encoded} | base64 -d > {quoted}"]
    else:
        # Chunked for the same reason write_repo_file is: WinError 206 on large payloads.
        tmp = shlex.quote(path + ".b64part")
        parent_mkdir = f"mkdir -p {shlex.quote(parent_dir)} && " if parent_dir else ""
        commands = [f"{parent_mkdir}: > {tmp}"]
        commands += [
            f"printf %s {encoded[i : i + _EXEC_CMD_BUDGET]} >> {tmp}"
            for i in range(0, len(encoded), _EXEC_CMD_BUDGET)
        ]
        commands.append(f"base64 -d < {tmp} > {quoted} && rm -f {tmp}")

    for command in commands:
        result = await provider.exec_in_sandbox(thread_id, command)
        if not result.ok:
            raise RuntimeError(f"failed to write {path}: {result.stderr}")


def _build_startup_command(
    command: str,
    prompt_path: str,
    out_path: str,
    err_path: str,
    exit_path: str,
    pid_path: str,
) -> str:
    """Build the backgrounded setsid/nohup startup command string.

    Pure string building, no I/O. Factors out the command-construction logic so both
    run_turn and _demo can call the same code path, ensuring the self-check tests
    the actual production command syntax.
    """
    sh_script = (
        f"{command} < {shlex.quote(prompt_path)} > {shlex.quote(out_path)} 2> {shlex.quote(err_path)}; "
        f"echo $? > {shlex.quote(exit_path)}"
    )
    return f"setsid nohup sh -c {shlex.quote(sh_script)} >/dev/null 2>&1 & echo $! > {shlex.quote(pid_path)}"


def _pid_state_probe_script(pid_path: str) -> str:
    """POSIX sh expression: echoes DEAD if the pid in pid_path is confirmed dead
    (/proc/$pid/stat unreadable, or its 3rd field is Z -- zombie, since this sandbox's PID 1 has
    no reaper). ALIVE otherwise, including when pid_path is empty/unreadable (nothing recorded
    yet -- an instant-early race, deliberately not treated as dead).

    Naive whitespace field-splitting of /proc/pid/stat is generally unsafe (its 2nd field, comm,
    is parenthesized and can itself contain spaces or `)`, shifting field indices) but safe here
    specifically: the pid in pid_path is the setsid'd `sh` process ITSELF (setsid execve's in
    place rather than forking when already a process-group leader, so the pid never changes
    through setsid -> nohup -> sh -c -- see _build_startup_command's own comment on why `;` not
    `&&` precedes the backgrounded setsid, for the same "this pid is the kill target" reasoning),
    never the arbitrary user command running as ITS child. comm is therefore always the literal
    2-byte string "sh", which can't contain a space or `)`.
    """
    quoted = shlex.quote(pid_path)
    return (
        f"p=$(cat {quoted} 2>/dev/null); "
        f'if [ -z "$p" ]; then echo ALIVE; else '
        f's=$(cut -d" " -f3 /proc/"$p"/stat 2>/dev/null); '
        f'if [ -z "$s" ] || [ "$s" = Z ]; then echo DEAD; else echo ALIVE; fi; fi'
    )


class _NarrationStreamer:
    """Incremental JSONL-line consumer backing run_turn's Agent Narration Drawer streaming.

    Extracted as its own class (rather than closures inline inside run_turn) specifically so
    _demo() can drive the one-line-lag flush contract directly with a fake classify_line and
    monkeypatched run_event_store/run_event_stream, without a live sandbox -- see that function's
    own docstring for the full contract this implements.
    """

    def __init__(self, classify_line: Callable[[dict[str, Any]], list[RunEvent]]) -> None:
        self._classify_line = classify_line
        self.byte_offset = 0
        self.streamed_line_count = 0
        self._line_buffer = ""
        self._pending_events: list[RunEvent] = []

    async def flush_pending(self) -> None:
        if not self._pending_events:
            return
        # append_events/emit_live are already fail-soft internally (their own docstrings) -- this
        # try/except is belt-and-suspenders against something else going wrong in this call, not a
        # second copy of their own error handling.
        try:
            appended = await run_event_store.append_events(self._pending_events)
            for appended_event in appended:
                await run_event_stream.emit_live(appended_event)
        except Exception:  # noqa: BLE001 -- narration streaming must never abort the real turn
            logger.warning("failed to flush streamed narration events -- continuing turn", exc_info=True)
        self._pending_events = []

    async def consume_tail(self, new_bytes: bytes) -> None:
        if not new_bytes:
            return
        self.byte_offset += len(new_bytes)
        # ponytail: errors="replace" can mangle a multi-byte UTF-8 character that straddles the
        # _MAX_TAIL_BYTES cap exactly on an incomplete trailing line -- extremely rare (requires
        # hitting that boundary mid-character AND mid-line) and cosmetic (one possibly-garbled
        # display character in narration text, nothing functional -- the unconditional final `cat`
        # of out_path in run_turn is still byte-exact). Upgrade path: track the line buffer as raw
        # bytes instead of decoded text if this ever actually bites.
        text = self._line_buffer + new_bytes.decode("utf-8", errors="replace")
        text_lines = text.split("\n")
        self._line_buffer = text_lines.pop()
        for raw_line in text_lines:
            parsed = parse_jsonl_line(raw_line)
            if parsed is None:
                continue
            self.streamed_line_count += 1
            # One-line-lag: only flush the PREVIOUS line's classified events once a next line has
            # actually arrived, proving the held-back one wasn't the turn's terminal result line.
            await self.flush_pending()
            try:
                self._pending_events = self._classify_line(parsed) or []
            except Exception:  # noqa: BLE001 -- a bad line must never abort the real turn
                logger.warning(
                    "classify_line raised for a streamed JSONL line -- dropping narration for it",
                    exc_info=True,
                )
                self._pending_events = []


async def run_turn(
    provider: SandboxProvider,
    thread_id: str,
    command: str,
    prompt: str,
    scratch_prefix: str,
    timeout_seconds: float,
    *,
    classify_line: Callable[[dict[str, Any]], list[RunEvent]] | None = None,
) -> TurnResult:
    """Executes a backgrounded provider CLI turn in the sandbox.

    The provider's CLI invocation (argv already built by the caller as a single shell-safe
    string via shlex.join) is launched backgrounded and polled to keep the sandbox idle-reaper's
    clock ticking -- multi-minute turns would otherwise time out.

    Args:
        provider: Sandbox provider.
        thread_id: Thread ID.
        command: Shell-safe provider CLI invocation (pre-built via shlex.join).
        prompt: Prompt text to write to scratch file.
        scratch_prefix: Absolute prefix for scratch files (prompt_path = f"{scratch_prefix}").
        timeout_seconds: Timeout for the entire turn.
        classify_line: Agent Narration Drawer feature. When given, turns on incremental narration
            streaming: every complete JSONL line the CLI writes to its output file during the turn
            is parsed (parse_jsonl_line) and handed to this callback, which classifies it into zero
            or more already-persistence-ready RunEvents (each provider chat model supplies its own
            per-line classifier -- see copilot_chat_model._classify_one_event/
            claude_chat_model._classify_one_event). This function owns WHEN a classified event
            reaches run_event_store/run_event_stream (a one-line-lag flush: a line's events are
            held until the NEXT line arrives, proving the held-back one wasn't the turn's terminal
            result line, which is never a narration event); classify_line itself stays pure. None
            (the default) skips the tail-read entirely -- zero exec-call-cadence cost for a caller
            that doesn't want live narration, and the completion-wait loop is byte-for-byte the
            original _ACTIVITY_CHUNK_SECONDS-chunked behavior in that case.

    Returns:
        TurnResult with stdout, stderr, exit_code, and streamed_line_count (0 when classify_line is
        None) -- the number of complete JSONL lines already streamed live, so callers can slice
        their own post-hoc parsed `events` list from that index instead of re-processing everything
        the streaming path already handled.

    Raises:
        TimeoutError: If the turn does not complete within timeout_seconds.
        RuntimeError: If file operations fail.
    """
    prompt_path = scratch_prefix
    pid_path = f"{scratch_prefix}.pid"
    out_path = f"{scratch_prefix}.out"
    err_path = f"{scratch_prefix}.err"
    exit_path = f"{scratch_prefix}.exit"

    streaming = classify_line is not None
    # Scoped to this one run_turn() call only -- nothing here survives past this function
    # returning or raising, and nothing needs to.
    streamer = _NarrationStreamer(classify_line) if classify_line is not None else None

    try:
        # Write prompt to scratch file.
        await write_scratch_file(provider, thread_id, prompt_path, prompt)

        # Launch backgrounded. Use `;` before the backgrounded setsid, NEVER `&&`: with `&&`,
        # `cmd1 && cmd2 &` backgrounds the whole compound as one job, so `$!` would report the
        # wrong PID and a timeout-kill would target the wrong process group.
        startup_command = _build_startup_command(command, prompt_path, out_path, err_path, exit_path, pid_path)
        result = await provider.exec_in_sandbox(thread_id, startup_command)
        if not result.ok:
            raise RuntimeError(f"failed to launch turn: {result.stderr}")

        # Poll for completion.
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                # Timeout: kill the process group, then raise.
                kill_cmd = (
                    f"kill -TERM -$(cat {shlex.quote(pid_path)} 2>/dev/null) 2>/dev/null; "
                    f"kill -KILL -$(cat {shlex.quote(pid_path)} 2>/dev/null) 2>/dev/null; true"
                )
                await provider.exec_in_sandbox(thread_id, kill_cmd)
                if streamer is not None:
                    # A killed turn has no terminal result line -- whatever's pending is genuine,
                    # real, in-progress narration, not a presumed-terminal line to discard. Without
                    # this, a killed/timed-out turn's in-flight narration was silently lost entirely
                    # (the flip side of the same TurnTimeout.partial_stdout comment above, applied
                    # to narration instead of session-id recovery).
                    await streamer.flush_pending()
                # Head only: the init line is the first line, and a 90-minute turn's stdout can
                # be megabytes of tool events nobody needs here.
                partial = await provider.exec_in_sandbox(thread_id, f"head -c 65536 {shlex.quote(out_path)} 2>/dev/null || true")
                raise TurnTimeout(
                    f"turn did not complete within {timeout_seconds} seconds",
                    partial_stdout=partial.stdout if partial.ok else "",
                )

            # Block inside the sandbox until the exit file appears or this chunk elapses,
            # whichever first -- `timeout` bounds the remote wait so this exec always returns on
            # its own (no host-side cancellation, so nothing is left running in the sandbox).
            #
            # NOT `tail --pid=<turn pid>` (what this used to be): this sandbox's PID 1 is a bare
            # `sleep infinity` (no init/reaper), so every backgrounded turn's process becomes an
            # unreaped ZOMBIE the instant it exits rather than disappearing -- confirmed live
            # 2026-09-01, `ps aux` showed dozens of `[sh] <defunct>` entries, one per chunk, going
            # back to session start. `kill -0`/`tail --pid` both see a zombie's PID as still
            # "alive" (the /proc entry persists until something reaps it, which never happens
            # here), so `tail --pid` never returned early -- it silently degraded into "always
            # block the full chunk," turning a turn that finished in under a minute (confirmed:
            # .exit file timestamped ~60s after start) into a 5-minute wait before the host loop
            # ever rechecked. Polling the exit file directly has no such failure mode -- a file's
            # existence doesn't depend on process-reaping semantics -- and the 2s remote sleep
            # interval is still all inside one exec call, so it costs nothing extra host-side.
            chunk = min(remaining, _NARRATION_POLL_SECONDS if streaming else _ACTIVITY_CHUNK_SECONDS)
            probe = _pid_state_probe_script(pid_path)
            # The nested `timeout N sh -c '...'` is a separate process -- shell functions don't
            # cross that boundary -- so the probe body is inlined both places rather than defined
            # once as a function; it's four lines, not worth the indirection.
            inner_script = (
                f"while [ ! -f {shlex.quote(exit_path)} ]; do "
                f'state=$({probe}); [ "$state" = DEAD ] && break; sleep 2; done'
            )
            status_check = (
                f"if [ -f {shlex.quote(exit_path)} ]; then echo DONE; "
                f'else state=$({probe}); if [ "$state" = DEAD ]; then echo DEAD; else echo PENDING; fi; fi'
            )
            if streaming:
                # Folded into the SAME per-iteration exec call as the completion check above --
                # not a second exec call every poll, which would double an already ~40x call-volume
                # increase (_NARRATION_POLL_SECONDS' own comment). The tailed bytes are base64'd
                # BEFORE they cross exec_in_sandbox's own `.strip()` (local_docker._run_docker /
                # azure_aci._run_az): splicing raw bytes into that same `.strip()`-ed stdout would
                # silently corrupt trailing whitespace/newlines and drift byte_offset. `tr -d '\n'`
                # strips base64's own line-wrapping so the payload is exactly one line between the
                # two markers (_split_tail_poll_output's own docstring).
                assert streamer is not None  # streaming implies classify_line was given
                wait_cmd = (
                    f"timeout {int(chunk) + 1} sh -c {shlex.quote(inner_script)} 2>/dev/null; "
                    f"echo {shlex.quote(_TAIL_MARKER_START)}; "
                    f"tail -c +{streamer.byte_offset + 1} {shlex.quote(out_path)} 2>/dev/null "
                    f"| head -c {_MAX_TAIL_BYTES} | base64 | tr -d '\\n'; "
                    f"echo ''; "
                    f"echo {shlex.quote(_TAIL_MARKER_END)}; "
                    f"{status_check}"
                )
            else:
                wait_cmd = f"timeout {int(chunk) + 1} sh -c {shlex.quote(inner_script)} 2>/dev/null; {status_check}"
            check_result = await asyncio.wait_for(
                provider.exec_in_sandbox(thread_id, wait_cmd, timeout_seconds=chunk + 20.0),
                timeout=chunk + 30.0,
            )
            if streaming:
                assert streamer is not None
                stdout_text = check_result.stdout if check_result.ok else ""
                b64_payload, status = _split_tail_poll_output(stdout_text)
                new_bytes = b""
                if b64_payload:
                    try:
                        new_bytes = base64.b64decode(b64_payload)
                    except (binascii.Error, ValueError):
                        new_bytes = b""
                # A read that hit the cap may not have reached the current end of out_path yet --
                # if DONE also fired this same iteration, don't break (and don't discard pending as
                # "presumed terminal") until a follow-up, uncapped read actually catches up. Once
                # exit_path exists, the wrapped `timeout N sh -c '...'` above returns near-instantly
                # on every subsequent iteration (its `while` condition is false immediately), so
                # this costs a few fast extra round trips, never a real wait.
                hit_cap = len(new_bytes) >= _MAX_TAIL_BYTES
                await streamer.consume_tail(new_bytes)
                if status == "DONE" and not hit_cap:
                    break
                if status == "DEAD":
                    await streamer.flush_pending()
                    partial = await provider.exec_in_sandbox(
                        thread_id, f"head -c 65536 {shlex.quote(out_path)} 2>/dev/null || true"
                    )
                    raise TurnCrashed(
                        f"backgrounded turn process (pid file {pid_path}) died without writing "
                        f"{exit_path} -- zombie/gone in /proc, exit code never captured "
                        f"(elapsed={elapsed:.0f}s of {timeout_seconds}s budget)",
                        partial_stdout=partial.stdout if partial.ok else "",
                    )
                continue
            if check_result.ok and "DONE" in check_result.stdout:
                break
            if check_result.ok and "DEAD" in check_result.stdout:
                partial = await provider.exec_in_sandbox(
                    thread_id, f"head -c 65536 {shlex.quote(out_path)} 2>/dev/null || true"
                )
                raise TurnCrashed(
                    f"backgrounded turn process (pid file {pid_path}) died without writing "
                    f"{exit_path} -- zombie/gone in /proc, exit code never captured "
                    f"(elapsed={elapsed:.0f}s of {timeout_seconds}s budget)",
                    partial_stdout=partial.stdout if partial.ok else "",
                )

        # Read results. Long timeout, not the fast-admin default: a 90-minute turn's stdout can
        # be megabytes of tool events (see the head -c 65536 comment above) and this reads it back
        # unbounded/untruncated.
        stdout_result = await provider.exec_in_sandbox(
            thread_id, f"cat {shlex.quote(out_path)} 2>/dev/null || true",
            timeout_seconds=config.SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS,
        )
        stdout = stdout_result.stdout if stdout_result.ok else ""

        stderr_result = await provider.exec_in_sandbox(
            thread_id, f"cat {shlex.quote(err_path)} 2>/dev/null || true",
            timeout_seconds=config.SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS,
        )
        stderr = stderr_result.stdout if stderr_result.ok else ""

        exit_code_result = await provider.exec_in_sandbox(
            thread_id, f"cat {shlex.quote(exit_path)} 2>/dev/null || echo 1",
            timeout_seconds=config.SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS,
        )
        try:
            exit_code = int(exit_code_result.stdout.strip()) if exit_code_result.ok else 1
        except (ValueError, AttributeError):
            exit_code = 1

        if os.environ.get("AIDW_LOG_LLM_IO"):
            # Best-effort, never the reason a turn fails -- same contract as record_toolchain's
            # host-side sink (preflight_nodes.py).
            try:
                log_path = _llm_io_log_path()
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "thread_id": thread_id,
                                "command": command[:200],
                                "exit_code": exit_code,
                                "stdout": stdout,
                            }
                        )
                        + "\n"
                    )
            except OSError:
                logger.warning("could not append to the host-side LLM I/O log", exc_info=True)

        return TurnResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code,
            streamed_line_count=streamer.streamed_line_count if streamer is not None else 0,
        )
    finally:
        # Cleanup (rm -f with glob removes the prefix and all siblings sharing it) now runs on
        # EVERY exit path, not just success -- previously a TimeoutError or a launch RuntimeError
        # returned/raised before this line was ever reached, leaving the prompt/pid/out/err/exit
        # scratch files behind in /tmp/aidw-agent inside the container. Wrapped in its own
        # try/except so a cleanup failure (e.g. the sandbox is already gone) can never replace
        # whatever real exception this function is in the middle of propagating -- same
        # "best-effort, result ignored" contract the success path already had.
        try:
            await provider.exec_in_sandbox(thread_id, f"rm -f {shlex.quote(scratch_prefix)}*")
        except Exception:
            pass


def _demo() -> None:
    """Self-check for command-string construction and chunking boundary math.

    The actual exec/backgrounding path needs a live sandbox and cannot be unit-tested here --
    see copilot_chat_model.py's own demo for the same scoping pattern ("the pure half").
    """
    # Test chunking boundary math: edge cases at chunk boundaries.
    short_encoded = base64.b64encode(b"hello").decode("ascii")
    assert len(short_encoded) < _EXEC_CMD_BUDGET, "short payload overflowed budget"

    long_payload = "x" * (_EXEC_CMD_BUDGET * 2 + 100)
    long_encoded = base64.b64encode(long_payload.encode("utf-8")).decode("ascii")
    assert len(long_encoded) > _EXEC_CMD_BUDGET, "long payload should exceed budget"
    chunk_count = (len(long_encoded) + _EXEC_CMD_BUDGET - 1) // _EXEC_CMD_BUDGET
    assert chunk_count == 3, f"expected 3 chunks, got {chunk_count}"

    # Test command-string construction: call the actual production code path, not a hand-copied
    # duplicate. This ensures a future regression (e.g., someone "fixing" `;` back to `&&`)
    # would fail the self-check, catching the mistake at runtime rather than silently
    # in production.
    scratch_prefix = "/tmp/test-prefix"
    prompt_path = scratch_prefix
    out_path = f"{scratch_prefix}.out"
    err_path = f"{scratch_prefix}.err"
    exit_path = f"{scratch_prefix}.exit"
    pid_path = f"{scratch_prefix}.pid"

    test_command = "echo hello"

    startup_cmd = _build_startup_command(test_command, prompt_path, out_path, err_path, exit_path, pid_path)

    # Verify the command structure via the actual production path.
    assert " & echo $! > " in startup_cmd, "pidfile capture should follow backgrounding"
    assert "setsid nohup sh -c" in startup_cmd, "should use setsid nohup sh -c structure"
    assert ">/dev/null 2>&1 &" in startup_cmd, "should redirect setsid/nohup output before backgrounding"

    # --- Phase E audit C-2: classify_resume, including the REAL killed-turn experiment's outcome ---
    #
    # No resume requested this turn -> nothing to classify, regardless of how the turn went.
    assert classify_resume(None, "any-id", False, "", ()) is None
    assert classify_resume("", "any-id", True, "session not found", ("session not found",)) is None

    # REAL (fix-e3a-report.md): a Claude turn SIGKILLed mid-tool-call, then really `--resume`d --
    # came back with the SAME session_id and is_error=False. This is exactly RESUMED, not a
    # hedge -- the real experiment's own actual result, not a synthetic stand-in for it.
    real_requested = "96d76a68-6f54-47d3-97ac-c75b896b0717"
    assert classify_resume(real_requested, real_requested, False, "resumed-ok", ()) == "resumed"

    # A returned id that DIFFERS from what was requested is a silent-fresh-start suspect, even
    # with is_error=False -- never trust a same-shaped success to mean "continued," only a
    # matching id does.
    assert classify_resume(real_requested, "some-other-session-id", False, "ok", ()) == "unknown"

    # A missing/unparseable returned id on an otherwise-clean turn: still unknown, not resumed --
    # no id match was actually confirmed.
    assert classify_resume(real_requested, None, False, "ok", ()) == "unknown"

    # is_error=True with text matching one of the caller's (inference-labelled) rejection markers
    # -> REJECTED. Synthetic: neither provider has a real captured rejection message yet (module
    # docstring), so this exercises the marker-matching RULE, not a claim about real CLI text.
    assert classify_resume(real_requested, None, True, "Error: no conversation found for that id", (
        "no conversation found",
    )) == "rejected"

    # is_error=True but the text matches none of the markers: conservative default is UNKNOWN, not
    # a guessed REJECTED -- an unrelated content-level error must not be misread as a resume
    # rejection just because a resume happened to be requested that turn.
    assert classify_resume(real_requested, None, True, "Error: the model output was truncated", (
        "no conversation found",
    )) == "unknown"

    # TurnTimeout is a TimeoutError (existing `except TimeoutError` handlers keep working) that
    # carries the killed turn's stdout head for session-id recovery.
    timeout_exc = TurnTimeout("turn did not complete within 5 seconds", partial_stdout='{"type":"system","subtype":"init"}')
    assert isinstance(timeout_exc, TimeoutError) and "init" in timeout_exc.partial_stdout
    assert TurnTimeout("x").partial_stdout == ""

    # TurnCrashed subclasses TurnTimeout (itself a TimeoutError) -- both chat models' existing
    # `except TimeoutError` handlers must catch it with zero call-site changes.
    crashed_exc = TurnCrashed("died without writing exit_path", partial_stdout='{"type":"system"}')
    assert isinstance(crashed_exc, TurnTimeout) and isinstance(crashed_exc, TimeoutError)
    assert crashed_exc.partial_stdout == '{"type":"system"}'

    # _pid_state_probe_script: pure string construction, no live /proc reads here -- checks the
    # shell expression shape (echoes ALIVE for an empty pid file, DEAD for a Z state or a missing
    # /proc entry), not real process state.
    probe = _pid_state_probe_script("/tmp/aidw-agent/test.pid")
    assert "/tmp/aidw-agent/test.pid" in probe
    assert 'echo ALIVE' in probe and 'echo DEAD' in probe
    assert '"$s" = Z' in probe, "must check the zombie state character"

    # --- Agent Narration Drawer feature: parse_jsonl_line ---
    assert parse_jsonl_line("") is None
    assert parse_jsonl_line("   ") is None
    assert parse_jsonl_line("not json") is None
    assert parse_jsonl_line("[1, 2, 3]") is None, "a JSON array is not object-shaped"
    assert parse_jsonl_line('{"a": 1}') == {"a": 1}
    assert parse_jsonl_line('  {"a": 1}  ') == {"a": 1}, "surrounding whitespace must be stripped"

    # --- _split_tail_poll_output: marker-delimited payload/status extraction ---
    combined = f"{_TAIL_MARKER_START}\nSGVsbG8=\n{_TAIL_MARKER_END}\nDONE"
    payload, status = _split_tail_poll_output(combined)
    assert payload == "SGVsbG8=" and status == "DONE", (payload, status)

    empty_payload_combined = f"{_TAIL_MARKER_START}\n\n{_TAIL_MARKER_END}\nPENDING"
    payload, status = _split_tail_poll_output(empty_payload_combined)
    assert payload == "" and status == "PENDING", (payload, status)

    # A base64 blob that happens to spell DONE/DEAD must never be mistaken for the status line --
    # only the LAST non-empty line (past the END marker) counts.
    tricky_combined = f"{_TAIL_MARKER_START}\nDEADBEEFDONE==\n{_TAIL_MARKER_END}\nPENDING"
    payload, status = _split_tail_poll_output(tricky_combined)
    assert payload == "DEADBEEFDONE==" and status == "PENDING", (
        f"a payload containing DONE/DEAD as substrings must not be mistaken for the status: {(payload, status)}"
    )

    # Missing markers entirely (e.g. the tail-read commands themselves failed) falls back to
    # treating the whole output as status text -- same leniency the old substring search had.
    payload, status = _split_tail_poll_output("DONE")
    assert payload == "" and status == "DONE"

    # --- _NarrationStreamer: the one-line-lag flush contract ---
    #
    # Fake persistence: records what was "persisted"/"emitted" without a real DB or graph run, same
    # monkeypatching technique run_event_store._demo()/copilot_chat_model._demo() already use
    # ("plain global reassignment").
    flushed_batches: list[list[RunEvent]] = []
    emitted: list[RunEvent] = []

    async def _fake_append_events(events: list[RunEvent]) -> list[RunEvent]:
        flushed_batches.append(list(events))
        return events

    async def _fake_emit_live(event: RunEvent) -> None:
        emitted.append(event)

    real_append_events = run_event_store.append_events
    real_emit_live = run_event_stream.emit_live
    run_event_store.append_events = _fake_append_events
    run_event_stream.emit_live = _fake_emit_live

    def _fake_classify(parsed: dict[str, Any]) -> list[RunEvent]:
        return [RunEvent(run_id="r", session_id="s", type=RunEventType.REASONING, summary=parsed["marker"])]

    try:
        streamer = _NarrationStreamer(_fake_classify)

        async def _drive() -> None:
            # Two complete lines arrive in the SAME tail read: line1 flushes the instant line2 is
            # recognized (a next line arriving is what proves the held-back one wasn't terminal --
            # batch boundaries don't matter, only line-arrival order does), leaving line2 pending.
            await streamer.consume_tail(b'{"marker": "line1"}\n{"marker": "line2"}\n')
            assert [e.summary for e in emitted] == ["line1"], (
                f"line1 should flush as soon as line2 proved it wasn't terminal, got {[e.summary for e in emitted]}"
            )
            assert streamer.streamed_line_count == 2

            # A third line, in a LATER tail read, flushes line2 (the previously held-back line).
            await streamer.consume_tail(b'{"marker": "line3"}\n')
            assert [e.summary for e in emitted] == ["line1", "line2"], (
                f"expected line1 and line2 flushed, line3 still pending, got {[e.summary for e in emitted]}"
            )

        asyncio.run(_drive())

        # Simulated DONE: run_turn simply never calls flush_pending again on this path -- line3
        # stays pending forever, discarded as the presumed terminal line (nothing new appears).
        assert [e.summary for e in emitted] == ["line1", "line2"], (
            "DONE must discard the final pending line, not flush it"
        )

        # Simulated DEAD/timeout instead: flush_pending IS called explicitly (run_turn's own
        # timeout/DEAD branches) -- the pending line3 must flush, not be discarded, since a killed
        # turn has no terminal result line at all.
        asyncio.run(streamer.flush_pending())
        assert [e.summary for e in emitted] == ["line1", "line2", "line3"], (
            f"DEAD/timeout must flush the pending line too, got {[e.summary for e in emitted]}"
        )
        assert len(flushed_batches) == 3 and all(len(batch) == 1 for batch in flushed_batches)

        # A classify_line that raises must not crash the streamer -- the bad line's narration is
        # dropped, but streamed_line_count still advances (matches the legacy parser's own
        # per-line-independent skip contract) and the turn keeps going.
        def _raising_classify(parsed: dict[str, Any]) -> list[RunEvent]:
            raise ValueError("boom")

        raising_streamer = _NarrationStreamer(_raising_classify)
        asyncio.run(raising_streamer.consume_tail(b'{"marker": "x"}\n{"marker": "y"}\n'))
        assert raising_streamer.streamed_line_count == 2, "a raising classify_line must not stop line counting"
    finally:
        run_event_store.append_events = real_append_events
        run_event_stream.emit_live = real_emit_live

    print("cli_agent_exec self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.cli_agent_exec` loads this
    # file as "__main__", so a direct `_demo()` call would import this module a second time as a
    # non-package import. Re-dispatching through `from src.cli_agent_exec import` ensures there is
    # only one copy of this module in sys.modules, matching how production imports it (graph.py ->
    # provider tasks -> this module). This convention is unconditional across this codebase.
    from src.cli_agent_exec import _demo as _packaged_demo

    _packaged_demo()
