import { A2UIMiddleware } from "@ag-ui/a2ui-middleware";
import { LangGraphHttpAgent } from "@copilotkit/runtime/langgraph";
import { CopilotRuntime, createCopilotRuntimeHandler } from "@copilotkit/runtime/v2";
import { CATALOG_ID } from "@/lib/a2ui-surface-ids";

const AGENT_URL = process.env.AGENT_URL ?? "http://localhost:8123/";

const workflowAgent = new LangGraphHttpAgent({ url: AGENT_URL });
// Fixed-schema A2UI (Section 3.3): scans the agent's own event stream for
// backend tool results containing an `a2ui_operations` envelope and turns
// them into ACTIVITY_SNAPSHOT/"a2ui-surface" events, independent of
// CopilotRuntime's client transport mode. Applied per-agent (not also via
// runtime-level `a2ui` config) per the ag-ui-a2ui-integration skill's
// "don't double-apply A2UIMiddleware" rule. defaultCatalogId matches the
// catalogId our drafting nodes already stamp on every createSurface op.
workflowAgent.use(new A2UIMiddleware({ defaultCatalogId: CATALOG_ID }));

const runtime = new CopilotRuntime({
  agents: {
    workflow: workflowAgent,
  },
});

// The installed client (@copilotkit/core 1.66.4) calls fetchRuntimeInfoSingle
// (a single POST to the bare runtimeUrl with a { method, params, body }
// envelope) directly rather than auto-detecting REST, so the server must
// speak the same single-route protocol.
const handler = createCopilotRuntimeHandler({
  runtime,
  basePath: "/api/copilotkit",
  mode: "single-route",
});

export async function GET(request: Request) {
  return handler(request);
}

export async function POST(request: Request) {
  return handler(request);
}
