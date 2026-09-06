import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { isAuthenticated } from "@/lib/session-access";
import type { TechStackCatalogResponse } from "@/lib/workflow-types";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * The 8 canned monorepo stacks the Tech Stack tab's dropdown offers -- static, session-independent
 * data (agent's load_stack_catalog is @lru_cache'd), so this route takes no query params at all.
 * Still gated behind a real session: this is workflow-internal data, not public marketing content.
 */
export async function GET() {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ stacks: [] } satisfies TechStackCatalogResponse, { status: 401, headers: NO_STORE });
  }

  const response = await agentFetch("tech-stack-catalog");
  if (!response.ok) {
    return NextResponse.json({ stacks: [] } satisfies TechStackCatalogResponse, { headers: NO_STORE });
  }
  const body = (await response.json()) as TechStackCatalogResponse;
  return NextResponse.json(body, { headers: NO_STORE });
}
