/**
 * One place that knows how to reach the Python agent's HTTP API -- every server route that calls
 * it (provision, session list/lookup) goes through here instead of each re-deriving AGENT_URL and
 * the optional shared-secret header on its own.
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
