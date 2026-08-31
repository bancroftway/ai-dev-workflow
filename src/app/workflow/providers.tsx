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
 * copy offers both reattach-by-reload and Resume. */
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
    });
    return () => subscription.unsubscribe();
  }, [copilotkit]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {transportError && (
        <div className="flex shrink-0 items-center justify-between gap-4 border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-900">
          <span>
            Agent connection lost ({transportError}) — the run stream ended unexpectedly. The backend may be
            down and the state below may be stale. Reload to reattach, or Resume from the Overview tab once the
            backend is back.
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
