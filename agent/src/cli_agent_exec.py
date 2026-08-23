"""Shared CLI-subprocess runner for per-turn provider execution inside the sandbox.

Both providers (GitHub Copilot and Claude Code) become per-turn subprocess execs launched
inside the sandbox. This module factors out what's identical between them: scratch-file
writing, backgrounded turn launch, polling, timeout handling, and cleanup. Only the argv
construction and per-line/whole-output JSON parsing differ per provider (Tasks 2 and 3).
"""

from __future__ import annotations

import asyncio
import base64
import shlex
import time
from dataclasses import dataclass

from .sandbox.provider import SandboxProvider

# Keep each exec's command line well under Windows' ~32K CreateProcess cap (WinError 206).
_EXEC_CMD_BUDGET = 16000

# Scratch-file directory for all provider execs (shared, not provider-specific).
_SCRATCH_DIR = "/tmp/aidw-agent"

# Poll interval for backgrounded process completion.
_POLL_INTERVAL_SECONDS = 5.0


@dataclass
class TurnResult:
    """Result of a backgrounded provider turn execution."""

    stdout: str
    stderr: str
    exit_code: int


async def write_scratch_file(provider: SandboxProvider, thread_id: str, path: str, content: str) -> None:
    """Writes content to an arbitrary absolute path via chunked printf execs when needed.

    Base64-encodes `content` to avoid shell-quoting hazards for arbitrary content (quotes,
    backticks, `$`, newlines). Unlike repo_files.write_repo_file, this accepts arbitrary
    absolute paths (no validate_repo_relative_path call), since the file is NOT repo-relative.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
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
            if elapsed > timeout_seconds:
                # Timeout: kill the process group, then raise.
                kill_cmd = (
                    f"kill -TERM -$(cat {shlex.quote(pid_path)} 2>/dev/null) 2>/dev/null; "
                    f"kill -KILL -$(cat {shlex.quote(pid_path)} 2>/dev/null) 2>/dev/null; true"
                )
                await provider.exec_in_sandbox(thread_id, kill_cmd)
                raise TimeoutError(f"turn did not complete within {timeout_seconds} seconds")

            # Check if exit file exists.
            check_cmd = f"test -f {shlex.quote(exit_path)} && echo DONE || echo PENDING"
            check_result = await provider.exec_in_sandbox(thread_id, check_cmd)
            if check_result.ok and "DONE" in check_result.stdout:
                break

            # Sleep before next poll.
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

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

    print("cli_agent_exec self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.cli_agent_exec` loads this
    # file as "__main__", so a direct `_demo()` call would import this module a second time as a
    # non-package import. Re-dispatching through `from src.cli_agent_exec import` ensures there is
    # only one copy of this module in sys.modules, matching how production imports it (graph.py ->
    # provider tasks -> this module). This convention is unconditional across this codebase.
    from src.cli_agent_exec import _demo as _packaged_demo

    _packaged_demo()
