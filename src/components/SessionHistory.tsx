"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ContainerStatusButton } from "@/components/ContainerStatus";
import { terminateSession } from "@/lib/agent-client";
import { STAGE_KEYS_IN_ORDER, type Session } from "@/lib/session-types";

// Exported (Part 3 Task 9 fix round 1) so the project Board's own cards can reuse this exact
// palette instead of keeping a second copy -- this was already the one real definition; the
// workflow page's separate copy predates this and is left alone, out of this fix's scope.
export const STATUS_BADGE: Record<Session["status"], string> = {
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  rejected: "bg-amber-100 text-amber-800",
  in_progress: "bg-blue-100 text-blue-800",
};

function ProgressIndicator({ currentStage }: { currentStage: string | null }) {
  if (!currentStage) return <span className="text-xs text-neutral-500">Starting…</span>;
  const index = STAGE_KEYS_IN_ORDER.indexOf(currentStage as (typeof STAGE_KEYS_IN_ORDER)[number]);
  const label = index === -1 ? currentStage : `Stage ${index + 1} of ${STAGE_KEYS_IN_ORDER.length}: ${currentStage}`;
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-neutral-600">{label}</span>
      {index !== -1 && (
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-neutral-100">
          <div
            className="h-full rounded-full bg-blue-500 transition-all"
            style={{ width: `${((index + 1) / STAGE_KEYS_IN_ORDER.length) * 100}%` }}
          />
        </div>
      )}
    </div>
  );
}

/**
 * Session list for /select, rendered once a repo AND branch are both chosen (SelectPage keys this
 * by (repo, branch) so switching either always re-fetches from scratch). Reports whether any
 * session on this repo/branch is in_progress via `onInProgressChange` for cosmetic use only --
 * concurrency is fully open now (branch-per-session), there is no provision-time lock to warn
 * about, unlike the old per-repo 409 guard this replaced.
 */
export function SessionHistory({
  owner,
  repo,
  sourceBranch,
  onInProgressChange,
}: {
  owner: string;
  repo: string;
  sourceBranch: string;
  onInProgressChange?: (inProgress: boolean) => void;
}) {
  const router = useRouter();
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  const [supportIssueId, setSupportIssueId] = useState<string | null>(null);
  const [supportIssueError, setSupportIssueError] = useState<string | null>(null);

  async function openSupportIssue(session: Session) {
    setSupportIssueId(session.session_id);
    setSupportIssueError(null);
    try {
      const res = await fetch("/api/sessions/support-issue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: session.session_id }),
      });
      const body = (await res.json()) as { url?: string; detail?: string };
      if (res.ok && body.url) {
        window.open(body.url, "_blank", "noreferrer");
      } else if (res.status === 409) {
        setSupportIssueError("No support repo configured — set one in organization settings.");
      } else {
        setSupportIssueError(body.detail ?? `Could not open support issue (${res.status})`);
      }
    } catch {
      setSupportIssueError("Could not open support issue: network error");
    } finally {
      setSupportIssueId(null);
    }
  }

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({ owner, repo, source_branch: sourceBranch });
    fetch(`/api/sessions/list?${params}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load session history (${res.status})`);
        return res.json();
      })
      .then((data: { sessions: Session[] }) => {
        if (cancelled) return;
        setSessions(data.sessions);
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
  }, [owner, repo, sourceBranch]);

  function resume(session: Session) {
    router.push(`/workflow/${owner}/${repo}/${session.session_id}/${session.source_branch}?resume=1`);
  }

  async function stopContainer(session: Session) {
    setStoppingId(session.session_id);
    try {
      if (await terminateSession(session.session_id)) {
        setSessions((prev) =>
          prev?.map((s) => (s.session_id === session.session_id ? { ...s, container_alive: false } : s)) ?? prev,
        );
      }
    } finally {
      setStoppingId(null);
    }
  }

  async function deleteSession(session: Session) {
    const confirmed = window.confirm(
      `Delete "${session.title}"? This stops its container if one is running, deletes its ` +
        `GitHub branch (${session.work_branch}), and removes it from this list. This cannot be undone.`,
    );
    if (!confirmed) return;
    setDeleteError(null);
    setDeletingId(session.session_id);
    try {
      const res = await fetch("/api/sessions/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: session.session_id }),
      });
      const body = (await res.json()) as { detail?: string };
      if (res.ok) {
        setSessions((prev) => prev?.filter((s) => s.session_id !== session.session_id) ?? prev);
      } else {
        setDeleteError(body.detail ?? `Delete failed (${res.status})`);
      }
    } catch {
      setDeleteError("Delete failed: network error");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-sm font-medium text-neutral-700">Sessions on this branch</h2>
      {error && <p className="text-sm text-red-600">{error}</p>}
      {deleteError && <p className="text-sm text-red-600">{deleteError}</p>}
      {supportIssueError && <p className="text-sm text-red-600">{supportIssueError}</p>}
      {!error && sessions === null && <p className="text-sm text-neutral-500">Loading sessions…</p>}
      {sessions?.length === 0 && (
        <p className="text-sm text-neutral-500">No sessions yet for this repository/branch.</p>
      )}
      {sessions && sessions.length > 0 && (
        <ul className="flex flex-col gap-2">
          {sessions.map((s) => (
            <li
              key={s.session_id}
              className="flex flex-col gap-1 rounded-md border border-neutral-200 px-3 py-2 text-sm"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-medium text-neutral-900">{s.title}</span>
                <div className="flex shrink-0 items-center gap-2">
                  <ContainerStatusButton
                    status={s.container_alive ? "ready" : "terminated"}
                    onStop={() => stopContainer(s)}
                    stopping={stoppingId === s.session_id}
                  />
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[s.status]}`}
                  >
                    {s.status.replace("_", " ")}
                  </span>
                </div>
              </div>
              <div className="text-xs text-neutral-500">
                {s.user_login || "unknown"} · started {s.started_at} · ended {s.ended_at ?? "—"}
              </div>
              {s.status === "in_progress" && <ProgressIndicator currentStage={s.current_stage} />}
              {s.status === "failed" && s.failure_message && (
                <p className="text-xs text-red-700">
                  {s.failure_stage}: {s.failure_type} — {s.failure_message}
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
                {s.status === "failed" && (
                  <button
                    type="button"
                    title="Files an issue in the org-configured support repo with the thread id and failure details (or opens the existing one)."
                    className="self-start rounded-md border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 disabled:opacity-40"
                    disabled={supportIssueId === s.session_id}
                    onClick={() => openSupportIssue(s)}
                  >
                    {supportIssueId === s.session_id ? "Filing…" : "Open support issue"}
                  </button>
                )}
                {s.status === "completed" && (
                  <>
                    <button
                      type="button"
                      className="self-start rounded-md border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700"
                      onClick={() => router.push(`/sessions/${owner}/${repo}/${s.session_id}/${s.run_id}/report`)}
                    >
                      View report
                    </button>
                    {s.pr_url && (
                      <a
                        href={s.pr_url}
                        target="_blank"
                        rel="noreferrer"
                        className="self-start rounded-md border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700"
                      >
                        View PR
                      </a>
                    )}
                  </>
                )}
                <button
                  type="button"
                  title="Stops its container if running, deletes its GitHub branch, and removes it from this list."
                  className="self-start rounded-md border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-500 hover:border-red-300 hover:bg-red-50 hover:text-red-700 disabled:opacity-40"
                  disabled={deletingId === s.session_id}
                  onClick={() => deleteSession(s)}
                >
                  {deletingId === s.session_id ? "Deleting…" : "Delete"}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
