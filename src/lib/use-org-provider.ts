"use client";

import { useEffect, useState } from "react";

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
    fetch("/api/settings/organization")
      .then((res) => (res.ok ? (res.json() as Promise<{ provider?: string }>) : null))
      .then((data) => {
        if (!cancelled && data?.provider) setProvider(data.provider);
      })
      .catch(() => {
        // Best-effort positive affordance only -- never surface an error for this.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return provider;
}

/** Same two labels as settings/organization/page.tsx's own PROVIDER_LABELS (kept local there --
 * that map predates this hook and isn't exported). Only two providers exist (Non-goal, Part 4
 * Spec), so an unrecognized string reads as Copilot rather than rendering nothing. */
export function providerLabel(provider: string): string {
  return provider === "claude" ? "Claude Code" : "GitHub Copilot";
}
