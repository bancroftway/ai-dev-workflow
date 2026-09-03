"""Consolidated schema for stage 6 (remediation: quality+security+dedup+license)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .schemas import ClarifyingQuestion, NonBlankStr, PresenceList


class RemediationDraftResponse(BaseModel):
    """What the remediation stage must report (prompts/remediation_draft.md).

    The fields below exist so the stage's output is CHECKABLE rather than prose: a summary alone
    cannot be compared against the scanner's own finding ids, and the previous version of this stage
    returned exactly that -- one free-text string, from a prompt that was handed no findings at all.
    It therefore fixed nothing, and 43 gating findings (including a critical pre-auth RCE in the
    pinned Next.js version, with a fixed_version published) sailed through to the metrics gate.
    """

    readiness: bool
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    remediation_summary: NonBlankStr = Field(
        description="What was actually changed, grouped by kind: dependencies upgraded, code "
        "findings fixed, findings deliberately left alone. The one field the deterministic gate "
        "can't already cross-check against the scan -- never blank.",
    )
    findings_addressed: PresenceList = Field(
        description="The `id` of each finding fixed, copied verbatim from repo-scan-latest.json, "
        "or an explicit absent+reason when nothing was fixed this run.",
    )
    dependencies_upgraded: PresenceList = Field(
        description='One entry per package moved, as "name: old -> new" (e.g. "next: 15.4.6 -> '
        '15.4.9"), or an explicit absent+reason when no dependency was upgraded.',
    )
    known_gaps: PresenceList = Field(
        description="Every finding deliberately NOT fixed, each with its real reason, or an "
        "explicit absent+reason when nothing was left open. An honest gap is a valid outcome; a "
        "silently skipped finding is not.",
    )

    skills_invoked: list[str] = Field(
        default_factory=list,
        description="Exact names of skills you invoked with your `skill` tool this turn (a plugin "
        "slash command counts -- report its bare name, e.g. 'code-review'), plus any subagents you "
        "launched with your subagent (Agent/Task) tool, reported as 'agent:<name>'. Only what you ACTUALLY invoked. "
        "Cross-checked against the session's own recorded invocations -- a name you did not "
        "invoke shows up as an unsubstantiated claim. An empty list is a valid answer.",
    )


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.schemas_remediation`
    _base_kwargs: dict[str, Any] = dict(
        readiness=True,
        remediation_summary="Upgraded next, fixed 2 sast findings, left 1 gap.",
        findings_addressed=PresenceList(status="present", values=["aaa111"]),
        dependencies_upgraded=PresenceList(status="present", values=["next: 15.4.6 -> 15.4.9"]),
        known_gaps=PresenceList(status="absent", reason="nothing left unfixed"),
    )

    # NonBlankStr: remediation_summary rejects blank/whitespace-only -- the one field the
    # deterministic gate can't already cross-check against the scan, so it must always say
    # something real.
    assert RemediationDraftResponse(**_base_kwargs).remediation_summary.startswith("Upgraded")
    try:
        RemediationDraftResponse(**{**_base_kwargs, "remediation_summary": "   "})
        raise AssertionError("expected ValidationError for blank remediation_summary")
    except ValidationError:
        pass

    # The 3 PresenceList fields: present/absent both validate, and round-trip through JSON as the
    # typed shape (not silently flattened back to a bare list).
    present_resp = RemediationDraftResponse(
        **{
            **_base_kwargs,
            "known_gaps": PresenceList(status="present", values=["bbb222: no fixed version yet"]),
        }
    )
    assert present_resp.known_gaps.values == ["bbb222: no fixed version yet"]
    reloaded = RemediationDraftResponse.model_validate_json(present_resp.model_dump_json())
    assert reloaded == present_resp
    for _field in ("findings_addressed", "dependencies_upgraded", "known_gaps"):
        assert "status" in json.loads(present_resp.model_dump_json())[_field], _field

    absent_resp = RemediationDraftResponse(
        **{
            **_base_kwargs,
            "findings_addressed": PresenceList(status="absent", reason="nothing fixed"),
            "dependencies_upgraded": PresenceList(status="absent", reason="no upgrades"),
        }
    )
    assert absent_resp.findings_addressed.values == []
    assert absent_resp.dependencies_upgraded.values == []

    # Legacy bare-list/None coercion (older sidecars/model output stored these as plain
    # list[str]/None before this task) -- must still validate, not crash.
    legacy_resp = RemediationDraftResponse.model_validate(
        {
            **_base_kwargs,
            "findings_addressed": ["ccc333"],
            "dependencies_upgraded": [],
            "known_gaps": None,
        }
    )
    assert legacy_resp.findings_addressed.status == "present"
    assert legacy_resp.findings_addressed.values == ["ccc333"]
    assert legacy_resp.dependencies_upgraded.status == "absent"
    assert legacy_resp.known_gaps.status == "absent"

    print("schemas_remediation self-check: all assertions passed")