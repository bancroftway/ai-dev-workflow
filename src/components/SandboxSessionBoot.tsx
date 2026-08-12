"use client";

import { useEffect, useState } from "react";

type BootStatus = "provisioning" | "ready" | "error";

/**
 * Fires sandbox provisioning (architecture plan Section C) in the background when a workflow
 * session opens. Non-blocking: graph.py falls back to local-stdio Copilot execution when no
 * sandbox is registered yet for a thread, so the rest of the page is fully usable while this is
 * still in flight -- this only surfaces a small status banner, it never blocks rendering.
 */
export function SandboxSessionBoot({
  owner,
  repo,
  branch,
}: {
  owner: string;
  repo: string;
  branch: string;
}) {
  const [status, setStatus] = useState<BootStatus>("provisioning");

  useEffect(() => {
    let cancelled = false;
    fetch("/api/sessions/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner, repo, branch }),
    })
      .then((res) => {
        if (cancelled) return;
        setStatus(res.ok ? "ready" : "error");
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [owner, repo, branch]);

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
        ? "Couldn't prepare a dev-tool sandbox for this session — chat still works, but Copilot won't have repo/tool access yet."
        : "Preparing dev-tool sandbox…"}
    </div>
  );
}
