"use client";

import { useEffect, useState } from "react";

/** The only two providers that exist (Non-goal, Part 4 Spec). Single source for the display
 * labels -- settings/organization/page.tsx renders its picker off this same map. */
export const PROVIDER_LABELS: Record<"copilot" | "claude", string> = {
  copilot: "GitHub Copilot",
  claude: "Claude Code",
};

// Module-level shared fetch: pages mount several consumers of this hook per load, and each used
// to issue its own GET /api/settings/organization. First caller fetches, concurrent/later mounts
// reuse the same promise. Only a successful load is cached -- a failed/non-ok fetch clears the
// slot so the next mount retries, same per-mount retry behavior as before.
let orgProviderFetch: Promise<string | null> | null = null;

function fetchOrgProvider(): Promise<string | null> {
  if (!orgProviderFetch) {
    orgProviderFetch = fetch("/api/settings/organization")
      .then((res) => (res.ok ? (res.json() as Promise<{ provider?: string }>) : null))
      .then((data) => {
        const provider = data?.provider ?? null;
        if (provider == null) orgProviderFetch = null;
        return provider;
      })
      .catch(() => {
        orgProviderFetch = null;
        return null;
      });
  }
  return orgProviderFetch;
}

/** Drop the cached fetch so the next mount re-reads the org settings -- called by the settings
 * page after a successful save, else already-visited pages keep the stale provider label. */
export function invalidateOrgProvider(): void {
  orgProviderFetch = null;
}

/**
 * Org-wide coding-agent provider, for the two positive "runs on <provider>" affordances the Spec
 * asks for (New Ticket form, board header -- Phase E audit Minor 10; the only existing consumer of
 * `/api/settings/organization`, settings-checks.ts, only ever renders the negative "not configured"
 * case). Factored out once a second caller needed the identical fetch -- same precedent
 * use-run-events.ts's own docstring already set in this codebase (EventLogView.tsx -> Swimlane.tsx).
 *
 * Best-effort only: null until the fetch succeeds, and stays null forever on any failure -- per
 * Minor 10's own instruction, no label is strictly better than a wrong or invented one, and this
 * must never block the page it's mounted on.
 */
export function useOrgProvider(): string | null {
  const [provider, setProvider] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchOrgProvider().then((value) => {
      if (!cancelled && value) setProvider(value);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return provider;
}

/** Only two providers exist (Non-goal, Part 4 Spec), so an unrecognized string reads as Copilot
 * rather than rendering nothing. */
export function providerLabel(provider: string): string {
  return provider === "claude" ? PROVIDER_LABELS.claude : PROVIDER_LABELS.copilot;
}
