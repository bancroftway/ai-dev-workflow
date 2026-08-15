"""Runnable-application discovery content models.

The workflow only applies to repositories containing at least one startable application (web app,
API, or Azure Function) -- a library/package repo has nothing for P4's tests to exercise or P6's
code to make green. These models carry what the discovery stage found; the suitability *verdict*
is never taken from the model (see app_discovery.decide_suitability).

Deliberately no `id` field on DiscoveredApp: graph.py's _extract_ids walks every content dict for
"id" keys and folds them into StageState.used_ids -- the US-####/AC-####.# identifier registry.
An app id would pollute that namespace. `path` is the identity here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .schemas import ClarifyingQuestion

AppClass = Literal["web", "api", "azure_function", "mobile", "library", "cli", "unknown"]


class DiscoveredApp(BaseModel):
    path: str = Field(description="Repo-relative app root ('.' for a single-app repo).")
    name: str
    app_class: AppClass
    runtime: str = Field(description="e.g. 'dotnet10', 'node22', 'python3.12'.")
    start_command: str | None = Field(
        default=None,
        description="How this app is started from the repo root, e.g. "
        "'dotnet run --project src/Api', 'npm run dev', 'func start'. None for a library.",
    )
    port: int | None = Field(
        default=None, description="Port the app listens on, when a file states it. Never guessed."
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Concrete 'path: matched marker' facts. Never speculation -- an app with no "
        "evidence is dropped by the deterministic decision node.",
    )
    confidence: Literal["high", "medium", "low"] = "medium"


class RunnableAppReport(BaseModel):
    apps: list[DiscoveredApp] = Field(
        default_factory=list,
        description="Every application found, INCLUDING mobile and library ones -- the "
        "suitability decision needs to see what was rejected, not just what passed.",
    )
    suitable: bool = Field(
        default=False,
        description="Advisory only. app_discovery.decide_suitability recomputes this "
        "deterministically from `apps` and ignores whatever is reported here.",
    )
    rejection_reasons: list[str] = Field(default_factory=list)
    notes: str = ""


class AppDiscoveryDraftResponse(BaseModel):
    readiness: bool = Field(
        description="True if this report is complete. 'No runnable app exists' is a complete "
        "answer, not a reason to withhold readiness."
    )
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    app_detection: RunnableAppReport | None = Field(default=None)


