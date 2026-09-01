"""Runnable-application discovery content models.

DiscoveredApp is what the deterministic scan (app_discovery.classify_candidates/candidates_to_apps)
reports per app -- no LLM classification stage exists anymore; a repository's app list is grounded
entirely in matched marker files.

Deliberately no `id` field on DiscoveredApp: graph.py's _extract_ids walks every content dict for
"id" keys and folds them into StageState.used_ids -- the US-####/AC-####.# identifier registry.
An app id would pollute that namespace. `path` is the identity here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
