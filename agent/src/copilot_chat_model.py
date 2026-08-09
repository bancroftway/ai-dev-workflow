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
import logging
import re
import uuid
from typing import Any, TypeVar

from copilot import CopilotClient, CopilotSession
from copilot.session import Attachment, BlobAttachment, ExitPlanModeRequest, ExitPlanModeResult, PermissionHandler
from copilot.session_events import SessionEventType
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, PrivateAttr

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
    """A LangChain chat model driving a persistent GitHub Copilot SDK session."""

    thread_id: str
    stage: str
    role: str
    model_name: str | None = None
    github_token: str | None = None

    _closing: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self) -> str:
        return "github-copilot"

    @property
    def _session_key(self) -> str:
        return f"{self.thread_id}:{self.stage}:{self.role}"

    async def _get_session(self) -> CopilotSession:
        session_key = self._session_key
        lock = _session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            existing = _sessions.get(session_key)
            if existing is not None:
                return existing

            client = CopilotClient(github_token=self.github_token, log_level="error")
            await client.__aenter__()
            _clients[session_key] = client

            session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                on_exit_plan_mode_request=_on_exit_plan_mode_request,
                model=self.model_name,
                streaming=True,
            )
            _sessions[session_key] = session
            return session

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        session = await self._get_session()
        prompt, attachments = _messages_to_prompt(messages)
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
            await session.send(prompt, agent_mode="plan", attachments=attachments or None)
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
    thread_id: str,
    stage: str,
    role: str,
    *,
    github_token: str | None = None,
    model_name: str | None = None,
) -> CopilotChatModel:
    """Return the chat model for the given LangGraph thread's (stage, role) Copilot session.

    stage/role together (e.g. "specification"/"draft" vs "plan"/"audit") identify which of the
    up-to-four persistent Copilot sessions a single thread can have open at once -- see the
    module docstring on _sessions for why bare thread_id isn't a fine-grained-enough key.
    """
    return CopilotChatModel(
        thread_id=thread_id, stage=stage, role=role, github_token=github_token, model_name=model_name
    )


async def close_thread_session(thread_id: str) -> None:
    """Close and forget every Copilot session for a thread (call on graph run completion/error)."""
    prefix = f"{thread_id}:"
    for session_key in [key for key in _sessions if key.startswith(prefix)]:
        session = _sessions.pop(session_key, None)
        client = _clients.pop(session_key, None)
        _session_locks.pop(session_key, None)
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
                        f"That was not valid JSON matching the schema (error: {exc}). "
                        "Reply again with ONLY the corrected JSON object, no other text."
                    )
                )
            )
    assert last_error is not None
    raise last_error
