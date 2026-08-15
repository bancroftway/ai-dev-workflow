"""The LangGraph workflow graph (SPECIFICATION.md Section 3.2, Section 5).

Built from a data-driven `STAGES` list (Decision 6 / BR-7 extensibility):
appending a future third stage means adding one more `StageSpec` entry, not
restructuring the nodes/edges below. Every stage gets the same generated
graph segment: draft -> (gate | needs_clarification | auto_approve) -> next
stage's draft (or END for the last stage).

Every run (initial submission or any later revision) enters at `intake` and
unconditionally proceeds to the Specification stage's draft node (AC-6.2),
regardless of which stage/gate a prior run left paused at — a fresh
`.invoke()`/`.astream()` on the same thread simply starts a new super-step
from the entry point; any interrupt left open from a previous run is never
resumed and is abandoned by construction (BR-4's cascade).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt

from . import app_discovery
from . import approvals
from . import config as workflow_config
from . import git_ops
from . import model_config
from . import preflight_nodes
from . import repo_files
from . import repo_scan
from . import requirements_nodes
from . import finding_cluster_nodes
from . import test_hardening_nodes
from . import metrics_nodes
from . import exit_nodes
from . import rebuild
from . import spec_ledger
from . import telemetry
from . import workflow_persistence
from .gates import audit_gates
from .quality_security import quality_nodes, security_nodes
from .gates.diagram_gate import verify_plan_diagrams
from .gates.test_coverage_gate import verify_coverage
from .gates.write_scope_gate import pre_tool_use_write_scope_hook, verify_ac_to_tests
from .a2ui_tools import (
    build_ac_to_tests_envelope,
    build_adversarial_audit_envelope,
    build_app_discovery_envelope,
    build_dedup_envelope,
    build_exit_envelope,
    build_license_audit_envelope,
    build_minimal_code_to_green_envelope,
    build_brownfield_baseline_envelope,
    build_plan_envelope,
    build_raw_requirements_envelope,
    build_specification_envelope,
    build_tech_stack_envelope,
    present_surface_messages,
)
from .copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from .markdown_render import (
    render_ac_to_tests_markdown,
    render_adversarial_audit_markdown,
    render_app_discovery_markdown,
    render_dedup_markdown,
    render_exit_markdown,
    render_license_audit_markdown,
    render_minimal_code_to_green_markdown,
    render_brownfield_baseline_markdown,
    render_plan_markdown,
    render_raw_requirements_markdown,
    render_specification_markdown,
    render_tech_stack_markdown,
)
from .prompt_loader import load_prompt
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider
from .sandbox.provider import SandboxProvider
from .schemas import (
    PlanAuditResponse,
    PlanDraftResponse,
    RawRequirementsAuditResponse,
    RawRequirementsDraftResponse,
    SpecificationAuditResponse,
    SpecificationDraftResponse,
    TechStackAuditResponse,
    TechStackDraftResponse,
)
from .schemas_codegen import (
    AcceptanceCriteriaTestsAuditResponse,
    AcceptanceCriteriaTestsDraftResponse,
    MinimalCodeToGreenAuditResponse,
    MinimalCodeToGreenDraftResponse,
)
from .schemas_audit import (
    AdversarialAuditAuditResponse,
    AdversarialAuditDraftResponse,
    DedupAuditResponse,
    DedupDraftResponse,
    LicenseAuditAuditResponse,
    LicenseAuditDraftResponse,
)
from .schemas_app_discovery import AppDiscoveryAuditResponse, AppDiscoveryDraftResponse
from .schemas_brownfield import BrownfieldBaselineAuditResponse, BrownfieldBaselineDraftResponse
from .schemas_exit import ExitAuditResponse, ExitDraftResponse

logger = logging.getLogger(__name__)

StageStatus = Literal[
    "not_started", "drafting", "needs_clarification", "ready_for_review", "approved"
]


class StageState(TypedDict):
    status: StageStatus
    draft: dict[str, Any] | None
    clarifying_questions: list[dict[str, Any]]
    readiness: bool
    cycle_count: int
    approved_content: dict[str, Any] | None
    ever_ready_for_review: bool
    used_ids: list[str]
    audit_findings: list[str]
    # Independent of cycle_count (the LLM's own clarification-loop counter) -- tracks retries
    # through a StageSpec.deterministic_verify gate, when one is set (unused by specification/plan).
    verify_cycle_count: int
    last_verification: dict[str, Any] | None
    # Set once per run by make_draft_node when StageSpec.capture_baseline_commit is True (P4's
    # write-scope gate); None for every other stage.
    baseline_commit: str | None


class GraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # Minted fresh by intake_node on every genuinely new run (module docstring's definition of a
    # "run" -- not regenerated across a gate-approval resume, since those don't re-enter intake).
    # Used as the ledger/approvals systems' run identifier (spec_ledger.py's
    # first_seen_run_id/last_revised_run_id, approvals.py's ApprovalRecord.run_id) and, later,
    # metrics-report/exit's history/<run_id>-*.json snapshot naming.
    run_id: str
    # Set by scaffold_node (preflight_nodes.py) -- manifest.json absence is the canonical
    # "never onboarded before" signal, routing into brownfield-baseline's brownfield sub-flow. Read once at
    # scaffold time and routed on from state, never re-read: app discovery writes to manifest.json
    # mid-run, so a fresh read at the branch point would always report "onboarded".
    manifest_exists: bool
    # `git rev-parse HEAD` captured by scaffold_node before this run writes anything -- the
    # reference point app_discovery's reject path resets back to, so a rejected repository is left
    # exactly as it arrived.
    run_baseline_commit: str | None
    # app_discovery.py's deterministic scan output (candidates/evidence/fingerprint), grounding the
    # discovery stage's prompt and bounding which paths its report may cite.
    app_scan: dict[str, Any]
    # Set only when the repository has no runnable application: the one hard stop in this graph.
    # Read by _route_after_app_discovery and by the frontend's rejection banner.
    app_rejection: dict[str, Any] | None
    # Deterministic schema/migration/route grep, grounding brownfield-baseline brownfield's draft prompt.
    brownfield_context: str
    raw_requirements_text: str
    # Non-text InputContent parts (screenshots/documents) from the latest submission's
    # HumanMessage, if any -- only ever consumed by the specification stage's draft prompt
    # (BR-2: the plan stage's input is the approved Specification, never raw attachments).
    requirements_attachments: list[dict[str, Any]]
    stages: dict[str, StageState]
    # Keyed by RebuildSpec.key (rebuild.py) -- each R placement's own RebuildState. Accessed via
    # .get("rebuild") or {} everywhere (rebuild.py's nodes tolerate it being absent on a thread
    # that predates R), so this key is genuinely optional at the TypedDict level in practice.
    rebuild: dict[str, Any]
    # quality-remediation/security-remediation's own bespoke-cluster state (quality_security/quality_nodes.py, security_nodes.py) -- not a
    # StageState, since neither is a StageSpec. Same absent-is-tolerated pattern as `rebuild` above.
    quality_remediation: dict[str, Any]
    security_remediation: dict[str, Any]
    # audit-cluster's own cross-substage scratch state (jscpd/license-scan reports for prompt grounding,
    # exit-gate attempt tracking) -- see agent/src/gates/audit_gates.py.
    audit_cluster: dict[str, Any]
    # finding-cluster's dependency-upgrade bespoke cluster state -- see agent/src/finding_cluster_nodes.py.
    finding_cluster: dict[str, Any]
    # test-hardening's own bespoke-cluster state -- see agent/src/test_hardening_nodes.py.
    test_hardening: dict[str, Any]
    # metrics-report's own metrics state -- see agent/src/metrics_nodes.py.
    metrics_report: dict[str, Any]
    # The baseline repo scan taken once at the top of the graph -- see agent/src/repo_scan.py.
    repo_scan: dict[str, Any]
    # Outcome of the most recent push to the ai-dev-workflow/<branch> work branch
    # (git_ops.push_head): {ok, error, at}. Streamed so the frontend can warn when GitHub
    # persistence is failing (e.g. the user lacks push permission) instead of silently lying.
    last_push: dict[str, Any] | None


def default_stage_state() -> StageState:
    return {
        "status": "not_started",
        "draft": None,
        "clarifying_questions": [],
        "readiness": False,
        "cycle_count": 0,
        "approved_content": None,
        "ever_ready_for_review": False,
        "used_ids": [],
        "audit_findings": [],
        "verify_cycle_count": 0,
        "last_verification": None,
        "baseline_commit": None,
    }


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of a StageSpec.deterministic_verify check -- never LLM self-attestation, always a
    real script/parse. `feedback` is injected as extra prompt context on the next draft retry;
    `report` is the full structured detail, persisted and surfaced to the frontend even though it
    wasn't produced by an LLM call."""

    passed: bool
    feedback: str
    report: dict[str, Any]


def _extract_ids(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, val in value.items():
            if key == "id" and isinstance(val, str):
                out.add(val)
            else:
                _extract_ids(val, out)
    elif isinstance(value, list):
        for item in value:
            _extract_ids(item, out)


SPEC_SYSTEM_PROMPT = load_prompt("specification_draft")

PLAN_SYSTEM_PROMPT = load_prompt("plan_draft")


def _build_specification_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    requirements_text = f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"
    attachments = state.get("requirements_attachments") or []
    # Attachments (screenshots/documents) ride alongside the text as a multimodal content list
    # so copilot_chat_model.py's translator can forward them to the model as real attachments,
    # not just note their existence -- a plain string content here would lose them entirely.
    requirements_content: str | list[dict[str, Any]] = (
        [{"type": "text", "text": requirements_text}, *attachments] if attachments else requirements_text
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=SPEC_SYSTEM_PROMPT),
        HumanMessage(content=requirements_content),
    ]
    if stage["draft"] is not None:
        messages.append(
            HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}")
        )
    if stage["used_ids"]:
        messages.append(
            HumanMessage(content=f"Identifiers already used at some point, never reuse: {stage['used_ids']}")
        )
    return messages


