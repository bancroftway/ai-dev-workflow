"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { SessionEntry } from "@/app/api/sessions/list/route";

const STATUS_BADGE: Record<SessionEntry["status"], string> = {
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  rejected: "bg-amber-100 text-amber-800",
  in_progress: "bg-blue-100 text-blue-800",
  superseded: "bg-neutral-100 text-neutral-600",
};

/**
 * Per-repo session history for /select, rendered inside RepoBranchSection (keyed by repo there,
 * so switching repos always re-fetches from scratch). Reports whether any session on this repo
 * is in_progress via `onInProgressChange` so the parent can show its own amber note near the
 * Continue button -- the provision route's 409 guard (Task 1) is the real enforcement; this is
 * just advance notice.
 */
export function SessionHistory({
  owner,
  repo,
  onInProgressChange,
}: {
  owner: string;
  repo: string;
  onInProgressChange?: (inProgress: boolean) => void;
}) {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionEntry[] | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({ owner, repo });
    fetch(`/api/sessions/list?${params}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load session history (${res.status})`);
        return res.json();
      })
      .then((data: { sessions: SessionEntry[]; warning?: string }) => {
        if (cancelled) return;
        setSessions(data.sessions);
        setWarning(data.warning ?? null);
        onInProgressChange?.(data.sessions.some((s) => s.status === "in_progress"));
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
    // onInProgressChange intentionally excluded -- a parent-supplied setState function's identity
    // must not re-trigger this fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner, repo]);

  function resume(session: SessionEntry) {
    router.push(`/workflow/${owner}/${repo}/${session.target_branch}?resume=1`);
  }

  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-sm font-medium text-neutral-700">Session history</h2>
      {error && <p className="text-sm text-red-600">{error}</p>}
      {warning && <p className="text-xs text-amber-700">{warning}</p>}
      {!error && sessions === null && <p className="text-sm text-neutral-500">Loading session history…</p>}
      {sessions?.length === 0 && (
        <p className="text-sm text-neutral-500">No past sessions for this repository.</p>
      )}
      {sessions && sessions.length > 0 && (
        <ul className="flex flex-col gap-2">
          {sessions.map((s) => (
            <li
              key={s.run_id}
              className="flex flex-col gap-1 rounded-md border border-neutral-200 px-3 py-2 text-sm"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-medium text-neutral-900">{s.title}</span>
                <span
                  className={`shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[s.status]}`}
                >
                  {s.status.replace("_", " ")}
                </span>
              </div>
              <div className="text-xs text-neutral-500">
                {s.user || "unknown"} · started {s.started_at} · ended {s.ended_at ?? "—"}
              </div>
              {s.status === "failed" && s.failure && (
                <p className="text-xs text-red-700">
                  {s.failure.stage}: {s.failure.type} — {s.failure.message}
                </p>
              )}
              <div className="flex gap-2">
                {(s.status === "failed" || s.status === "in_progress") && (
                  <button
                    type="button"
                    title="Resumes from the last approved stage, or restarts from intake if nothing was approved yet."
                    className="self-start rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white"
                    onClick={() => resume(s)}
                  >
                    Resume
                  </button>
                )}
                {s.status === "completed" && (
                  <button
                    type="button"
                    className="self-start rounded-md border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700"
                    onClick={() => router.push(`/sessions/${owner}/${repo}/${s.run_id}/report`)}
                  >
                    View report
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
