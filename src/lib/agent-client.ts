import type { ProjectListResponse, ProjectSummary } from "@/app/api/projects/route";

/**
 * One place that knows how to reach the Python agent's HTTP API -- every server route that calls
 * it (provision, session list/lookup) goes through here instead of each re-deriving AGENT_URL and
 * the optional shared-secret header on its own. Also holds the small client-side helpers shared
 * by pages/components that talk to the same session/project APIs via the BFF routes.
 */
const AGENT_URL = process.env.AGENT_URL ?? "http://localhost:8123/";
const AGENT_BASE_URL = AGENT_URL.endsWith("/") ? AGENT_URL : `${AGENT_URL}/`;

// Optional shared secret the agent's sessions_api.py checks on every /sessions route when set
// (AIDW_AGENT_SHARED_SECRET, unset by default -- see that module's docstring for the known gap
// this only partially closes). Absent here, the header is simply omitted and the agent's own
// check no-ops identically.
const AGENT_SHARED_SECRET = process.env.AIDW_AGENT_SHARED_SECRET;

/** Fetches a path relative to the agent's base URL, with the shared-secret header attached when
 * configured. `path` is joined the same way for every caller -- e.g. "sessions/provision",
 * "sessions?owner=x&repo=y", "sessions/<id>". */
export function agentFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(new URL(path, AGENT_BASE_URL), {
    ...init,
    headers: {
      ...(AGENT_SHARED_SECRET ? { "x-aidw-secret": AGENT_SHARED_SECRET } : {}),
      ...init?.headers,
    },
  });
}

/** Client-side: re-reads the project list and finds this one -- no `GET /api/projects/:id`
 * exists, so the full-list-then-match technique is the only lookup available. */
export async function fetchProject(projectId: string): Promise<ProjectSummary | null> {
  const res = await fetch("/api/projects");
  if (!res.ok) throw new Error(`Failed to load projects (${res.status})`);
  const data = (await res.json()) as ProjectListResponse;
  return data.projects.find((p) => p.project_id === projectId) ?? null;
}

/** Client-side: confirm-then-stop for a session's dev-tool container (POST
 * /api/sessions/terminate). Returns true only when the user confirmed AND the call succeeded --
 * each caller does its own post-success UI update. */
export async function terminateSession(sessionId: string): Promise<boolean> {
  const confirmed = window.confirm(
    "Stop this session's dev-tool container? This discards its in-progress sandbox workspace. " +
      "Work already pushed to GitHub is unaffected, but the container can't be reconnected -- " +
      "resuming this session later provisions a fresh one.",
  );
  if (!confirmed) return false;
  const res = await fetch("/api/sessions/terminate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
  return res.ok;
}
