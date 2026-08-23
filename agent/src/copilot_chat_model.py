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
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import uuid
from typing import Any, Literal

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, PrivateAttr

from . import config
from . import telemetry
from .cli_agent_exec import _SCRATCH_DIR, run_turn, write_scratch_file
from .sandbox import SandboxProvider, SandboxSession, get_sandbox_provider

logger = logging.getLogger(__name__)

# Keyed "{thread_id}:{stage}:{role}", exactly like claude_chat_model._session_ids -- a single
# LangGraph thread runs multiple stages, each with a draft and an audit (and sometimes fix) role,
# and each of those (stage, role) pairs is its own independent Copilot CLI conversation (its own
# --session-id chain). The value is the CLI's own session_id string, nothing more -- there is no
# client or connection object to key alongside it anymore now that every turn is a fresh subprocess
# exec rather than a persistent TCP session (see this module's own docstring).
_session_ids: dict[str, str] = {}


def _messages_to_prompt(messages: list[BaseMessage]) -> str:
    """Flatten a LangChain message list into a single Copilot CLI prompt string.

    Mirrors claude_chat_model._messages_to_prompt exactly (SystemMessage gets an "Instructions:"
    prefix, everything else passes through verbatim, parts joined with a blank line) and, like that
    function, drops multimodal content instead of translating it to an attachment. The old SDK
    version translated image_url parts into a Copilot Attachment over the live session
    (_content_part_to_attachment, now deleted along with the rest of the SDK plumbing); the
    verified Copilot CLI flags table (task-3-brief.md) has no attachment/file flag to translate one
    into over `-p`'s stdin-string interface, and no stage in this pipeline currently sends Copilot
    an image. Not attempted here as a ponytail-style deliberate cut, not an oversight -- same
    upgrade path as Claude's: mirror write_scratch_file for binary payloads and pass the result via
    a flag per part, if a real Copilot CLI flag for it is ever confirmed.
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
                logger.warning(
                    "dropped %d non-text content part(s) -- CopilotChatModel has no multimodal "
                    "support over the CLI",
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
        candidate = event.get("session_id")
        if isinstance(candidate, str) and candidate:
            session_id = candidate
    return events, session_id


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
    model_name: str | None = None
    # Vestigial: every real call site (graph.py, e2e_nodes.py, metrics_nodes.py,
    # preflight_nodes.py, test_hardening_nodes.py, rebuild.py) unconditionally passes
    # github_token=os.environ.get("GITHUB_TOKEN") into get_chat_model_for_thread, so the kwarg
    # must keep accepting a value without erroring -- but nothing in _agenerate_inner reads
    # self.github_token anymore. The old SDK version honored it only for a locally-spawned
    # (no-sandbox) CopilotClient's own environment; that whole code path -- and the
    # local-child-process mode it belonged to -- no longer exists under CLI-exec, where every turn
    # always execs into an already-provisioned sandbox (cli_agent_exec.run_turn) whose own
    # COPILOT_GITHUB_TOKEN env var (see secret_env_names below) is what the copilot CLI actually
    # reads (task-12b fix-round-1: not the plain GITHUB_TOKEN name, which `gh`/git/npm also read
    # ambiently -- see this task's report for why that matters).
    # Not warned on the way pre_tool_use_hook/tools/custom_agents/disabled_skills are below --
    # unlike those, this is set on EVERY call, so a warning here would be constant noise, not
    # signal.
    github_token: str | None = None
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

    # Same shape as claude_chat_model.ClaudeChatModel._last_usage, for the provider-agnostic OTEL
    # span attributes _agenerate sets below. reasoning_tokens/cache_read_tokens/cache_write_tokens
    # are NOT included -- unlike the old SDK's ASSISTANT_USAGE event, the JSONL shape this parses
    # is unverified (module docstring) and a fabricated 0 would read as "measured zero" instead of
    # "not reported" to anything that later reads this dict.
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
        if disabled_skills:
            # Unlike claude_chat_model (which works around this exact gap with
            # --append-system-prompt), the verified Copilot CLI flags table has no equivalent flag
            # at all -- inventing one here would be exactly the "speculative extra flag beyond the
            # brief's table" this task's own self-review explicitly warns against. Logged and
            # ignored rather than silently dropped.
            logger.warning(
                "CopilotChatModel.disabled_skills=%s requested but no verified Copilot CLI flag "
                "exists to translate it into -- this turn proceeds with those skills still "
                "reachable",
                disabled_skills,
            )

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
        result = await run_turn(
            provider,
            self.thread_id,
            command,
            prompt,
            scratch_prefix,
            timeout_seconds=config.CLI_AGENT_TURN_TIMEOUT_SECONDS,
        )

        events, new_session_id = _parse_copilot_jsonl(result.stdout)
        if not events:
            raise RuntimeError(
                f"Copilot CLI turn for {self._session_key!r} produced no parseable JSONL lines "
                f"under --output-format json: stdout={result.stdout!r}\nstderr={result.stderr!r}"
            )
        if new_session_id:
            _session_ids[self._session_key] = new_session_id

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

        if final.get("is_error"):
            raise RuntimeError(
                f"Copilot CLI turn for {self._session_key!r} reported an error "
                f"(stop_reason={final.get('stop_reason')!r}): {final.get('result')!r}"
            )

        usage = final.get("usage") or {}
        self._last_usage = {
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
    github_token: str | None = None,
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

    Every kwarg name and default is unchanged from the pre-rewrite SDK version -- every call site
    in this codebase (graph.py, e2e_nodes.py, metrics_nodes.py, preflight_nodes.py,
    test_hardening_nodes.py, rebuild.py) depends on this exact surface staying stable; only
    _agenerate_inner's insides changed. github_token is now vestigial -- kept only so those call
    sites' unconditional `github_token=os.environ.get("GITHUB_TOKEN")` keeps working unchanged, see
    CopilotChatModel.github_token's own comment for why it is never read anymore.
    """
    return CopilotChatModel(
        thread_id=thread_id,
        stage=stage,
        role=role,
        github_token=github_token,
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
    that already awaits this function; eviction itself is the same pure dict pop as
    forget_thread_sessions, not a network call that can fail or hang against an already-dead
    sandbox.
    """
    forget_thread_sessions(thread_id)


def forget_thread_sessions(thread_id: str) -> None:
    """Drop cached Copilot session ids for a thread whose sandbox is already gone.

    Sync and network-free, same as claude_chat_model.forget_thread_sessions -- there is no live
    client/connection to close gracefully anymore, only a session_id string to forget. Called from
    sandbox.registry.pop() (already wired there today), the one choke point every
    container-destruction path routes through (both providers' terminate(), the idle reaper, and
    the DELETE /{thread_id} endpoint).
    """
    prefix = f"{thread_id}:"
    stale = [key for key in _session_ids if key.startswith(prefix)]
    for key in stale:
        _session_ids.pop(key, None)
    if stale:
        logger.info("forgot %d Copilot session id(s) for thread_id=%s (sandbox gone)", len(stale), thread_id)


async def close_session(thread_id: str, stage: str, role: str) -> None:
    """Drop one (thread, stage, role) Copilot session id so the next call starts fresh (omits
    --session-id), the same recovery mechanism as claude_chat_model.close_session for a stage whose
    session history now contains a fabricated claim -- see that function's docstring for why a
    fresh session, not a retry in the same one, is what actually recovers from it.

    Async for the same call-site-parity reason as close_thread_session; nothing here awaits either.
    """
    session_key = f"{thread_id}:{stage}:{role}"
    _session_ids.pop(session_key, None)
    logger.info("closed Copilot session %r so the next attempt starts fresh", session_key)


def get_session_id(thread_id: str, stage: str, role: str) -> str | None:
    """The Copilot session id backing one (thread, stage, role), or None if none was created yet.

    Exists so a gate can verify what a stage's session actually did -- gates/skill_gate.py already
    calls this today. read_skill_invocations below is where that verification logic is moving to
    (task-3-brief.md), not this function.
    """
    return _session_ids.get(f"{thread_id}:{stage}:{role}")


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
    just the assumed-final one), the Bug A wrapper-script shape (task-12b), and the session-cache
    eviction path -- the live CLI-exec path needs a sandbox, see cli_agent_exec.py's and
    claude_chat_model.py's own demos for the same "pure half only" scoping.
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

    # Session-cache eviction: one thread's dead sandbox must not evict another thread's live
    # sessions (mirrors claude_chat_model._demo's doomed/survivor shape).
    doomed, survivor = "thread-doomed", "thread-survivor"
    for thread in (doomed, survivor):
        for suffix in ("specification:draft", "plan:audit"):
            _session_ids[f"{thread}:{suffix}"] = f"sess-{thread}-{suffix}"

    forget_thread_sessions(doomed)
    assert not [k for k in _session_ids if k.startswith(f"{doomed}:")], "doomed sessions survived"
    assert len([k for k in _session_ids if k.startswith(f"{survivor}:")]) == 2, "evicted the wrong thread"

    forget_thread_sessions("never-existed")  # must not raise

    asyncio.run(close_session(survivor, "specification", "draft"))
    assert get_session_id(survivor, "specification", "draft") is None, "close_session did not evict"
    assert get_session_id(survivor, "plan", "audit") is not None, "close_session evicted the wrong key"

    asyncio.run(close_thread_session(survivor))
    assert not [k for k in _session_ids if k.startswith(f"{survivor}:")], "close_thread_session did not evict"

    assert secret_env_names() == {
        "COPILOT_SDK_AUTH_TOKEN",
        "COPILOT_CONNECTION_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GITHUB_TOKEN",
    }

    # read_skill_invocations must fail open unconditionally (module docstring) -- None args are
    # safe here because the function never touches them, by contract.
    assert asyncio.run(read_skill_invocations(None, "thread", "session")) is None, "must always fail open"

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
