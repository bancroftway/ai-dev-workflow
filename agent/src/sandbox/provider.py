"""SandboxProvider interface: the seam between the agent and per-session sandboxes.

Both copilot_chat_model.py and claude_chat_model.py drive their sandbox purely through one-shot
`exec` calls (docker exec / az container exec -- see cli_agent_exec.py) -- there is no persistent
server and no long-lived connection between the agent process and the sandbox for either provider.
Copilot's own JSON-RPC/TCP session mechanism (RuntimeConnection.for_uri, a real `copilot --server`
process, and the connect handshake this module used to perform) was fully retired by Task 3's
CLI-exec rewrite; nothing on either sandbox backend publishes or listens on a port anymore.
wait_for_cli_ready() below is the one readiness check every SandboxProvider.provision() now polls
before returning: exec a version-check command until the sandbox's CLI tooling actually responds,
rather than trusting the container's "running" state (which flips true well before
bootstrap.sh/toolchain setup has finished).
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

_READY_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class SandboxSession:
    """Bookkeeping a SandboxProvider hands back after provision().

    session_id/host are real and load-bearing (exec_in_sandbox's dispatch key, and diagnostic
    logging). port/connection_token are inert leftovers of the retired TCP/for_uri connection
    scheme described in this module's own docstring -- always a dummy/empty value now, kept only
    because removing them ripples into copilot_chat_model.py/claude_chat_model.py's own
    `sandbox: SandboxSession | None` field.
    """

    session_id: str
    host: str
    port: int
    connection_token: str


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SandboxProvider(abc.ABC):
    """Provisions, tracks, and tears down per-session sandboxes.

    One sandbox per session_id (which callers derive from (owner, repo, branch, user) per the
    architecture plan's Section A) -- calling provision() again for a session_id that already
    has a live sandbox returns the existing one rather than starting a second.
    """

    @abc.abstractmethod
    async def provision(
        self,
        *,
        session_id: str,
        repo_clone_url: str,
        branch: str,
        work_branch: str,
        git_user_token: str,
        runtime_auth_token: str,
        provider: str,
        runtime_auth_kind: str | None = None,
        image: str | None = None,
        scaffold_new_repo: bool = False,
        project_name: str | None = None,
    ) -> SandboxSession:
        """Start (or reuse) the sandbox for session_id and return how to reach it.

        repo_clone_url/branch/git_user_token are used once, inside the sandbox, for the single
        git clone -- never written to a long-lived env var visible to arbitrary child processes
        (plan Section C.4's ordering guarantee). work_branch is this session's own unique git
        branch (agent/src/branch_naming.py, computed once at session creation and never
        recomputed here) -- passed straight through as the sandbox's WORK_BRANCH env var.

        provider ("copilot"/"claude", Phase E audit I-3): which CLI/credential shape to bake into
        this container -- the caller's own already-resolved choice, not something this method
        re-derives. sessions_api.provision_session resolves it as "this session's own stored
        provider (dbo.sessions), or the live org setting if there is no prior row" before ever
        calling here. `graph.py`'s `intake_node` resolves `GraphState.provider` the SAME way
        (state -> dbo.sessions.provider -> live, Important-1 follow-up review) -- so a container
        being reprovisioned after an idle reap OR after a full backend restart (which wipes
        LangGraph's in-memory checkpoint entirely, per GraphState.provider's own comment) is built
        for whichever provider `dbo.sessions.provider` durably records, and dispatch resolves to
        that exact same value independently -- the two sides agree because they consult the SAME
        durable row, not because either one is reading the other's live state. A genuinely
        brand-new session has no prior row to prefer, so each side's own live fallback is what
        supplies this value in that case -- exactly the "provisioning a new session is the moment a
        live change should take effect" behavior this method used to implement itself, before I-3
        found that doing it here (rather than once, at the caller) missed the reprovision case
        entirely.

        scaffold_new_repo (Part 3 plan, Ruling 6) is True only for the "+ New Project" provision
        call that just created repo_clone_url's own (empty) GitHub repo via repo_scaffold.
        create_repo -- entrypoint.sh reads this as SCAFFOLD_NEW_REPO to `git init` + push an
        initial commit instead of cloning. False (the default) for every ordinary Connect-
        Repository/`/select` provision, exactly as before this parameter existed. project_name is
        required (by entrypoint.sh, not this signature) whenever scaffold_new_repo is True --
        it names the README.md the initial commit writes -- and must stay None otherwise, so a
        caller never sets SCAFFOLD_NEW_REPO on an ordinary provision by accident.
        runtime_auth_token is the active provider's own secret -- the shared Copilot PAT, an
        Anthropic API key, an Anthropic subscription OAuth token, or an admin's Settings-UI-saved
        credential (org_credential_vault.py, Part 4) -- resolved by
        chat_model.get_runtime_auth_token(provider=provider) for this SAME `provider` value
        (Phase E audit I-3: no longer a second, independent call to chat_model.get_provider() that
        could silently disagree with the `provider` argument above) -- written into the sandbox as
        COPILOT_GITHUB_TOKEN, ANTHROPIC_API_KEY, or
        CLAUDE_CODE_OAUTH_TOKEN. runtime_auth_kind ("api_key" | "oauth" | None, the second half of
        get_runtime_auth_token()'s return) picks WHICH of the two Claude-only names gets the real
        value when provider == "claude"; ignored (no such choice exists) when provider ==
        "copilot". Phase E audit finding C-1: exactly one of ANTHROPIC_API_KEY /
        CLAUDE_CODE_OAUTH_TOKEN is ever set on the container -- the OTHER one is omitted from the
        container's env entirely, not set to "". A real-plus-empty pair was the original design
        (matching COPILOT_GITHUB_TOKEN vs. the Anthropic side, still real-plus-empty across THAT
        provider dimension below) but the Spec's own precedence warning -- "ANTHROPIC_API_KEY
        always wins if both are set" -- doesn't cite whether "set" means non-empty or merely
        present, so an empty ANTHROPIC_API_KEY could in principle still shadow a real
        CLAUDE_CODE_OAUTH_TOKEN; omitting it entirely removes the question rather than betting on
        an unverified reading of "set". (Was documented here
        as COPILOT_SDK_AUTH_TOKEN, the correct name only for the old, fully-retired SDK-based
        `copilot --server` process -- task-12-report.md BUG B / task-12b traced this docstring as
        the design doc for what turned out to be a real defect in local_docker.py/azure_aci.py's
        own env-var name. Fixed once to plain GITHUB_TOKEN, per the real Copilot CLI's own
        documented env vars -- then corrected again, same task's fix-round-1, to
        COPILOT_GITHUB_TOKEN specifically: `gh`, git credential helpers, and any repo-supplied
        script all read a plain GITHUB_TOKEN ambiently with zero extra config, which would silently
        hand the shared fleet PAT to arbitrary repo-supplied tooling running under --no-ask-user.
        COPILOT_GITHUB_TOKEN authenticates the same CLI identically -- it is one of the same three
        documented names -- without that ambient exposure.)
        """

    @abc.abstractmethod
    async def terminate(self, session_id: str) -> None:
        """Tear down the sandbox for session_id. No-op if it isn't running."""

    async def discard_workspace(self, session_id: str) -> None:
        """Delete any persistent workspace state for session_id (explicit user close only --
        never called by idle reaping). Default no-op for providers without one."""

    @abc.abstractmethod
    async def touch(self, session_id: str) -> None:
        """Reset the idle-timeout clock for session_id's sandbox. No-op if it isn't running."""

    @abc.abstractmethod
    async def list_active(self) -> list[str]:
        """Return the session_ids of currently-running sandboxes."""

    @abc.abstractmethod
    async def exec_in_sandbox(self, session_id: str, command: str) -> ExecResult:
        """Run a shell command inside session_id's sandbox and return its result.

        This is the persistence layer's (workflow_persistence.py, git_ops.py) only channel to the
        working tree -- per the architecture plan's Section B, file I/O and git operations must
        happen wherever the clone actually lives (inside the sandbox), not in the agent's own
        process. `command` is run via `sh -c`, so the caller is responsible for shell-safe
        quoting (workflow_persistence.py base64-encodes file content for exactly this reason).
        """


def runtime_auth_env(provider: str, runtime_auth_token: str, runtime_auth_kind: str | None) -> list[tuple[str, str]]:
    """The provider-credential env pairs every provisioner bakes into a sandbox, in a fixed order
    -- each backend formats them into its own CLI syntax (docker `-e NAME=value`, az `NAME=value`)
    rather than duplicating the name/kind choice itself.

    COPILOT_GITHUB_TOKEN is always present, real-or-empty, so no caller needs to branch on
    provider for THAT name -- the sandbox-image entrypoint is what picks whether it actually needs
    it; a harmless unused env when empty. (COPILOT_GITHUB_TOKEN specifically, not
    COPILOT_SDK_AUTH_TOKEN -- task-12-report.md BUG B -- and not plain GITHUB_TOKEN either --
    task-12b fix-round-1: `gh`, git credential helpers, and repo-supplied scripts all read a plain
    GITHUB_TOKEN ambiently under --no-ask-user, exactly the ambient-long-lived-credential pattern
    provision()'s docstring above says to avoid; the CLI reads COPILOT_GITHUB_TOKEN identically
    without that exposure.)

    Phase E audit C-1: when provider == "claude", exactly ONE of ANTHROPIC_API_KEY /
    CLAUDE_CODE_OAUTH_TOKEN is included (picked by runtime_auth_kind) -- the other is omitted
    entirely, never emitted as an empty string; see provision()'s docstring above for why even a
    real-plus-empty pair is avoided on this dimension.
    """
    pairs = [("COPILOT_GITHUB_TOKEN", runtime_auth_token if provider == "copilot" else "")]
    if provider == "claude":
        claude_var_name = "CLAUDE_CODE_OAUTH_TOKEN" if runtime_auth_kind == "oauth" else "ANTHROPIC_API_KEY"
        pairs.append((claude_var_name, runtime_auth_token))
    return pairs


async def wait_for_cli_ready(
    exec_fn: Callable[[str], Awaitable[tuple[int, str, str]]], version_command: str
) -> None:
    """Block until the CLI tool in the sandbox is ready to accept commands.

    exec_fn is a thin provider-specific wrapper (docker exec / az container exec) that executes
    a command in the sandbox and returns (returncode, stdout, stderr). version_command is the
    caller-supplied version-check command for whichever CLI is actually active (e.g. "claude
    --version" or "copilot --version") -- no default here, deliberately: a fallback would silently
    check the wrong binary for any caller that forgot to pass one, exactly the bug this parameter
    replaces.

    The retry loop itself now runs INSIDE the sandbox as a single blocking exec (a `until ...;
    sleep 1; done` shell loop, self-bounded by the same deadline), not as repeated host-side
    exec_fn calls every 0.5s -- provisioning retries used to each start their own 0.5s poll loop
    with nothing cancelling a prior attempt's, and a stack of those hammering `docker exec` is
    what drove Docker Desktop's backend into a VM reset (2026-09-01). One exec call per
    provisioning attempt instead of up to 120 removes that failure mode outright. The outer
    asyncio.wait_for is a safety margin in case exec_fn itself hangs (e.g. an unresponsive
    daemon), not the normal exit path.
    """
    wait_command = (
        f"deadline=$(( $(date +%s) + {int(_READY_TIMEOUT_SECONDS)} )); "
        f"until {version_command} >/dev/null 2>&1; do "
        f"[ \"$(date +%s)\" -ge \"$deadline\" ] && exit 1; sleep 1; done"
    )
    try:
        returncode, _, stderr = await asyncio.wait_for(
            exec_fn(wait_command), timeout=_READY_TIMEOUT_SECONDS + 10.0
        )
    except Exception as exc:
        raise RuntimeError(
            f"CLI tool in sandbox did not become ready within {_READY_TIMEOUT_SECONDS}s "
            f"(last error: {exc})"
        ) from exc

    if returncode != 0:
        raise RuntimeError(
            f"CLI tool in sandbox did not become ready within {_READY_TIMEOUT_SECONDS}s "
            f"(last error: returncode {returncode}: {stderr})"
        )
