"""Provider-agnostic structured JSON output via schema validation and retry.

Extracted from copilot_chat_model.py -- ainvoke_structured drives structured output through
explicit JSON-schema prompting + validate-and-retry, since neither Copilot CLI nor the
JSON-schema-prompt-and-validate-and-retry pattern are provider-specific. The model parameter
is a BaseChatModel (both ClaudeChatModel and CopilotChatModel satisfy this interface), and the
invocation path (model.ainvoke) is standard across both.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

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
    model: BaseChatModel,
    messages: list[BaseMessage],
    schema: type[SchemaT],
    *,
    max_attempts: int = 3,
) -> SchemaT:
    """Invoke the model and parse its response as the given Pydantic schema.

    Provider-agnostic structured-output mechanism: works with any BaseChatModel (Claude or
    Copilot). Drives structured output via explicit JSON-schema prompting plus a validate-and-
    retry loop, since neither provider's CLI has a native structured-output/tool-calling contract
    that both satisfy.

    The retry loop itself needs a live model, but the regex/formatting is pure and testable (see
    _demo below).
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


def _demo() -> None:
    """Self-check for regex/formatting assertions only (the retry loop itself needs a live model).

    Verifies that _CODE_FENCE_RE correctly strips markdown code fences from JSON responses.
    """
    # Bare JSON: no fences, should pass through unchanged
    raw_json = '{"key": "value", "number": 42}'
    stripped = _CODE_FENCE_RE.sub("", raw_json).strip()
    assert stripped == raw_json, f"bare JSON was modified: {stripped!r}"

    # JSON with triple-backtick fences (common case)
    fenced = '```json\n{"key": "value"}\n```'
    stripped = _CODE_FENCE_RE.sub("", fenced).strip()
    assert stripped == '{"key": "value"}', f"triple-backtick fences not stripped: {stripped!r}"

    # JSON with plain backticks (no 'json' language marker)
    plain_fences = '```\n{"key": "value"}\n```'
    stripped = _CODE_FENCE_RE.sub("", plain_fences).strip()
    assert stripped == '{"key": "value"}', f"plain backticks not stripped: {stripped!r}"

    # Mixed content: fences with surrounding text (before the regex, not typical but verify idempotence)
    mixed = '```\n{"a": 1}\n```'
    stripped = _CODE_FENCE_RE.sub("", mixed).strip()
    assert stripped == '{"a": 1}', f"mixed content not handled: {stripped!r}"

    # Multiline JSON
    multiline = '```json\n{\n  "key": "value",\n  "nested": {"a": 1}\n}\n```'
    stripped = _CODE_FENCE_RE.sub("", multiline).strip()
    expected = '{\n  "key": "value",\n  "nested": {"a": 1}\n}'
    assert stripped == expected, f"multiline JSON mangled: {stripped!r}"

    print("structured_output self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose (same pattern as all other modules).
    from src.structured_output import _demo as _packaged_demo

    _packaged_demo()
