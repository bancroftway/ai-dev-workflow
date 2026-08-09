"""Fixed-schema A2UI emission for the Specification/Plan review surfaces.

Follows the ag-ui-a2ui-integration skill's "Fixed Schema Mode": the app owns
the component tree (one custom root component per surface whose Zod props
schema on the client mirrors SPECIFICATION.md Section 4 exactly), and only
the data changes per draft. The envelope is carried back to the client as a
normal AG-UI tool-call result, which CopilotRuntime's A2UI detection (enabled
by the client forwarding a catalog, see the frontend `a2ui/catalog.tsx`)
turns into `ACTIVITY_SNAPSHOT` / `a2ui-surface` events.

CATALOG_ID must match the `catalogId` the frontend catalog registers
(`src/a2ui/catalog.tsx`) — `createCatalog`'s default is a generated,
unstable URI, so both sides pin the same explicit id here.
"""

from __future__ import annotations

import json
import uuid

from ag_ui_a2ui_toolkit import (
    A2UI_OPERATIONS_KEY,
    create_surface,
    update_components,
    update_data_model,
)
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

CATALOG_ID = "urn:ai-dev-workflow:catalog"

SPECIFICATION_SURFACE_ID = "specification"
PLAN_SURFACE_ID = "plan"


def build_specification_envelope(specification: dict) -> dict:
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(SPECIFICATION_SURFACE_ID, CATALOG_ID),
            update_components(
                SPECIFICATION_SURFACE_ID,
                [{"id": "root", "component": "SpecificationSurface", "specification": {"path": "/"}}],
            ),
            update_data_model(SPECIFICATION_SURFACE_ID, specification),
        ]
    }


def build_plan_envelope(plan: dict) -> dict:
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(PLAN_SURFACE_ID, CATALOG_ID),
            update_components(
                PLAN_SURFACE_ID,
                [{"id": "root", "component": "PlanSurface", "plan": {"path": "/"}}],
            ),
            update_data_model(PLAN_SURFACE_ID, plan),
        ]
    }


def present_surface_messages(tool_name: str, envelope: dict) -> list[BaseMessage]:
    """Build a synthetic AIMessage(tool_call) + ToolMessage(result) pair.

    ag_ui_langgraph streams these as ordinary AG-UI tool-call events; no
    actual LLM tool-calling round-trip is needed since we already have the
    deterministic structured content in hand.
    """
    call_id = f"call_{uuid.uuid4().hex[:24]}"
    ai_message = AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": call_id}])
    tool_message = ToolMessage(content=json.dumps(envelope), tool_call_id=call_id, name=tool_name)
    return [ai_message, tool_message]
