# E2E Test Scenarios

Five progressively more complex scenarios against `bancroftway/empty_sample_repo` to validate the stack-discovery pivot and test the full pipeline.

**Status**: Foundation code complete (step 0-1 committed). Ready to execute.

## Prerequisites

1. **GitHub Tokens** (both required):
   - `E2E_GITHUB_TOKEN`: Fine-grained PAT with `contents: write` to `bancroftway/empty_sample_repo`
   - `GITHUB_TOKEN`: Copilot SDK token (get from `gh auth status`)

2. **Environment**:
   - Docker Desktop running (the sandbox runs in a container)
   - `uv` installed (`uvenv` manages Python)
   - Terminal in `agent/` directory

3. **Repo Access**:
   - `empty_sample_repo` exists in `bancroftway` org
   - All workflow branches can be deleted/recreated

## Running Scenarios

### Run All 5
```bash
cd agent
export E2E_GITHUB_TOKEN="ghp_xxx..."
export GITHUB_TOKEN="gho_xxx..."
uv run python test_e2e_scenarios.py
```

Each scenario runs in sequence. Wall clock time: ~2-4 hours total (25-40+ LLM calls per run).

Results written to `e2e_results.json`.

### Run One Specific Scenario
```bash
cd agent
uv run python run_headless.py \
  bancroftway empty_sample_repo my-test-branch \
  --requirements-file /path/to/requirements.md \
  --greenfield-stack nextjs-fastapi \
  --discard-sandbox
```

Available stacks (from `src/templates/tech_stacks/*.md`):
- `nextjs-plain` — React only
- `nextjs-fastapi` — React + Python FastAPI
- `dotnet-console` — C# console app
- `nextjs-python-monorepo` — Multi-app monorepo
- `react-django-cli` — Complex SPA + backend + CLI

### Resume a Failed Run
```bash
cd agent
uv run python run_headless.py \
  bancroftway empty_sample_repo same-branch \
  --requirements-file /path/to/requirements.md \
  --thread <original-thread-id> \
  --discard-sandbox
```

The `--thread` flag reattaches to the same sandbox/state and skips already-approved stages.

## The 5 Scenarios

### Scenario 1: Simple React App (Difficulty 1)
**Tech Stack**: `nextjs-plain`

Basic Next.js app. One page. One test. Tests the entire happy path with minimal complexity.

**Expected**: ✓ Should pass easily if the foundation works.

**If it fails**: Likely a schema issue, import error, or session creation error in `stack_discovery.py`.

---

### Scenario 2: Next.js + FastAPI (Difficulty 2)
**Tech Stack**: `nextjs-fastapi`

