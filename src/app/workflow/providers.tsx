"use client";

import { CopilotKit, useCopilotKit } from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";
import { A2UIProvider } from "@copilotkit/a2ui-renderer";
import { useEffect, useState, type ReactNode } from "react";
import { catalog } from "@/a2ui/catalog";

/** The one error surface the whole workflow tree has. CopilotKit's own dev-console banner is
 * disabled (it shouts raw stack traces), and until 2026-08-30 transport errors only reached
 * console.warn -- a dead backend mid-run froze the page with zero user-visible signal. Run
 * errors do NOT reach the provider's `onError` prop (verified live: a killed agent produced
 * only the library's own console.error); the only app-reachable channel is
 * `copilotkit.subscribe({ onError })`, so a subscriber component inside the provider raises the
 * banner. The run does NOT always continue server-side (a killed agent process is gone), so the
 * copy offers both reattach-by-reload and the Resume/Reattach banner (AppShell.tsx, visible on
 * every tab, not just Overview -- the copy here named "the Overview tab" specifically until
 * 2026-09-11, stale since that banner moved). */
export function WorkflowProviders({ children }: { children: ReactNode }) {
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" a2ui={{ catalog }} showDevConsole={false}>
      <A2UIProvider catalog={catalog}>
        <TransportErrorBanner>{children}</TransportErrorBanner>
      </A2UIProvider>
    </CopilotKit>
  );
}

function TransportErrorBanner({ children }: { children: ReactNode }) {
  const { copilotkit } = useCopilotKit();
  const [transportError, setTransportError] = useState<string | null>(null);

  useEffect(() => {
    const subscription = copilotkit.subscribe({
      onError: ({ error, code, context }) => {
        console.warn("[workflow] agent transport error:", code, error);
        setTransportError(String((context as { runtimeErrorCode?: string })?.runtimeErrorCode ?? code ?? "unknown"));
      },
      // Root-cause note (2026-09-11): a transient transportError with no auto-recovery signal
      // used to stay pinned forever once set -- only the Dismiss button below ever cleared it.
      // In dev, React Strict Mode double-invokes this provider's mount, which can cancel the
      // FIRST connection attempt (reported here as onError) right before the second, healthy one
      // takes over -- the run was actually fine, but the banner never knew that. A fresh agent
      // run/connect beginning is direct evidence the transport is alive again, so clear the stale
      // error here rather than requiring a manual Dismiss for something that already resolved
      // itself. A genuinely still-broken backend re-fires onError immediately after this anyway.
      onAgentRunStarted: () => setTransportError(null),
    });
    return () => subscription.unsubscribe();
  }, [copilotkit]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {transportError && (
        <div className="flex shrink-0 items-center justify-between gap-4 border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-900">
          <span>
            Agent connection lost ({transportError}) — the run stream ended unexpectedly. The backend may be
            down and the state below may be stale. Reload to reattach, or use the Resume/Reattach control
            below once the backend is back.
          </span>
          <button
            type="button"
            className="shrink-0 rounded-md border border-red-300 px-2 py-1 text-xs hover:bg-red-100"
            onClick={() => setTransportError(null)}
          >
            Dismiss
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}
