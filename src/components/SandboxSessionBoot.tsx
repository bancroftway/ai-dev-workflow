"use client";

import { useEffect, useState } from "react";
import { useSandboxStatus } from "@/lib/sandbox-status-context";

/**
 * Fires sandbox provisioning (architecture plan Section C) in the background when a workflow
 * session opens. Non-blocking: graph.py falls back to local-stdio Copilot execution when no
 * sandbox is registered yet for a thread, so the rest of the page is fully usable while this is
 * still in flight -- this only surfaces a small status banner, it never blocks rendering.
 *
 * Status lives in SandboxStatusProvider (not local state) so AppShell's auto-trigger effect can
 * gate on the same readiness signal without prop-drilling.
 */
export function SandboxSessionBoot({
  sessionId,
  owner,
  repo,
  branch,
  resume,
}: {
  /** This session's own id -- a UUID minted client-side for a new session, or the historical
   * session being resumed. Forwarded as-is to the provision route/agent; never derived here. */
  sessionId: string;
  owner: string;
  repo: string;
  branch: string;
  /** ?resume=1 from the workflow page's searchParams (set by SessionHistory's Resume button) --
   * forwarded to the provision route so the agent's registry meta carries it into intake_node. */
  resume?: boolean;
}) {
  const [status, setStatus] = useSandboxStatus();
  // Provision can fail with an explanatory message worth showing verbatim (e.g. the agent's
  // 404/409 resume guards) -- falls back to the generic copy below when the response has no (or
  // an unparseable) error body.
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/sessions/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, owner, repo, branch, resume: Boolean(resume) }),
    })
      .then(async (res) => {
        if (cancelled) return;
        if (res.ok) {
          setStatus("ready");
          return;
        }
        const body = await res.json().catch(() => null);
        setErrorMessage(typeof body?.error === "string" ? body.error : null);
        setStatus("error");
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, owner, repo, branch, resume, setStatus]);

  if (status === "ready") return null;

  return (
    <div
      className={
        status === "error"
          ? "border-b border-red-200 bg-red-50 px-4 py-1.5 text-xs text-red-700"
          : "border-b border-neutral-200 bg-neutral-50 px-4 py-1.5 text-xs text-neutral-500"
      }
    >
      {status === "error"
        ? (errorMessage ??
          "Couldn't prepare a dev-tool sandbox for this session — chat still works, but Copilot won't have repo/tool access yet.")
        : "Preparing dev-tool sandbox…"}
    </div>
  );
}