Two apps (frontend + backend) in a monorepo. This is the **main test case from the postmortem** — rebuild gate must discover `apps/web` as root (not repo root) to avoid the "bare tsc at repo root" failure (issues #2/#3/#7).

**Expected**: ✓ `stack_discovery` should find `apps/web` correctly and rebuild should pass.

**If it fails**: Either:
- `stack_discovery` returned wrong root (audit model didn't read the monorepo structure correctly)
- Python's `cd <root> && <command>` had a shell syntax error
- The command the model discovered was wrong

Check logs for the exact command Python tried to run.

---

### Scenario 3: .NET Console App (Difficulty 2)
**Tech Stack**: `dotnet-console`

C# project. Tests that the stack_discovery mechanism works for .NET (which should discover `dotnet build`, `dotnet test`, `dotnet test` with coverage flags).

**Expected**: ✓ Should pass if the mechanism generalizes across ecosystems.

**If it fails**: .NET-specific discovery isn't working (audit model may not understand `.csproj` files correctly, or Python's dotnet command invocation has a syntax error).

---

### Scenario 4: Monorepo + Tests + Coverage (Difficulty 4)
**Tech Stack**: `nextjs-python-monorepo`

Three separate app directories, each with its own build/test/coverage commands. This tests:
- Multi-root discovery (which of multiple `package.json` files does the model pick?)
- Coverage artifact paths (different formats per app)
- Parallel builds (if the pipeline tries to optimize)

**Expected**: ✓ If scenarios 2-3 work, this should too.

**If it fails**: Edge case in multi-app discovery or coverage parsing.

---

### Scenario 5: Complex Full-Stack (Difficulty 5)
**Tech Stack**: `react-django-cli`

React SPA + Django backend + CLI tool. Covers:
- Mixed ecosystems (JS + Python + shell script)
- E2E tests (full stack, not just units)
- Complex feature acceptance criteria
- Performance/memory constraints

**Expected**: ⚠️ Likely to fail on one or more gates (coverage, E2E, etc.). This is a stress test.

**If it fails**: Identify which gate (rebuild, coverage, E2E, etc.) and focus debugging there.

---

## What to Look For

### Stack Discovery Errors
```
error: stack_discovery.py line X: JSON parse failed
```
The audit model's response wasn't valid JSON. Check:
- Does `stack_discovery_audit.md`'s prompt clearly instruct JSON-only output?
- Is the audit model overfitting to the repo layout and adding commentary?

**Fix**: Tweak the prompt to be more rigid about the output format.

### Command Execution Errors
```
/bin/sh: 1: npx: not found
```
The sandbox doesn't have the tool. Check:
- Is the sandbox image baking the required toolchain?
- Did the build command include shebangs or env vars that don't exist?

**Fix**: Update sandbox image or the discovery prompt to find commands that use pre-installed tools.

### Monorepo Root Errors
```
npm run build # runs at repo root, fails
```
Stack discovery found the wrong root (repo root instead of `apps/web`). This is **the core issue from the postmortem**.

**Fix**: Audit model needs better instructions for finding monorepo markers (`pnpm-workspace.yaml`, `lerna.json`, workspace arrays in `package.json`).

### Coverage Parsing Errors
```
error: _parse_cobertura_counts: expected 'coverage.xml' at path X, got 404
```
Discovery found the wrong artifact path. The model said `coverage/coverage-final.json` but the tool actually wrote to `.nyc_output/coverage.json`.

**Fix**: Improve the coverage discovery in the prompt (more examples, explicit instructions to check test runner config files).

---

## Next Steps After First Run

Based on errors from scenarios 1-2:

1. **If S1 fails**: Fix SDK/import issues in `stack_discovery.py` or `custom_agent_loader.py`.
2. **If S2 fails but S1 passes**: Fix monorepo discovery (the main test case).
3. **If S2 passes**: Run S3-S5 and fix ecosystem-specific edge cases.
4. **After S1-S2 stable**: Continue graph.py refactor (stages 1-8, gate refactors) based on what stage breaks first.

---

## Code Changes in This Version

**Completed**:
- ✓ `custom_agent_loader.py` — YAML frontmatter + markdown body parser
- ✓ `stack-discovery-audit.md` — Agent file (read-only audit role)
- ✓ `stack_discovery.py` — Model + Python execution loop
- ✓ `rebuild.py` — Refactored to use `discover_stack_commands` instead of hardcoded `_resolve_build_command`
- ✓ `copilot_chat_model.py` — Added `custom_agents`/`agent` wiring

**TODO (for next iteration after E2E results)**:
- Graph.py stages 1-8 restructure (currently 17 stages, no change yet)
- Agent files for all remaining stages (custom-agent migration)
- Gate refactors: `test_coverage_gate.py`, `ac_coverage_gate.py`, `test_hardening_nodes.py`
- `_check_model_diversity.py` guardrail script
- Update `AGENTS.md` comment block (generated by `next dev`, but won't change)

---

## Logging & Debugging

Each run writes logs to stderr. Grep for:
- `discover_stack_commands` — audit model interaction
- `exec_in_sandbox` — command execution result
- `stack_discovery` — discovery errors

For detailed audit model conversation:
```bash
grep -i "copilot\|session\|model" <logfile> | tail -50
```

---

## Cleanup Between Runs

The `test_e2e_scenarios.py` runner cleans the sandbox after each scenario (via `--discard-sandbox`). If you manually run `run_headless.py`, clean manually:

```bash
# Delete the workflow branch from the repo
git -C ~/bancroftway/empty_sample_repo push origin :ai-dev-workflow-<thread-id>

# List active sandboxes (from Docker)
docker ps | grep aidw

# Kill a sandbox if stuck
docker stop <container-id>
docker volume rm <volume-id>
```