def _build_plan_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    messages: list[BaseMessage] = [
        SystemMessage(content=PLAN_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
    ]
    if _tech_stack_has_ui_framework(state):
        messages.append(HumanMessage(content=IMPECCABLE_PLAN_SEGMENT))
    if plan_stage["draft"] is not None:
        messages.append(
            HumanMessage(content=f"Your immediately-prior draft (JSON):\n{plan_stage['draft']}")
        )
    if plan_stage["used_ids"]:
        messages.append(
            HumanMessage(content=f"Identifiers already used at some point, never reuse: {plan_stage['used_ids']}")
        )
    return messages


_UI_FRAMEWORK_MARKERS = ("react", "vue", "angular", "blazor", "svelte", "next", "nuxt", "flutter", "swiftui", "jetpack compose")


def _tech_stack_has_ui_framework(state: GraphState) -> bool:
    """Shared signal for P3's wireframe requirement and P4's Playwright MCP -- both the plan's own
    wording ("for UI-based applications"/"UI-relevant ACs") gate on whether this repo has a UI
    framework at all, using brownfield-baseline's own TechStack.frameworks report (already available before either
    stage's draft runs, unlike a per-diagram/per-AC ui_relevant flag the model hasn't produced
    yet at session_options time -- a real, deliberate simplification from "only for UI-relevant
    content specifically" to "only for UI-framework repos at all").
    """
    tech_stack = (state.get("stages") or {}).get("tech-stack", {}).get("approved_content") or {}
    frameworks = [str(f).lower() for f in (tech_stack.get("frameworks") or [])]
    return any(marker in fw for fw in frameworks for marker in _UI_FRAMEWORK_MARKERS)


# Impeccable (vendored design skill, plugins/vendor/pbakaus-impeccable) -- per-stage prompt
# segments appended ONLY for UI-framework repos (same _tech_stack_has_ui_framework gate as the
# Playwright/Excalidraw MCPs), so non-UI runs never spend context on design guidance. Each segment
# is scoped to what that stage's session can actually execute: P3's read-only allowlist has no
# bash, so it gets methodology only; P6/dedup-simplify run autopilot and may run the skill's node scripts;
# adversarial-audit's default plan-mode session gets the LLM-only critique, never the detector scripts.
_IMPECCABLE_SKILL_DIR = (
    f"{workflow_config.COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/pbakaus-impeccable/impeccable/skills/impeccable"
)

IMPECCABLE_PLAN_SEGMENT = (
    "This repository has a UI framework. Before drafting any UI-touching plan steps, load the"
    " `impeccable` skill and read its `reference/shape.md` -- apply that shape methodology (plan"
    " UX/UI decisions before code) to the UI portions of the plan. Product context is the approved"
    " Specification above; do NOT run any impeccable scripts and do NOT run `init` -- this session"
    " has no shell, and the methodology is what matters here."
)

IMPECCABLE_CODEGEN_SEGMENT = (
    "This repository has a UI framework. Before writing UI code: (1) if PRODUCT.md is missing at"
    " the repo root, create it from the approved Specification above (product truth only -- never"
    " run the interactive `impeccable init` interview); (2) if DESIGN.md is missing, load the"
    " `impeccable` skill and follow its `document` command to capture the existing visual system."
    " While implementing UI code, follow the impeccable skill's design rules (its SKILL.md general"
    " rules and craft-floor reference: contrast, typography, layout, motion, absolute bans),"
    " product/Operate register. The impeccable skill lives at"
    f" {_IMPECCABLE_SKILL_DIR} -- its scripts run with plain `node`."
)

IMPECCABLE_CRITIQUE_SEGMENT = (
    "This repository has a UI framework. Additionally run an `impeccable critique`-style review"
    " over the UI surfaces the implementation touched: load the `impeccable` skill's"
    " `reference/critique.md` and apply its heuristic review by READING code only -- this session"
    " cannot run scripts, so skip every `node ...` step and score from the source. Fold any brownfield-baseline/P1"
    " design findings into `divergence_findings` with concrete file evidence, severity mapped"
    " honestly (a brownfield-baseline design defect is at most `major` here unless it violates the Specification)."
)

IMPECCABLE_DEDUP_SEGMENT = (
    "This repository has a UI framework. After the de-dup work, also run the `impeccable` skill's"
    " deterministic design detector over the UI files you touched or that adversarial-audit flagged:"
    f" `node {_IMPECCABLE_SKILL_DIR}/scripts/detect.mjs --json <files>` -- and fix the mechanical"
    " findings it reports. If the adversarial-audit report below carries design (impeccable critique) findings,"
    " apply the `impeccable polish` flow (its `reference/polish.md`) scoped to those findings'"
    " files. Design fixes must never change observable behavior -- same bar as the de-dup work."
)


# MCP server configs -- confirmed real and working via a live spike (mcp_servers= reaches an
# actual Copilot CLI session; tool names surface as "<server_key>-<tool_name>", e.g.
# "playwright-browser_navigate"; "mcp:*" in available_tools is required to let them through an
# allowlist-tier stage, otherwise the allowlist silently filters every MCP tool out -- caught by
# testing, not guessed). Playwright MCP itself (spawns its own browser via npx, no external
# service needed) was the one actually exercised; Excalidraw below uses the identical config shape
# but has NOT been spike-tested (it needs its own MCP server process, which doesn't exist here).
# Baked into the sandbox image (Dockerfile's PLAYWRIGHT_MCP_VERSION npm -g install), not fetched
# via `npx -y @playwright/mcp@latest` at session time -- deterministic version, no runtime npm
# fetch in a container running untrusted repos. The env points ONLY this server at the
# build-baked browser (see the Dockerfile's /opt/playwright-browsers comment); target repos' own
# playwright runs keep the global PLAYWRIGHT_BROWSERS_PATH cache-volume path.
PLAYWRIGHT_MCP_CONFIG: dict[str, Any] = {
    "playwright": {
        "type": "stdio",
        "command": "mcp-server-playwright",
        "args": ["--headless", "--isolated"],
        "env": {"PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright-browsers"},
        "tools": ["*"],
    }
}
# The plan stage used to attach an Excalidraw MCP server here for wireframes. Deleted rather
# than left dormant: it was never spike-tested, fetched unpinned `npx -y mcp-excalidraw` over
# the network at session time, and had no scene-to-SVG export path. Wireframes are now
# LLM-emitted self-contained HTML, validated deterministically in gates/diagram_gate.py.
# quality-remediation used to attach a SonarQube MCP server here too. Deleted rather than left dormant: it was never
# spike-tested, needed SONARQUBE_URL/SONARQUBE_TOKEN that nothing sets, fetched unpinned
# `sonarqube-mcp-server@latest` over the network into a container running untrusted repositories,
# and SonarQube is LGPL-3.0 -- the same bar that kept it out of repo_scan.py's tool set.


SPEC_AUDIT_SYSTEM_PROMPT = load_prompt("specification_audit")

PLAN_AUDIT_SYSTEM_PROMPT = load_prompt("plan_audit")


def _build_specification_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    return [
        SystemMessage(content=SPEC_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"),
        HumanMessage(content=f"Draft Specification to audit (JSON):\n{stage['draft']}"),
    ]


def _build_plan_audit_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    return [
        SystemMessage(content=PLAN_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Draft Plan to audit (JSON):\n{plan_stage['draft']}"),
    ]


TECH_STACK_SYSTEM_PROMPT = load_prompt("tech_stack_draft")

TECH_STACK_AUDIT_SYSTEM_PROMPT = load_prompt("tech_stack_audit")


def _build_tech_stack_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["tech-stack"]
    messages: list[BaseMessage] = [SystemMessage(content=TECH_STACK_SYSTEM_PROMPT)]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages


def _build_tech_stack_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["tech-stack"]
    return [
        SystemMessage(content=TECH_STACK_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft tech stack to audit (JSON):\n{stage['draft']}"),
    ]


APP_DISCOVERY_SYSTEM_PROMPT = load_prompt("app_discovery_draft")

APP_DISCOVERY_AUDIT_SYSTEM_PROMPT = load_prompt("app_discovery_audit")


def _build_app_discovery_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"][app_discovery.STAGE_KEY]
    scan = state.get("app_scan") or {}
    messages: list[BaseMessage] = [
        SystemMessage(content=APP_DISCOVERY_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Deterministic scan -- candidate applications:\n\n{json.dumps(scan.get('candidates') or [], indent=2)}"
        ),
        HumanMessage(content=f"Deterministic scan -- marker file contents:\n\n{scan.get('evidence') or '(nothing found)'}"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages


def _build_app_discovery_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"][app_discovery.STAGE_KEY]
    scan = state.get("app_scan") or {}
    return [
        SystemMessage(content=APP_DISCOVERY_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Deterministic scan -- marker file contents:\n\n{scan.get('evidence') or '(nothing found)'}"),
        HumanMessage(content=f"Draft runnable-application report to audit (JSON):\n{stage['draft']}"),
    ]


BROWNFIELD_BASELINE_SYSTEM_PROMPT = load_prompt("brownfield_baseline_draft")
BROWNFIELD_BASELINE_AUDIT_SYSTEM_PROMPT = load_prompt("brownfield_baseline_audit")


def _build_brownfield_baseline_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["brownfield-baseline"]
    messages: list[BaseMessage] = [
        SystemMessage(content=BROWNFIELD_BASELINE_SYSTEM_PROMPT),
        HumanMessage(content=state.get("brownfield_context") or "(no grounding context available)"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages


def _build_brownfield_baseline_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["brownfield-baseline"]
    return [
        SystemMessage(content=BROWNFIELD_BASELINE_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=state.get("brownfield_context") or "(no grounding context available)"),
        HumanMessage(content=f"Draft baseline to audit (JSON):\n{stage['draft']}"),
    ]


RAW_REQUIREMENTS_SYSTEM_PROMPT = load_prompt("raw_requirements_draft")

RAW_REQUIREMENTS_AUDIT_SYSTEM_PROMPT = load_prompt("raw_requirements_audit")


def _build_raw_requirements_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["raw-requirements"]
    messages: list[BaseMessage] = [SystemMessage(content=RAW_REQUIREMENTS_SYSTEM_PROMPT)]
    seed_text = state.get("raw_requirements_text", "")
    if seed_text:
        messages.append(HumanMessage(content=f"Human-submitted requirements text (seed/edit):\n\n{seed_text}"))
    if stage["approved_content"] is not None:
        messages.append(
            HumanMessage(
                content=f"Previously approved Raw Requirements document (JSON), being revised:\n"
                f"{stage['approved_content']}"
            )
        )
    elif stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages


def _build_raw_requirements_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["raw-requirements"]
    return [
        SystemMessage(content=RAW_REQUIREMENTS_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft Raw Requirements document to audit (JSON):\n{stage['draft']}"),
    ]


async def _verify_specification_ledger(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: SandboxProvider
) -> VerificationResult:
    """StageSpec.deterministic_verify for the specification stage: resolves/validates every User
    Story's and Acceptance Criterion's id against spec/ledger.json (spec_ledger.py's real logic --
    this is just the SandboxProvider-I/O wrapper) and persists the ledger on success.

    content_dict is stage["draft"] (the just-audited, revised Specification, mutated in place by
    sync_ledger to carry ledger-resolved ids).
    """
    entries = await spec_ledger.load_ledger(provider, thread_id)
    user_stories = content_dict.get("user_stories") or []
    result = spec_ledger.sync_ledger(entries, user_stories, run_id)
    if result.passed:
        await spec_ledger.save_ledger(provider, thread_id, result.updated_entries)
        await git_ops.commit_paths(provider, thread_id, [spec_ledger.LEDGER_PATH], "ai-dev-workflow: spec ledger sync")
    return VerificationResult(
        passed=result.passed,
        feedback="; ".join(result.reasons) if result.reasons else "Ledger sync passed: every id resolved cleanly.",
        report={"reasons": result.reasons, "ledger_entry_count": len(result.updated_entries)},
    )


AC_TO_TESTS_SYSTEM_PROMPT = load_prompt("ac_to_tests_draft")

AC_TO_TESTS_AUDIT_SYSTEM_PROMPT = load_prompt("ac_to_tests_audit")


def _build_ac_to_tests_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    stage = state["stages"]["ac-to-tests"]
    messages: list[BaseMessage] = [
        SystemMessage(content=AC_TO_TESTS_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{plan_stage['approved_content']}"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    if stage.get("last_verification"):
        messages.append(
            HumanMessage(content=f"The last verification attempt failed with: {stage['last_verification'].get('feedback')}")
        )
    return messages


def _build_ac_to_tests_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["ac-to-tests"]
    return [
        SystemMessage(content=AC_TO_TESTS_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft AC-to-Tests suite to audit (JSON):\n{stage['draft']}"),
    ]


MINIMAL_CODE_TO_GREEN_SYSTEM_PROMPT = load_prompt("minimal_code_to_green_draft")

MINIMAL_CODE_TO_GREEN_AUDIT_SYSTEM_PROMPT = load_prompt("minimal_code_to_green_audit")


def _build_minimal_code_to_green_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    tests_stage = state["stages"]["ac-to-tests"]
    stage = state["stages"]["minimal-code-to-green"]
    messages: list[BaseMessage] = [
        SystemMessage(content=MINIMAL_CODE_TO_GREEN_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{plan_stage['approved_content']}"),
        HumanMessage(content=f"Approved Test Suite from P4 (JSON):\n\n{tests_stage['approved_content']}"),
    ]
    if _tech_stack_has_ui_framework(state):
        messages.append(HumanMessage(content=IMPECCABLE_CODEGEN_SEGMENT))
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior iteration (JSON):\n{stage['draft']}"))
    if stage.get("last_verification"):
        messages.append(
            HumanMessage(content=f"The last coverage verification failed with: {stage['last_verification'].get('feedback')}")
        )
    return messages


def _build_minimal_code_to_green_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["minimal-code-to-green"]
    return [
        SystemMessage(content=MINIMAL_CODE_TO_GREEN_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft code-change iteration to audit (JSON):\n{stage['draft']}"),
    ]


ADVERSARIAL_AUDIT_SYSTEM_PROMPT = load_prompt("adversarial_audit_draft")
ADVERSARIAL_AUDIT_AUDIT_SYSTEM_PROMPT = load_prompt("adversarial_audit_audit")


def _build_adversarial_audit_prompt(state: GraphState) -> list[BaseMessage]:
    spec_stage = state["stages"]["specification"]
    plan_stage = state["stages"]["plan"]
    stage = state["stages"]["adversarial-audit"]
    messages: list[BaseMessage] = [
        SystemMessage(content=ADVERSARIAL_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{spec_stage['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{plan_stage['approved_content']}"),
    ]
    if _tech_stack_has_ui_framework(state):
        messages.append(HumanMessage(content=IMPECCABLE_CRITIQUE_SEGMENT))
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior report (JSON):\n{stage['draft']}"))
    return messages


def _build_adversarial_audit_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["adversarial-audit"]
    return [
        SystemMessage(content=ADVERSARIAL_AUDIT_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft divergence report to audit (JSON):\n{stage['draft']}"),
    ]


DEDUP_SYSTEM_PROMPT = load_prompt("dedup_draft")
DEDUP_AUDIT_SYSTEM_PROMPT = load_prompt("dedup_audit")


def _build_dedup_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["dedup-simplify"]
    jscpd_report = (state.get("audit_cluster") or {}).get("jscpd_report_for_dedup", "(no jscpd report available)")
    messages: list[BaseMessage] = [
        SystemMessage(content=DEDUP_SYSTEM_PROMPT),
        HumanMessage(content=f"jscpd duplication-cluster report:\n\n{jscpd_report}"),
    ]
    if _tech_stack_has_ui_framework(state):
        messages.append(HumanMessage(content=IMPECCABLE_DEDUP_SEGMENT))
        # adversarial-audit's approved divergence report is where impeccable critique findings land -- inject
        # it here rather than relying on impeccable's own cross-session critique storage, which a
        # different Copilot session (adversarial-audit's) may or may not have been able to write.
        adversarial_report = (state["stages"].get("adversarial-audit") or {}).get("approved_content")
        if adversarial_report:
            messages.append(HumanMessage(content=f"adversarial-audit report (JSON), including any design findings:\n\n{adversarial_report}"))
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages


def _build_dedup_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["dedup-simplify"]
    return [
        SystemMessage(content=DEDUP_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft de-dup result to audit (JSON):\n{stage['draft']}"),
    ]


LICENSE_AUDIT_SYSTEM_PROMPT = load_prompt("license_audit_draft")
LICENSE_AUDIT_AUDIT_SYSTEM_PROMPT = load_prompt("license_audit_audit")


def _build_license_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["license-audit"]
    scan_report = (state.get("audit_cluster") or {}).get("license_scan_report", "(no deterministic scan report available)")
    messages: list[BaseMessage] = [
        SystemMessage(content=LICENSE_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Deterministic license scan (declared/detected licenses per package):\n\n{scan_report}"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior draft (JSON):\n{stage['draft']}"))
    return messages


def _build_license_audit_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["license-audit"]
    return [
        SystemMessage(content=LICENSE_AUDIT_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft license classification report to audit (JSON):\n{stage['draft']}"),
    ]


EXIT_SYSTEM_PROMPT = load_prompt("exit_draft")
EXIT_AUDIT_SYSTEM_PROMPT = load_prompt("exit_audit")


def _build_exit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["exit"]
    metrics_compute = (state.get("metrics_report") or {}).get("metrics", {})
    messages: list[BaseMessage] = [
        SystemMessage(content=EXIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Approved Specification (JSON):\n\n{state['stages']['specification']['approved_content']}"),
        HumanMessage(content=f"Approved Implementation Plan (JSON):\n\n{state['stages']['plan']['approved_content']}"),
        HumanMessage(content=f"metrics-report metrics summary (JSON):\n\n{json.dumps(metrics_compute)[:8000]}"),
    ]
    if stage["draft"] is not None:
        messages.append(HumanMessage(content=f"Your immediately-prior report (JSON):\n{stage['draft']}"))
    return messages


def _build_exit_audit_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["exit"]
    return [
        SystemMessage(content=EXIT_AUDIT_SYSTEM_PROMPT),
        HumanMessage(content=f"Draft merge-readiness report to audit (JSON):\n{stage['draft']}"),
    ]


@dataclass(frozen=True)
class StageSpec:
    key: str
    response_schema: (
        type[SpecificationDraftResponse]
        | type[PlanDraftResponse]
        | type[TechStackDraftResponse]
        | type[RawRequirementsDraftResponse]
        | type[AcceptanceCriteriaTestsDraftResponse]
        | type[MinimalCodeToGreenDraftResponse]
        | type[AdversarialAuditDraftResponse]
        | type[DedupDraftResponse]
        | type[LicenseAuditDraftResponse]
        | type[ExitDraftResponse]
        | type[AppDiscoveryDraftResponse]
    )
    content_field: str
    surface_tool_name: str
    build_envelope: Callable[[dict[str, Any], list[str] | None], dict[str, Any]]
    build_prompt: Callable[[GraphState], list[BaseMessage]]
    max_cycles: int
    audit_response_schema: (
        type[SpecificationAuditResponse]
        | type[PlanAuditResponse]
        | type[TechStackAuditResponse]
        | type[RawRequirementsAuditResponse]
        | type[AcceptanceCriteriaTestsAuditResponse]
        | type[MinimalCodeToGreenAuditResponse]
        | type[AdversarialAuditAuditResponse]
        | type[DedupAuditResponse]
        | type[LicenseAuditAuditResponse]
        | type[ExitAuditResponse]
        | type[AppDiscoveryAuditResponse]
    )
    audit_content_field: str
    build_audit_prompt: Callable[[GraphState], list[BaseMessage]]
    render_markdown: Callable[[dict[str, Any]], str]

    # Everything below is optional and defaults to today's exact behavior -- specification/plan
    # pass none of these, so build_graph()'s generated segment for them is byte-identical to
    # before. Added for the brownfield-baseline-exit pipeline's stages that need something more than plain
    # draft->audit->gate (Agent Plugin infrastructure plan, Part B).
    requires_human_gate: bool = True
    """False for stages that are supporting infrastructure with no tab to review them in (e.g.
    tech-stack detection) -- make_gate_node skips the interrupt() and proceeds straight to
    approved, same body that already runs post-interrupt-resolve for every other stage."""

    post_audit_hook: Callable[[str, dict[str, Any], "GraphState", SandboxProvider], Awaitable[None]] | None = None
    """Fire-and-forget side effect called at the end of make_audit_node, right after persistence
    (thread_id, revised content dict, full GraphState, provider). Used for deterministic follow-up
    writes driven by a stage's approved content (e.g. writing Directory.Build.props once tech-stack
    detection reports dotnet_detected) or by this run's own input (e.g. raw-requirements
    persisting the seed text that produced this draft, for its own hydrate_from_repo_file to
    compare future runs against)."""

    post_approve_hook: Callable[[str, dict[str, Any], "GraphState", SandboxProvider], Awaitable[None]] | None = None
    """Same signature as post_audit_hook, but fired from every place a stage reaches "approved" --
    gate_node, auto_approve_node, AND make_draft_node's hydrate_from_repo_file short-circuit.

    That last one is the reason this exists as a separate hook rather than more post_audit_hook
    users: hydration marks a stage approved inside draft_node and routes "already_approved"
    straight to the next stage, bypassing audit_node entirely -- so a post_audit_hook never runs
    again for a repo that has been onboarded once. Deterministic follow-up writes that must stay
    applied for the life of the repo (tech-stack's convention files) therefore belong here, not
    there; writes that are genuinely about *this run's* draft (raw-requirements' seed text) stay
    on post_audit_hook.

    Called with the stage's approved content, after persistence, and must be idempotent -- it runs
    on every single run, including pure no-op re-runs."""

    deterministic_verify: (
        Callable[[str, dict[str, Any], str, str | None, SandboxProvider], Awaitable[VerificationResult]] | None
    ) = None
    """A routing-capable check (thread_id, revised content dict, run_id, baseline_commit,
    provider) -> VerificationResult, inserted between audit and gate when set. baseline_commit is
    the value StageSpec.capture_baseline_commit stored on this stage's StageState (None if that
    flag is unset) -- write_scope_gate.py's write-scope check is the reason this exists; ledger-
    /diagram-style checks that don't need it just ignore the argument. Never LLM self-attestation
    -- a real script/parse. Failing routes back to draft (with VerificationResult.feedback as
    context) up to max_verify_cycles, then to a human-interrupt escalation node -- never
    auto-approved past a failed deterministic gate."""

    max_verify_cycles: int = 3
    """Safety cap for the verify->draft retry loop, independent of max_cycles (the LLM's own
    clarification-loop cap)."""

    hydrate_from_repo_file: Callable[[str, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
    """Idempotency short-circuit (thread_id, state, provider) -> pre-approved content dict, or
    None to draft normally. Invoked by make_draft_node before it ever calls ainvoke_structured --
    lets a stage skip its LLM call entirely and hydrate as already-approved when its own artifact
    already exists on disk (and, where relevant, there's no fresh human-submitted input this run
    that should override the skip)."""

    session_options: Callable[[GraphState, str], dict[str, Any]] | None = None
    """Extra kwargs (agent_mode/available_tools/excluded_tools/pre_tool_use_hook/mcp_servers) to
    forward to get_chat_model_for_thread, called with (state, "draft"|"audit") so a stage can
    give its audit pass different (typically stricter/read-only) options than its draft pass --
    P4's audit is always read-only even though its draft gets real write access, per the
    "adversarial second opinion never has more trust than it needs" principle every other audit
    node already follows implicitly. None preserves today's behavior exactly (unrestricted "plan"
    mode) -- set for a stage that needs the read-only available_tools allowlist (Phase A0's spike
    finding: an allowlist, not a blocklist, is what actually enforces read-only) or, later, real
    write access."""

    capture_baseline_commit: bool = False
    """When True, make_draft_node captures `git rev-parse HEAD` into this stage's StageState the
    first time draft_node runs this run (guarded by "not already set," see make_draft_node) --
    the write-scope gate's (agent/src/gates/write_scope_gate.py) "before this stage touched
    anything" reference point. False (the default) preserves today's behavior for every stage
    that doesn't need to diff its own writes against a pre-stage baseline."""

    sign_approval: bool = False
    """When True, make_gate_node appends a content-hash-signed row to APPROVALS.md (approvals.py)
    on approval, in the same commit as the rest of that gate's persistence. False (the default)
    preserves today's behavior for stages that don't need approval-integrity tracking (tech-stack,
    raw-requirements) -- set for specification/plan, whose approved content other stages build on
    and whose approval is worth being able to detect tampering/accidental edits against later."""


STAGES: list[StageSpec] = [
    StageSpec(
        key="tech-stack",
        response_schema=TechStackDraftResponse,
        content_field="tech_stack",
        surface_tool_name="present_tech_stack",
        build_envelope=build_tech_stack_envelope,
        build_prompt=_build_tech_stack_prompt,
        max_cycles=workflow_config.TECH_STACK_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=TechStackAuditResponse,
        audit_content_field="revised_tech_stack",
        build_audit_prompt=_build_tech_stack_audit_prompt,
        render_markdown=render_tech_stack_markdown,
        requires_human_gate=False,
        post_approve_hook=preflight_nodes.apply_stack_conventions,
        hydrate_from_repo_file=preflight_nodes.hydrate_tech_stack_from_repo_file,
        session_options=lambda _state, _role: {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS},
    ),
    StageSpec(
        key="raw-requirements",
        response_schema=RawRequirementsDraftResponse,
        content_field="raw_requirements",
        surface_tool_name="present_raw_requirements",
        build_envelope=build_raw_requirements_envelope,
        build_prompt=_build_raw_requirements_prompt,
        max_cycles=workflow_config.RAW_REQUIREMENTS_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=RawRequirementsAuditResponse,
        audit_content_field="revised_raw_requirements",
        build_audit_prompt=_build_raw_requirements_audit_prompt,
        render_markdown=render_raw_requirements_markdown,
        post_audit_hook=requirements_nodes.persist_raw_requirements_seed,
        hydrate_from_repo_file=requirements_nodes.hydrate_raw_requirements_from_repo_file,
        session_options=lambda _state, _role: {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS},
    ),
    StageSpec(
        key="specification",
        response_schema=SpecificationDraftResponse,
        content_field="specification",
        surface_tool_name="present_specification",
        build_envelope=build_specification_envelope,
        build_prompt=_build_specification_prompt,
        max_cycles=workflow_config.SPEC_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=SpecificationAuditResponse,
        audit_content_field="revised_specification",
        build_audit_prompt=_build_specification_audit_prompt,
        render_markdown=render_specification_markdown,
        deterministic_verify=_verify_specification_ledger,
        sign_approval=True,
    ),
    StageSpec(
        key="plan",
        response_schema=PlanDraftResponse,
        content_field="plan",
        surface_tool_name="present_plan",
        build_envelope=build_plan_envelope,
        build_prompt=_build_plan_prompt,
        max_cycles=workflow_config.PLAN_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=PlanAuditResponse,
        audit_content_field="revised_plan",
        build_audit_prompt=_build_plan_audit_prompt,
        render_markdown=render_plan_markdown,
        sign_approval=True,
        deterministic_verify=verify_plan_diagrams,
        # Wireframes are LLM-emitted self-contained HTML validated by verify_plan_diagrams -- no
        # MCP servers needed (Excalidraw MCP deleted: never spike-tested, fetched unpinned
        # `npx -y mcp-excalidraw` at runtime, and had no export path). The UI-repo branch keeps
        # the same read-only allowlist it had when the MCP was attached.
        session_options=lambda state, _role: (
            {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS}
            if _tech_stack_has_ui_framework(state)
            else {}
        ),
    ),
    StageSpec(
        key="ac-to-tests",
        response_schema=AcceptanceCriteriaTestsDraftResponse,
        content_field="test_suite",
        surface_tool_name="present_ac_to_tests",
        build_envelope=build_ac_to_tests_envelope,
        build_prompt=_build_ac_to_tests_prompt,
        max_cycles=workflow_config.AC_TO_TESTS_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=AcceptanceCriteriaTestsAuditResponse,
        audit_content_field="revised_test_suite",
        build_audit_prompt=_build_ac_to_tests_audit_prompt,
        render_markdown=render_ac_to_tests_markdown,
        requires_human_gate=False,
        capture_baseline_commit=True,
        deterministic_verify=verify_ac_to_tests,
        session_options=lambda state, role: (
            {
                "agent_mode": "autopilot",
                "excluded_tools": ["builtin:bash"],
                "pre_tool_use_hook": pre_tool_use_write_scope_hook,
                **({"mcp_servers": PLAYWRIGHT_MCP_CONFIG} if _tech_stack_has_ui_framework(state) else {}),
            }
            if role == "draft"
            else {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS}
        ),
    ),
    StageSpec(
        key="minimal-code-to-green",
        response_schema=MinimalCodeToGreenDraftResponse,
        content_field="iteration",
        surface_tool_name="present_minimal_code_to_green",
        build_envelope=build_minimal_code_to_green_envelope,
        build_prompt=_build_minimal_code_to_green_prompt,
        max_cycles=workflow_config.MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES,
        audit_response_schema=MinimalCodeToGreenAuditResponse,
        audit_content_field="revised_iteration",
        build_audit_prompt=_build_minimal_code_to_green_audit_prompt,
        render_markdown=render_minimal_code_to_green_markdown,
        deterministic_verify=verify_coverage,
        # Draft gets full, unscoped write access -- "minimal code to green" is definitionally a
        # code-writing task (Part A Decisions point 6, tier (iii)). Audit stays read-only, same
        # asymmetry as P4's session_options.
        session_options=lambda _state, role: (
            {"agent_mode": "autopilot"} if role == "draft" else {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS}
        ),
    ),
]



async def _persist_if_sandboxed(
    thread_id: str, state: GraphState, stages: dict[str, Any], commit_message: str
) -> None:
    """Best-effort persistence (architecture plan Section B) -- a no-op when no sandbox is
    registered for this thread (Section A not wired up, or the thread predates sandboxing).

    Failures are logged and swallowed, not raised: this runs inside gate/audit/auto_approve
    nodes, and a transient persistence failure (e.g. the sandbox idled out between provisioning
    and this gate resolving -- flagged as an open gap in the plan's Section B, "re-provision
    sandbox on demand" is not implemented here) should not block the human's actual approval
    action, which is durable in the in-memory checkpoint regardless.
    """
    sandbox = sandbox_registry.get(thread_id)
    if sandbox is None:
        return
    try:
        provider = get_sandbox_provider()
        await workflow_persistence.persist_state(
            provider,
            thread_id,
            stages=stages,
            render_markdown=_RENDER_MARKDOWN_BY_STAGE,
        )
        await git_ops.commit_ai_dev_workflow(provider, thread_id, commit_message)
    except Exception:
        logger.warning("Failed to persist workflow state for thread_id=%s", thread_id, exc_info=True)


async def _run_post_approve_hook(
    stage_spec: "StageSpec", thread_id: str, content: dict[str, Any] | None, state: GraphState
) -> None:
    """Fires StageSpec.post_approve_hook from all three places a stage becomes "approved"
    (gate_node, auto_approve_node, and make_draft_node's hydrate short-circuit).

    Failures are logged and swallowed for the same reason _persist_if_sandboxed swallows its own:
    a convention file that couldn't be written must not take down a run whose approval already
    happened. The hook itself is expected to record its own partial failures where they matter.
    """
    if stage_spec.post_approve_hook is None or not content:
        return
    if sandbox_registry.get(thread_id) is None:
        return
    try:
        await stage_spec.post_approve_hook(thread_id, content, state, get_sandbox_provider())
    except Exception:
        logger.warning(
            "post_approve_hook failed for stage=%s thread_id=%s", stage_spec.key, thread_id, exc_info=True
        )


def _split_text_and_attachments(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Split a HumanMessage's content into its text and any non-text (AG-UI InputContent)
    parts. A plain string (every submission before multimodal attachments existed, and every
    text-only submission since) passes through unchanged with no attachments.
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
            elif isinstance(part, dict):
                attachments.append(part)
        return "\n".join(text_parts), attachments
    return str(content), []


async def intake_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    stages = {key: dict(value) for key, value in state.get("stages", {}).items()}

    # Hydration (architecture plan Section B.2): only when this thread has never had any stage
    # state in this process's memory yet -- i.e. genuinely the first invoke for this thread since
    # the agent process started, whether because it's a returning session after a restart, or a
    # different session picking up the same repo/branch/user. A thread already mid-session (any
    # prior invoke populated `stages`) never re-hydrates; its in-memory checkpoint is authoritative.
    if not stages and sandbox_registry.get(thread_id) is not None:
        hydrated = await workflow_persistence.hydrate_state(get_sandbox_provider(), thread_id, _STAGE_KEYS)
        if hydrated is not None:
            stages = hydrated
            logger.info("intake_node: hydrated prior workflow state for thread_id=%s", thread_id)

    # _ALL_STAGE_SPECS (STAGES + every standalone StageSpec: adversarial-audit/b/d, exit, brownfield-baseline-brownfield) is
    # assigned near the bottom of this module, after all of them are defined -- referencing it
    # here is fine, since intake_node only ever runs after the module has finished loading. Fixes
    # a real bug: standalone StageSpecs were never getting a default_stage_state() here at all,
    # so the first node touching e.g. stages["adversarial-audit"] would KeyError.
    for stage_spec in _ALL_STAGE_SPECS:
        stages.setdefault(stage_spec.key, default_stage_state())

    # AC-6.3: a Plan that had already advanced is reset to Not Started; its
    # last content stays visible (AC-8.4) but is no longer current/approved.
    # tech-stack and raw-requirements are both supporting/evergreen artifacts (not "review this
    # run's draft" stages the way specification/plan still are) -- once approved, they stay
    # approved across every later fresh run on the same thread; each one's own draft node
    # idempotency check (not this reset loop) is what decides whether it needs to redraft. Every
    # other stage (STAGES[2:] plus every standalone StageSpec) resets on a fresh run.
    for stage_spec in STAGES[2:] + _STANDALONE_STAGE_SPECS:
        stage = stages[stage_spec.key]
        if stage["status"] in ("ready_for_review", "approved"):
            stage["status"] = "not_started"
            stage["cycle_count"] = 0
            stage["readiness"] = False
            stage["clarifying_questions"] = []
        # Per-run mechanics reset unconditionally -- a stage whose previous run escalated at the
        # verify cap otherwise re-enters every later run already AT the cap, so its first
        # transient failure escalates instantly (observed live: spec verify logged cycle 3 on a
        # fresh run's very first attempt).
        stage["verify_cycle_count"] = 0
        stage["last_verification"] = None

    # The Raw Requirements Text (AC-1.3/AC-6.2) is submitted as an ordinary
    # chat message — the human's "submit" action is agent.addMessage(...) +
    # runAgent() on the frontend — so every run's current, complete text is
    # simply the latest HumanMessage, never a delta. That message's content is a plain string
    # for a text-only submission, or a multimodal InputContent list when screenshots/documents
    # were attached in the Requirements area -- either way, only the text half becomes
    # raw_requirements_text; any attachments are carried separately (see GraphState) since they
    # only matter to this specific run's specification draft, not the persisted text itself.
    raw_requirements_text = state.get("raw_requirements_text", "")
    requirements_attachments: list[dict[str, Any]] = []
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            raw_requirements_text, requirements_attachments = _split_text_and_attachments(
                message.content
            )
            break

    return {
        "stages": stages,
        "run_id": uuid.uuid4().hex[:8],
        "raw_requirements_text": raw_requirements_text,
        "requirements_attachments": requirements_attachments,
        # Cleared explicitly: a rejection is a verdict about one run's view of the repository, and
        # a repo that has since gained an application must get a fresh assessment, not a stale no.
        "app_rejection": None,
    }


def make_draft_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def draft_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]

        if stage_spec.hydrate_from_repo_file is not None and sandbox_registry.get(thread_id) is not None:
            hydrated = await stage_spec.hydrate_from_repo_file(thread_id, state, get_sandbox_provider())
            if hydrated is not None:
                stages = {key: dict(value) for key, value in state["stages"].items()}
                stage = stages[stage_spec.key]
                used_ids: set[str] = set(stage["used_ids"])
                _extract_ids(hydrated, used_ids)
                stage["draft"] = hydrated
                stage["approved_content"] = hydrated
                stage["status"] = "approved"
                stage["readiness"] = True
                stage["ever_ready_for_review"] = True
                stage["used_ids"] = sorted(used_ids)
                stages[stage_spec.key] = stage
                # The whole point of post_approve_hook (vs post_audit_hook): this branch routes
                # "already_approved" straight past audit_node and gate_node, so this is the ONLY
                # place a hook can run for a repo that has been onboarded before.
                await _run_post_approve_hook(stage_spec, thread_id, hydrated, state)
                return {"stages": stages}

        if (
            stage_spec.capture_baseline_commit
            and state["stages"][stage_spec.key].get("baseline_commit") is None
            and sandbox_registry.get(thread_id) is not None
        ):
            # Captured once (guarded by "not already set," not by cycle count) -- persists across
            # retry cycles within this run via LangGraph's checkpointed state; only reset to None
            # by an escalate node when a human is assumed to have intervened out-of-band. This is
            # the write-scope gate's (agent/src/gates/write_scope_gate.py) "before this stage
            # touched anything" reference point.
            provider = get_sandbox_provider()
            head_result = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
            stages = {key: dict(value) for key, value in state["stages"].items()}
            stages[stage_spec.key]["baseline_commit"] = head_result.stdout.strip() if head_result.ok else None
            state = {**state, "stages": stages}

        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name(stage_spec.key, "draft"),
            sandbox=sandbox_registry.get(thread_id),
            **(stage_spec.session_options(state, "draft") if stage_spec.session_options is not None else {}),
        )

        prompt_messages = stage_spec.build_prompt(state)
        response = await ainvoke_structured(model, prompt_messages, stage_spec.response_schema)

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        content = getattr(response, stage_spec.content_field)
        content_dict = content.model_dump(mode="json") if content is not None else stage["draft"]

        used_ids: set[str] = set(stage["used_ids"])
        if content_dict is not None:
            _extract_ids(content_dict, used_ids)

        stage["draft"] = content_dict
        stage["clarifying_questions"] = [q.model_dump(mode="json") for q in response.clarifying_questions]
        stage["readiness"] = response.readiness
        stage["used_ids"] = sorted(used_ids)

        # Note: no A2UI envelope is built here even when readiness=true. That happens once, in
        # make_audit_node, against the *audited* (revised) content -- building it here too would
        # double-emit the surface (once pre-audit, once post-audit) for every ready draft.
        if response.readiness:
            stage["status"] = "ready_for_review"
            stage["ever_ready_for_review"] = True
        else:
            stage["status"] = "needs_clarification"
            stage["cycle_count"] = stage["cycle_count"] + 1

        stages[stage_spec.key] = stage

        if sandbox_registry.get(thread_id) is not None:
            # metrics-report's token-consumption tracking reads these ledger entries. model._last_usage is
            # None if no ASSISTANT_USAGE event fired (shouldn't happen in practice, but the field
            # is optional upstream, so tolerated here rather than assumed).
            await repo_files.append_ledger_entry(
                get_sandbox_provider(),
                thread_id,
                {"stage": stage_spec.key, "node": "draft", "readiness": response.readiness, "token_usage": model._last_usage},
            )

        return {"stages": stages}

    return draft_node


def make_audit_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Stringent second-opinion pass (SPECIFICATION.md-adjacent, see plan doc) run once per draft
    that reaches readiness=true, by a separately-configured model, before the human ever sees it.

    Only wired onto the "gate" routing branch (see build_graph) -- a draft forced through via
    auto_approve (the clarification-cycle safety cap) skips the audit entirely; it's already
    known-incomplete, and an adversarial pass over admittedly-incomplete content mostly just
    re-describes its own incompleteness.
    """

    async def audit_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        model = get_chat_model_for_thread(
            thread_id,
            stage_spec.key,
            "audit",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name(stage_spec.key, "audit"),
            sandbox=sandbox_registry.get(thread_id),
            **(stage_spec.session_options(state, "audit") if stage_spec.session_options is not None else {}),
        )

        prompt_messages = stage_spec.build_audit_prompt(state)
        response = await ainvoke_structured(model, prompt_messages, stage_spec.audit_response_schema)

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        revised_content = getattr(response, stage_spec.audit_content_field)
        content_dict = revised_content.model_dump(mode="json")

        used_ids: set[str] = set(stage["used_ids"])
        _extract_ids(content_dict, used_ids)

        stage["draft"] = content_dict
        stage["used_ids"] = sorted(used_ids)
        stage["audit_findings"] = list(response.audit_findings)
        stages[stage_spec.key] = stage

        # A stage with a deterministic_verify gate (e.g. specification's ledger sync) can still
        # rewrite this exact content_dict's own ids/fields between here and the gate -- building
        # the human-facing A2UI surface now would show content that's about to change out from
        # under it. Those stages instead build+send their surface from make_verify_node, once
        # verification has actually passed and the content is final. Every other stage's behavior
        # (build+send here) is byte-identical to before deterministic_verify existed.
        extra_messages: list[BaseMessage] = []
        if stage_spec.deterministic_verify is None:
            envelope = stage_spec.build_envelope(content_dict, stage["audit_findings"])
            extra_messages = present_surface_messages(stage_spec.surface_tool_name, envelope)

        thread_id = config["configurable"]["thread_id"]
        await _persist_if_sandboxed(
            thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} draft revised (audit)"
        )

        if sandbox_registry.get(thread_id) is not None:
            await repo_files.append_ledger_entry(
                get_sandbox_provider(),
                thread_id,
                {"stage": stage_spec.key, "node": "audit", "audit_findings_count": len(stage["audit_findings"]), "token_usage": model._last_usage},
            )

        if stage_spec.post_audit_hook is not None and sandbox_registry.get(thread_id) is not None:
            await stage_spec.post_audit_hook(thread_id, content_dict, state, get_sandbox_provider())

        return {"stages": stages, "messages": extra_messages, "last_push": git_ops.get_last_push(thread_id)}

    return audit_node


def make_verify_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Runs stage_spec.deterministic_verify (a real script/parse, never LLM self-attestation)
    between audit and gate. Only wired in when the StageSpec sets deterministic_verify -- see
    build_graph()."""

    async def verify_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        assert stage_spec.deterministic_verify is not None
        thread_id = config["configurable"]["thread_id"]
        provider = get_sandbox_provider()

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]

        if sandbox_registry.get(thread_id) is None:
            # Every deterministic_verify (ledger sync, mermaid render, write-scope/AC-coverage git
            # diff, coverage run, license scan) needs the sandbox. Without one the check cannot run,
            # so escalate to a human rather than let the stage pass unverified (route reads
            # cannot_verify).
            stage["last_verification"] = {
                "passed": False,
                "cannot_verify": True,
                "feedback": "no sandbox -- deterministic verification did not run",
                "report": {},
            }
            stages[stage_spec.key] = stage
            return {"stages": stages}

        result = await stage_spec.deterministic_verify(
            thread_id, stage["draft"], state.get("run_id", "unknown"), stage.get("baseline_commit"), provider
        )
        stage["last_verification"] = {"passed": result.passed, "feedback": result.feedback, "report": result.report}
        if not result.passed:
            stage["verify_cycle_count"] = stage.get("verify_cycle_count", 0) + 1
        stages[stage_spec.key] = stage

        extra_messages: list[BaseMessage] = []
        if result.passed:
            # deterministic_verify (e.g. spec_ledger.sync_ledger) may have mutated stage["draft"]
            # in place (ids resolved/overwritten) -- build+send the human-facing surface only now,
            # against the final, ledger-correct content (see make_audit_node's matching comment).
            envelope = stage_spec.build_envelope(stage["draft"], stage["audit_findings"])
            extra_messages = present_surface_messages(stage_spec.surface_tool_name, envelope)
            await _persist_if_sandboxed(
                thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} draft revised (verify)"
            )

        if sandbox_registry.get(thread_id) is not None:
            await repo_files.append_ledger_entry(
                provider,
                thread_id,
                {
                    "stage": stage_spec.key,
                    "node": "verify",
                    "passed": result.passed,
                    "cycle": stage["verify_cycle_count"],
                },
            )

        return {"stages": stages, "messages": extra_messages}

    return verify_node


def make_route_after_verify(stage_spec: StageSpec) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        stage = state["stages"][stage_spec.key]
        last = stage.get("last_verification") or {}
        if last.get("cannot_verify"):
            return "escalate"  # no sandbox -- never loop or pass, a human must see it
        if last.get("passed"):
            return "gate"
        if stage.get("verify_cycle_count", 0) < stage_spec.max_verify_cycles:
            return "retry"
        return "escalate"

    return route


def make_escalate_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    """Deterministic-verify cap exhaustion, e.g. a gate that keeps failing a real check (coverage,
    write-scope, ledger-sync). Never auto-approved past a failed deterministic gate -- pauses for
    an explicit human decision, distinct from the normal approval gate's interrupt payload shape
    so the frontend can render it differently. On resume, retries from draft with the cycle
    counter reset (the human is assumed to have intervened, e.g. fixed something out-of-band)."""

    async def escalate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        stage = state["stages"][stage_spec.key]
        last = stage.get("last_verification") or {}
        interrupt(
            {
                "stage": stage_spec.key,
                "type": "cannot_verify" if last.get("cannot_verify") else "verification_cap_exceeded",
                "draft": stage["draft"],
                "report": last.get("report"),
                "feedback": last.get("feedback"),
            }
        )

        stages = {key: dict(value) for key, value in state["stages"].items()}
        stages[stage_spec.key]["verify_cycle_count"] = 0
        return {"stages": stages}

    return escalate_node


def make_route_after_draft(stage_spec: StageSpec) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        stage = state["stages"][stage_spec.key]
        # A hydrate_from_repo_file short-circuit (see make_draft_node) marks the stage "approved"
        # directly, in draft_node itself -- this content was already audited and approved in a
        # prior run, so it must bypass BOTH audit_node and gate_node entirely. Routing on
        # readiness alone (the pre-hydrate design) would send it through audit_node anyway,
        # re-running a live, non-deterministic LLM call on already-approved content on every
        # single idempotent re-run -- caught by real end-to-end testing (a second, unaffected
        # gate re-interrupt and slightly-reworded "approved" content on a run that should have
        # been a no-op), not by inspection.
        if stage["status"] == "approved":
            return "already_approved"
        if stage["readiness"]:
            return "gate"
        if stage["cycle_count"] >= stage_spec.max_cycles:
            return "auto_approve"
        return "needs_clarification"

    return route


def make_gate_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def gate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        stage = state["stages"][stage_spec.key]
        # Pauses here (BR-4/Section 6 Gate) until the frontend's useInterrupt
        # resolve(payload) resumes this exact node with that payload -- unless this stage is
        # supporting infrastructure with no tab to review it in (requires_human_gate=False), in
        # which case it proceeds straight through to the same approved-marking body every other
        # stage already runs post-interrupt-resolve.
        if stage_spec.requires_human_gate:
            interrupt({"stage": stage_spec.key, "draft": stage["draft"]})

        stages = {key: dict(value) for key, value in state["stages"].items()}
        approved = stages[stage_spec.key]
        approved["status"] = "approved"
        approved["approved_content"] = approved["draft"]
        approved["cycle_count"] = 0
        stages[stage_spec.key] = approved

        thread_id = config["configurable"]["thread_id"]
        await _persist_if_sandboxed(thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} approved")

        if stage_spec.sign_approval and sandbox_registry.get(thread_id) is not None:
            provider = get_sandbox_provider()
            await approvals.record_approval(
                provider, thread_id, stage_spec.key, state.get("run_id", "unknown"), approved["approved_content"]
            )
            await git_ops.commit_paths(
                provider, thread_id, [approvals.APPROVALS_PATH], f"ai-dev-workflow: {stage_spec.key} approval signed"
            )

        await _run_post_approve_hook(stage_spec, thread_id, approved["approved_content"], state)
        return {"stages": stages, "last_push": git_ops.get_last_push(thread_id)}

    return gate_node


def make_auto_approve_node(stage_spec: StageSpec) -> Callable[[GraphState, RunnableConfig], Any]:
    async def auto_approve_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        # US-10/AC-10.3: safety cap hit while still not-ready. Proceed to
        # Approved exactly as if the human had approved, bypassing the gate.
        stages = {key: dict(value) for key, value in state["stages"].items()}
        stage = stages[stage_spec.key]
        stage["status"] = "approved"
        stage["approved_content"] = stage["draft"]
        stage["cycle_count"] = 0
        stages[stage_spec.key] = stage

        thread_id = config["configurable"]["thread_id"]
        await _persist_if_sandboxed(
            thread_id, state, stages, f"ai-dev-workflow: {stage_spec.key} auto-approved (safety cap)"
        )

        await _run_post_approve_hook(stage_spec, thread_id, stage["approved_content"], state)
        return {"stages": stages, "last_push": git_ops.get_last_push(thread_id)}

    return auto_approve_node


# R placements (agent/src/rebuild.py). Keyed by the STAGES entry whose gate/auto_approve should
# route into R instead of straight to the next stage's draft -- see build_graph()'s use of this
# dict below. "After P4" uses fix_scope="scaffold_only" since brand-new tests against
# not-yet-existing production symbols won't compile at all, TDD-red or not -- that fix node may
# only add compile-enabling stubs, never real behavior (see rebuild.py's own docstring). "After
# P6" gets a full-scope fix, real bug-fixing being exactly what a failed rebuild after real
# implementation work calls for. Both currently route to END on success since quality-remediation (the next real
# stage after P6) doesn't exist yet -- update REBUILD_AFTER_P6.next_node the moment quality-remediation is wired in.
REBUILD_AFTER_AC_TO_TESTS = rebuild.RebuildSpec(
    key="r_ac_to_tests",
    max_fix_cycles=3,
    fix_prompt_addendum="",  # unused for scaffold_only -- rebuild.py substitutes its own addendum
    fix_scope="scaffold_only",
    next_node="minimal-code-to-green_draft",
)

REBUILD_AFTER_P6 = rebuild.RebuildSpec(
    key="r_minimal_code_to_green",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    next_node="quality_scan",
)

# Maps a STAGES entry's key -> the R placement immediately after it, so build_graph()'s per-stage
# loop can route that stage's gate/auto_approve into R instead of straight to the next draft node.
POST_STAGE_REBUILD: dict[str, rebuild.RebuildSpec] = {
    "ac-to-tests": REBUILD_AFTER_AC_TO_TESTS,
    "minimal-code-to-green": REBUILD_AFTER_P6,
}

# R(quality_remediation): sits between quality_fix and quality_gate_check in the plan's own chain (quality_scan -> quality_triage ->
# quality_ledger_write -> quality_fix -> R(quality_remediation) -> quality_gate_check -> loop|human_gate). Full fix scope --
# genuine bug-fixing after a real quality-fix pass, same reasoning as REBUILD_AFTER_P6.
REBUILD_FOR_P8 = rebuild.RebuildSpec(
    key="r_p8",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    next_node="quality_gate_check",
)

# R(security_remediation): same placement pattern as R(quality_remediation), between security_fix and security_gate_check.
REBUILD_FOR_P10 = rebuild.RebuildSpec(
    key="r_p10",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    next_node="security_gate_check",
)


def _wire_p8(builder: StateGraph) -> None:
    """Wires quality-remediation's bespoke node cluster (quality_security/quality_nodes.py) -- NOT exercised against a
    real sandbox yet, see quality_nodes.py's own module docstring for exactly what's unverified.
    quality_gate_check's "next" routes into security-remediation's own scan node."""
    builder.add_node("quality_scan", quality_nodes.quality_scan_node)
    builder.add_node("quality_triage", quality_nodes.quality_triage_node)
    builder.add_node("quality_ledger_write", quality_nodes.quality_ledger_write_node)
    builder.add_node("quality_fix", quality_nodes.quality_fix_node)
    builder.add_node("quality_gate_check", quality_nodes.quality_gate_check_node)
    builder.add_node("quality_human_gate", quality_nodes.quality_human_gate_node)

    r_quality_entry_name = _wire_rebuild(builder, REBUILD_FOR_P8)

    builder.add_edge("quality_scan", "quality_triage")
    builder.add_edge("quality_triage", "quality_ledger_write")
    builder.add_edge("quality_ledger_write", "quality_fix")
    builder.add_edge("quality_fix", r_quality_entry_name)
    builder.add_conditional_edges(
        "quality_gate_check",
        quality_nodes.make_quality_route_after_gate(),
        {"next": "security_scan", "retry": "quality_scan", "escalate": "quality_human_gate"},
    )
    builder.add_edge("quality_human_gate", "quality_scan")


ADVERSARIAL_AUDIT_SPEC = StageSpec(
    key="adversarial-audit",
    response_schema=AdversarialAuditDraftResponse,
    content_field="report",
    surface_tool_name="present_adversarial_audit",
    build_envelope=build_adversarial_audit_envelope,
    build_prompt=_build_adversarial_audit_prompt,
    max_cycles=workflow_config.ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES,
    audit_response_schema=AdversarialAuditAuditResponse,
    audit_content_field="revised_report",
    build_audit_prompt=_build_adversarial_audit_audit_prompt,
    render_markdown=render_adversarial_audit_markdown,
    # requires_human_gate defaults True -- adversarial-audit's own human review of divergence findings, per
    # the pipeline diagram (the one interactive checkpoint inside audit-cluster besides low-confidence
    # license findings).
)

DEDUP_SPEC = StageSpec(
    key="dedup-simplify",
    response_schema=DedupDraftResponse,
    content_field="result",
    surface_tool_name="present_dedup",
    build_envelope=build_dedup_envelope,
    build_prompt=_build_dedup_prompt,
    max_cycles=workflow_config.DEDUP_MAX_CLARIFICATION_CYCLES,
    audit_response_schema=DedupAuditResponse,
    audit_content_field="revised_result",
    build_audit_prompt=_build_dedup_audit_prompt,
    render_markdown=render_dedup_markdown,
    requires_human_gate=False,  # bounded by jscpd's objective re-check at audit-cluster's exit gate instead
    post_audit_hook=audit_gates.rerun_jscpd_after_dedup,
    session_options=lambda _state, role: (
        {"agent_mode": "autopilot"} if role == "draft" else {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS}
    ),
)

LICENSE_AUDIT_SPEC = StageSpec(
    key="license-audit",
    response_schema=LicenseAuditDraftResponse,
    content_field="report",
    surface_tool_name="present_license_audit",
    build_envelope=build_license_audit_envelope,
    build_prompt=_build_license_audit_prompt,
    max_cycles=workflow_config.LICENSE_AUDIT_MAX_CLARIFICATION_CYCLES,
    audit_response_schema=LicenseAuditAuditResponse,
    audit_content_field="revised_report",
    build_audit_prompt=_build_license_audit_audit_prompt,
    render_markdown=render_license_audit_markdown,
    requires_human_gate=False,  # the "gate" for flagged packages IS the deterministic_verify escalate below
    deterministic_verify=audit_gates.verify_license_audit,
    max_verify_cycles=0,  # any flagged package escalates immediately -- redrafting can't change a license
)

EXIT_SPEC = StageSpec(
    key="exit",
    response_schema=ExitDraftResponse,
    content_field="report",
    surface_tool_name="present_exit",
    build_envelope=build_exit_envelope,
    build_prompt=_build_exit_prompt,
    max_cycles=workflow_config.EXIT_MAX_CLARIFICATION_CYCLES,
    audit_response_schema=ExitAuditResponse,
    audit_content_field="revised_report",
    build_audit_prompt=_build_exit_audit_prompt,
    render_markdown=render_exit_markdown,
    # requires_human_gate defaults True -- the final human checkpoint of the entire pipeline.
    sign_approval=True,  # APPROVALS.md covers P2/P3/exit per the plan -- exit_finalize_node also reads this row
)


def _wire_p15(builder: StateGraph) -> None:
    """Wires exit: EXIT_SPEC (a StageSpec, reusing the standard draft->audit->gate template for
    consistency with every other stage -- the plan's own diagram sketched a single LLM box, but an
    adversarial second opinion on "is this merge-ready" is worth having here too) -> exit_finalize
    (deterministic: manifest.json + CHANGELOG.md + commit, never the model's job) -> END.

    Verification status: NOT exercised against a real sandbox, same caveat as every quality-remediation+ cluster.
    """
    builder.add_node("exit_finalize", exit_nodes.exit_finalize_node)
    _wire_stage(builder, EXIT_SPEC, "exit_finalize")
    builder.add_edge("exit_finalize", END)


APP_DISCOVERY_SPEC = StageSpec(
    key=app_discovery.STAGE_KEY,
    response_schema=AppDiscoveryDraftResponse,
    content_field="app_detection",
    surface_tool_name="present_app_discovery",
    build_envelope=build_app_discovery_envelope,
    build_prompt=_build_app_discovery_prompt,
    max_cycles=workflow_config.APP_DISCOVERY_MAX_CLARIFICATION_CYCLES,
    audit_response_schema=AppDiscoveryAuditResponse,
    audit_content_field="revised_app_detection",
    build_audit_prompt=_build_app_discovery_audit_prompt,
    render_markdown=render_app_discovery_markdown,
    # No human gate: the verdict is app_discovery_decide_node's deterministic policy, and there is
    # nothing here for a human to approve -- either the repository has a runnable app or it does
    # not.
    requires_human_gate=False,
    hydrate_from_repo_file=app_discovery.hydrate_from_manifest,
    # Read-only, but with real sandbox tools on purpose: the deterministic scan's marker table has
    # no rules for Go/Rails/Spring/PHP, and a false rejection is unrecoverable within a run. The
    # model exploring past the evidence blob is the safety margin.
    session_options=lambda _state, _role: {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS},
)


def _route_after_app_discovery(state: GraphState) -> str:
    return "reject" if state.get("app_rejection") else "next"


def _route_after_tech_stack(state: GraphState) -> str:
    """The brownfield branch, moved here from scaffold: app discovery and tech-stack detection both
    run before brownfield-baseline now, so an unsuitable repository is rejected before any human is asked to ratify
    a baseline -- and before anything is written to the repo."""
    return "next" if state.get("manifest_exists", True) else "brownfield_baseline_pre"


async def _manifest_branch_node(_state: GraphState) -> dict[str, Any]:
    """Pass-through branch point. `_wire_stage` gives every stage a plain gate -> next edge, so a
    conditional branch after tech-stack needs a node of its own to hang the edges on."""
    return {}


def _wire_app_discovery(builder: StateGraph) -> None:
    """Wires the suitability gate and the repo-write ordering that depends on it:

    scaffold (read-mostly) -> app_discovery_pre (deterministic scan) -> APP_DISCOVERY_SPEC
    (draft -> audit -> auto-gate) -> app_discovery_decide -> either app_discovery_reject -> END
    (the one hard stop in this graph) or scaffold_finalize -> tech-stack -> manifest_branch ->
    (brownfield-baseline brownfield | app_check_record) -> repo_scan_baseline -> raw-requirements.

    repo_scan_baseline sits at the convergence point of both branches and immediately before P1,
    so it measures the repository as it arrived: the clone exists, the tech stack is known, and
    nothing has written application code yet. It is idempotent on its own committed artifact
    because this node -- like every node on the main path -- is re-entered on every clarification
    round; see repo_scan.repo_scan_baseline_node's docstring for why that matters.

    Verification status: the deterministic scan and the reject path have NOT been exercised
    against a real sandbox; app_discovery.py's own self-check covers the pure half only.
    """
    builder.add_node("app_discovery_pre", app_discovery.app_discovery_pre_node)
    builder.add_node("app_discovery_decide", app_discovery.app_discovery_decide_node)
    builder.add_node("app_discovery_reject", app_discovery.app_discovery_reject_node)
    builder.add_node("app_check_record", app_discovery.app_check_record_node)
    builder.add_node("repo_scan_baseline", repo_scan.repo_scan_baseline_node)
    builder.add_node("scaffold_finalize", preflight_nodes.scaffold_finalize_node)
    builder.add_node("manifest_branch", _manifest_branch_node)

    builder.add_edge("app_discovery_pre", f"{APP_DISCOVERY_SPEC.key}_draft")
    _wire_stage(builder, APP_DISCOVERY_SPEC, "app_discovery_decide")
    builder.add_conditional_edges(
        "app_discovery_decide",
        _route_after_app_discovery,
        {"reject": "app_discovery_reject", "next": "scaffold_finalize"},
    )
    builder.add_edge("app_discovery_reject", END)
    builder.add_edge("scaffold_finalize", f"{STAGES[0].key}_draft")
    builder.add_conditional_edges(
        "manifest_branch",
        _route_after_tech_stack,
        {"next": "app_check_record", "brownfield_baseline_pre": "brownfield_baseline_pre"},
    )
    builder.add_edge("app_check_record", "repo_scan_baseline")
    builder.add_edge("repo_scan_baseline", f"{STAGES[1].key}_draft")


BROWNFIELD_BASELINE_SPEC = StageSpec(
    key="brownfield-baseline",
    response_schema=BrownfieldBaselineDraftResponse,
    content_field="baseline",
    surface_tool_name="present_brownfield_baseline",
    build_envelope=build_brownfield_baseline_envelope,
    build_prompt=_build_brownfield_baseline_prompt,
    max_cycles=2,
    audit_response_schema=BrownfieldBaselineAuditResponse,
    audit_content_field="revised_baseline",
    build_audit_prompt=_build_brownfield_baseline_audit_prompt,
    render_markdown=render_brownfield_baseline_markdown,
    # requires_human_gate defaults True -- ratification is what flips manifest.json from absent
    # to present (brownfield_write_manifest_node, wired as this stage's own next_draft_name below).
    session_options=lambda _state, _role: {"available_tools": workflow_config.READ_ONLY_AVAILABLE_TOOLS},
)


def _wire_brownfield(builder: StateGraph) -> None:
    """Wires brownfield-baseline's brownfield sub-flow: only reached when scaffold's manifest_exists check finds
    no manifest.json (_wire_app_discovery's "manifest_branch" conditional edge, not this function,
    does that branch). brownfield_baseline_pre (deterministic schema/migration/route grep) -> BROWNFIELD_BASELINE_SPEC
    (draft->audit->gate) -> brownfield_write_manifest (deterministic: ratification IS what creates
    manifest.json) -> app_check_record, where both branches converge before raw-requirements.
    Verification status: NOT exercised against a real sandbox."""
    builder.add_node("brownfield_baseline_pre", preflight_nodes.brownfield_baseline_context_node)
    builder.add_node("brownfield_write_manifest", preflight_nodes.brownfield_write_manifest_node)
    _wire_stage(builder, BROWNFIELD_BASELINE_SPEC, "brownfield_write_manifest")
    builder.add_edge("brownfield_baseline_pre", "brownfield-baseline_draft")
    builder.add_edge("brownfield_write_manifest", "app_check_record")


REBUILD_FOR_AUDIT_CLUSTER = rebuild.RebuildSpec(
    key="r_audit_cluster",
    max_fix_cycles=3,
    fix_prompt_addendum="Fix the build using the systematic-debugging skill's 4-phase root-cause analysis.",
    fix_scope="full",
    next_node="test_hardening_run_tests",
)


def _wire_audit_cluster(builder: StateGraph) -> None:
    """Wires all of audit-cluster: adversarial-audit (StageSpec) -> dedup_simplify_pre (deterministic)
    -> dedup-simplify (StageSpec) -> finding_cluster (bespoke verify-loop cluster,
    finding_cluster_nodes.py) -> license_audit_pre (deterministic) -> license-audit (StageSpec)
    -> audit_exit_gate (deterministic, retry-once-then-escalate) -> R(audit_cluster) -> END.

    adversarial-audit/dedup-simplify/license-audit are standalone StageSpec instances, not appended to the flat STAGES list --
    finding-cluster's bespoke cluster interrupts what would otherwise be linear STAGES-style chaining, so
    _wire_stage (extracted from build_graph()'s STAGES loop) is called directly here with each
    stage's own explicit next_draft_name instead.

    Verification status: NOT exercised against a real sandbox -- same caveat as quality-remediation/security-remediation.
    """
    builder.add_node("dedup_simplify_pre", audit_gates.dedup_simplify_pre_node)
    builder.add_node("finding_cluster_pre", finding_cluster_nodes.finding_cluster_pre_node)
    builder.add_node("finding_cluster_draft", finding_cluster_nodes.finding_cluster_draft_node)
    builder.add_node("finding_cluster_verify", finding_cluster_nodes.finding_cluster_verify_node)
    builder.add_node("finding_cluster_audit", finding_cluster_nodes.finding_cluster_audit_node)
    builder.add_node("finding_cluster_revert", finding_cluster_nodes.finding_cluster_revert_node)
    builder.add_node("finding_cluster_notice_gate", finding_cluster_nodes.finding_cluster_notice_gate_node)
    builder.add_node("license_audit_pre", audit_gates.license_audit_pre_node)
    builder.add_node("audit_exit_gate", audit_gates.audit_exit_gate_node)
    builder.add_node("audit_exit_human_gate", audit_gates.audit_exit_human_gate_node)

    r_audit_entry_name = _wire_rebuild(builder, REBUILD_FOR_AUDIT_CLUSTER)

    _wire_stage(builder, ADVERSARIAL_AUDIT_SPEC, "dedup_simplify_pre")
    builder.add_edge("dedup_simplify_pre", "dedup-simplify_draft")
    _wire_stage(builder, DEDUP_SPEC, "finding_cluster_pre")

    builder.add_edge("finding_cluster_pre", "finding_cluster_draft")
    builder.add_edge("finding_cluster_draft", "finding_cluster_verify")
    builder.add_conditional_edges(
        "finding_cluster_verify",
        finding_cluster_nodes.make_finding_cluster_route_after_verify(),
        {"audit": "finding_cluster_audit", "retry": "finding_cluster_draft", "revert": "finding_cluster_revert"},
    )
    builder.add_edge("finding_cluster_audit", "license_audit_pre")
    builder.add_edge("finding_cluster_revert", "finding_cluster_notice_gate")
    builder.add_edge("finding_cluster_notice_gate", "license_audit_pre")  # informational gate -- never blocks audit-cluster

    builder.add_edge("license_audit_pre", "license-audit_draft")
    _wire_stage(builder, LICENSE_AUDIT_SPEC, "audit_exit_gate")

    builder.add_conditional_edges(
        "audit_exit_gate",
        audit_gates.make_audit_exit_route(),
        {"next": r_audit_entry_name, "retry": "audit_exit_gate", "escalate": "audit_exit_human_gate"},
    )
    builder.add_edge("audit_exit_human_gate", "audit_exit_gate")


def _wire_p14(builder: StateGraph) -> None:
    """Wires metrics-report's metrics + traceability + token-tracking node, plus the one named LLM
    exception (ponytail-gain). Routes into exit's own draft node. Verification status: NOT
    exercised against a real sandbox, same caveat as quality-remediation/security-remediation/audit-cluster/test-hardening."""
    builder.add_node("metrics_compute", metrics_nodes.metrics_compute_node)
    builder.add_node("metrics_ponytail_gain", metrics_nodes.metrics_ponytail_gain_node)
    builder.add_edge("metrics_compute", "metrics_ponytail_gain")
    builder.add_edge("metrics_ponytail_gain", "exit_draft")


def _wire_p13(builder: StateGraph) -> None:
    """Wires test-hardening's node cluster (test_hardening_nodes.py). "next" from test_hardening_exit_check routes to END for now
    (metrics-report, the real next stage, doesn't exist yet). Verification status: NOT exercised against a
    real sandbox, same caveat as quality-remediation/security-remediation/audit-cluster."""
    builder.add_node("test_hardening_run_tests", test_hardening_nodes.test_hardening_run_tests_node)
    builder.add_node("test_hardening_regression_gate", test_hardening_nodes.test_hardening_regression_gate_node)
    builder.add_node("test_hardening_flake_triage", test_hardening_nodes.test_hardening_flake_triage_node)
    builder.add_node("test_hardening_mint_tickets", test_hardening_nodes.test_hardening_mint_tickets_node)
    builder.add_node("test_hardening_exit_check", test_hardening_nodes.test_hardening_exit_check_node)
    builder.add_node("test_hardening_exit_escalate", test_hardening_nodes.test_hardening_exit_escalate_node)

    builder.add_conditional_edges(
        "test_hardening_run_tests", test_hardening_nodes.make_test_hardening_route_after_run(), {"regression": "test_hardening_regression_gate", "triage": "test_hardening_flake_triage"}
    )
    builder.add_edge("test_hardening_regression_gate", "test_hardening_run_tests")  # resumes to re-run after a human fixes the regression out-of-band
    builder.add_edge("test_hardening_flake_triage", "test_hardening_mint_tickets")
    builder.add_edge("test_hardening_mint_tickets", "test_hardening_exit_check")
    builder.add_conditional_edges(
        "test_hardening_exit_check", test_hardening_nodes.make_test_hardening_route_after_exit(), {"next": "metrics_compute", "escalate": "test_hardening_exit_escalate"}
    )
    builder.add_edge("test_hardening_exit_escalate", "test_hardening_flake_triage")


def _wire_p10(builder: StateGraph) -> None:
    """Wires security-remediation's bespoke node cluster (quality_security/security_nodes.py) -- NOT exercised against a
    real sandbox yet, see security_nodes.py's own module docstring for exactly what's unverified.
    security_gate_check's "next" routes into adversarial-audit's own draft node."""
    builder.add_node("security_scan", security_nodes.security_scan_node)
    builder.add_node("security_triage", security_nodes.security_triage_node)
    builder.add_node("security_ledger_write", security_nodes.security_ledger_write_node)
    builder.add_node("security_fix", security_nodes.security_fix_node)
    builder.add_node("security_gate_check", security_nodes.security_gate_check_node)
    builder.add_node("security_human_gate", security_nodes.security_human_gate_node)

    r_security_entry_name = _wire_rebuild(builder, REBUILD_FOR_P10)

    builder.add_edge("security_scan", "security_triage")
    builder.add_edge("security_triage", "security_ledger_write")
    builder.add_edge("security_ledger_write", "security_fix")
    builder.add_edge("security_fix", r_security_entry_name)
    builder.add_conditional_edges(
        "security_gate_check",
        security_nodes.make_security_route_after_gate(),
        {"next": "adversarial-audit_draft", "retry": "security_scan", "escalate": "security_human_gate"},
    )
    builder.add_edge("security_human_gate", "security_scan")


def _wire_rebuild(builder: StateGraph, spec: rebuild.RebuildSpec) -> str:
    """Adds one RebuildSpec's rebuild/fix/escalate nodes and routing to `builder`. Returns the
    rebuild node's own name -- the edge callers should route *into* to enter this R placement."""
    rebuild_name = f"{spec.key}_rebuild"
    fix_name = f"{spec.key}_fix"
    escalate_name = f"{spec.key}_escalate"

    builder.add_node(rebuild_name, rebuild.make_rebuild_node(spec))
    builder.add_node(fix_name, rebuild.make_fix_node(spec))
    builder.add_node(escalate_name, rebuild.make_escalate_node(spec))

    builder.add_conditional_edges(
        rebuild_name,
        rebuild.make_route_after_rebuild(spec),
        {"next": spec.next_node, "fix": fix_name, "escalate": escalate_name},
    )
    builder.add_edge(fix_name, rebuild_name)
    builder.add_edge(escalate_name, rebuild_name)
    return rebuild_name


def _wire_stage(builder: StateGraph, stage_spec: StageSpec, next_draft_name: str) -> None:
    """Registers one StageSpec's full draft->audit->[verify]->gate/auto_approve subgraph and
    routes its exit into `next_draft_name`. Extracted from build_graph()'s STAGES loop (which
    still computes next_draft_name for each STAGES entry and calls this) so adversarial-audit/b/d -- which sit
    outside the flat STAGES list because finding-cluster's bespoke cluster interrupts the otherwise-linear
    chain -- can reuse the exact same per-stage wiring with an explicit custom next target,
    without fighting STAGES' automatic "next entry in the list" assumption."""
    draft_name = f"{stage_spec.key}_draft"
    audit_name = f"{stage_spec.key}_audit"
    gate_name = f"{stage_spec.key}_gate"
    auto_approve_name = f"{stage_spec.key}_auto_approve"

    builder.add_node(draft_name, make_draft_node(stage_spec))
    builder.add_node(audit_name, make_audit_node(stage_spec))
    builder.add_node(gate_name, make_gate_node(stage_spec))
    builder.add_node(auto_approve_name, make_auto_approve_node(stage_spec))

    builder.add_conditional_edges(
        draft_name,
        make_route_after_draft(stage_spec),
        {
            "gate": audit_name,
            "auto_approve": auto_approve_name,
            "needs_clarification": END,
            "already_approved": next_draft_name,
        },
    )

    if stage_spec.deterministic_verify is not None:
        # Real script/parse gate inserted between audit and gate -- fail routes back to draft
        # (with feedback context) up to max_verify_cycles, then to a human-interrupt
        # escalation, never straight through to gate/auto_approve. Byte-identical to today
        # when deterministic_verify is unset (the `else` branch below).
        verify_name = f"{stage_spec.key}_verify"
        escalate_name = f"{stage_spec.key}_escalate"
        builder.add_node(verify_name, make_verify_node(stage_spec))
        builder.add_node(escalate_name, make_escalate_node(stage_spec))
        builder.add_edge(audit_name, verify_name)
        builder.add_conditional_edges(
            verify_name,
            make_route_after_verify(stage_spec),
            {"gate": gate_name, "retry": draft_name, "escalate": escalate_name},
        )
        builder.add_edge(escalate_name, draft_name)
    else:
        builder.add_edge(audit_name, gate_name)

    builder.add_edge(gate_name, next_draft_name)
    builder.add_edge(auto_approve_name, next_draft_name)


# Every standalone StageSpec (not in the flat STAGES list, because a bespoke cluster sits between
# it and its neighbors -- see audit-cluster's own note above). intake_node's setdefault/reset loops need
# every stage key that will ever appear in GraphState.stages, not just STAGES' own five.
_STANDALONE_STAGE_SPECS: list[StageSpec] = [
    APP_DISCOVERY_SPEC,
    BROWNFIELD_BASELINE_SPEC,
    ADVERSARIAL_AUDIT_SPEC,
    DEDUP_SPEC,
    LICENSE_AUDIT_SPEC,
    EXIT_SPEC,
]
_ALL_STAGE_SPECS: list[StageSpec] = STAGES + _STANDALONE_STAGE_SPECS
_STAGE_KEYS = [stage.key for stage in _ALL_STAGE_SPECS]
_RENDER_MARKDOWN_BY_STAGE = {stage.key: stage.render_markdown for stage in _ALL_STAGE_SPECS}


class _TracedStateGraph(StateGraph):
    """Wraps every node callable in a telemetry span at the single choke point all 51
    add_node call sites flow through -- no per-node edits, no per-cluster wiring."""

    def add_node(self, node, action=None, **kwargs):  # type: ignore[override]
        if isinstance(node, str) and callable(action):
            action = telemetry.traced_node(node, action)
        return super().add_node(node, action, **kwargs)


def build_graph() -> StateGraph:
    builder = _TracedStateGraph(GraphState)
    builder.add_node("intake", intake_node)
    builder.add_node("scaffold", preflight_nodes.scaffold_node)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "scaffold")
    # Suitability first: app discovery decides whether this workflow applies at all, before
    # tech-stack detection, before brownfield-baseline's human ratification gate, and before anything is written to
    # the repository (see preflight_nodes.scaffold_finalize_node).
    builder.add_edge("scaffold", "app_discovery_pre")

    post_stage_rebuild_entry_name = {key: _wire_rebuild(builder, spec) for key, spec in POST_STAGE_REBUILD.items()}
    _wire_app_discovery(builder)
    _wire_brownfield(builder)
    _wire_p8(builder)
    _wire_p10(builder)
    _wire_audit_cluster(builder)
    _wire_p13(builder)
    _wire_p14(builder)
    _wire_p15(builder)

    for index, stage_spec in enumerate(STAGES):
        if stage_spec.key == STAGES[0].key:
            # tech-stack exits into the brownfield branch, not straight into raw-requirements --
            # _wire_app_discovery owns that edge (and everything before it).
            next_draft_name = "manifest_branch"
        elif stage_spec.key in post_stage_rebuild_entry_name:
            # This stage has an R placement immediately after it -- route into R's rebuild node,
            # never straight to the next stage's draft (checked before the plain "next stage in
            # STAGES" case below, since R's own next_node is what eventually reaches that draft).
            next_draft_name = post_stage_rebuild_entry_name[stage_spec.key]
        elif index + 1 < len(STAGES):
            next_draft_name = f"{STAGES[index + 1].key}_draft"
        else:
            next_draft_name = END

        _wire_stage(builder, stage_spec, next_draft_name)

    return builder


def compile_graph():
    builder = build_graph()
    checkpointer = InMemorySaver()
    store = InMemoryStore()
    # Async checkpoint durability (Section 3.5): "async" is the documented
    # default for invoke/stream/astream_events, so not overriding it here is
    # sufficient; noted explicitly rather than left as an unremarked default.
    return builder.compile(checkpointer=checkpointer, store=store)


graph = compile_graph()
