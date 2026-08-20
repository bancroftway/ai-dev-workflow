"""LangChain chat-model adapter backed by the GitHub Copilot SDK (SPECIFICATION.md Section 3.4).

This is the one bespoke piece of the system: LangGraph/LangChain have no
built-in GitHub Copilot integration, so this module presents a Copilot SDK
session as a standard `BaseChatModel`. Everything downstream (tool calls,
streaming, LangGraph state, checkpointing) works unmodified against it.

Session lifecycle: the Copilot SDK models a persistent conversational
session, not a stateless completions call, so one Copilot session is created
and reused per LangGraph `thread_id` (see `get_chat_model_for_thread`) rather
than flattening full history into every call.

Full-authority mode (BR-6): every session is created with
`on_permission_request=PermissionHandler.approve_all`, so Copilot never
pauses to ask permission for a tool call. The only pauses in this system are
the Gates implemented as LangGraph interrupts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from typing import Any, Literal, TypeVar

from copilot import CopilotClient, CopilotSession, RuntimeConnection
from copilot.session import (
    Attachment,
    BlobAttachment,
    ExitPlanModeRequest,
    ExitPlanModeResult,
    MCPServerConfig,
    PermissionHandler,
    PreToolUseHandler,
    SessionHooks,
)
from copilot.session_events import SessionEventType
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, PrivateAttr

from . import config
from . import telemetry
from .sandbox import SandboxSession, get_sandbox_provider

logger = logging.getLogger(__name__)

# Sessions are keyed by "{thread_id}:{stage}:{role}", not bare thread_id -- a single LangGraph
# thread runs both the specification and plan stages, each with a draft and an audit role, and
# each of those four (stage, role) combinations can be configured with a different model
# (SPECIFICATION.md Section 3.4 / models.yaml). Keying by thread_id alone would let whichever
# (stage, role) creates its session first silently lock in its model for every later call on the
# same thread.
_clients: dict[str, CopilotClient] = {}
_sessions: dict[str, CopilotSession] = {}
_session_locks: dict[str, asyncio.Lock] = {}

# TRUE idle timeout: the clock resets on every session event (deltas, usage), so a long but
# actively-streaming call never dies -- only genuine silence does. The old version was a flat
# 300s cap on the whole call via wait_for(done.wait()), which killed legitimate large-input
# audits mid-stream (observed live: specification_audit at ~100K input tokens).
_SESSION_IDLE_TIMEOUT_SECONDS = 300.0


async def _on_exit_plan_mode_request(
    request: ExitPlanModeRequest, _context: dict[str, str]
) -> ExitPlanModeResult:
    """Auto-approve GitHub Copilot's own Plan Mode exit requests.

    BR-6 (SPECIFICATION.md Section 7): the model provider must never introduce its own
    approval pause outside this app's own Gates. Registering this handler (rather than leaving
    it unset) is required, not cosmetic -- create_session sends requestExitPlanMode=False to the
    runtime when no handler is registered, which appears to suppress routing exit-plan-mode
    requests to the client at all rather than auto-approving them.
    """
    logger.info("Copilot Plan Mode exit requested: %s", request.get("summary"))
    return {"approved": True}


def _content_part_to_attachment(part: dict[str, Any]) -> Attachment | None:
    """Translate one non-text LangChain multimodal content part into a Copilot SDK Attachment.

    By the time a HumanMessage's content reaches this module, ag_ui_langgraph has already
    converted the wire-level AG-UI InputContent parts into LangChain's own multimodal
    convention -- verified directly against the installed ag_ui_langgraph's
    convert_agui_multimodal_to_langchain() (utils.py): every non-text media type (image,
    document, etc.) is routed through a single {"type": "image_url", "image_url": {"url":
    "data:<mime>;base64,<data>"}} shape, since "image_url" is the only media block type
    LangChain itself supports -- not the AG-UI-native {"source": {"type": "data", ...}} shape.
    A remote (non data:) URL isn't translated here; the Copilot SDK's FileAttachment/
    DirectoryAttachment expect a local filesystem path, not an arbitrary remote URL.
    """
    if part.get("type") != "image_url":
        return None
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    mime_type = header[len("data:") :].split(";")[0] or "application/octet-stream"

    metadata = part.get("metadata")
    display_name = metadata.get("filename") if isinstance(metadata, dict) else None
    attachment: BlobAttachment = {"type": "blob", "data": data, "mimeType": mime_type}
    if display_name:
        attachment["displayName"] = display_name
    return attachment


def _messages_to_prompt(messages: list[BaseMessage]) -> tuple[str, list[Attachment]]:
    """Flatten a LangChain message list into a single Copilot session prompt plus any
    multimodal attachments found in list-shaped message content (see graph.py's
    _build_specification_prompt, which is the only prompt builder that can produce these).
    """
    parts: list[str] = []
    attachments: list[Attachment] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                    continue
                attachment = _content_part_to_attachment(item)
                if attachment is not None:
                    attachments.append(attachment)
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if isinstance(message, SystemMessage):
            parts.append(f"Instructions:\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts), attachments


class CopilotChatModel(BaseChatModel):
    """A LangChain chat model driving a persistent GitHub Copilot SDK session.

    When ``sandbox`` is set, the Copilot runtime is NOT spawned as a local child process of this
    (agent) process -- it connects over the network to a `copilot --server` process already
    running inside that sandbox (architecture plan Section C.1). This is not a drop-in swap:
    per CopilotClient's own docstring, "Options are ignored when connecting to an existing
    runtime via RuntimeConnection.for_uri" -- concretely, `github_token` is only ever written
    into a locally-spawned child process's environment (client.py's _start_cli_server), which is
    skipped entirely for a for_uri connection. So `github_token` on this model is only honored
    in local/stdio mode (no sandbox); once a sandbox is set, auth is instead whatever
    COPILOT_SDK_AUTH_TOKEN the *sandbox's own* `copilot --server` process was started with (see
    agent/sandbox-image/entrypoint.sh) -- github_token is silently inert in that case, by SDK
    design, not a bug here.
    """

    thread_id: str
    stage: str
    role: str
    model_name: str | None = None
    github_token: str | None = None
    sandbox: SandboxSession | None = None

    # Agent Plugin / write-access controls (Part A of the plugin plan). All default to today's
    # behavior -- specification/plan pass none of these and are unaffected. Phase A0's spike
    # found available_tools (an allowlist) is the only reliable read-only boundary: excluded_tools
    # (a blocklist) is incomplete, since a write-capable model can reach create/bash/edit/
    # apply_patch interchangeably. available_tools/excluded_tools entries must be source-qualified
    # ("builtin:<name>"), never bare names (confirmed via copilot._mode.ToolSet).
    agent_mode: Literal["interactive", "plan", "autopilot", "shell"] = "plan"
    available_tools: list[str] | None = None
    excluded_tools: list[str] | None = None
    pre_tool_use_hook: PreToolUseHandler | None = None
    mcp_servers: dict[str, MCPServerConfig] | None = None

    # Custom agents (replaces scattered session_options lambdas)
    custom_agents: list[dict] | None = None
    agent: str | None = None

    # Handler-backed custom tools passed straight to create_session. The structured-output
    # mechanism (src/stack_runner.py) registers one `is_terminal=True` reporting tool here whose
    # `parameters` IS the stage's JSON schema: a schema-valid call ends the agent's turn, an
    # invalid one is rejected by our own client-side handler with field-level Pydantic errors and
    # the model retries in-session. Tools are captured at session-creation time (same as
    # available_tools), which is why each stage keeps its own session key.
    tools: list[Any] | None = None

    # Per-stage override of config.COPILOT_DISABLED_SKILLS. Exists because the vendored packs'
    # two mandate-style skills belong in exactly one stage each rather than nowhere: brainstorming
    # ("explore intent/requirements before implementation") is correct for specification and
    # actively harmful in a mechanical stage like ac-to-tests, where it burned whole turns.
    disabled_skills: list[str] | None = None

    _closing: bool = PrivateAttr(default=False)
    # Confirmed real by Phase A0's spike (SessionEventType.ASSISTANT_USAGE carries actual measured
    # token/cost data, not an estimate). Captures the LAST ASSISTANT_USAGE event seen during the
    # most recent _agenerate call -- metrics-report's token tracking reads this right after a node's own
    # model call. A real, stated limitation: ainvoke_structured's validate-and-retry loop can call
    # _agenerate more than once per logical "turn"; only the final (successful) call's usage is
    # kept, so a turn that needed retries under-reports its true token cost.
    _last_usage: dict[str, Any] | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "github-copilot"

    @property
    def _session_key(self) -> str:
        return f"{self.thread_id}:{self.stage}:{self.role}"

    async def _touch_sandbox(self) -> None:
        """Reset this session's sandbox idle-timeout clock (no-op without a sandbox).

        A long silent Copilot turn happens entirely over the Copilot TCP session -- no
        agent-side exec touches the sandbox -- so without this the idle reaper's clock keeps
        advancing and can reap the container mid-turn. Errors are swallowed: a touch failure
        (e.g. sandbox already reaped) must never fail the LLM call itself.
        """
        if self.sandbox is None:
            return
        with contextlib.suppress(Exception):
            await get_sandbox_provider().touch(self.sandbox.session_id)

    def _build_client(self) -> CopilotClient:
        if self.sandbox is not None:
            connection = RuntimeConnection.for_uri(
                f"{self.sandbox.host}:{self.sandbox.port}",
                connection_token=self.sandbox.connection_token,
            )
            return CopilotClient(connection=connection, log_level="error")
        return CopilotClient(github_token=self.github_token, log_level="error")

    async def _get_session(self) -> CopilotSession:
        session_key = self._session_key
        lock = _session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            existing = _sessions.get(session_key)
            if existing is not None:
                return existing

            logger.info(
                "Creating Copilot session %r with model=%r sandbox=%s",
                session_key,
                self.model_name,
                self.sandbox.session_id if self.sandbox else None,
            )
            client = self._build_client()
            # Hard cap on connect + session create: a wedged CLI server inside the container
            # otherwise hangs _connect_via_tcp forever (observed: 8 hours, headless run 2).
            # Failing loudly lets the node's own error path (telemetry + graph) take over.
            try:
                await asyncio.wait_for(client.__aenter__(), timeout=120)
                _clients[session_key] = client

                # A stdio MCP server whose `command` doesn't resolve fails to spawn -- and that
                # failure is otherwise completely silent: it doesn't appear in our logs, our OTEL
                # spans, or even the CLI's own session event stream (confirmed live, PLAYWRIGHT_MCP_
                # CONFIG's wrong "mcp-server-playwright" vs the real "playwright-mcp" binary name).
                # The session just quietly loses every tool -- not only the MCP server's own --
                # and 8+ headless runs burned 5-10 minutes each escalating with zero test files
                # before this got traced back to a missing binary. Fail loud, at session-creation
                # time, instead of after 3 costly draft/verify cycles.
                if self.mcp_servers and self.sandbox is not None:
                    provider = get_sandbox_provider()
                    for server_name, server_cfg in self.mcp_servers.items():
                        cmd = server_cfg.get("command") if isinstance(server_cfg, dict) else None
                        if not cmd:
                            continue
                        probe = await provider.exec_in_sandbox(self.sandbox.session_id, f"command -v {cmd}")
                        if not probe.ok:
                            raise RuntimeError(
                                f"MCP server {server_name!r} command {cmd!r} not found on PATH in "
                                f"sandbox {self.sandbox.session_id} (stage={self.stage} role={self.role}) "
                                "-- it would otherwise fail to spawn silently and take the session's "
                                "other tools down with it"
                            )

                # plugin_directories only means anything inside the sandbox's own filesystem -- a
                # locally-spawned (no-sandbox) Copilot process has no such content to point at.
                plugin_directories = config.COPILOT_PLUGIN_DIRECTORIES if self.sandbox is not None else None
                # Loaded-but-forbidden skills (config.COPILOT_DISABLED_SKILLS): the vendored packs
                # carry a couple of standing "invoke a skill before ANY response" mandates that
                # hijack a stage's turn. Disabling those two by name keeps every other skill this
                # repo's prompts actually reference reachable.
                disabled_skills = (
                    (self.disabled_skills if self.disabled_skills is not None else config.COPILOT_DISABLED_SKILLS)
                    if plugin_directories
                    else None
                )
                hooks: SessionHooks | None = (
                    {"on_pre_tool_use": self.pre_tool_use_hook} if self.pre_tool_use_hook is not None else None
                )
                # Without an explicit working_directory, file tools (view/edit/create) are never
                # even attempted -- confirmed via a standalone spike against a bare sandbox: the
                # exact same session config with working_directory unset produced zero tool calls
                # and a model claiming no file tool existed; with it set, the model actually
                # attempted (and, once available_tools also included builtin:create, succeeded at)
                # the write. Must match entrypoint.sh's WORKSPACE_DIR /
                # sandbox.local_docker.WORKSPACE_DIR_IN_CONTAINER.
                working_directory = "/workspace/repo" if self.sandbox is not None else None

                session = await asyncio.wait_for(
                    client.create_session(
                        on_permission_request=PermissionHandler.approve_all,
                        on_exit_plan_mode_request=_on_exit_plan_mode_request,
                        model=self.model_name,
                        streaming=True,
                        plugin_directories=plugin_directories,
                        disabled_skills=disabled_skills,
                        available_tools=self.available_tools,
                        excluded_tools=self.excluded_tools,
                        hooks=hooks,
                        mcp_servers=self.mcp_servers,
                        custom_agents=self.custom_agents,
                        agent=self.agent,
                        tools=self.tools,
                        working_directory=working_directory,
                    ),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                _clients.pop(session_key, None)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.__aexit__(None, None, None), timeout=10)
                raise RuntimeError(
                    f"Copilot session {session_key!r} timed out connecting to the sandbox CLI server"
                ) from None
            except Exception:
                _clients.pop(session_key, None)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.__aexit__(None, None, None), timeout=10)
                raise
            _sessions[session_key] = session
            return session

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
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
            # Success path only, and only when a usage event actually arrived: _last_usage is a
            # PrivateAttr that survives from the previous call, so reading it on error or absence
            # would attach stale numbers.
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
        session = await self._get_session()
        await self._touch_sandbox()
        prompt, attachments = _messages_to_prompt(messages)
        message_id = f"run-{uuid.uuid4().hex}"

        delta_parts: list[str] = []
        final_text: list[str] = []
        done = asyncio.Event()
        error_message: list[str] = []
        last_event_at = [asyncio.get_running_loop().time()]

        def handler(event: Any) -> None:
            last_event_at[0] = asyncio.get_running_loop().time()
            if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                delta_parts.append(event.data.delta_content)
                if run_manager is not None:
                    chunk = ChatGenerationChunk(
                        message=AIMessageChunk(content=event.data.delta_content, id=message_id)
                    )
                    asyncio.create_task(
                        run_manager.on_llm_new_token(event.data.delta_content, chunk=chunk)
                    )
            elif event.type == SessionEventType.ASSISTANT_MESSAGE:
                final_text.append(event.data.content)
            elif event.type == SessionEventType.ASSISTANT_USAGE:
                usage = event.data
                self._last_usage = {
                    "model": usage.model,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "cost": usage.cost,
                }
            elif event.type == SessionEventType.SESSION_IDLE:
                done.set()
            elif event.type == SessionEventType.SESSION_ERROR:
                error_message.append(event.data.message)
                done.set()

        unsubscribe = session.on(handler)
        try:
            await session.send(prompt, agent_mode=self.agent_mode, attachments=attachments or None)
            loop = asyncio.get_running_loop()
            while not done.is_set():
                await self._touch_sandbox()
                remaining = _SESSION_IDLE_TIMEOUT_SECONDS - (loop.time() - last_event_at[0])
                if remaining <= 0:
                    raise TimeoutError(
                        f"Copilot session silent for {_SESSION_IDLE_TIMEOUT_SECONDS:.0f}s "
                        f"(stage={self.stage} role={self.role})"
                    )
                try:
                    await asyncio.wait_for(done.wait(), timeout=min(remaining, 15.0))
                except TimeoutError:
                    continue  # no completion yet -- re-check the activity clock
        finally:
            unsubscribe()

        if error_message:
            raise RuntimeError(f"Copilot session error: {error_message[0]}")

        content = final_text[-1] if final_text else "".join(delta_parts)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content, id=message_id))])

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
    pre_tool_use_hook: PreToolUseHandler | None = None,
    mcp_servers: dict[str, MCPServerConfig] | None = None,
    custom_agents: list[dict] | None = None,
    agent: str | None = None,
    tools: list[Any] | None = None,
    disabled_skills: list[str] | None = None,
) -> CopilotChatModel:
    """Return the chat model for the given LangGraph thread's (stage, role) Copilot session.

    stage/role together (e.g. "specification"/"draft" vs "plan"/"audit") identify which of the
    up-to-four persistent Copilot sessions a single thread can have open at once -- see the
    module docstring on _sessions for why bare thread_id isn't a fine-grained-enough key.

    sandbox, when provided, routes this session's Copilot runtime to the given per-session
    sandbox instead of spawning Copilot locally -- see CopilotChatModel's docstring for why
    github_token becomes inert once sandbox is set.

    agent_mode/available_tools/excluded_tools/pre_tool_use_hook/mcp_servers all default to
    today's read-only "plan"-mode, no-restriction behavior -- a caller only needs to pass these
    for a stage that genuinely needs write access or a narrower tool allowlist (Part A of the
    plugin plan; Phase A0's spike found available_tools, not excluded_tools, is the reliable
    read-only boundary -- see config.READ_ONLY_AVAILABLE_TOOLS).
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
    """Close and forget every Copilot session for a thread (call on graph run completion/error).

    Per-session failures are logged and stepped over rather than raised: this runs at the END of a
    run (exit_nodes, escalate_node, run_headless), so letting one unreachable session abort the
    loop would both leak the remaining sessions and turn a completed run into a failed one. Popping
    the dicts is what actually matters and cannot fail; the awaits are best-effort resource release.
    """
    prefix = f"{thread_id}:"
    for session_key in [key for key in _sessions if key.startswith(prefix)]:
        session = _sessions.pop(session_key, None)
        client = _clients.pop(session_key, None)
        _session_locks.pop(session_key, None)
        if session is not None:
            try:
                await session.disconnect()
            except Exception:
                logger.warning("disconnect failed for session %r during thread close", session_key, exc_info=True)
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                logger.warning("client close failed for session %r during thread close", session_key, exc_info=True)


