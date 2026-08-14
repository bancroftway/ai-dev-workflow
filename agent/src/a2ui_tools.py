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
ADVERSARIAL_AUDIT_SURFACE_ID = "adversarial-audit"
DEDUP_SURFACE_ID = "dedup-simplify"
LICENSE_AUDIT_SURFACE_ID = "license-audit"
EXIT_SURFACE_ID = "exit"
BROWNFIELD_BASELINE_SURFACE_ID = "brownfield-baseline"
APP_DISCOVERY_SURFACE_ID = "app-discovery"


def _build_generic_envelope(surface_id: str, component_name: str, data_field: str, data: dict, audit_findings: list[str] | None = None) -> dict:
    """Every surface's envelope. All twelve differ only in the surface id, the component name, and
    the name of the single data field -- which is exactly this function's parameter list, so each
    builder below is one line. `_demo()` pins the shape against a hand-built copy of what the
    expanded versions used to produce."""
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


def build_brownfield_baseline_envelope(baseline: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(BROWNFIELD_BASELINE_SURFACE_ID, "BrownfieldBaselineSurface", "baseline", baseline, audit_findings)


def build_app_discovery_envelope(report: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(APP_DISCOVERY_SURFACE_ID, "AppDiscoverySurface", "report", report, audit_findings)



def build_specification_envelope(specification: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(SPECIFICATION_SURFACE_ID, "SpecificationSurface", "specification", specification, audit_findings)


def build_plan_envelope(plan: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(PLAN_SURFACE_ID, "PlanSurface", "plan", plan, audit_findings)


def build_tech_stack_envelope(tech_stack: dict, audit_findings: list[str] | None = None) -> dict:
    """No frontend renderer is registered for this surface yet (tech-stack has no visible tab,
    per the pipeline's own design -- requires_human_gate=False on its StageSpec) -- emitted for
    parity with every other stage's audit node and so a future session-overview panel can read
    it, but harmless/inert until a catalog entry exists client-side."""
    return _build_generic_envelope(TECH_STACK_SURFACE_ID, "TechStackSurface", "tech_stack", tech_stack, audit_findings)


def build_raw_requirements_envelope(raw_requirements: dict, audit_findings: list[str] | None = None) -> dict:
    return _build_generic_envelope(
        RAW_REQUIREMENTS_SURFACE_ID, "RawRequirementsSurface", "raw_requirements", raw_requirements, audit_findings
    )


def build_ac_to_tests_envelope(test_suite: dict, audit_findings: list[str] | None = None) -> dict:
    """No frontend renderer registered yet (P4 has requires_human_gate=False, no tab of its own,
    per the pipeline diagram's own design) -- emitted for parity with every other stage's audit/
    verify node, same rationale as build_tech_stack_envelope."""
    return _build_generic_envelope(AC_TO_TESTS_SURFACE_ID, "AcToTestsSurface", "test_suite", test_suite, audit_findings)


def build_minimal_code_to_green_envelope(iteration: dict, audit_findings: list[str] | None = None) -> dict:
    """No frontend renderer registered yet -- P6 has requires_human_gate=True per the pipeline
    diagram, so this *will* eventually need a real tab, unlike P4/tech-stack's inert parity-only
    envelopes; not yet built here since the frontend redesign work hasn't started."""
    return _build_generic_envelope(
        MINIMAL_CODE_TO_GREEN_SURFACE_ID, "MinimalCodeToGreenSurface", "iteration", iteration, audit_findings
    )


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


def _demo() -> None:  # pragma: no cover -- `cd agent && uv run python -m src.a2ui_tools`
    """Pins the wire format the frontend reads.

    Every builder above used to be this expression written out by hand. Rebuilding it here and
    asserting equality is what makes collapsing them onto `_build_generic_envelope` safe: if the
    payload shape ever drifts from what `src/a2ui/catalog.tsx` expects, this fails instead of the
    surface silently rendering nothing.
    """

    def expanded(surface_id: str, component_name: str, data_field: str, data: dict, audit_findings: list[str]) -> dict:
        return {
            A2UI_OPERATIONS_KEY: [
                create_surface(surface_id, CATALOG_ID),
                update_components(
                    surface_id,
                    [
                        {
                            "id": "root",
                            "component": component_name,
                            data_field: {"path": f"/{data_field}"},
                            "audit_findings": {"path": "/audit_findings"},
                        }
                    ],
                ),
                update_data_model(surface_id, {data_field: data, "audit_findings": audit_findings}),
            ]
        }

    spec = {"title": "T", "summary": "S", "user_stories": [], "assumptions": [], "out_of_scope": []}
    findings = ["a finding"]
    assert build_specification_envelope(spec, findings) == expanded(
        "specification", "SpecificationSurface", "specification", spec, findings
    )

    plan = {"overview": "O", "plan_steps": [], "risk_notes": []}
    assert build_plan_envelope(plan, findings) == expanded("plan", "PlanSurface", "plan", plan, findings)

    # Omitted audit_findings must serialize as [], never null -- the catalog's Zod props declare
    # `audit_findings: z.array(z.string())`, so a null would fail validation client-side.
    assert build_plan_envelope(plan) == expanded("plan", "PlanSurface", "plan", plan, [])

    # The surface ids the frontend pins in src/lib/a2ui-surface-ids.ts.
    assert (SPECIFICATION_SURFACE_ID, PLAN_SURFACE_ID, CATALOG_ID) == (
        "specification", "plan", "urn:ai-dev-workflow:catalog",
    )

    # Every builder is reachable and produces the same three-operation envelope.
    builders = [
        (build_tech_stack_envelope, "tech-stack", "TechStackSurface", "tech_stack"),
        (build_raw_requirements_envelope, "raw-requirements", "RawRequirementsSurface", "raw_requirements"),
        (build_ac_to_tests_envelope, "ac-to-tests", "AcToTestsSurface", "test_suite"),
        (build_minimal_code_to_green_envelope, "minimal-code-to-green", "MinimalCodeToGreenSurface", "iteration"),
        (build_adversarial_audit_envelope, "adversarial-audit", "AdversarialAuditSurface", "report"),
        (build_dedup_envelope, "dedup-simplify", "DedupSurface", "result"),
        (build_license_audit_envelope, "license-audit", "LicenseAuditSurface", "report"),
        (build_exit_envelope, "exit", "ExitSurface", "report"),
        (build_brownfield_baseline_envelope, "brownfield-baseline", "BrownfieldBaselineSurface", "baseline"),
        (build_app_discovery_envelope, "app-discovery", "AppDiscoverySurface", "report"),
    ]
    payload = {"k": "v"}
    for build, surface_id, component, field in builders:
        assert build(payload, findings) == expanded(surface_id, component, field, payload, findings), surface_id

    messages = present_surface_messages("present_specification", build_specification_envelope(spec))
    assert len(messages) == 2
    assert messages[0].tool_calls[0]["id"] == messages[1].tool_call_id, "tool call and result must pair up"

    print("a2ui_tools self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
