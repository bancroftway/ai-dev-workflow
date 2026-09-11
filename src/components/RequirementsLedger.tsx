"use client";

import { useEffect, useState } from "react";
import type { LedgerEntry } from "@/app/api/repos/ledger/route";
import type { Session } from "@/lib/session-types";

const STATUS_STYLE: Record<LedgerEntry["status"], string> = {
  active: "bg-blue-100 text-blue-800",
  revised: "bg-blue-100 text-blue-800",
  deferred: "bg-neutral-100 text-neutral-600",
  retired: "bg-neutral-100 text-neutral-400 line-through",
};

/**
 * Repo-scoped Tickets page's Requirements section (Task: Tickets View, Scope §1) -- renders
 * `.ai-dev-workflow/spec/ledger.json` (via /api/repos/ledger) as the elevated requirements source
 * of truth: every approved User Story/Acceptance Criterion accumulated across this repo's whole
 * ticket history, grouped by story, plus which ticket introduced each one (source_ticket_id,
 * resolved to that ticket's own title via the same sessions list the tickets section already
 * fetches independently -- a second, small GET, not a new backend endpoint).
 */
export function RequirementsLedger({ owner, repo }: { owner: string; repo: string }) {
  const [entries, setEntries] = useState<LedgerEntry[] | null>(null);
  const [titleByTicket, setTitleByTicket] = useState<Map<string, string>>(new Map());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/repos/ledger?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load requirements (${res.status})`);
        return res.json();
      })
      .then((data: { entries: LedgerEntry[] }) => {
        if (!cancelled) setEntries(data.entries);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    fetch(`/api/sessions/list?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { sessions: Session[] } | null) => {
        if (cancelled || !data) return;
        setTitleByTicket(new Map(data.sessions.map((s) => [s.session_id, s.title])));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [owner, repo]);

  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (entries === null) return <p className="text-sm text-neutral-500">Loading requirements…</p>;
  if (entries.length === 0) {
    return <p className="text-sm text-neutral-500">No approved requirements recorded for this repository yet.</p>;
  }

  const stories = entries.filter((e) => e.kind === "user_story");
  const acsByStory = new Map<string, LedgerEntry[]>();
  for (const e of entries) {
    if (e.kind !== "acceptance_criterion" || !e.parent_us_id) continue;
    acsByStory.set(e.parent_us_id, [...(acsByStory.get(e.parent_us_id) ?? []), e]);
  }

  function originLabel(e: LedgerEntry): string | null {
    if (!e.source_ticket_id) return null;
    return titleByTicket.get(e.source_ticket_id) ?? `ticket ${e.source_ticket_id.slice(0, 8)}`;
  }

  return (
    <ul className="flex flex-col gap-3">
      {stories.map((story) => (
        <li key={story.id} className="rounded-md border border-neutral-200 p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className={`font-medium text-neutral-900 ${story.status === "retired" ? "line-through opacity-50" : ""}`}>
              {story.title}
            </span>
            <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[story.status]}`}>
              {story.status}
            </span>
            {story.resolved_at && (
              <span className="shrink-0 rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800">
                Resolved
              </span>
            )}
          </div>
          {originLabel(story) && (
            <p className="mt-0.5 text-xs text-neutral-400">from {originLabel(story)}</p>
          )}
          {(acsByStory.get(story.id) ?? []).length > 0 && (
            <ul className="mt-2 flex flex-col gap-1 border-l border-neutral-100 pl-3">
              {(acsByStory.get(story.id) ?? []).map((ac) => (
                <li key={ac.id} className="flex items-center justify-between gap-2 text-xs">
                  <span className={ac.status === "retired" ? "text-neutral-400 line-through" : "text-neutral-700"}>
                    {ac.description}
                  </span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 font-medium ${STATUS_STYLE[ac.status]}`}>
                    {ac.status}
                  </span>
                  {ac.resolved_at && (
                    <span className="shrink-0 rounded-full bg-green-100 px-2 py-0.5 font-medium text-green-800">
                      Resolved
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ul>
  );
}