def forget_thread_sessions(thread_id: str) -> None:
    """Drop cached Copilot handles for a thread whose sandbox is already gone.

    Sync and network-free ON PURPOSE, which is why this is not close_thread_session: once the
    container is destroyed, session.disconnect() and client.__aexit__() are calls to a dead
    endpoint that only block until their timeout. There is nothing to close gracefully.

    Called from sandbox.registry.pop() -- the one choke point every container-destruction path
    routes through (both providers' terminate(), the idle reaper via terminate(), and the
    DELETE /{thread_id} endpoint). Without it, _sessions kept handing out up to ~26 handles per
    thread pointing into a container that no longer exists: the same phantom-state bug the
    `registry.pop` comment in local_docker.terminate describes, one layer up.
    """
    prefix = f"{thread_id}:"
    stale = [key for key in _sessions if key.startswith(prefix)]
    stale += [key for key in _clients if key.startswith(prefix) and key not in stale]
    for session_key in stale:
        _sessions.pop(session_key, None)
        _clients.pop(session_key, None)
        _session_locks.pop(session_key, None)
    if stale:
        logger.info("forgot %d Copilot session(s) for thread_id=%s (sandbox gone)", len(stale), thread_id)


_STRUCTURED_OUTPUT_INSTRUCTION = (
    "If completing this task requires using your tools (e.g. writing files), do that FIRST, "
    "using as many turns as you need -- only once that work is actually done, respond with ONLY "
    "a single JSON object matching this JSON Schema as your final message. "
    "No markdown code fences, no commentary before or after the JSON.\n\n"
    "JSON Schema:\n{schema}"
)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


