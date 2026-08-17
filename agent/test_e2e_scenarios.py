#!/usr/bin/env python
"""E2E test runner: 5 scenarios against empty_sample_repo with random tech stacks.

Usage:
    cd agent && uv run python test_e2e_scenarios.py

Environment:
    E2E_GITHUB_TOKEN: GitHub PAT with repo write access for empty_sample_repo
    GITHUB_TOKEN: Copilot SDK token

Each scenario:
    1. Picks a random tech stack
    2. Creates a minimal requirements file
    3. Runs headless pipeline
    4. Cleans workflow branch for next run
    5. Reports pass/fail + logs
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import NamedTuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("test_e2e_scenarios")


class Scenario(NamedTuple):
    """One test scenario."""

    name: str
    """Scenario name."""

    tech_stack: str
    """Tech stack to use (e.g., 'nextjs-fastapi')."""

    requirements: str
    """Requirements markdown (the actual spec)."""

    difficulty: int
    """Difficulty level (1-5)."""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="Simple React app",
        tech_stack="nextjs-plain",
        requirements="""# Simple React App

A basic next.js app with a landing page showing "Hello, World!".

## Acceptance Criteria
- Landing page renders
- Shows greeting text
- Styles are clean
""",
        difficulty=1,
    ),
    Scenario(
        name="Next.js + FastAPI + Tests",
        tech_stack="nextjs-fastapi",
        requirements="""# Next.js + FastAPI Stack

Frontend: Next.js with TypeScript
Backend: FastAPI Python API
Database: Optional

## Acceptance Criteria
- Frontend calls backend API
- API returns JSON response
- Tests pass for both services
- Coverage > 80%
""",
        difficulty=2,
    ),
    Scenario(
        name=".NET Console App with Coverage",
        tech_stack="dotnet-console",
        requirements="""# .NET Console Application

C# console app with unit tests and coverage reporting.

## Acceptance Criteria
- App starts and prints output
- Unit tests run and pass
- Code coverage >= 70%
- No warnings on build
""",
        difficulty=2,
    ),
    Scenario(
        name="Multi-app Monorepo (Node + Python)",
        tech_stack="nextjs-python-monorepo",
        requirements="""# Monorepo with Node + Python

Root level: minimal coordination
apps/web: React + TypeScript
apps/api: FastAPI
shared: common code

## Acceptance Criteria
- Both apps build independently
- Web calls API correctly
- Tests for each app pass
- Coverage > 75% per app
""",
        difficulty=4,
    ),
    Scenario(
        name="Complex SPA + Backend + CLI",
        tech_stack="react-django-cli",
        requirements="""# Complex Full-Stack App

React SPA (TypeScript)
Django REST API
CLI tool for management

## Acceptance Criteria
- SPA loads and renders
- API handles CRUD operations
- CLI tool works end-to-end
- All three parts tested
- E2E tests pass for critical user flows
- No memory leaks
- Deployment-ready
""",
        difficulty=5,
    ),
]


async def run_scenario(scenario: Scenario, scenario_num: int, total: int) -> dict[str, bool | str]:
    """Run one E2E scenario against empty_sample_repo.

    Returns: {"passed": bool, "error": str | None, "logs": str}
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Scenario {scenario_num}/{total}: {scenario.name} (difficulty={scenario.difficulty})")
    logger.info(f"Tech stack: {scenario.tech_stack}")
    logger.info(f"{'='*60}\n")

    # Generate a unique thread ID for this scenario
    thread_id = f"e2e-s{scenario_num}-{uuid.uuid4().hex[:8]}"
    branch_name = f"ai-dev-workflow-{thread_id}"

    try:
        # Create temp requirements file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(scenario.requirements)
            req_file = f.name

        logger.info(f"Running: python run_headless.py bancroftway empty_sample_repo {branch_name}")
        logger.info(f"  --requirements-file {req_file}")
        logger.info(f"  --greenfield-stack {scenario.tech_stack}")
        logger.info(f"  --thread {thread_id}")

        # Run the headless pipeline
        result = subprocess.run(
            [
                "python",
                "run_headless.py",
                "bancroftway",
                "empty_sample_repo",
                branch_name,
                "--requirements-file",
                req_file,
                "--greenfield-stack",
                scenario.tech_stack,
                "--thread",
                thread_id,
                "--discard-sandbox",
            ],
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour per scenario
            cwd=Path(__file__).parent,
        )

        logs = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0

        if passed:
            logger.info(f"✓ PASSED")
        else:
            logger.error(f"✗ FAILED with exit code {result.returncode}")
            logger.error(f"Last 500 chars of output:\n{logs[-500:]}")

        return {"passed": passed, "error": None if passed else logs[-1000:], "logs": logs}

    except subprocess.TimeoutExpired:
        logger.error(f"✗ TIMEOUT after 1 hour")
        return {"passed": False, "error": "Timeout after 1 hour", "logs": ""}
    except Exception as e:
        logger.error(f"✗ EXCEPTION: {e}")
        return {"passed": False, "error": str(e), "logs": ""}
    finally:
        # Clean up temp file
        try:
            os.unlink(req_file)
        except:
            pass


async def main() -> int:
    """Run all 5 scenarios and report results."""
    # Validate environment
    git_token = os.environ.get("E2E_GITHUB_TOKEN")
    copilot_token = os.environ.get("GITHUB_TOKEN")
    if not git_token or not copilot_token:
        logger.error("E2E_GITHUB_TOKEN and GITHUB_TOKEN must be set in environment")
        return 1

    logger.info(f"Running {len(SCENARIOS)} E2E scenarios against bancroftway/empty_sample_repo")
    logger.info(f"Tokens found: git={bool(git_token)}, copilot={bool(copilot_token)}")

    results: list[dict] = []
    passed_count = 0

    for i, scenario in enumerate(SCENARIOS, 1):
        result = await run_scenario(scenario, i, len(SCENARIOS))
        result["scenario"] = scenario.name
        result["tech_stack"] = scenario.tech_stack
        result["difficulty"] = scenario.difficulty
        results.append(result)

        if result["passed"]:
            passed_count += 1

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info(f"SUMMARY: {passed_count}/{len(SCENARIOS)} scenarios passed")
    logger.info(f"{'='*60}\n")

    for i, result in enumerate(results, 1):
        status = "✓ PASS" if result["passed"] else "✗ FAIL"
        logger.info(f"{status} | S{i} | {result['scenario']} | {result['tech_stack']}")

    # Write detailed results to file
    report_file = Path(__file__).parent / "e2e_results.json"
    with open(report_file, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"\nDetailed results written to: {report_file}")

    return 0 if passed_count == len(SCENARIOS) else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
