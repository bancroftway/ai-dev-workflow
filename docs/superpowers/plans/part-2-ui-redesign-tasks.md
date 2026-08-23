# Part 2 — Run-visibility UI redesign: task breakdown

Spec (binding authority): `C:\Users\jblis\.claude\plans\inside-the-staging-container-sunny-tome.md`,
"Part 2 — Run-visibility UI redesign". Research (ground truth against the real, current code, not
the Spec's own speculative description):
`docs/superpowers/plans/part-2-ui-backend-research-notes.md` and
`docs/superpowers/plans/part-2-ui-frontend-research-notes.md`. Both dispatched fresh against this
branch on 2026-08-23, after Parts 1/3/4 were already built — exactly the "resolve with production
evidence, not speculatively" moment the Spec's own rollout section called for.

## What Parts 1/3/4 actually built that this plan builds on (verified 2026-08-23; full citations in
## the two research docs above)

- The AG-UI/LangGraph bridge is real and already mounted: `agent/main.py` wires
  `add_langgraph_fastapi_endpoint(agent=LangGraphAGUIAgent(name="workflow", graph=graph), path="/")`
  directly over `graph.py`'s compiled graph. The real chain is React
  (`@copilotkit/react-core/v2`) → Next.js `/api/copilotkit` (`CopilotRuntime` in `single-route`
  mode) → FastAPI `/` (`ag_ui_langgraph`). This part of the Spec's premise holds exactly as
  written — nothing to rebuild here.
- **`@copilotkit/react-ui` does not exist in this project** — not installed, not even
  transitively. In the installed `@copilotkit/react-core@1.66.4`, hooks (`useAgent`,
  `useInterrupt`, `useHumanInTheLoop`, `useRenderTool`, `useRenderToolCall`) and chat components
  (`CopilotSidebar`, `CopilotChatInput`, `CopilotChatView`, ...) ship from the **same** module,
  `@copilotkit/react-core/v2`. `AppShell.tsx:3-11` imports both kinds from that one module today.
  See Ruling 1.
- The entire Gate/approval mechanism is a plain LangGraph `interrupt()` call
  (`graph.py:2371-2403`, `make_gate_node`/`gate_node`), resumed only by CopilotKit's `useInterrupt`
  `resolve(payload)` — confirmed by the backend's own code comment. **There is no REST
  approve/reject endpoint anywhere**; `sessions_api.py`'s only named action is
  `"refresh-secrets"`. Only 3 of the 8 stages (`tech-stack`, `specification`, `plan`) ever call
  `interrupt()` at all — the other 5 (`ac-to-tests`, `minimal-code-to-green`, `remediation`,
  `adversarial-compliance`, `metrics-exit`) are deterministic-verify-or-auto-approve only and have
  nothing for a Gate UI to render beyond pass/fail/retry. See Ruling 2/3.
- **No unified event vocabulary exists.** One (`SessionEventType`) existed in the pre-Part-1
  SDK-server implementation and was deliberately deleted as part of the CLI-exec rewrite
  (`copilot_chat_model.py:5-8`). Today, Claude's turn output is one terminal JSON blob
  (`claude -p --output-format json`, read only after the whole turn finishes — no intermediate
  events ever reach this process); Copilot's is a real per-line JSONL stream, but
  `_agenerate_inner` only reads the **last** line (`copilot_chat_model.py:523`) and discards the
  rest. Neither provider's intermediate tool-call/reasoning events survive today. See Ruling 4/5.
- The only per-turn timestamped record that exists at all is `repo_files.py`'s
  `.ai-dev-workflow/ledger.jsonl` (`append_ledger_entry`, written at real call sites already inside
  `graph.py`, e.g. `graph.py:1830-1834`) — real, but ephemeral (lives only inside the disposable
  sandbox's own working tree, reset every fresh run, never git-committed, never DB-backed) and
  coarse (one line per node/turn, not per tool call). It does not survive container teardown. See
  Ruling 4.
- Cost/tokens ARE computed every turn (`self._last_usage`, identical shape in both provider
  modules) but never reach anywhere live: OTEL spans drop the cost field entirely
  (`claude_chat_model.py:314-339` only attaches token counts + model name), and the only place
  anything sums totals is `metrics_nodes._sum_token_usage`, once, at the `metrics-exit` stage,
  reading back the same ephemeral ledger. See Ruling 4 (cost rides the same new plumbing).
- Redaction exists in three separate, non-overlapping places, none of which touches anything a
  human would see from a tool call: `e2e_nodes.py`'s env-unset protects the E2E-booted
  *application under test*, not the coding agent's own output; `copilot_chat_model.secret_env_names()`
  (`--secret-env-vars`) scrubs 4 named Copilot env vars from Copilot's own CLI output only —
  **Claude has no equivalent at all**, a real, previously-undocumented gap; `telemetry.py`'s
  `_B64_RUN` regex scrubs one OTEL span attribute, not a transcript. See Ruling 7.
- `session_store.py`/`project_store.py` do carry `project_id`/`awaiting_gate` (sessions) and
  `default_branch` (projects) at the DB layer, but `SessionResponse`
  (`sessions_api.py:264-299`) declares `awaiting_gate` and **not** `project_id` — Pydantic v2's
  `extra="ignore"` silently drops it, confirmed deliberate by the file's own self-check
  (`sessions_api.py:888`: `assert not hasattr(resp, "project_id")`). See Ruling 8.
- Part 3's Board already made its own live-vs-polling call for itself: `board/page.tsx` polls
  every 15s **on purpose** ("Ruling 5 (this Part's own plan): plain polling, no CopilotKit/AG-UI
  live subscription... that question is Part 2's own, explicitly deferred elsewhere" — its own
  code comment). This plan answers that deferred question for the run-**detail** page only; the
  Board's own polling is out of scope here and stays exactly as Part 3 left it. See Ruling 9.
- `MetricsBar.tsx`'s `costChip`/`Chip` pattern (lines 159-173) is real, reusable cost-display
  infrastructure already exported and already reused by `ReportView.tsx` — the one piece of this
  redesign's target UI that already exists in some form.
- No folding tool-call row, no wall-clock swimlane, no line-level diff/patch viewer exists
  anywhere in the eleven current view components — all four are net-new builds, not reskins.
- Tailwind v4, no config file, no real token system — `globals.css` is the unmodified
  `create-next-app` starter. The actual visual language is ad hoc but consistent inline utility
  classes (neutral borders/text, emerald/amber/red/blue/gray status convention), most densely
  demonstrated in `MetricsBar.tsx` and `AppShell.tsx`. New views should copy that convention
  directly rather than invent tokens that nothing else uses.

## Ruling 1 — "keep react-core, drop react-ui" reframed: there is no package to drop, only
## specific exports to stop rendering

The Spec's Part 2 section frames this as "keep `@copilotkit/react-core`'s hooks... drop
`@copilotkit/react-ui`'s chat components entirely" — a package-boundary split. Research (frontend
notes, Gap 1) confirms `@copilotkit/react-ui` isn't installed at all; every hook and every chat
component ships from the identical module, `@copilotkit/react-core/v2`. **Ruling: the intent is
unchanged (stop rendering a chat transcript, keep the underlying state/subscription layer); the
mechanism is a surgical per-import change, not an uninstall.** Task 7 removes
`CopilotSidebar`/`CopilotChatInput` and their supporting JSX from `AppShell.tsx` while every hook
import (`useAgent`, `useInterrupt`, `useCopilotKit`) stays exactly as it is today, from the same
module. Cost if wrong: none functionally — this is a documentation-accuracy correction, and the
actual diff Task 7 makes is the same regardless of which package-boundary story is told about it.

## Ruling 2 — the Gate UI keeps using `useInterrupt`; it does not migrate to `useHumanInTheLoop`

`useHumanInTheLoop` exists in the installed version but is used nowhere in this codebase (zero
matches). It gates a *registered tool call the agent invokes by name* — a materially different
shape from this backend's actual mechanism, a generic LangGraph `interrupt()` carrying an
arbitrary JSON payload, which `useInterrupt` already handles correctly today for all 3 gated
stages. Migrating to `useHumanInTheLoop` would mean new backend work (the agent would need to
invoke a named, schema'd tool to request human input, replacing `interrupt()` entirely) for zero
new capability. **Ruling: keep `useInterrupt` as the resume mechanism; extend what already works
(add a reject path, Ruling 3) rather than replace it.** `useRenderTool`/`useRenderToolCall` remain
in scope for a *different* concern — Task 8's tool-call-row rendering — and are not affected by
this ruling. Cost if wrong: if a future need for named-tool-level human gating arises (distinct
from stage-gate approval), adopting `useHumanInTheLoop` then is still available; nothing this pass
does forecloses it.

## Ruling 3 — reject is a new, additive resume-value contract; a rejection re-drafts the stage,
## it does not fail the run

No reject path exists today for an ordinary Gate — only "Approve" and (for the escalation card
only) "Acknowledge & retry." Adding one means deciding what a rejected `resolve()` payload means
to the paused `gate_node` on the other side of the resume. **Ruling: `resolve({decision:
"approved"})` stays exactly as today; add `resolve({decision: "rejected", feedback: string})`.**
On the backend, a rejected resume routes the stage back to its own draft node with `feedback`
folded into the next draft prompt as reviewer guidance, rather than ending the run — this mirrors
the pipeline's own existing precedent of a failed verification looping back to fix/re-draft rather
than hard-failing. Task 10's implementer must read `graph.py`'s actual post-`interrupt()` handling
for the 3 gated stages before wiring this — the exact re-entry point (does resuming into the same
node re-run drafting automatically, or does the graph need an explicit edge added?) is a real
verification item, not something this plan pre-solves. Cost if wrong: contained to the 3 gated
stages' own resume logic; a wrong first attempt fails Task 10's own review before merging, not a
silent runtime hazard.

## Ruling 4 — a new unified event schema + durable per-run event log is in scope, additive to (not
## replacing) the existing ephemeral ledger

The Spec assumed reusing "the existing `SessionEventType`-style vocabulary already established in
`copilot_chat_model.py`" — that vocabulary was deleted pre-Part-1 and nothing replaced it
(backend notes, item 1). Building any of Part 2's event-log/tool-call-row/swimlane views against
fabricated or mocked data would repeat this session's own already-learned lesson about gates that
"measured nothing while looking healthy" — the new UI needs a real capture point, not a reskin of
something that already exists, because nothing at this granularity already exists.

**Ruling: Task 1 defines a new unified event schema (Python dataclass/enum, one shape both
providers populate) and a new durable, DB-backed per-run event store** (new table + a
`run_event_store.py` module, same shape as `session_store.py`), written at the same points
`graph.py` already calls `repo_files.append_ledger_entry` — as an *addition* alongside that call,
not a replacement. `metrics_nodes._sum_token_usage` keeps reading the existing ephemeral ledger
exactly as it does today; consolidating the two mechanisms is explicitly out of scope for this
pass (Non-goals) since the ephemeral ledger's consumer already works and is already reviewed —
touching it here would be scope creep, not a requirement. The new store exists to (a) survive
container teardown, so a finished run's event log is still viewable afterward, and (b) let a
mid-run page reconnect (a browser refresh) replay history instead of only showing events since
reconnect. Cost if wrong: the new table is additive and unread by any existing code path;
worst case is dead weight, not breakage of anything already working.

## Ruling 5 — event granularity is provider-asymmetric by verified CLI capability, not by
## assumption either way

Copilot's CLI already emits real per-line JSONL during a turn — today discarded except the last
line (`copilot_chat_model.py:523`) — so tool-call-granularity capture for Copilot is "stop
discarding data that already flows," a cheap, real win (Task 3). Claude's `-p --output-format
json` mode is one-shot/blocking: the whole turn runs before this process reads anything, so no
intermediate tool-call event can reach this process under the current execution model. **Ruling:
Task 4 is a verification spike first** — confirm directly against the installed Claude CLI
(`claude --help`, a live `-p` call) whether any output mode exists that streams incrementally
without abandoning Part 1's already-reviewed backgrounded-process-and-poll execution model. If
one exists and fits, adopt it for symmetric granularity. **If it does not, accept the asymmetry
explicitly** (coarse per-node events for Claude, fine per-tool-call events for Copilot) rather than
building a fake mid-turn signal — this mirrors Part 1's own precedent of accepting an honest
capability ceiling instead of faking parity ("no more kwarg-vocabulary ceilings... both providers
have the same real ceilings" was that Part's own framing for a structurally similar asymmetry).
Every frontend view built on this schema (Tasks 8/9) must render gracefully at either granularity
— a coarse per-node block is still a valid row/segment, just a wider one. Cost if wrong: if Task 4
wrongly concludes "no streaming mode exists" when one does, the fix later is additive (Claude
turns get finer rows), not a rework of anything Task 1-3/8-9 already built.

## Ruling 6 — live transport reuses the existing AG-UI stream via LangGraph custom events, not a
## new transport

The new event store (Ruling 4) needs a live path to the browser while a run is active, not just a
DB row written after the fact. Rather than inventing a second transport (a new WebSocket/SSE
endpoint), **Ruling: emit each captured event as a LangGraph custom stream event on the graph's
existing execution**, relayed automatically by the already-mounted `ag_ui_langgraph` bridge
(backend notes, item 3) as an AG-UI `CUSTOM` event type (confirmed present in `@ag-ui/core`'s
`EventType` enum, frontend notes item 1) over the exact pipe `useAgent`/`AbstractAgent.subscribe`
already read. This has a direct, working precedent in this same codebase: `A2UIMiddleware`
already scans this identical AG-UI stream for an unrelated purpose (turning `a2ui_operations` tool
envelopes into generative-UI surface events) that CopilotKit's own UI doesn't natively support —
proof this class of extension is proven here, not novel risk. Task 2 verifies LangGraph's actual
custom-event-emission API (e.g. `get_stream_writer()`/`adispatch_custom_event` or whatever the
installed `langgraph` version actually names it — confirm against the installed package, not
assumed) before wiring it. Cost if wrong: if LangGraph's custom-event API doesn't fit cleanly, the
fallback is a polling read of the new durable store (Ruling 4) from a new small GET endpoint —
strictly worse UX (matches the Board's own already-accepted polling cadence) but not a dead end.

## Ruling 7 — redaction at the point of capture, reusing `telemetry.py`'s existing regex, applied
## uniformly to both providers

Today nothing captures a coding agent's own raw tool output for human display at all, for either
provider — so today's redaction gaps (zero for Claude, 4 named env vars only for Copilot) have
never mattered in practice. The moment Task 1's event log makes raw tool output visible in a UI
for the first time, that changes. **Ruling: apply the same point-of-capture redaction to every
newly-captured event's payload, for both providers, reusing `telemetry.py`'s existing `_B64_RUN`
regex (`r"[A-Za-z0-9+/=_-]{40,}"`) as the scrub** — already written, already proven against a real
credential-shaped leak scenario (`git_ops.push_head`'s inline credential helper), the ponytail-
ladder "already in this codebase" rung, not a new detector. This is defense-in-depth alongside
(not instead of) Copilot's existing `--secret-env-vars`, and is the *only* redaction Claude's
captured events get — a real security-relevant addition, not a nice-to-have, per this session's
standing instruction to fix rather than ship a spotted vulnerability. Cost if wrong: an
over-broad regex match harmlessly redacts a long non-secret token in a displayed tool-output line
(annoying, not unsafe); an under-broad one is the same gap that exists today, not a regression.

## Ruling 8 — `SessionResponse.project_id` gap gets a one-line fix, bundled into Task 1

A run-detail page needs to know its own project (for breadcrumb nav back to the Board) but
`GET /sessions/{id}` silently drops `project_id` today despite the DB row carrying it
(`sessions_api.py:888`'s own self-check pins this as deliberate-but-incomplete, not a design
choice this plan should preserve). **Ruling: add `project_id: str | None = None` to
`SessionResponse`**, mirroring exactly how `awaiting_gate` was added in Part 3 Task 1/Task 9's
fix — small and mechanical enough to bundle into Task 1 rather than spend a whole task on it.
Cost if wrong: none — purely additive, no existing caller reads a `SessionResponse` positionally.

## Ruling 9 — the Board's own polling (Part 3 Ruling 5) is unaffected; only the run-detail page
## becomes AG-UI-live

Part 3's Board already made its own explicit, ledgered choice to poll rather than subscribe live,
deferring "the CopilotKit/AG-UI live subscription question" to this Part. **Ruling: this plan
answers that question only for the run-**detail** page** (where a human is actively watching one
run) — the Board (where a human is scanning many tickets at once) keeps its existing 15s poll
unchanged. Revisiting the Board's own transport is explicitly out of scope here (Non-goals); it
was Part 3's call to make and it already made it. Cost if wrong: if the Board's polling later
proves too slow, upgrading it to subscribe to the same per-run custom events this Part adds is a
small, additive follow-up — nothing here forecloses it.

## Global Constraints (apply to every task)

- **Never build a view against mocked/fabricated event data.** Every frontend task must be
  verified against a real running session's real events (via the actual dev server + a live
  backend run), not a hand-written fixture — the same standard this session's pipeline-gate-fidelity
  work already established for backend gates.
- **The draft → audit → gate structure and its per-role model selection are untouched.** Nothing
  in this Part changes `graph.py`'s node structure beyond what Ruling 3/6 explicitly call for
  (a reject resume-edge, a custom-event emission point). This Part is additive instrumentation and
  a frontend rebuild, not a pipeline redesign.
- **No new external dependency without checking the ladder first** (already in this codebase →
  stdlib → native platform → already-installed dependency → one line → minimum code). The
  frontend already has everything needed (`@copilotkit/react-core/v2`, `@ag-ui/client`, Tailwind);
  do not add a chart/diff/virtualization library without first confirming plain divs/CSS can't do
  it, matching how the Spec itself already ruled out a chart library for the swimlane.
  Syntax highlighting for the diff viewer is the one place a small, focused library may be
  justified — confirm nothing already in `node_modules` (transitively, via `react-markdown` or
  similar) covers it before adding one.
- **Every non-trivial backend change gets a `_demo()` self-check**, matching every other module
  touched so far this session (`graph.py`, `spec_ledger.py`, etc.).
- **Redact at the point of capture (Ruling 7), not just at render time**, for any new code path
  that captures raw tool/model output.
- **Match the existing ad hoc visual language** (neutral/emerald/amber/red/blue/gray, `text-sm`/
  `text-xs`, `rounded-lg`/`rounded-md`) rather than introducing new design tokens nothing else uses.

## Task 1: Unified event schema + durable per-run event store (backend foundation)

Define a normalized event shape (e.g. `agent/src/run_events.py`: a `RunEventType` enum —
`NODE_STARTED`, `NODE_FINISHED`, `TOOL_CALL` (optional, populated only when granularity allows),
`REASONING`, `GATE_PAUSED`, `GATE_RESOLVED` — plus a `RunEvent` dataclass: `run_id`, `session_id`,
`seq`, `ts`, `stage`, `type`, `summary`, `payload`, `token_usage`). Add a new migration
(`agent/db/migrations/0006_create_run_events.sql`) and a `run_event_store.py` module (mirroring
`session_store.py`'s shape: `append_event`, `list_events(run_id)`) for durable persistence. Wire
`graph.py`'s existing `append_ledger_entry` call sites (e.g. `graph.py:1830-1834`, `:1942`) to also
call `run_event_store.append_event` with the equivalent data, additive to the existing ephemeral
ledger write, not replacing it. Bundle Ruling 8's fix here: add `project_id: str | None = None` to
`SessionResponse` (`sessions_api.py:264-299`), and update its own self-check
(`sessions_api.py:888`) to assert the field IS now populated instead of asserting its absence.
Self-check: a `_demo()` proving an appended event round-trips through the store unchanged, and the
existing `SessionResponse` self-check still passes with the corrected assertion.

## Task 2: Live transport — emit events as LangGraph custom events over the existing AG-UI stream

Verify LangGraph's actual custom-event API against the installed `langgraph` version (check
`agent/.venv`'s installed package, not assumed) — confirm the real mechanism (likely
`get_stream_writer()` inside a node, or an equivalent `adispatch_custom_event` call) for emitting
an arbitrary payload that `ag_ui_langgraph`'s bridge relays as an AG-UI `CUSTOM` event. Wire Task
1's event-append call sites to also emit through this mechanism. Confirm on the frontend side (a
throwaway `agent.subscribe({ onCustomEvent: ... })` or whatever `@ag-ui/client`'s
`AgentSubscriber` actually names that callback — verify against its real type declarations, not
assumed) that a real emitted event is actually received end-to-end, against a real running
session — this is the task that proves Ruling 6's mechanism is real, not just plausible. If the
real API doesn't fit cleanly, fall back to Ruling 6's documented fallback (a small polling GET
endpoint over Task 1's durable store) and record that as a Ruling amendment, not a silent scope
change.

## Task 3: Copilot JSONL capture — stop discarding intermediate lines

In `copilot_chat_model.py`'s `_agenerate_inner`, the JSONL lines already parsed by
`_parse_copilot_jsonl` (lines 110-143) are held in a local list and discarded after only the last
element is read (line 523). Change this to translate each intermediate line into a Task-1-shaped
`RunEvent` (tool-call-granularity where the line represents one) and emit/persist it via Tasks
1/2's new plumbing, while the existing final-result parsing (the last line) is untouched. Self-check:
a `_demo()` feeding a synthetic multi-line JSONL fixture through the translation function and
asserting the right number/shape of events comes out — this is testing the *translation* function
in isolation; Task 14's whole-Part sweep is what proves it against a real Copilot run.

## Task 4: Claude mid-turn granularity — verification spike, then implement or document

Verify directly against the installed Claude CLI (`claude --help`; a live `-p` call, matching this
session's own established verification standard for CLI flags) whether any output mode exists
that streams incrementally without abandoning the backgrounded-process-and-poll execution model
Part 1 built (`cli_agent_exec.py`'s setsid/nohup/pidfile/poll runner). If a viable mode exists,
extend `claude_chat_model.py` to capture and translate its intermediate events the same way Task 3
does for Copilot. If no viable mode exists, do not force a fake signal — write the finding plainly
into this plan's own record (a Ruling 5 amendment) and confirm the frontend (Tasks 8/9) already
degrades gracefully to Claude's coarser per-node granularity, since Ruling 5 already required that
of them. Either outcome is a valid completion of this task; "verified no such mode exists" is not
a blocker.

## Task 5: Redaction at the point of capture

Extract `telemetry.py`'s `_B64_RUN` regex (or an equivalent scrub covering the same class of
long-token secrets) into a small shared helper, and apply it to every event payload Tasks 1/3/4
capture, for both providers, before the event is persisted or emitted — not only at render time.
Self-check: a `_demo()` feeding a payload containing a long base64-shaped token through the
capture path and asserting the token never appears in the stored/emitted event.

## Task 6: (folded into Task 1 — no separate task)

Ruling 8's `SessionResponse.project_id` fix is small enough to bundle into Task 1 directly (see
above); this number is intentionally retired to keep the task list's numbering stable against this
plan's own Rulings section, which already cites "Task 1" for that fix.

## Task 7: Frontend — drop the chat surface from `AppShell.tsx`, keep every hook import

Remove the `CopilotSidebar`/`CopilotChatInput` mount (`AppShell.tsx:261` and its supporting
`GatedChatInput` wiring) and any JSX that exists only to feed the chat feed. Keep every hook
import (`useAgent`, `useInterrupt`, `useCopilotKit`) exactly as they are — this task changes what
renders, not what's imported from `@copilotkit/react-core/v2` (Ruling 1). The existing
`useInterrupt<EscalationPayload>` registration (`AppShell.tsx:128-141`) stays; only its `render`
target changes in Task 10, not here. Confirm via the real dev server that the tab bar (Tech Stack
/ Requirements / Specification / Plan / Build / Quality / Report / Overview) still renders and
still functions with the sidebar gone before calling this done — per this session's standing UI
verification rule.

## Task 8: Frontend — folding tool-call-row component + diff/patch viewer primitive

Build a new component (e.g. `src/components/EventLogView.tsx`) that renders Task 1-4's event
stream (consumed via `useAgent`'s subscription layer, extended per Task 2, falling back to a
fetch of Task 1's durable store for a finished run) as dense, one-line-per-event rows: an icon
derived from event type, a one-line summary, a duration where available. Consecutive same-tool
events collapse into one expandable group. Build the diff/patch viewer as a genuinely separate,
reusable component (e.g. `src/components/DiffView.tsx`) — syntax-highlighted, truncate-and-expand
for long changes — since the Gate UI (Task 10) and this view both need it independently. Reuse
`ViewContainer.tsx`'s wrapping convention and the existing neutral/status-color palette
(`MetricsBar.tsx`/`AppShell.tsx`'s `DOT_CLASS`/`CHIP_CLASS` as the reference). Verify against a
real running session in a real browser, not a static mock.

## Task 9: Frontend — wall-clock swimlane

Build a model-thinking-time vs. tool-time swimlane (two horizontal bars, zoomable, click-to-seek)
from the same event stream's timestamps — `REASONING_START`/`REASONING_END`-equivalent and
`TOOL_CALL_START`/`TOOL_CALL_END`-equivalent spans in Task 1's schema. Absolutely-positioned divs,
no chart library (Global Constraints). Must render sensibly at either granularity Ruling 5 allows
(a Claude run's coarser per-node blocks; a Copilot run's finer per-tool-call blocks) — verify
against one real run of each provider if both are available in this environment, or explicitly
note in the task report which provider(s) could actually be exercised live.

## Task 10: Frontend — Gate UI reject path (approve/reject/edit)

Extend the existing `InterruptCard`/`useOpenInterrupt` plumbing (`interrupt-context.tsx`,
`AppShell.tsx:284-347`) with a real "Reject" control alongside the existing "Approve," submitting
`resolve({decision: "rejected", feedback})` per Ruling 3's contract (a feedback text field is part
of this UI). On the backend, read `graph.py`'s actual current post-`interrupt()` handling for the
3 gated stages and wire the rejected-decision branch to loop back to that stage's own draft node
with `feedback` folded into the next draft prompt, per Ruling 3 — confirm the exact re-entry
mechanics against the real code rather than assuming. `TechStackView.tsx`'s bespoke
edit-then-submit path (which always resolves as an implicit approval today) should gain the same
reject affordance for consistency, not be left as the one stage-tab exception. Verify end-to-end
against a real paused session: reject with feedback, confirm the stage actually re-drafts
incorporating it, not just that the UI submits without erroring.

## Task 11: Frontend — live cost/token display

Thread Task 1's event schema's `token_usage`/cost field into a `MetricsBar`-style chip
(reusing the existing `Chip` component directly, `MetricsBar.tsx`) shown on the new event-log
view, updating live as events arrive rather than only appearing once at Metrics Exit. Check
whether a per-project aggregate (summed across a project's tickets) is realistically reachable
from data this plan actually builds (Task 1's durable store, keyed by `run_id`/`session_id`, would
need a project-scoped query) — if it needs infrastructure beyond this task's own scope, explicitly
defer it as a Non-goal rather than half-building it, and say so in the task report.

## Task 12: Frontend — scroll-anchoring / live-follow

Pin the event-log view (Task 8) to the newest event while a run is active; any manual scroll
(wheel, touch, keyboard) disengages auto-follow; scrolling back to the bottom re-engages it. This
is the Spec's own explicitly-flagged footgun ("a naive implementation fights the user's own scroll
position") — budget real verification time in a real browser with a genuinely live, actively-
updating run, not just a static fixture, before calling this done.

## Task 13: Frontend — wire the new event-log view into `AppShell`'s tab structure

Decide the new view's placement in the existing 8-tab layout (`AppShell.tsx:186-238`) —
`SessionOverview.tsx`'s own comment already calls itself "the seed of a stage timeline," making it
the natural tab to absorb or be replaced by Task 8/9's views; confirm this against the real
component before assuming. Confirm the Board (`board/page.tsx:246-249`) and `SessionHistory.tsx`'s
existing link construction into `/workflow/[owner]/[repo]/[sessionId]/[...branch]` needs no
changes (both already land on the page this task modifies internally, not a different route) —
this is a verification step, not new navigation work.

## Task 14: Final verification sweep

Mirror Part 3's Task 10 exactly: re-run `py_compile` across `agent/src`, the dual-provider whole-
app import, every touched module's own `_demo()`, and `npx tsc --noEmit` across the frontend, all
fresh, as this Part's own closing verification. Additionally — because this Part is UI-heavy in a
way Parts 1/3/4 were not — start the real dev server and the Python backend, provision a real
session, and exercise the new event-log view, swimlane, diff viewer, Gate reject path, and
cost display against that real, live run in a real browser, per this session's standing "test the
feature in a browser before reporting complete" rule; do not report this Part complete on type-
checking and unit self-checks alone. Sweep any Minors carried forward from Tasks 1-13's own
reviews, batched, same as Part 3's precedent.

## Non-goals (this pass)

- Consolidating/removing the existing ephemeral `repo_files.ledger.jsonl` mechanism now that Task
  1 adds a durable alternative — `metrics_nodes._sum_token_usage` already works against it;
  touching that is scope creep, not a requirement of this Part.
- Revisiting the Board's own polling-vs-live choice (Part 3 Ruling 5) — that question was already
  answered for the Board specifically; this Part answers it only for the run-detail page (Ruling 9).
- A per-project cross-run historical analytics dashboard — only this-run and (where cheaply
  reachable, Task 11) this-project-aggregate cost views are in scope.
- Forcing Claude to a mid-turn-streaming execution model if Task 4's verification finds none
  exists — an honest capability asymmetry is accepted, not papered over (Ruling 5).
- A full kanban drag-and-drop board, multi-tenant role management, sandboxing beyond the existing
  per-session container, or non-GitHub repo sources — all already ruled out by the Spec itself and
  unaffected by anything in this Part.