async def ainvoke_structured(
    model: CopilotChatModel,
    messages: list[BaseMessage],
    schema: type[SchemaT],
    *,
    max_attempts: int = 3,
) -> SchemaT:
    """Invoke the model and parse its response as the given Pydantic schema.

    The Copilot SDK has no native structured-output/tool-calling contract to
    bind to, so this drives structured output via explicit JSON-schema
    prompting plus a validate-and-retry loop, rather than emulating
    OpenAI-style function calling in the adapter.
    """
    schema_json = json.dumps(schema.model_json_schema())
    request_messages = [
        *messages,
        HumanMessage(content=_STRUCTURED_OUTPUT_INSTRUCTION.format(schema=schema_json)),
    ]

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        # emit-messages=False (ag_ui_langgraph's convention, agent.py:993) keeps this raw
        # JSON-schema-constrained output out of the AG-UI text-message stream -- with the
        # CopilotSidebar now showing a real chat transcript (unlike the old top-banner-only
        # UI), an unsuppressed stream would render this structured output as if it were
        # assistant chat prose, which it was never meant to be.
        response = await model.ainvoke(request_messages, config={"metadata": {"emit-messages": False}})
        raw = _CODE_FENCE_RE.sub("", str(response.content)).strip()
        try:
            return schema.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001 - retry loop deliberately broad
            last_error = exc
            request_messages.append(response)
            request_messages.append(
                HumanMessage(
                    content=(
                        f"That response was rejected (error: {exc}).\n"
                        # Not every rejection is a syntax problem. A schema may also enforce a
                        # WORK rule -- e.g. ac-to-tests refuses readiness=true with no test files
                        # -- and the old wording ("reply again with ONLY the corrected JSON, no
                        # other text") told the model to re-answer without touching its tools,
                        # which is exactly the wrong move when the rejection means the work is
                        # missing. Read the error and act on what it actually says.
                        "If the error is about the JSON itself (syntax, a wrong or missing "
                        "field), reply again with ONLY the corrected JSON object.\n"
                        "If the error says required WORK is missing, do that work first with "
                        "your tools -- write the files, run the command -- and only then reply "
                        "with the JSON describing what you actually did."
                    )
                )
            )
    assert last_error is not None
    raise last_error


