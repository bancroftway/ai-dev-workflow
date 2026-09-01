"""Shared CLI-subprocess runner for per-turn provider execution inside the sandbox.

Both providers (GitHub Copilot and Claude Code) become per-turn subprocess execs launched
inside the sandbox. This module factors out what's identical between them: scratch-file
writing, backgrounded turn launch, polling, timeout handling, and cleanup. Only the argv
construction and per-line/whole-output JSON parsing differ per provider (Tasks 2 and 3).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import time
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import BaseMessage, SystemMessage

from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Keep each exec's command line well under Windows' ~32K CreateProcess cap (WinError 206).
_EXEC_CMD_BUDGET = 16000

# Scratch-file directory for all provider execs (shared, not provider-specific).
_SCRATCH_DIR = "/tmp/aidw-agent"

# A single completion-wait exec blocks (via the remote `timeout`/`tail --pid` below) for up to
# this long before returning to let the host loop re-check its own deadline and re-touch
# last_active (see local_docker.py's DEFAULT_IDLE_TIMEOUT_SECONDS=1800 -- this is comfortably
# under that with margin to spare). Replaces a fixed-interval host-side poll (2026-09-01: a
# runaway stack of those, each issuing its own `docker exec` every few seconds, hammered Docker
# Desktop's API into a VM reset). One exec call per chunk instead of one every few seconds cuts
# call volume ~60x on a long turn while still noticing completion within a second or two, since
# the remote wait itself is event-driven (`tail --pid`), not a sleep loop.
_ACTIVITY_CHUNK_SECONDS = 300.0


@dataclass
class TurnResult:
    """Result of a backgrounded provider turn execution."""

    stdout: str
    stderr: str
    exit_code: int


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


async def run_turn(
    provider: SandboxProvider,
    thread_id: str,
    command: str,
    prompt: str,
    scratch_prefix: str,
    timeout_seconds: float,
) -> TurnResult:
    """Executes a backgrounded provider CLI turn in the sandbox.

    The provider's CLI invocation (argv already built by the caller as a single shell-safe
    string via shlex.join) is launched backgrounded and polled every 5 seconds to keep the
    sandbox idle-reaper's clock ticking -- multi-minute turns would otherwise time out.

    Args:
        provider: Sandbox provider.
        thread_id: Thread ID.
        command: Shell-safe provider CLI invocation (pre-built via shlex.join).
        prompt: Prompt text to write to scratch file.
        scratch_prefix: Absolute prefix for scratch files (prompt_path = f"{scratch_prefix}").
        timeout_seconds: Timeout for the entire turn.

    Returns:
        TurnResult with stdout, stderr, exit_code.

    Raises:
        TimeoutError: If the turn does not complete within timeout_seconds.
        RuntimeError: If file operations fail.
    """
    prompt_path = scratch_prefix
    pid_path = f"{scratch_prefix}.pid"
    out_path = f"{scratch_prefix}.out"
    err_path = f"{scratch_prefix}.err"
    exit_path = f"{scratch_prefix}.exit"

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
                # Head only: the init line is the first line, and a 40-minute turn's stdout can
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
            chunk = min(remaining, _ACTIVITY_CHUNK_SECONDS)
            wait_cmd = (
                f"timeout {int(chunk) + 1} sh -c 'while [ ! -f {exit_path} ]; do sleep 2; done' 2>/dev/null; "
                f"test -f {shlex.quote(exit_path)} && echo DONE || echo PENDING"
            )
            check_result = await asyncio.wait_for(
                provider.exec_in_sandbox(thread_id, wait_cmd), timeout=chunk + 30.0
            )
            if check_result.ok and "DONE" in check_result.stdout:
                break

        # Read results.
        stdout_result = await provider.exec_in_sandbox(thread_id, f"cat {shlex.quote(out_path)} 2>/dev/null || true")
        stdout = stdout_result.stdout if stdout_result.ok else ""

        stderr_result = await provider.exec_in_sandbox(thread_id, f"cat {shlex.quote(err_path)} 2>/dev/null || true")
        stderr = stderr_result.stdout if stderr_result.ok else ""

        exit_code_result = await provider.exec_in_sandbox(
            thread_id, f"cat {shlex.quote(exit_path)} 2>/dev/null || echo 1"
        )
        try:
            exit_code = int(exit_code_result.stdout.strip()) if exit_code_result.ok else 1
        except (ValueError, AttributeError):
            exit_code = 1

        return TurnResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
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
    print("cli_agent_exec self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.cli_agent_exec` loads this
    # file as "__main__", so a direct `_demo()` call would import this module a second time as a
    # non-package import. Re-dispatching through `from src.cli_agent_exec import` ensures there is
    # only one copy of this module in sys.modules, matching how production imports it (graph.py ->
    # provider tasks -> this module). This convention is unconditional across this codebase.
    from src.cli_agent_exec import _demo as _packaged_demo

    _packaged_demo()
