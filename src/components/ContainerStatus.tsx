"use client";

import type { SandboxStatus } from "@/lib/sandbox-status-context";

/**
 * The one dot+label+hover-to-stop look for "is this session's dev-tool container running" --
 * shared by WorkspaceHeader (the currently-open workflow page's live status) and SessionHistory
 * (a per-row snapshot from the session list). Same component, two different data sources: the
 * header reads live client state from SandboxStatusProvider; history rows read the agent's
 * `container_alive` flag from `GET /sessions` and only ever resolve to "ready" or "terminated".
 */
const CONTAINER_STATUS_META: Record<SandboxStatus, { label: string; dot: string; pulse: boolean }> = {
  provisioning: { label: "Connecting…", dot: "bg-amber-400", pulse: true },
  ready: { label: "Connected", dot: "bg-green-500", pulse: true },
  error: { label: "Disconnected", dot: "bg-red-500", pulse: false },
  terminated: { label: "Stopped", dot: "bg-neutral-400", pulse: false },
};

export function ContainerStatusButton({
  status,
  onStop,
  stopping,
}: {
  status: SandboxStatus;
  /** Omit to render a plain, non-interactive pill (nothing to stop). */
  onStop?: () => void;
  stopping?: boolean;
}) {
  const meta = CONTAINER_STATUS_META[status];
  const dot = <span className={`h-2 w-2 shrink-0 rounded-full ${meta.dot} ${meta.pulse ? "animate-pulse" : ""}`} />;

  if (status !== "ready" || !onStop) {
    return (
      <span className="flex items-center gap-1.5 rounded-md border border-neutral-200 px-2 py-1 text-xs text-neutral-500">
        {dot}
        {meta.label}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={onStop}
      disabled={stopping}
      title="Click to stop this session's dev-tool container"
      className="group flex items-center gap-1.5 rounded-md border border-neutral-300 px-2 py-1 text-xs text-neutral-600 hover:border-red-300 hover:bg-red-50 hover:text-red-700 disabled:opacity-40"
    >
      {dot}
      <span className="group-hover:hidden">{stopping ? "Stopping…" : "Connected"}</span>
      <span className="hidden group-hover:inline">{stopping ? "Stopping…" : "Stop container"}</span>
    </button>
  );
}
