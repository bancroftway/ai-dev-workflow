"""A compressed codebase map (repomix: signatures, not bodies) for draft turns that would otherwise
start cold -- every verify lap opens a fresh CLI session and re-explores the repo from scratch.

Written to a file in the sandbox and referenced from the prompt (codebase_map_segment.md), never
inlined: a structured retry resends the whole prompt, and Copilot passes the prompt as a single
argument capped at 128 KiB, so an inlined map would be paid again on every retry and could break the
turn outright.
"""

from __future__ import annotations

import logging
import shlex
import time
from collections.abc import Iterable

from langchain_core.messages import HumanMessage

from . import config, repo_files
from .prompt_loader import load_prompt, render_prompt
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# agent-work/ is gitignored in every target repo (git_ops) and already the scanners' output dir.
MAP_PATH = "agent-work/codebase-map.md"
STAMP_PATH = "agent-work/codebase-map.stamp"  # "<run_id>:<stage_key>" the map was generated for
FALLBACK_COMMAND_PREFIX = "{ echo '# Codebase map: file list only"

CODEBASE_MAP_SEGMENT = load_prompt("codebase_map_segment")


def build_map_command(include: Iterable[str], ignore: Iterable[str], timeout_seconds: int) -> str:
    """The repomix run, killed in-container at `timeout_seconds` (a host-side timeout alone leaves
    the process running). Arguments are shell-quoted so the command survives Azure ACI's own
    `sh -c "..."` wrapping. Output order is fixed (--no-git-sort-by-changes), so an unchanged repo
    gives the same map. Pure."""
    include_csv = ",".join(include)
    return (
        f"mkdir -p agent-work && timeout {timeout_seconds} repomix --compress --style markdown "
        f"--no-file-summary --no-git-sort-by-changes --quiet "
        + (f"--include {shlex.quote(include_csv)} " if include_csv else "")
        + f"--ignore {shlex.quote(','.join(ignore))} --output {MAP_PATH}"
    )


def _fallback_command(max_chars: int) -> str:
    """A map too big to be useful becomes the repo's file list, cut at `max_chars`."""
    return (
        f"{FALLBACK_COMMAND_PREFIX} (the compressed map was over {max_chars} characters)'; echo; "
        f"git ls-files --cached --others --exclude-standard; }} | head -c {max_chars} > {MAP_PATH}"
    )


async def ensure_codebase_map(provider: SandboxProvider, thread_id: str, stage_key: str, run_id: str) -> str | None:
    """MAP_PATH once a usable map exists for this (run, stage), else None. Generated once per stage
    per run -- every retry and verify lap of the same stage reuses it, since a draft's prompt is
    rebuilt each time -- and regenerated for the next stage, because the code has changed since.
    Any failure (repomix missing, timeout, empty output) returns None with a warning: the map is a
    head start, never a reason to block a stage."""
    stamp = f"{run_id}:{stage_key}"
    if ((await repo_files.read_repo_file(provider, thread_id, STAMP_PATH)) or "").strip() == stamp:
        return MAP_PATH
    timeout = config.CODEBASE_MAP_TIMEOUT_SECONDS
    started = time.monotonic()
    result = await provider.exec_in_sandbox(
        thread_id,
        build_map_command(config.CODEBASE_MAP_INCLUDE, config.CODEBASE_MAP_IGNORE, timeout),
        timeout_seconds=timeout + config.SANDBOX_DOCKER_TIMEOUT_SECONDS,
    )
    if not result.ok:
        logger.warning("codebase map: repomix failed for %s (thread %s) -- drafting without a map", stage_key, thread_id)
        return None
    size_result = await provider.exec_in_sandbox(thread_id, f"wc -c < {MAP_PATH}")
    try:
        size = int((size_result.stdout or "0").strip() or 0) if size_result.ok else 0
    except ValueError:
        size = 0
    if size == 0:
        logger.warning("codebase map: empty output for %s (thread %s) -- drafting without a map", stage_key, thread_id)
        return None
    max_chars = config.CODEBASE_MAP_MAX_CHARS
    if size > max_chars:
        await provider.exec_in_sandbox(thread_id, _fallback_command(max_chars))
    await repo_files.write_repo_file(provider, thread_id, STAMP_PATH, stamp)
    logger.info(
        "codebase map for %s: %d chars%s in %.1fs (thread %s)", stage_key, size,
        " -> file list (over cap)" if size > max_chars else "", time.monotonic() - started, thread_id,
    )
    return MAP_PATH


