# Part 2 UI/frontend research notes: current-state ground truth for the run-visibility redesign

**Ref used:** working tree of the checked-out branch `feature/react-langgraph` (no commit pinned;
this is uncommitted-changes-included ground truth, read directly off disk with `Read`/`Grep`/`Glob`
and off the installed `node_modules` tree for library-shape claims). Read-only research: nothing was
edited, nothing was committed.

**Bottom line up front:** the redesign's architectural premise ("keep `@copilotkit/react-core`'s
hooks, drop `@copilotkit/react-ui`'s chat components") describes a package split that does not exist
in this app. `@copilotkit/react-ui` is not installed — at all, not even transitively. Every hook
*and* every chat component (hooks to keep and components to drop alike) ships from the same module:
`@copilotkit/react-core/v2`. See Section 1 and "Gaps" item 1.

---

## 1. Package versions and hook availability

`package.json` (repo root), dependencies block:

```json
12	    "@ag-ui/a2ui-middleware": "^0.0.10",
13	    "@ag-ui/langgraph": "^0.0.42",
14	    "@copilotkit/a2ui-renderer": "^1.66.4",
15	    "@copilotkit/react-core": "^1.66.4",
16	    "@copilotkit/runtime": "^1.66.4",
```

`@copilotkit/react-ui` is **not present anywhere** in `package.json` (dependencies or
devDependencies). Installed versions, confirmed from each package's own `package.json`:

| Package | Declared range | Installed |
|---|---|---|
| `@copilotkit/react-core` | `^1.66.4` | `1.66.4` |
| `@ag-ui/langgraph` | `^0.0.42` | `0.0.42` |
| `@ag-ui/a2ui-middleware` | `^0.0.10` | `0.0.10` |
| `@ag-ui/core` (transitive, via react-core) | — | `0.0.57` |
| `@ag-ui/client` (transitive, via react-core) | — | `0.0.57` |
| `@copilotkit/react-ui` | — | **not installed** (absent from `node_modules/@copilotkit/` entirely) |

`ls node_modules/@copilotkit/` lists exactly: `a2ui-renderer, channels-core, channels-intelligence,
channels-slack, channels-teams, channels-ui, core, license-verifier, react-core, runtime,
runtime-client-gql, shared, web-components, web-inspector`. No `react-ui` directory.

### Where the hooks and components actually live

`node_modules/@copilotkit/react-core/package.json`'s `exports` map:

```json
"exports": {
  ".": { "import": "./dist/index.mjs", "require": "./dist/index.cjs" },
  "./v2": { "import": "./dist/v2/index.mjs", "require": "./dist/v2/index.cjs" },
  "./v2/context": { ... },
  "./v2/headless": { ... },
  "./v2/styles.css": "./dist/v2/index.css"
}
```

`node_modules/@copilotkit/react-core/dist/v2/index.d.mts` re-exports (barrel, aliased — real names
shown here) **all four** hooks the plan wants to keep, confirmed present in the installed version:

- `useAgent` ✅
- `useInterrupt` ✅
- `useHumanInTheLoop` ✅
- `useRenderTool` ✅

...from the exact same module that also exports `CopilotChat`, `CopilotSidebar`, `CopilotPopup`,
`CopilotChatInput`, `CopilotChatView`, `CopilotChatMessageView`, `CopilotChatToolCallsView`,
`useCopilotKit`, `useFrontendTool`, `useComponent`, `useDefaultRenderTool`, `useRenderToolCall`, etc.
There is no separate "hooks package" vs. "components package" split — `@copilotkit/react-core/v2`
is both.

The package ships its own bundled skill docs (`node_modules/@copilotkit/react-core/skills/react-core/`)
which state this explicitly. `SKILL.md` line 93:

> `CopilotPanel` does not exist. v2 chat components ship from `react-core/v2` — **not** `react-ui`
> (v2 `react-ui` is CSS-only).

`references/chat-components.md` lines 7-9:

> All chat components live on `@copilotkit/react-core/v2`. The legacy `@copilotkit/react-ui` package
> is v1-only; its `/v2` subpath is a CSS-only import.

### Hook signatures / contracts (quoted from the bundled skill docs, since the installed version's
own docs are more precise than the minified `.d.mts` barrel)

`useHumanInTheLoop` (`references/human-in-the-loop.md`): "`useHumanInTheLoop` is `useFrontendTool`
minus the `handler` plus a `render` that receives a `respond` function. The hook synthesizes a
Promise-based handler — the Promise resolves when `respond(result)` is called. No `respond` call →
infinite hang." Status is camelCase: `"inProgress" | "executing" | "complete"`; `respond` is
`undefined` except during `"executing"`. Unmounting mid-`"executing"` abandons the run (the renderer
is removed on unmount, unlike `useFrontendTool`).

