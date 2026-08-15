"use client";

import { CopilotKit } from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";
import { A2UIProvider } from "@copilotkit/a2ui-renderer";
import type { ReactNode } from "react";
import { catalog } from "@/a2ui/catalog";

export function WorkflowProviders({ children }: { children: ReactNode }) {
  return (
    // showDevConsole={false}: CopilotKit's dev console is on by default on localhost and renders
    // every transport hiccup as a raw "terminated" banner with a stack trace. A dropped stream
    // is recoverable (the run continues server-side; reload reattaches), so it is logged, not
    // shouted at the user.
    <CopilotKit
      runtimeUrl="/api/copilotkit"
      a2ui={{ catalog }}
      showDevConsole={false}
      onError={(error) => {
        console.warn("[workflow] agent transport error (run continues server-side; reload to reattach):", error);
      }}
    >
      <A2UIProvider catalog={catalog}>{children}</A2UIProvider>
    </CopilotKit>
  );
}