async def codebase_map_message(
    provider: SandboxProvider, thread_id: str, stage_key: str, run_id: str
) -> HumanMessage | None:
    """The draft prompt's pointer to the map, for a stage in config.CODEBASE_MAP_STAGES with a usable
    map; None otherwise."""
    if stage_key not in config.CODEBASE_MAP_STAGES:
        return None
    path = await ensure_codebase_map(provider, thread_id, stage_key, run_id)
    return HumanMessage(content=render_prompt(CODEBASE_MAP_SEGMENT, path=path)) if path else None


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.codebase_map`
    """Self-check: command shape, once-per-(run, stage) reuse, size cap fallback, never blocking."""
    import asyncio
    import shlex
    from dataclasses import dataclass
    from unittest import mock

    from . import config, repo_files

    cmd = build_map_command(("**/*.ts", "**/*.py"), (".ai-dev-workflow/**", "agent-work/**"), 90)
    assert cmd.startswith("mkdir -p agent-work && timeout 90 repomix --compress --style markdown"), cmd
    assert "--no-git-sort-by-changes" in cmd and f"--output {MAP_PATH}" in cmd
    # Quoted for a POSIX shell, so the command survives Azure ACI's own `sh -c "..."` wrapping too.
    assert f"--include {shlex.quote('**/*.ts,**/*.py')}" in cmd and f"--ignore {shlex.quote('.ai-dev-workflow/**,agent-work/**')}" in cmd
    assert "--include" not in build_map_command((), ("x/**",), 90), "no include list -- repomix's own default"

    @dataclass
    class _Result:
        ok: bool
        stdout: str = ""

    class _FakeProvider:
        def __init__(self, map_chars: int = 5000, fail: bool = False) -> None:
            self.commands: list[str] = []
            self.map_chars, self.fail = map_chars, fail

        async def exec_in_sandbox(self, _thread_id: str, command: str, *, timeout_seconds: float | None = None):  # noqa: ANN201
            self.commands.append(command)
            if "repomix" in command:
                return _Result(not self.fail)
            if command.startswith("wc -c"):
                return _Result(True, str(self.map_chars))
            return _Result(True)

    store: dict[str, str] = {}

    async def _read(_p, _t, path):  # noqa: ANN001, ANN202
        return store.get(path)

    async def _write(_p, _t, path, content):  # noqa: ANN001, ANN202
        store[path] = content

    real = (repo_files.read_repo_file, repo_files.write_repo_file)
    repo_files.read_repo_file, repo_files.write_repo_file = _read, _write
    try:
        provider = _FakeProvider()
        assert asyncio.run(ensure_codebase_map(provider, "t", "plan", "run-1")) == MAP_PATH  # type: ignore[arg-type]
        assert any("repomix" in c for c in provider.commands) and store[STAMP_PATH] == "run-1:plan"
        provider.commands.clear()
        assert asyncio.run(ensure_codebase_map(provider, "t", "plan", "run-1")) == MAP_PATH  # type: ignore[arg-type]
        assert provider.commands == [], "a retry or later lap of the same stage reuses the map"
        asyncio.run(ensure_codebase_map(provider, "t", "minimal-code-to-green", "run-1"))  # type: ignore[arg-type]
        assert any("repomix" in c for c in provider.commands), "the next stage regenerates: code changed since"

        store.clear()
        failing = _FakeProvider(fail=True)
        assert asyncio.run(ensure_codebase_map(failing, "t", "plan", "run-1")) is None  # type: ignore[arg-type]
        assert STAMP_PATH not in store, "a failure never blocks the stage and is retried next time"

        store.clear()
        empty = _FakeProvider(map_chars=0)
        assert asyncio.run(ensure_codebase_map(empty, "t", "plan", "run-1")) is None  # type: ignore[arg-type]

        store.clear()
        with mock.patch.object(config, "CODEBASE_MAP_MAX_CHARS", 1000):
            huge = _FakeProvider(map_chars=50_000)
            assert asyncio.run(ensure_codebase_map(huge, "t", "plan", "run-1")) == MAP_PATH  # type: ignore[arg-type]
        assert any(c.startswith(FALLBACK_COMMAND_PREFIX) for c in huge.commands), (
            "over the cap: a file-list map, never a map cut mid-file"
        )

        store.clear()
        with mock.patch.object(config, "CODEBASE_MAP_STAGES", frozenset({"plan"})):
            message = asyncio.run(codebase_map_message(_FakeProvider(), "t", "plan", "run-1"))  # type: ignore[arg-type]
            assert message is not None and MAP_PATH in str(message.content)
            assert asyncio.run(codebase_map_message(_FakeProvider(), "t", "ac-to-tests", "run-1")) is None, (  # type: ignore[arg-type]
                "only the configured stages get a map"
            )
    finally:
        repo_files.read_repo_file, repo_files.write_repo_file = real

    print("codebase_map self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
