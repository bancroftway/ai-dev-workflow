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
import json
import re
import uuid
from typing import Any, TypeVar

from copilot import CopilotClient, CopilotSession
from copilot.session import PermissionHandler
from copilot.session_events import SessionEventType
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, PrivateAttr

_clients: dict[str, CopilotClient] = {}
_sessions: dict[str, CopilotSession] = {}
_session_locks: dict[str, asyncio.Lock] = {}

_SESSION_IDLE_TIMEOUT_SECONDS = 300.0


def _messages_to_prompt(messages: list[BaseMessage]) -> str:
    """Flatten a LangChain message list into a single Copilot session prompt."""
    parts: list[str] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            parts.append(f"Instructions:\n{message.content}")
        else:
            parts.append(str(message.content))
    return "\n\n".join(parts)


class CopilotChatModel(BaseChatModel):
    """A LangChain chat model driving a persistent GitHub Copilot SDK session."""

    thread_id: str
    model_name: str | None = None
    github_token: str | None = None

    _closing: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self) -> str:
        return "github-copilot"

    async def _get_session(self) -> CopilotSession:
        lock = _session_locks.setdefault(self.thread_id, asyncio.Lock())
        async with lock:
            existing = _sessions.get(self.thread_id)
            if existing is not None:
                return existing

            client = CopilotClient(github_token=self.github_token, log_level="error")
            await client.__aenter__()
            _clients[self.thread_id] = client

            session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                model=self.model_name,
                streaming=True,
            )
            _sessions[self.thread_id] = session
            return session

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        session = await self._get_session()
        prompt = _messages_to_prompt(messages)
        message_id = f"run-{uuid.uuid4().hex}"

        delta_parts: list[str] = []
        final_text: list[str] = []
        done = asyncio.Event()
        error_message: list[str] = []

        def handler(event: Any) -> None:
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
            elif event.type == SessionEventType.SESSION_IDLE:
                done.set()
            elif event.type == SessionEventType.SESSION_ERROR:
                error_message.append(event.data.message)
                done.set()

        unsubscribe = session.on(handler)
        try:
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout=_SESSION_IDLE_TIMEOUT_SECONDS)
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
    thread_id: str, *, github_token: str | None = None, model_name: str | None = None
) -> CopilotChatModel:
    """Return the (cached) chat model bound to the given LangGraph thread's Copilot session."""
    return CopilotChatModel(thread_id=thread_id, github_token=github_token, model_name=model_name)


async def close_thread_session(thread_id: str) -> None:
    """Close and forget the Copilot session for a thread (call on graph run completion/error)."""
    session = _sessions.pop(thread_id, None)
    client = _clients.pop(thread_id, None)
    _session_locks.pop(thread_id, None)
    if session is not None:
        await session.disconnect()
    if client is not None:
        await client.__aexit__(None, None, None)


_STRUCTURED_OUTPUT_INSTRUCTION = (
    "Respond with ONLY a single JSON object matching this JSON Schema. "
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
        response = await model.ainvoke(request_messages)
        raw = _CODE_FENCE_RE.sub("", str(response.content)).strip()
        try:
            return schema.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001 - retry loop deliberately broad
            last_error = exc
            request_messages.append(response)
            request_messages.append(
                HumanMessage(
                    content=(
                        f"That was not valid JSON matching the schema (error: {exc}). "
                        "Reply again with ONLY the corrected JSON object, no other text."
                    )
                )
            )
    assert last_error is not None
    raise last_error