def get_session_id(thread_id: str, stage: str, role: str) -> str | None:
    """The Copilot session id backing one (thread, stage, role), or None if none was created.

    Exists so a gate can verify what a stage's session ACTUALLY did -- gates/skill_gate.py reads
    that session's own `skill.invoked` events rather than trusting the model's self-report.
    """
    session = _sessions.get(f"{thread_id}:{stage}:{role}")
    return getattr(session, "session_id", None) if session is not None else None


async def close_session(thread_id: str, stage: str, role: str) -> None:
    """Drop one (thread, stage, role) Copilot session so the next call starts a fresh one.

    Sessions are conversational and cached per stage, which is normally what we want -- a retry
    keeps its context and the gate's feedback. But when a stage fails because the model reported
    work it never did, that fabricated claim is now in its own history and it repeats it: observed
    live, ac-to-tests asserted "created failing RED-phase tests for all active ACs" on six
    consecutive laps while making zero write calls. Retrying in the same session re-reads the lie;
    a fresh session re-reads only the prompt and the failure feedback.

    The freshness guarantee comes from popping _sessions, which cannot fail -- the awaits below are
    best-effort resource release, so a failure there does NOT mean the next attempt reuses the
    poisoned history. They are logged rather than silently suppressed because a runtime that can no
    longer close a session is worth seeing in the log, not because correctness depends on them.
    """
    session_key = f"{thread_id}:{stage}:{role}"
    session = _sessions.pop(session_key, None)
    client = _clients.pop(session_key, None)
    _session_locks.pop(session_key, None)
    if session is not None:
        try:
            await session.disconnect()
        except Exception:
            logger.warning("disconnect failed for session %r (already dropped anyway)", session_key, exc_info=True)
    if client is not None:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            logger.warning("client close failed for session %r (already dropped anyway)", session_key, exc_info=True)
    logger.info("closed session %r so the next attempt starts fresh", session_key)


