import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required for the Docker build (Dockerfile) -- traces only the files each route actually
  // needs into .next/standalone, so the deployed image doesn't need full node_modules (plan
  // Section D).
  output: "standalone",
  // Off deliberately (root-caused 2026-09-11): App Router Strict Mode defaults to true when
  // unset, and its dev-only mount->cleanup->mount double-invoke was cancelling the very first
  // CopilotKit agent connection on every workflow-page load -- the cancelled attempt fired
  // onError (surfaced as "Agent connection lost (INCOMPLETE_STREAM)"), then the second mount's
  // connection quietly worked, requiring a confusing extra Resume click to visually clear a
  // banner that had already resolved itself. `next build`/`next start` never double-invoke
  // effects regardless of this flag, so this has zero effect on the deployed app -- it only
  // removes a dev-mode false alarm. TransportErrorBanner (src/app/workflow/providers.tsx) was
  // also fixed to auto-clear on a fresh agent run as defense in depth, so a real backend outage
  // still surfaces correctly even if this ever gets flipped back on to investigate something else.
  reactStrictMode: false,
};

export default nextConfig;
