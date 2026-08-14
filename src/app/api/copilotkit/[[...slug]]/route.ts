import { A2UIMiddleware } from "@ag-ui/a2ui-middleware";
import { LangGraphHttpAgent } from "@copilotkit/runtime/langgraph";
import { CopilotRuntime, createCopilotRuntimeHandler } from "@copilotkit/runtime/v2";
import { CATALOG_ID } from "@/lib/a2ui-surface-ids";
import { auth } from "@/auth";
import { E2E_MODE } from "@/lib/e2e";

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

// Explicit in-handler check: proxy.ts already gates this route optimistically,
// but Next's own auth guide warns proxy matcher coverage can be bypassed by
// misconfiguration or route refactors, so the one real inbound entry point
// into the app's AI functionality verifies the session itself too.
async function requireAuth(): Promise<Response | null> {
  if (E2E_MODE) return null;
  const session = await auth();
  if (!session?.user) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  return null;
}

export async function GET(request: Request) {
  const unauthorized = await requireAuth();
  if (unauthorized) return unauthorized;
  return handler(request);
}

export async function POST(request: Request) {
  const unauthorized = await requireAuth();
  if (unauthorized) return unauthorized;
  return handler(request);
}