def _demo() -> None:
    """Self-check for the session-cache eviction path (the live session paths need a sandbox)."""
    doomed, survivor = "thread-doomed", "thread-survivor"
    for thread in (doomed, survivor):
        for key in (f"{thread}:specification:draft", f"{thread}:plan:audit"):
            _sessions[key] = object()  # type: ignore[assignment]
            _clients[key] = object()  # type: ignore[assignment]
            _session_locks[key] = asyncio.Lock()

    forget_thread_sessions(doomed)

    # The whole point: one thread's dead sandbox must not evict another thread's live sessions.
    assert not [k for k in _sessions if k.startswith(f"{doomed}:")], "doomed sessions survived"
    assert not [k for k in _clients if k.startswith(f"{doomed}:")], "doomed clients survived"
    assert not [k for k in _session_locks if k.startswith(f"{doomed}:")], "doomed locks survived"
    assert len([k for k in _sessions if k.startswith(f"{survivor}:")]) == 2, "evicted the wrong thread"

    # A client with no session entry (create_session failed after __aenter__) must still be evicted,
    # otherwise a half-built session leaks a TCP connection into a destroyed container forever.
    _clients[f"{doomed}:orphan:draft"] = object()  # type: ignore[assignment]
    forget_thread_sessions(doomed)
    assert f"{doomed}:orphan:draft" not in _clients, "orphaned client survived"

    forget_thread_sessions("never-existed")  # must not raise

    # The wiring itself: registry.pop is the choke point every container teardown routes through,
    # and it reaches this module via a function-local import. A broken import would otherwise only
    # surface as a silent leak in production.
    from .sandbox import registry

    registry.pop(survivor)
    assert not [k for k in _sessions if k.startswith(f"{survivor}:")], "registry.pop did not evict"
    assert not [k for k in _clients if k.startswith(f"{survivor}:")], "registry.pop left clients"
    # The third eviction trigger: an unhandled node exception is run-ending, but a
    # GraphBubbleUp (gate interrupt) is NOT -- it must leave the stage's conversation intact.
    # Getting this backwards would either leak every failed run's sessions or silently destroy
    # a stage's context every time it pauses for human approval.
    from langgraph.errors import GraphBubbleUp

    from .telemetry import traced_node

    async def _blows_up(_state, _config):
        raise RuntimeError("node blew up")

    async def _pauses_at_gate(_state, _config):
        raise GraphBubbleUp("gate interrupt")

    async def _check_node_error_paths() -> None:
        config = {"configurable": {"thread_id": "thread-node-error"}}
        key = "thread-node-error:plan:draft"
        for failing, should_survive in ((_blows_up, False), (_pauses_at_gate, True)):
            _sessions[key] = object()  # type: ignore[assignment]
            _clients[key] = object()  # type: ignore[assignment]
            with contextlib.suppress(RuntimeError, GraphBubbleUp):
                await traced_node("selfcheck", failing)({}, config)
            assert (key in _sessions) is should_survive, (
                f"{failing.__name__}: expected survive={should_survive}"
            )
            _sessions.pop(key, None)
            _clients.pop(key, None)

    # traced_node logger.exception()s the deliberate failure above. Left visible, a passing
    # self-check prints a full traceback, which teaches everyone to ignore this output.
    logging.getLogger("src.telemetry").setLevel(logging.CRITICAL)
    try:
        asyncio.run(_check_node_error_paths())
    finally:
        logging.getLogger("src.telemetry").setLevel(logging.NOTSET)

    print("copilot_chat_model self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.copilot_chat_model` loads this
    # file as "__main__", so registry.pop's `from ..copilot_chat_model import ...` would import a
    # SECOND copy of this module with its own _sessions/_clients dicts -- and the wiring assertion
    # below would fail against caches nothing ever wrote to. Production imports it exactly once
    # (main.py -> src.graph -> src.copilot_chat_model), which is what this reproduces.
    from src.copilot_chat_model import _demo as _packaged_demo

    _packaged_demo()