`useRenderTool` / `useDefaultRenderTool` / `useComponent` / `useRenderToolCall`
(`references/rendering-tool-calls.md`): four hooks, distinct roles — `useRenderTool` is the primary
registration hook for a named tool's progress/result UI; `useComponent` registers a *new* render-only
tool; `useDefaultRenderTool` is the sanctioned wildcard fallback; `useRenderToolCall` is a resolver
(not a registration hook) for building a fully custom chat surface — exactly the shape a new
folding-tool-call-row view would use:

```tsx
const { agent } = useAgent({ agentId: "default" });
const renderToolCall = useRenderToolCall();
const toolCalls = agent.messages.flatMap((m) => "toolCalls" in m ? (m.toolCalls ?? []) : []);
```

`useAgent` returns `{ agent }` only — run status lives on `agent.isRunning`, not a separate return
value (confirmed both by the skill doc and by this app's own usage, Section 2 below).

### `@ag-ui/core` — `AbstractAgent` and the event-type union

**`AbstractAgent` is not defined in `@ag-ui/core`.** A repo-wide grep of
`node_modules/@ag-ui/core/dist/index.d.mts` (432KB, 15,947 lines) for `AbstractAgent` returns exactly
one hit, a doc comment: `* Returned by getCapabilities() on AbstractAgent.` (line 3667). The class
itself lives in `@ag-ui/client` (`node_modules/@ag-ui/client/dist/index.d.mts`, 629 lines), which
re-exports everything from `@ag-ui/core` (`export * from "@ag-ui/core"`) and adds the class
hierarchy. Quoted in full (lines 478-540):

```ts
declare abstract class AbstractAgent {
  agentId?: string;
  description: string;
  threadId: string;
  messages: Message[];
  state: State;
  subscribers: AgentSubscriber[];
  isRunning: boolean;
  /** Interrupts emitted by the most recent run that have not yet been resolved.
   *  Populated when RUN_FINISHED arrives with outcome.type === "interrupt".
   *  Cleared when a subsequent run completes successfully. */
  pendingInterrupts: Interrupt[];
  ...
  subscribe(subscriber: AgentSubscriber): { unsubscribe: () => void };
  abstract run(input: RunAgentInput): Observable<BaseEvent>;
  getCapabilities?(): Promise<AgentCapabilities>;
  use(...middlewares: (Middleware | MiddlewareFunction)[]): this;
  runAgent(parameters?: RunAgentParameters, subscriber?: AgentSubscriber): Promise<RunAgentResult>;
  abortRun(): void;
  ...
  addMessage(message: Message): void;
  setState(state: State): void;
  clone(): any;
}
```

`HttpAgent extends AbstractAgent` (line 364) is the concrete class `@ag-ui/langgraph`'s
`LangGraphHttpAgent` (used by this app's backend route, Section 3) builds on.

The `EventType` enum (`@ag-ui/core`, lines 4142-4191), quoted in full — this is the real event
vocabulary a raw AG-UI stream carries, directly relevant to the planned wall-clock swimlane:

```ts
declare enum EventType {
  TEXT_MESSAGE_START, TEXT_MESSAGE_CONTENT, TEXT_MESSAGE_END, TEXT_MESSAGE_CHUNK,
  TOOL_CALL_START, TOOL_CALL_ARGS, TOOL_CALL_END, TOOL_CALL_CHUNK, TOOL_CALL_RESULT,
  THINKING_START,                 // @deprecated Use REASONING_START instead.
  THINKING_END,                   // @deprecated Use REASONING_END instead.
  THINKING_TEXT_MESSAGE_START,    // @deprecated Use REASONING_MESSAGE_START instead.
  THINKING_TEXT_MESSAGE_CONTENT,  // @deprecated Use REASONING_MESSAGE_CONTENT instead.
  THINKING_TEXT_MESSAGE_END,      // @deprecated Use REASONING_MESSAGE_END instead.
  STATE_SNAPSHOT, STATE_DELTA, MESSAGES_SNAPSHOT, ACTIVITY_SNAPSHOT, ACTIVITY_DELTA,
  RAW, CUSTOM,
  RUN_STARTED, RUN_FINISHED, RUN_ERROR,
  STEP_STARTED, STEP_FINISHED,
  REASONING_START, REASONING_MESSAGE_START, REASONING_MESSAGE_CONTENT, REASONING_MESSAGE_END,
  REASONING_MESSAGE_CHUNK, REASONING_END, REASONING_ENCRYPTED_VALUE
}
```

Every event extends `BaseEventSchema` which carries an optional `timestamp: number` (line 4194) —
so `TOOL_CALL_START`/`TOOL_CALL_END` and `REASONING_START`/`REASONING_END` events are individually
timestamped, which is exactly the raw material a thinking-vs-tool-time swimlane needs. Note the
`THINKING_*` family is **deprecated in favor of `REASONING_*`** as of the installed version — new
code should target `REASONING_START`/`REASONING_MESSAGE_*`/`REASONING_END`, not `THINKING_*`.

`@ag-ui/client`'s `AgentSubscriber` interface (lines 240-358) exposes these as typed per-event
callbacks (`onToolCallStartEvent`, `onToolCallEndEvent`, `onReasoningStartEvent`,
`onReasoningEndEvent`, `onRunStartedEvent`, `onRunFinishedEvent`, etc.), registered via
`agent.subscribe(subscriber)` on `AbstractAgent` — a lower-level integration point than `useAgent()`,
which only exposes derived `agent.messages`/`agent.state`, not the raw timestamped event stream.
**Nothing in this codebase currently uses `subscribe()`** (Section 2/5) — building the swimlane will
need this raw-event layer, not the state/messages layer the rest of the app uses today.

---

## 2. The actual current run-visibility page

`src/app/workflow/[owner]/[repo]/[sessionId]/[...branch]/page.tsx` is a **server component**. It does
auth + session-ownership checks, computes `metricThresholds` from env vars, then renders:

```
WorkflowThreadProvider(threadId=sessionId)
  > WorkflowProviders
    > SandboxStatusProvider
      > SandboxSessionBoot (provisions the sandbox)
      > AppShell (owner, repo, workBranch, metricThresholds, resume)
```

It renders **no chat UI itself** — all of that lives in `AppShell` (`src/components/AppShell.tsx`).

`src/app/workflow/providers.tsx` (`WorkflowProviders`), quoted in full:

```tsx
"use client";
import { CopilotKit } from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";
import { A2UIProvider } from "@copilotkit/a2ui-renderer";
...
export function WorkflowProviders({ children }: { children: ReactNode }) {
  return (
    <CopilotKit
      runtimeUrl="/api/copilotkit"
      a2ui={{ catalog }}
      showDevConsole={false}
      onError={(error) => { console.warn(...); }}
    >
      <A2UIProvider catalog={catalog}>{children}</A2UIProvider>
    </CopilotKit>
  );
}
```

### `AppShell.tsx` — what's actually rendered

Confirmed: this **is** genuinely a CopilotKit chat sidebar today, but it's the v2 chat surface, not
`@copilotkit/react-ui`. Import block (`src/components/AppShell.tsx:3-11`):

```tsx
import {
  CopilotChatInput, type CopilotChatInputProps, CopilotSidebar, UseAgentUpdate,
  useAgent, useCopilotKit, useInterrupt,
} from "@copilotkit/react-core/v2";
```

The sidebar mount (`AppShell.tsx:261`):

```tsx
<CopilotSidebar agentId={localAgentId} input={GatedChatInput} />
```

But `AppShell` is **not just a chat transcript** — it's a two-pane layout. To the left/main area it
renders a tab bar with 8 tabs (Tech Stack, Requirements, Specification, Plan, Build, Quality, Report,
Overview — `AppShell.tsx:186-238`), each a dedicated per-stage view component with a colored status
dot computed from `WorkflowState` (`stageGroupDot`, lines 45-56). The `CopilotSidebar` sits alongside
that tabbed content, not instead of it.

Live state comes from (`AppShell.tsx:81-92`):

```tsx
const { threadId, runtimeAgentId, localAgentId } = useWorkflowThread();
const { agent } = useAgent({
  agentId: localAgentId, runtimeAgentId, threadId,
  updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged],
});
...
const state = (agent.state ?? {}) as WorkflowState;
```

`agent.state` cast to `WorkflowState` is the single source of truth nearly every view component
reads (confirmed again per-component in Section 5).

The Gate-approval hook the plan wants is **already there**, just rendered inside the chat sidebar's
feed (`AppShell.tsx:128-141`):

```tsx
useInterrupt<EscalationPayload>({
  agentId: localAgentId,
  render: ({ resolve, event }) => {
    const raw: unknown = event?.value;
    let payload: EscalationPayload = {};
    try { payload = (typeof raw === "string" ? JSON.parse(raw) : raw) ?? {}; } catch { payload = {}; }
    if (typeof payload !== "object" || payload === null) payload = {};
    return <InterruptCard payload={payload} resolve={resolve} />;
  },
});
```

The code's own comment names the exact placement: "renderInChat defaults to true, publishing into
the CopilotSidebar's chat feed, which is mounted around every view below." One tab — `TechStackView`
— deliberately bypasses this generic chat-feed card and renders its own dedicated full-page Gate
editor instead (Section 4/5).

---

## 3. The CopilotKit backend route

`src/app/api/copilotkit/[[...slug]]/route.ts`, quoted in full:

```ts
import { A2UIMiddleware } from "@ag-ui/a2ui-middleware";
import { LangGraphHttpAgent } from "@copilotkit/runtime/langgraph";
import { CopilotRuntime, createCopilotRuntimeHandler } from "@copilotkit/runtime/v2";
import { Agent as UndiciAgent, setGlobalDispatcher } from "undici";
...
setGlobalDispatcher(new UndiciAgent({ bodyTimeout: 0, headersTimeout: 0 }));

const AGENT_URL = process.env.AGENT_URL ?? "http://localhost:8123/";
const workflowAgent = new LangGraphHttpAgent({ url: AGENT_URL });
workflowAgent.use(new A2UIMiddleware({ defaultCatalogId: CATALOG_ID }));

const runtime = new CopilotRuntime({ agents: { workflow: workflowAgent } });

const handler = createCopilotRuntimeHandler({
  runtime, basePath: "/api/copilotkit", mode: "single-route",
});

async function requireAuth(): Promise<Response | null> { ... }
export async function GET(request: Request) { ... return handler(request); }
export async function POST(request: Request) { ... return handler(request); }
```

This **is** a real AG-UI bridge to the Python LangGraph backend — `LangGraphHttpAgent` (from
`@copilotkit/runtime/langgraph`) makes real HTTP calls to `AGENT_URL` (default
`http://localhost:8123/`, the Python agent process), not a mocked or plain-REST arrangement. It is
not a second, independent CopilotKit runtime with its own state; it's the AG-UI-over-HTTP adapter for
the one real backend. `A2UIMiddleware` (from `@ag-ui/a2ui-middleware`) is layered on top of that same
agent to scan tool results for the `a2ui_operations` envelope the Python drafting nodes emit. Both
`CopilotRuntime` and `createCopilotRuntimeHandler` come from the package's `/v2` runtime subpath
(`@copilotkit/runtime` also declares a `./langgraph` and a `./v2` export, confirmed from its own
`package.json` `exports` map), run in `"single-route"` mode, matching the comment: "The installed
client (`@copilotkit/core` 1.66.4) calls `fetchRuntimeInfoSingle` ... directly rather than
auto-detecting REST, so the server must speak the same single-route protocol." A second,
route-level `requireAuth()` check runs before `handler(request)` on both `GET`/`POST`.

---

## 4. Supporting state/context layer

**`src/lib/workflow-thread-context.tsx`** — tiny, single-purpose: provides the
`{threadId, runtimeAgentId: "workflow", localAgentId: "workflow-thread-${threadId}"}` triple every
`useAgent({...})` call needs, via `useWorkflowThread()`. No network code.

**`src/lib/workflow-types.ts`** — pure TypeScript mirror of the Python backend's
`GraphState`/`StageState` (comment: "Mirrors `agent/src/graph.py`'s `GraphState`/`StageState` shape").
Defines `StageStatus`, `StageState`, `WorkflowState`, `PIPELINE_STAGE_ORDER` (10 real stage keys +
labels), `TAB_STAGE_GROUPS`, `GatePayload`, and — the key discriminator for Gate UI — `EscalationPayload`:
a plain approval-gate interrupt carries **no** `type` field; an escalation interrupt always does
(`type?: "cannot_verify" | "verification_cap_exceeded" | ... | string`). Any new Gate UI must
preserve this discrimination rule.

**`src/lib/interrupt-context.tsx`** — `InterruptProvider` / `useOpenInterrupt()`. Holds one
`InterruptInfo` object in `useState`:

```tsx
export interface InterruptInfo {
  open: boolean;
  stage?: string;
  draft?: unknown;
  draftMarkdown?: string;
  fileExisted?: boolean;
  resolve?: (value: unknown) => void;
}
```

This **is** the plumbing that answers both of the task's questions: "is a run paused awaiting Gate
approval" is `interrupt.open && interrupt.stage === "<key>"`; "how does it submit a decision" is
calling `interrupt.resolve(value)` — which is literally the `resolve` callback CopilotKit's
`useInterrupt` hands to `render(...)` in `AppShell.tsx`, stashed into this context by `InterruptCard`'s
effect so any tab (not just the chat feed) can reach it. This context is a thin pass-through only —
it does **not** itself talk to the network; the actual resume/network call happens inside
CopilotKit's `useInterrupt` internals when `resolve()` is invoked.

The actual submission code, verbatim:

Generic approve path (`AppShell.tsx:284-287, 342-347`):
```tsx
const done = (value: unknown) => {
  setInterrupt({ open: false });
  resolve(value);
};
...
<button onClick={() => done({ decision: "approved" })}>Approve</button>
```

Escalation "retry" path (`AppShell.tsx:327-331`, comment above it explains why a scalar, not `{}`):
```tsx
// Scalar resume on purpose: an empty object is classified by LangGraph as an empty
// resume MAP, delivering no value -- the interrupt would re-raise forever.
<button onClick={() => done("retry")}>Acknowledge &amp; retry</button>
```

Tech-stack edit-and-submit path (`TechStackView.tsx:61-68`), the one place that hands back edited
content instead of a bare approval:
```tsx
async function handleSubmit() {
  setSubmitting(true);
  try {
    interrupt.resolve?.({ markdown: text });
  } finally {
    setSubmitting(false);
  }
}
```

**There is no "reject" control anywhere in the current UI for a plain approval Gate.** The only
non-approve action that exists is the escalation card's "Acknowledge & retry," and tech-stack's
submit always resolves with (edited) content — never a rejection. See Gaps item 5.

**`src/lib/agent-client.ts`** — unrelated to Gate submission or AG-UI at all. It's `agentFetch(path,
init)`, a plain `fetch()` wrapper to the Python agent's own REST API (session provisioning/listing —
`sessions/provision`, `sessions?owner=...`), with an optional shared-secret header
(`AIDW_AGENT_SHARED_SECRET`). It never touches the interrupt/resume machinery. If the redesign plan
assumed this file was part of the approve/reject wiring, it isn't.

---

## 5. Existing per-stage view components

All eleven read in full. None contains a folding tool-call row, a wall-clock swimlane, or a real
code diff/patch viewer.

- **`BuildView.tsx`** — two fixed `StageCard`s (AC-to-Tests, Minimal Code to Green): status label,
  cycle/verify/audit-finding counts, and a red box for the last failed verification's feedback text.
  Nothing reusable for tool-calls/diff/cost.
- **`PlanView.tsx`** — `ClarifyingQuestions` + an `A2UISurfaceView` lookup (scans `agent.messages` for
  a tool message carrying an `a2ui_operations` envelope for the `plan` surface) with a
  markdown-rendered fallback. Nothing reusable for tool-calls/diff/cost.
- **`SpecificationView.tsx`** — identical shape to `PlanView`, for the `specification` stage/surface.
- **`QualityView.tsx`** — `FindingsTable` (Severity/Rule/Location/Message/Decision columns,
  lines 18-46) for quality + security remediation findings, a health-score before/after line,
  test-hardening stable/flaky lists, final coverage/traceability numbers. `FindingsTable` is the
  closest existing "dense list of rows" pattern worth reusing stylistically for a tool-call log — but
  it's a findings table, not tool calls. No diff viewer. No cost display here (that's `MetricsBar`).
- **`ReportView.tsx`** — merge-ready banner, PR title/description (`ReactMarkdown`), risk notes, a
  `MetricChips` row (reuses `Chip` from `MetricsBar.tsx` directly), a `DeltaTable` (before/after/Δ per
  metric — the closest thing to a "diff" here, but a numeric metrics table, not a code diff), plain
  `<pre>` blocks for `git diff --stat` text and commit list (`filesChanged.stat`/`.commits` — raw
  text, no line-level diff rendering or syntax highlighting), and an E2E screenshot grid. **No true
  patch/diff viewer exists anywhere in this component tree.**
- **`TechStackView.tsx`** — the one view that owns a full Gate UI directly via `useOpenInterrupt()`
  instead of the generic sidebar `InterruptCard`: an Edit/Preview toggle over a markdown `<textarea>`,
  a canned-stack dropdown, and a Submit button (Section 4). Worth studying as the one precedent for
  "a stage tab hosting its own Gate editor inline" — exactly the pattern a redesigned Gate UI would
  generalize.
- **`ClarifyingQuestions.tsx`** — small shared amber list of `{id, question, suggested_choices}` rows.
  Not tool-call/diff/cost, but a clean "small structured row list" precedent.
- **`SessionOverview.tsx`** — `Object.entries(state.stages)` rendered as a plain list of
  `{stageKey: statusLabel}` rows, **no timing/duration data at all** (no started-at, no elapsed time)
  — the seed of a stage timeline, but nowhere near a wall-clock swimlane. Its own comment: "ponytail:
  no live diagram, just the list -- matches the plan's own 'structured list in v1, diagram later'
  scope."
- **`ViewContainer.tsx`** — pure layout shell (`<div className="flex h-full w-full flex-col gap-4
  p-6">`), no content logic. Every stage view wraps in this; a new event-log view should too, for
  visual consistency.
- **`MetricsBar.tsx`** — **this is the existing cost/token display to reuse, not recreate.** The
  `costChip` (lines 159-173) already computes live spend from `state.token_usage_running` (re-summed
  on each background refresh scan), falling back to `state.metrics_report.metrics.token_usage_summary`
  for finished runs, rendering `$X.XX` with a hover tooltip showing input/output token counts:
  ```tsx
  const costChip = (() => {
    const cost = runningUsage?.cost ?? finalUsage?.total_cost;
    if (cost == null) return null;
    ...
    return <Chip key="cost" label="Cost" value={`$${cost.toFixed(2)}`} tone="gray" title={`LLM spend this run: ${inTokens...} tokens in / ${outTokens...} out. ...`} />;
  })();
  ```
  Also has graded chips (Security/Maintainability/Coverage/Duplication/Gate) with delta arrows vs.
  baseline, an active-stage trailing label, an e2e pill, and push/run-failure pills. The `Chip`
  component itself is exported and already reused by `ReportView.tsx`.
- **`ContainerStatus.tsx`** — a dot+label(+hover-to-stop) sandbox-liveness pill, shared between the
  live header (`WorkspaceHeader`, live state) and `SessionHistory` (a `container_alive` snapshot from
  `GET /sessions`). Not tool-call/diff/cost related.

**No `useRenderTool`/`useDefaultRenderTool`/`useComponent`/`useHumanInTheLoop` call exists anywhere
in `src/`** (confirmed by repo-wide grep — zero matches for all four). Whatever the chat sidebar shows
for a tool call today is CopilotKit's own uncustomized internal default rendering. The folding
tool-call row, the diff viewer, and the wall-clock swimlane are all genuinely net-new builds, not
reskins of something that already exists. The one piece of real reusable infrastructure is
`MetricsBar`'s cost/token `Chip` pattern.

*(Not in the requested list, but directly relevant: `src/components/A2UISurfaceView.tsx` is what
`PlanView`/`SpecificationView` actually delegate their main content to. It re-parses
`agent.messages` client-side looking for a tool message whose JSON body contains an
`a2ui_operations` envelope matching a given `surfaceId`, then dispatches by `component` name to a
catalog renderer — "generically, by the `component` name the backend stamped, mirroring what
CopilotKit's own A2UI renderer would do." This is a parallel, bespoke generative-UI mechanism
alongside the plain chat sidebar, worth knowing about since it also reads `agent.messages` directly,
the same message list a tool-call-row view would need to walk.)*

---

## 6. The separate report page

`src/app/sessions/[owner]/[repo]/[sessionId]/[runId]/report/page.tsx` is a plain **async server
component** — no `"use client"`, no CopilotKit, no AG-UI, no `useAgent`, nothing streamed. It reads a
committed artifact off GitHub via Octokit
(`.ai-dev-workflow/history/${runId}-report.json`, with a fallback path reconstructing from
`${runId}-metrics.json`/`${runId}-exit.md` for older runs that predate `report.json`), off the
session's `work_branch`, and feeds the parsed JSON into the exact same `<ReportView>` component
`AppShell`'s live "Report" tab uses. `ReportView.tsx`'s own module comment (lines 29-31) says this
directly:

> Presentational exit-report view, shared by AppShell's live Report tab and the past-session report
> page ... identical rendering whether the data came from live agent state or a committed
> `history/<run_id>-report.json`.

It is a **static, post-hoc, read-only** summary of one finished run. It cannot be "live" by
construction — there's no in-progress run to be live about; the two fallback cases the code handles
are "this run predates `report.json`" and "the run never reached exit finalize" (i.e., it failed
before producing one). This route is unaffected by the run-visibility redesign except insofar as it
already shares `ReportView` with the live page.

---

## 7. Board → run-detail navigation

**`src/components/SessionHistory.tsx`** (rendered on `/select`, one row per past session):
- "Resume" button (`SessionHistory.tsx:87`): `router.push(\`/workflow/${owner}/${repo}/${session.session_id}/${session.source_branch}?resume=1\`)` — the live workflow page, with `?resume=1`.
- "View report" button, completed sessions only (`SessionHistory.tsx:197`): `router.push(\`/sessions/${owner}/${repo}/${s.session_id}/${s.run_id}/report\`)` — the static report page.
- "View PR" link: external GitHub PR URL, new tab.

**`src/app/(boxed)/projects/[projectId]/board/page.tsx`**, `SessionCard`'s href construction, quoted
in full (lines 246-249):

```tsx
const href =
  session.status === "completed"
    ? `/sessions/${owner}/${repo}/${session.session_id}/${session.run_id}/report`
    : `/workflow/${owner}/${repo}/${session.session_id}/${session.source_branch}`;
```

Completed sessions link to the static report page; every other status (`in_progress`, `failed`,
`rejected`) links to the live workflow page — deliberately **without** `?resume=1` (the comment
directly above explains why: the workflow route's `SandboxSessionBoot` already unconditionally
POSTs `/sessions/provision` on mount regardless of that query param).

So: exactly two destinations exist today for "click a card/row," in both the Board and
`SessionHistory` — the live `/workflow/[owner]/[repo]/[sessionId]/[...branch]` page (where the new
event-log view slots in) and the static `/sessions/.../report` page (unaffected by this redesign).
Also notable, directly from this file's own comments (lines 10-11, 150-151): the Board itself
deliberately polls (`POLL_INTERVAL_MS = 15_000`) rather than subscribing live — *"Ruling 5 (this
Part's own plan): plain polling, no CopilotKit/AG-UI live subscription"* / *"deliberately no
CopilotKit/AG-UI subscription; that question is Part 2's own, explicitly deferred elsewhere."* Only
the workflow page itself is live; the Board is not, on purpose, by a prior explicit ruling.

---

## 8. Tailwind / design tokens

Tailwind v4 (`"tailwindcss": "^4"`, built via `@tailwindcss/postcss`). **No `tailwind.config.*` file
exists at all** (confirmed by glob) — v4's CSS-first config lives entirely in `src/app/globals.css`,
quoted in full:

```css
@import "tailwindcss";

:root {
  --background: #ffffff;
  --foreground: #171717;
}

@theme inline {
  --color-background: var(--background);
  --color-foreground: var(--foreground);
  --font-sans: var(--font-geist-sans);
  --font-mono: var(--font-geist-mono);
}

@media (prefers-color-scheme: dark) {
  :root { --background: #0a0a0a; --foreground: #ededed; }
}

body {
  background: var(--background);
  color: var(--foreground);
  font-family: Arial, Helvetica, sans-serif;
}
```

This is the unmodified Next.js `create-next-app` starter — essentially a placeholder, not a design
system. **No component in the app actually references these tokens.** The real visual language is
entirely repeated inline Tailwind utility classes: neutral-gray borders/text (`border-neutral-200`,
`text-neutral-500`), `rounded-lg`/`rounded-md`/`rounded-full`, `text-sm`/`text-xs` for nearly all body
text, and an ad hoc but consistent status-color convention — emerald = good/done, amber =
warning/awaiting, red = error/bad, blue = running, gray = unknown/placeholder — repeated
independently in at least `AppShell.tsx`'s `DOT_CLASS` and `MetricsBar.tsx`'s `CHIP_CLASS`. Dark mode
is not actually implemented beyond the unused starter tokens — every component hardcodes light-mode
classes (`bg-white`, `text-neutral-900`, etc.) directly rather than theme variables. A new event-log
view should copy this ad hoc neutral/emerald/amber/red/blue/gray + text-sm/text-xs convention
directly from `MetricsBar.tsx` and `AppShell.tsx` (the two densest, most representative examples)
rather than invent new tokens — there is effectively no token system to plug into instead.

`src/app/layout.tsx` loads `Geist`/`Geist_Mono` via `next/font/google` and renders the one shared app
chrome for every route: a frozen `WorkspaceHeader` + an independently scrolling body, wrapped in
`<Providers session={session}>`. `src/app/providers.tsx` mounts only `next-auth`'s `SessionProvider`
— explicitly **not** CopilotKit (its own comment: "CopilotKit/A2UIProvider deliberately live in
workflow/providers.tsx, not here: mounting them app-wide made the client fetch runtime info from
`/api/copilotkit` on every page, including the public, unauthenticated homepage — which now correctly
401s that request"). CopilotKit is scoped strictly to the workflow route tree.

---

## Gaps vs. the original plan's assumptions

1. **`@copilotkit/react-ui` does not exist in this project — not in `package.json`, not in
   `node_modules`, not even transitively.** The plan's framing ("keep react-core's hooks, drop
   react-ui's chat components entirely") describes a package boundary (hooks package vs. components
   package) that isn't this app's architecture and isn't even installed. In the installed CopilotKit
   1.66.4, `useAgent`/`useInterrupt`/`useHumanInTheLoop`/`useRenderTool` **and**
   `CopilotChat`/`CopilotSidebar`/`CopilotPopup`/`CopilotChatInput`/`CopilotChatView` all ship from
   the identical module, `@copilotkit/react-core/v2` — confirmed both by the barrel export list and
   by the package's own bundled docs ("v2 chat components ship from `react-core/v2` — not
   `react-ui`"). There is nothing to `npm uninstall`. The real "drop" step is narrower and more
   surgical than the plan implies: stop importing/rendering specific named exports
   (`CopilotSidebar`, `CopilotChatInput`, and whatever else backs the transcript) from a module the
   app continues to import six-plus other hooks from.

2. **`useHumanInTheLoop` and `useRenderTool` exist in the installed version but are used nowhere in
   this codebase today** (zero matches, repo-wide grep). The plan lists them as hooks to "keep"
   alongside `useAgent`/`useInterrupt` — but there's no existing usage to keep; adopting either would
   be a net-new integration. Today's entire Gate-approval mechanism runs through `useInterrupt` only.
   `useInterrupt` and `useHumanInTheLoop` are not interchangeable: `useHumanInTheLoop` is
   `useFrontendTool` minus a handler (it gates a *registered tool call* the agent invokes by name),
   whereas `useInterrupt` is the generic "an interrupt with this `agentId` fired" catch-all — which is
   what this backend's LangGraph `interrupt()` calls actually raise (an arbitrary JSON payload, not a
   named/schema'd tool call). The plan needs to explicitly decide whether the new Gate UI keeps using
   `useInterrupt` (extends what already works) or migrates to `useHumanInTheLoop` (bigger, unused,
   and its "always call `respond()` or the run hangs forever" contract doesn't obviously map onto
   this backend's `EscalationPayload`/`GatePayload` shapes without new backend-side work too).

3. **No tool-call rendering of any kind exists today** — `useRenderTool`/`useDefaultRenderTool`/
   `useComponent` are all unused; the chat sidebar shows CopilotKit's own uncustomized default. The
   "folding tool-call row" feature has nothing to reskin; it's a from-scratch build.

4. **No diff/patch viewer exists anywhere in the app.** The closest things are `ReportView`'s plain
   `<pre>` text blocks of `git diff --stat`/commit-log output (no line-level diff, no syntax
   highlighting) and a purely numeric `DeltaTable` (metrics before/after, not code). A real diff/patch
   viewer is net-new.

5. **The Gate UI has no "reject" path today.** Only "Approve" (generic `InterruptCard`), tech-stack's
   edit-then-submit (which always resolves with — implicitly approved — edited content, never a
   rejection), and the escalation card's "Acknowledge & retry" exist. An approve/reject/edit UI is not
   "wire up what's there" — reject has never existed for an ordinary Gate and needs a corresponding
   backend-resolve-value contract decided, not just a frontend button.

6. **`src/lib/agent-client.ts` has nothing to do with Gate submission or AG-UI.** It's a
   session-provisioning REST helper to the Python agent's plain HTTP API. If the plan assumed it was
   part of the interrupt/resume plumbing, it isn't — that plumbing is entirely internal to
   CopilotKit's `useInterrupt` hook, reached only via the `resolve()` callback threaded through
   `InterruptProvider`/`useOpenInterrupt`.

7. **The wall-clock "model-thinking-time vs. tool-time" swimlane has real support at the AG-UI event
   level, but not in anything the app currently consumes.** `@ag-ui/core`'s `EventType` enum has
   timestamped `REASONING_START`/`REASONING_MESSAGE_*`/`REASONING_END` (thinking) and
   `TOOL_CALL_START`/`TOOL_CALL_END` (tool) events — exactly the primitives a swimlane needs — but
   this app's `useAgent()` only exposes derived `agent.messages`/`agent.state`, never the raw event
   stream. Building the swimlane will need the lower-level `AbstractAgent.subscribe(subscriber)` API
   (`@ag-ui/client`'s `AgentSubscriber`, with per-event callbacks like `onToolCallStartEvent`/
   `onReasoningStartEvent`) or equivalent — nothing today wires that up. Also: use `REASONING_*`, not
   the deprecated `THINKING_*` aliases, in any new code.

8. **`AbstractAgent` is not in `@ag-ui/core`; it's in `@ag-ui/client`.** A grep of `@ag-ui/core`'s own
   432KB type declaration file turns up exactly one mention of `AbstractAgent`, in a doc comment —
   the class itself (and `HttpAgent extends AbstractAgent`, and the `Middleware` base class
   `A2UIMiddleware` extends) lives in `@ag-ui/client`, which re-exports all of `@ag-ui/core`'s types
   via `export *`. Not a contradiction of the plan, just a correction of which package to open.

9. **Confirmed, not a gap:** this is a real AG-UI bridge to a live Python LangGraph backend
   (`LangGraphHttpAgent` from `@copilotkit/runtime/langgraph`, talking to `AGENT_URL`), single-route
   CopilotKit runtime, genuinely SSE/streaming — not a mock, not a plain-REST substitute. That part
   of the plan's premise holds exactly as assumed.
