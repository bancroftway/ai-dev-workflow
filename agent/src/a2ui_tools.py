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
TECH_STACK_SURFACE_ID = "tech-stack"
RAW_REQUIREMENTS_SURFACE_ID = "raw-requirements"
AC_TO_TESTS_SURFACE_ID = "ac-to-tests"
MINIMAL_CODE_TO_GREEN_SURFACE_ID = "minimal-code-to-green"
ADVERSARIAL_AUDIT_SURFACE_ID = "p11a-adversarial-audit"
DEDUP_SURFACE_ID = "p11b-dedup"
LICENSE_AUDIT_SURFACE_ID = "p11d-license-audit"
EXIT_SURFACE_ID = "p15-exit"
P0_BASELINE_SURFACE_ID = "p0-brownfield"


def _build_generic_envelope(surface_id: str, component_name: str, data_field: str, data: dict, audit_findings: list[str] | None = None) -> dict:
    """Shared by P11a/b/d's inert (no frontend renderer yet) envelopes -- same parity-only
    rationale as build_tech_stack_envelope/build_ac_to_tests_envelope."""
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(surface_id, CATALOG_ID),
            update_components(surface_id, [{"id": "root", "component": component_name, data_field: {"path": f"/{data_field}"}, "audit_findings": {"path": "/audit_findings"}}]),
            update_data_model(surface_id, {data_field: data, "audit_findings": audit_findings or []}),
        ]
    }


def build_adversarial_audit_envelope(report: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(ADVERSARIAL_AUDIT_SURFACE_ID, "AdversarialAuditSurface", "report", report, audit_findings)


def build_dedup_envelope(result: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(DEDUP_SURFACE_ID, "DedupSurface", "result", result, audit_findings)


def build_license_audit_envelope(report: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(LICENSE_AUDIT_SURFACE_ID, "LicenseAuditSurface", "report", report, audit_findings)


def build_exit_envelope(report: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(EXIT_SURFACE_ID, "ExitSurface", "report", report, audit_findings)


def build_p0_baseline_envelope(baseline: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(P0_BASELINE_SURFACE_ID, "P0BaselineSurface", "baseline", baseline, audit_findings)



def build_specification_envelope(specification: dict, audit_findings: list[str] | None = None) -> dict:
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(SPECIFICATION_SURFACE_ID, CATALOG_ID),
            update_components(
                SPECIFICATION_SURFACE_ID,
                [
                    {
                        "id": "root",
                        "component": "SpecificationSurface",
                        "specification": {"path": "/specification"},
                        "audit_findings": {"path": "/audit_findings"},
                    }
                ],
            ),
            update_data_model(
                SPECIFICATION_SURFACE_ID,
                {"specification": specification, "audit_findings": audit_findings or []},
            ),
        ]
    }


def build_plan_envelope(plan: dict, audit_findings: list[str] | None = None) -> dict:
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(PLAN_SURFACE_ID, CATALOG_ID),
            update_components(
                PLAN_SURFACE_ID,
                [
                    {
                        "id": "root",
                        "component": "PlanSurface",
                        "plan": {"path": "/plan"},
                        "audit_findings": {"path": "/audit_findings"},
                    }
                ],
            ),
            update_data_model(
                PLAN_SURFACE_ID, {"plan": plan, "audit_findings": audit_findings or []}
            ),
        ]
    }


def build_tech_stack_envelope(tech_stack: dict, audit_findings: list[str] | None = None) -> dict:
    """No frontend renderer is registered for this surface yet (tech-stack has no visible tab,
    per the pipeline's own design -- requires_human_gate=False on its StageSpec) -- emitted for
    parity with every other stage's audit node and so a future session-overview panel can read
    it, but harmless/inert until a catalog entry exists client-side."""
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(TECH_STACK_SURFACE_ID, CATALOG_ID),
            update_components(
                TECH_STACK_SURFACE_ID,
                [
                    {
                        "id": "root",
                        "component": "TechStackSurface",
                        "tech_stack": {"path": "/tech_stack"},
                        "audit_findings": {"path": "/audit_findings"},
                    }
                ],
            ),
            update_data_model(
                TECH_STACK_SURFACE_ID, {"tech_stack": tech_stack, "audit_findings": audit_findings or []}
            ),
        ]
    }


def build_raw_requirements_envelope(raw_requirements: dict, audit_findings: list[str] | None = None) -> dict:
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(RAW_REQUIREMENTS_SURFACE_ID, CATALOG_ID),
            update_components(
                RAW_REQUIREMENTS_SURFACE_ID,
                [
                    {
                        "id": "root",
                        "component": "RawRequirementsSurface",
                        "raw_requirements": {"path": "/raw_requirements"},
                        "audit_findings": {"path": "/audit_findings"},
                    }
                ],
            ),
            update_data_model(
                RAW_REQUIREMENTS_SURFACE_ID,
                {"raw_requirements": raw_requirements, "audit_findings": audit_findings or []},
            ),
        ]
    }


def build_ac_to_tests_envelope(test_suite: dict, audit_findings: list[str] | None = None) -> dict:
    """No frontend renderer registered yet (P4 has requires_human_gate=False, no tab of its own,
    per the pipeline diagram's own design) -- emitted for parity with every other stage's audit/
    verify node, same rationale as build_tech_stack_envelope."""
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(AC_TO_TESTS_SURFACE_ID, CATALOG_ID),
            update_components(
                AC_TO_TESTS_SURFACE_ID,
                [
                    {
                        "id": "root",
                        "component": "AcToTestsSurface",
                        "test_suite": {"path": "/test_suite"},
                        "audit_findings": {"path": "/audit_findings"},
                    }
                ],
            ),
            update_data_model(
                AC_TO_TESTS_SURFACE_ID, {"test_suite": test_suite, "audit_findings": audit_findings or []}
            ),
        ]
    }


def build_minimal_code_to_green_envelope(iteration: dict, audit_findings: list[str] | None = None) -> dict:
    """No frontend renderer registered yet -- P6 has requires_human_gate=True per the pipeline
    diagram, so this *will* eventually need a real tab, unlike P4/tech-stack's inert parity-only
    envelopes; not yet built here since the frontend redesign work hasn't started."""
    return {
        A2UI_OPERATIONS_KEY: [
            create_surface(MINIMAL_CODE_TO_GREEN_SURFACE_ID, CATALOG_ID),
            update_components(
                MINIMAL_CODE_TO_GREEN_SURFACE_ID,
                [
                    {
                        "id": "root",
                        "component": "MinimalCodeToGreenSurface",
                        "iteration": {"path": "/iteration"},
                        "audit_findings": {"path": "/audit_findings"},
                    }
                ],
            ),
            update_data_model(
                MINIMAL_CODE_TO_GREEN_SURFACE_ID, {"iteration": iteration, "audit_findings": audit_findings or []}
            ),
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
