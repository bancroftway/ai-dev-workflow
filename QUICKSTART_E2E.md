# Quick Start: Run E2E Scenarios Now

**TL;DR**: Foundation is done. Ready to test.

## Prerequisites

```bash
# 1. Get GitHub tokens (both required)
gh auth status  # If no output, run: gh auth login

export E2E_GITHUB_TOKEN="ghp_xxxxxx"  # PAT with contents:write
export GITHUB_TOKEN="gho_xxxxxx"      # Copilot SDK token (from above)

# 2. Docker Desktop running
docker ps  # Confirm no errors

# 3. Terminal in agent/ directory
cd agent
```

## Run All 5 Scenarios

```bash
uv run python test_e2e_scenarios.py
```

That's it. Watch for:
- ✓ **PASS** — Stage succeeded
- ✗ **FAIL** — Stage failed; check logs
- Timeout — 1+ hour; check Docker

Results: `e2e_results.json` and stdout logs.

## Run One Scenario Manually

```bash
# Scenario 2: Next.js + FastAPI monorepo (the main test)
uv run python run_headless.py \
  bancroftway empty_sample_repo my-test-branch \
  --requirements-file <(echo "# Basic app") \
  --greenfield-stack nextjs-fastapi \
  --discard-sandbox
```

## If Something Fails

1. **Check logs**: Look for `stack_discovery` or `exec_in_sandbox` errors
2. **Restart from checkpoint**: Use `--thread` to resume
3. **Check Docker**: `docker ps`, `docker logs <container>`
4. **See full guide**: Read `E2E_SCENARIOS.md` (360 lines, comprehensive)

## What Was Done

- ✓ Custom-agent SDK integration
- ✓ Stack-discovery mechanism (fixes monorepo root issue)
- ✓ Rebuild gate refactored
- ✓ E2E scenario harness
- ✗ (deferred post-E2E) Graph restructure 17→8 stages
- ✗ (deferred post-E2E) Gate refactors + agent file migration

See `IMPLEMENTATION_STATUS.md` for full status.

## Expected Results

| Scenario | Tech Stack | Difficulty | Expected |
|----------|-----------|------------|----------|
| 1 | nextjs-plain | 1 | ✓ PASS |
| 2 | nextjs-fastapi | 2 | ✓ PASS (monorepo test) |
| 3 | dotnet-console | 2 | ✓ PASS |
| 4 | nextjs-python-monorepo | 4 | ✓/⚠️ PASS or edge case |
| 5 | react-django-cli | 5 | ⚠️ Stress test |

---

**After all 5 pass**: Continue with gate refactors and graph restructuring.

**If any fails**: Identify the stage, fix the code (likely `stack-discovery-audit.md` prompt or command execution), rerun.

**Estimated wall-clock**: 2-4 hours total for all 5.
