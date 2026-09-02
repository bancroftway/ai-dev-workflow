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


def _ref_name(ref: str) -> str:
    """`"#/$defs/PresenceList"` -> `"PresenceList"`."""
    return ref.rsplit("/", 1)[-1]


def _resolve_ref(prop: dict, defs: dict) -> dict | None:
    ref = prop.get("$ref")
    return defs.get(_ref_name(ref)) if ref else None


def _first_enum_field(model_def: dict, defs: dict) -> tuple[str, list] | None:
    """One level into an ordinary object $defs entry's OWN properties: the first one that is
    itself enum-shaped (a direct Literal, or a $ref to a named Enum) -- e.g. PresenceList's
    `status`, DotnetStatus's `status`, EcosystemRoot's `status` (schemas.py). Not hardcoded to the
    name "status": whichever field is enum-shaped first wins. Deliberately not recursive beyond
    this one level -- this codebase's wrapper types are all exactly one level deep."""
    for field_name, field_prop in model_def.get("properties", {}).items():
        if "enum" in field_prop:
            return field_name, field_prop["enum"]
        ref_target = _resolve_ref(field_prop, defs)
        if ref_target and "enum" in ref_target:
            return field_name, ref_target["enum"]
    return None


def _type_label(prop: dict, defs: dict) -> str:
    """Compact type label for one schema property -- resolves `$ref`/`anyOf`/`array` one level
    deep so a reader sees e.g. `TechStack | null` or `array[ClarifyingQuestion]` instead of a bare
    "object"/"array".

    A `$ref` to an ordinary nested BaseModel (not itself a named Enum -- that case is `_enum_values`'s
    job, via its own `[allowed: ...]` suffix) additionally surfaces THAT model's own wrapper enum,
    e.g. `PresenceList — status: present|absent` -- without this, build_schema_contract's "cannot
    drift from the schema by construction" claim would be hollow for PresenceList/DotnetStatus/
    EcosystemRoot, whose entire reason for existing is exactly that one status-shaped field."""
    if "$ref" in prop:
        ref_name = _ref_name(prop["$ref"])
        target = defs.get(ref_name)
        if target is not None and "enum" not in target:
            nested = _first_enum_field(target, defs)
            if nested:
                field_name, values = nested
                return f"{ref_name} — {field_name}: {'|'.join(str(v) for v in values)}"
        return ref_name
    prop_type = prop.get("type")
    if prop_type == "array":
        item_label = _type_label(prop["items"], defs) if "items" in prop else "any"
        return f"array[{item_label}]"
    if prop_type:
        return prop_type
    if "anyOf" in prop:
        labels = [_type_label(member, defs) for member in prop["anyOf"]]
        return " | ".join(dict.fromkeys(labels))  # dedupe, preserve order
    return "any"


def _enum_values(prop: dict, defs: dict) -> list | None:
    """The allowed values for one schema property ITSELF, or None if the property isn't
    enum-shaped.

    Checked directly on the property (how Pydantic renders a `Literal[...]` field -- inline
    `"enum": [...]` with no `$ref`, confirmed against this codebase's own `auth_kind` field) and,
    one level deep, on a `$ref` target (how a named Enum type alias would render: the enum lives
    on the $defs entry itself, not the property). A `$ref` to an ordinary nested BaseModel (e.g.
    `PresenceList`) has no top-level `enum` on its own $defs entry, so it correctly yields nothing
    here -- that model's own enum-shaped sub-field (`PresenceList.status`) is a DIFFERENT case,
    surfaced by `_type_label` instead (see its docstring), not duplicated into this `[allowed: ...]`
    suffix."""
    if "enum" in prop:
        return prop["enum"]
    target = _resolve_ref(prop, defs)
    if target and "enum" in target:
        return target["enum"]
    for key in ("anyOf", "allOf"):
        for member in prop.get(key, []):
            values = _enum_values(member, defs)
            if values:
                return values
    return None


def build_schema_contract(schema_json: dict, *, example: BaseModel | None = None, rules: str | None = None) -> str:
    """Compact schema summary + optional worked example + optional gate-rule text.

    Spliced into ainvoke_structured's tail message AFTER the raw JSON Schema dump it already
    sends (augments, never replaces -- the raw dump stays the source of truth a validator can
    check against; this is just a compact, easier-to-scan walk on top of it). The field/enum
    summary is walked purely from schema_json["properties"]'s own `description`/`enum` keys, so it
    cannot drift from the schema by construction -- there is nothing here for a future schema
    change to leave stale.
    """
    defs = schema_json.get("$defs", {})
    lines = ["Field summary:"]
    for name, prop in schema_json.get("properties", {}).items():
        line = f"- {name} ({_type_label(prop, defs)})"
        description = prop.get("description")
        if description:
            line += f": {description}"
        enum_values = _enum_values(prop, defs)
        if enum_values:
            line += f" [allowed: {', '.join(str(v) for v in enum_values)}]"
        lines.append(line)

    sections = ["\n".join(lines)]
    if example is not None:
        sections.append("Worked example:\n" + example.model_dump_json(indent=2))
    if rules:
        sections.append(rules)
    return "\n\n".join(sections)


