"""Generalized file I/O against a sandbox's own repo clone, plus the workflow action ledger.

Generalizes workflow_persistence.py's private _read_file/_write_file (which only ever operated
relative to .ai-dev-workflow/) to an arbitrary repo-root-relative path, so pipeline stages beyond
the original two (AGENTS.md, Directory.Build.props, spec/ledger.json, etc.) have one shared,
shell-safe read/write primitive instead of each stage reinventing the exec_in_sandbox pattern.

Also owns the workflow action ledger (.ai-dev-workflow/ledger.jsonl) -- a chronological log of
every node's activity, fresh per session (reset once at the true entry point of a from-scratch
run, per graph.py's own module docstring on what a "run" is -- never resumed across gate-approval
continuations, never preserved across a later fresh run on the same thread; an explicit, accepted
tradeoff, not an oversight). Distinct from spec/ledger.json's stable US-####/AC-####.# ID registry
(agent/src/spec_ledger.py), which is cumulative across the repo's whole lifetime -- the naming
collision between the two is unfortunate but intentional, matching the plan's own terminology.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
from typing import Any

from .sandbox.provider import SandboxProvider

LEDGER_PATH = ".ai-dev-workflow/ledger.jsonl"

# Repo-relative paths only: no leading "/", no ".." traversal, and a conservative character
# allowlist -- closes a real command-injection gap (found by automated security review) where a
# model-reported string (e.g. TechStack.dotnet_solution_root, a PlanDiagram's own `name`) could
# otherwise flow unquoted into a shell command built by f-string interpolation below.
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def validate_repo_relative_path(path: str) -> str:
    if (
        not path
        or path.startswith("/")
        or any(segment == ".." for segment in path.split("/"))
        or not _SAFE_PATH_RE.match(path)
    ):
        raise ValueError(f"unsafe or invalid repo-relative path: {path!r}")
    return path


async def read_repo_file(provider: SandboxProvider, thread_id: str, path: str) -> str | None:
    """Returns the file's content, or None if it doesn't exist (or can't be read)."""
    validate_repo_relative_path(path)
    result = await provider.exec_in_sandbox(thread_id, f"cat {shlex.quote(path)} 2>/dev/null")
    if not result.ok:
        return None
    return result.stdout


async def write_repo_file(provider: SandboxProvider, thread_id: str, path: str, content: str) -> None:
    """Writes content to `path` (repo-root-relative), creating parent directories as needed.

    base64 round-trip avoids shell-quoting hazards entirely for arbitrary *content* (quotes,
    backticks, `$`, newlines) -- but `path` itself was still being interpolated unquoted into the
    shell command below (a real command-injection gap when `path` is derived from model output,
    e.g. a TechStack.dotnet_solution_root or a PlanDiagram's own `name`), so it's now validated and
    shell-quoted too, same as read_repo_file.
    """
    validate_repo_relative_path(path)
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent_dir = path.rsplit("/", 1)[0] if "/" in path else "."
    command = f"mkdir -p {shlex.quote(parent_dir)} && echo {encoded} | base64 -d > {shlex.quote(path)}"
    result = await provider.exec_in_sandbox(thread_id, command)
    if not result.ok:
        raise RuntimeError(f"failed to write {path}: {result.stderr}")


async def reset_ledger(provider: SandboxProvider, thread_id: str) -> None:
    """Truncates (or creates) the workflow action ledger -- called once, by the true entry-point
    node of a from-scratch run (scaffold_node), never by a gate-approval resume."""
    parent_dir = LEDGER_PATH.rsplit("/", 1)[0]
    command = f"mkdir -p {shlex.quote(parent_dir)} && : > {shlex.quote(LEDGER_PATH)}"
    result = await provider.exec_in_sandbox(thread_id, command)
    if not result.ok:
        raise RuntimeError(f"failed to reset {LEDGER_PATH}: {result.stderr}")


async def append_ledger_entry(provider: SandboxProvider, thread_id: str, entry: dict[str, Any]) -> None:
    """Appends one JSON-line entry to the workflow action ledger.

    Every node in the pipeline calls this at its natural completion point -- deterministic tool
    nodes log what they ran, LLM nodes log what they produced, gates log how they resolved. Uses
    `>>` (append), not write_repo_file's `>` (overwrite), and the same base64 round-trip for
    shell safety. A missing ledger (e.g. this fires before scaffold_node's first reset, on an old
    thread predating the ledger's existence) is tolerated -- `mkdir -p` + append-create rather
    than requiring reset_ledger to have already run.
    """
    payload = {"timestamp": time.time(), **entry}
    line = json.dumps(payload, default=str) + "\n"
    encoded = base64.b64encode(line.encode("utf-8")).decode("ascii")
    parent_dir = LEDGER_PATH.rsplit("/", 1)[0]
    command = f"mkdir -p {shlex.quote(parent_dir)} && echo {encoded} | base64 -d >> {shlex.quote(LEDGER_PATH)}"
    result = await provider.exec_in_sandbox(thread_id, command)
    if not result.ok:
        raise RuntimeError(f"failed to append to {LEDGER_PATH}: {result.stderr}")