async def ainvoke_structured(
    model: BaseChatModel,
    messages: list[BaseMessage],
    schema: type[SchemaT],
    *,
    max_attempts: int = 3,
    example: BaseModel | None = None,
    rules: str | None = None,
) -> SchemaT:
    """Invoke the model and parse its response as the given Pydantic schema.

    Provider-agnostic structured-output mechanism: works with any BaseChatModel (Claude or
    Copilot). Drives structured output via explicit JSON-schema prompting plus a validate-and-
    retry loop, since neither provider's CLI has a native structured-output/tool-calling contract
    that both satisfy.

    The retry loop itself needs a live model, but the regex/formatting is pure and testable (see
    _demo below).

    `example`/`rules` (both default None -- zero behavior change for every existing call site) add
    a compact schema summary + worked example + gate-rule text (build_schema_contract) to the
    FIRST attempt's instruction message only. Token-cost note: the naive version of this feature
    would re-append that full block on every retry, since request_messages only ever grows -- with
    a real worked example (diagrams/wireframes for `plan`) and a stage with a high max_attempts,
    that's a lot of resent tokens for no benefit, since the block is already earlier in the same
    conversation the model can see. So it is built into attempt 1's message once and never
    repeated; a retry only gets a one-line pointer back to it (see below), never a re-paste.
    """
    schema_dict = schema.model_json_schema()
    instruction = _STRUCTURED_OUTPUT_INSTRUCTION.format(schema=json.dumps(schema_dict))
    if example is not None or rules:
        instruction += "\n\n" + build_schema_contract(schema_dict, example=example, rules=rules)
    request_messages = [*messages, HumanMessage(content=instruction)]

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
            retry_content = (
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
            if example is not None:
                # A one-line pointer back to attempt 1's message, not a re-paste of the worked
                # example itself -- see this function's own docstring for why.
                retry_content += "\n\nRe-check against the worked example above for this field's exact shape."
            request_messages.append(HumanMessage(content=retry_content))
    assert last_error is not None
    raise last_error


def assert_example_matches_schema(example: BaseModel, schema: type[BaseModel]) -> None:
    """Generic, reusable self-check for one worked example: it must both validate against
    `schema` and round-trip through JSON to the exact same canonical instance.

    "Validates" alone isn't proof an example uses today's canonical shape -- a model with its own
    coercing validators (e.g. PresenceList's legacy bare-list acceptance, schemas.py) can silently
    accept a stale-shaped example and coerce it, masking staleness. Round-tripping through
    model_dump_json/model_validate_json and comparing equality is the same technique schemas.py's
    own self-check already uses for TECH_STACK_DRAFT_EXAMPLE/TECH_STACK_EXTRACT_EXAMPLE; this is
    the generic version of it, callable by later tasks (7/8/13) as they add their own per-stage
    draft_example/audit_example content -- this task wires no example content of its own.
    """
    assert isinstance(example, schema), f"{example!r} is not an instance of {schema.__name__}"
    reloaded = schema.model_validate_json(example.model_dump_json())
    assert reloaded == example, f"{example!r} did not round-trip through model_validate_json"


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

    # build_schema_contract: field/enum summary, covering every shape _type_label/_enum_values/
    # _first_enum_field need to tell apart -- a direct Literal (inline enum, confirmed against this
    # codebase's real `auth_kind` field), a named Enum type ($ref to a $defs entry that itself
    # carries the enum), a plain nested object with NO enum of its own (must NOT show "[allowed:"),
    # a nested object whose OWN sub-field is enum-shaped under a name OTHER than "status" (proves
    # _first_enum_field isn't hardcoded to that one field name), an Optional nested object (anyOf +
    # null), and an array of nested objects.
    from enum import Enum
    from typing import Literal as _Literal

    from pydantic import Field

    class _DemoEnum(str, Enum):
        A = "a"
        B = "b"

    class _DemoNested(BaseModel):
        x: int = 0

    class _DemoWrapper(BaseModel):
        # Deliberately NOT named "status" -- _first_enum_field must find whichever field is
        # enum-shaped, not just one hardcoded name.
        mode: _Literal["alpha", "beta"] = "alpha"

    class _DemoSchema(BaseModel):
        name: str = Field(description="a name")
        kind: _Literal["one", "two"] = Field(description="a literal kind")
        tag: _DemoEnum = Field(description="a named enum")
        nested: _DemoNested = Field(description="a nested object")
        wrapped: _DemoWrapper = Field(description="a nested object with its own enum field")
        maybe_nested: _DemoNested | None = Field(default=None, description="optional nested")
        items: list[_DemoNested] = Field(default_factory=list, description="a list")

    demo_example = _DemoSchema(
        name="n", kind="one", tag=_DemoEnum.A, nested=_DemoNested(x=1),
        wrapped=_DemoWrapper(), items=[_DemoNested(x=2)],
    )
    contract = build_schema_contract(
        _DemoSchema.model_json_schema(), example=demo_example, rules="Rule: always do X."
    )
    lines = contract.splitlines()
    assert any(line.startswith("- name (string): a name") for line in lines), contract
    assert any(
        line.startswith("- kind (string): a literal kind") and "[allowed: one, two]" in line for line in lines
    ), contract
    assert any(
        line.startswith("- tag (") and "a named enum" in line and "[allowed: a, b]" in line for line in lines
    ), contract
    assert any(
        line.startswith("- nested (") and "a nested object" in line and "[allowed:" not in line and "—" not in line
        for line in lines
    ), "a plain nested object with no enum of its own must show neither [allowed:] nor a — hint"
    assert any(
        line.startswith("- wrapped (_DemoWrapper — mode: alpha|beta)") for line in lines
    ), "a nested object's OWN non-'status'-named enum field must surface in the type label"
    assert any(line.startswith("- maybe_nested (") and " | null" in line for line in lines), contract
    assert any(line.startswith("- items (array[") for line in lines), contract
    assert "Worked example:" in contract and '"name": "n"' in contract, contract
    assert contract.endswith("Rule: always do X."), contract

    # Same nested-wrapper-enum case, against a REAL schema from this codebase (not just the
    # synthetic one above) -- TechStack's `languages` field is a PresenceList (schemas.py), whose
    # own `status` field is the enum a model most needs to see right the first time.
    from .schemas import TechStack

    real_contract = build_schema_contract(TechStack.model_json_schema())
    assert (
        "- languages (PresenceList — status: present|absent): Programming languages found evidence for."
        in real_contract
    ), real_contract

    # No example/rules given -> build_schema_contract renders no worked example/rules section
    # (still just the field summary) -- exercised directly since ainvoke_structured only calls it
    # at all when at least one of example/rules is truthy.
    bare_contract = build_schema_contract(_DemoSchema.model_json_schema())
    assert "Worked example:" not in bare_contract and "Rule: always do X." not in bare_contract

    # assert_example_matches_schema: proves the CHECKING LOGIC itself, both ways --
    # a genuine Task 2 example passes (reusing what schemas.py's own self-check already trusts,
    # not duplicating its field-specific assertions), and a mismatched type is actually rejected
    # rather than silently accepted.
    from .schemas import TECH_STACK_DRAFT_EXAMPLE, TechStackDraftResponse

    assert_example_matches_schema(TECH_STACK_DRAFT_EXAMPLE, TechStackDraftResponse)
    rejected = False
    wrong_type_example = _DemoSchema(
        kind="one", tag=_DemoEnum.A, nested=_DemoNested(), wrapped=_DemoWrapper(), name="n"
    )
    try:
        assert_example_matches_schema(wrong_type_example, TechStackDraftResponse)  # type: ignore[arg-type]
    except AssertionError:
        rejected = True
    assert rejected, "assert_example_matches_schema must reject an example of the wrong type"

    # Token-cost design (the trickiest part of Task 6): the full contract block must be sent on
    # attempt 1 only. A naive version would re-append build_schema_contract's full text into every
    # retry message too -- this drives ainvoke_structured's real retry loop against a fake model
    # (attempt 1 rejected, attempt 2 accepted) and inspects exactly what was sent each time.
    import asyncio

    from langchain_core.messages import AIMessage

    class _FakeModel:
        def __init__(self, replies: list[str]) -> None:
            self._replies = list(replies)
            self.calls: list[list[BaseMessage]] = []

        async def ainvoke(self, messages: list[BaseMessage], config: dict | None = None) -> AIMessage:  # noqa: ARG002
            self.calls.append(list(messages))
            return AIMessage(content=self._replies.pop(0))

    class _Answer(BaseModel):
        value: int

    fake_model = _FakeModel(replies=["not json", '{"value": 1}'])
    result = asyncio.run(
        ainvoke_structured(
            fake_model,  # type: ignore[arg-type]
            [],
            _Answer,
            max_attempts=3,
            example=_Answer(value=42),
            rules="Rule: be terse.",
        )
    )
    assert result.value == 1
    assert len(fake_model.calls) == 2, "should succeed on the 2nd attempt, not exhaust all 3"

    first_call_text = "\n".join(str(m.content) for m in fake_model.calls[0])
    assert first_call_text.count("Field summary:") == 1, "attempt 1 must carry the full contract block"
    assert first_call_text.count("Worked example:") == 1

    second_call_text = "\n".join(str(m.content) for m in fake_model.calls[1])
    # The full block is still present in the CONVERSATION HISTORY (it was never removed), but it
    # must appear exactly once -- the retry must not have appended a second copy alongside it.
    assert second_call_text.count("Field summary:") == 1, "retry re-sent the full contract block -- token-cost bug"
    assert second_call_text.count("Worked example:") == 1, "retry re-sent the worked example -- token-cost bug"
    assert "Re-check against the worked example above" in second_call_text

    print("structured_output self-check: all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose (same pattern as all other modules).
    from src.structured_output import _demo as _packaged_demo

    _packaged_demo()
