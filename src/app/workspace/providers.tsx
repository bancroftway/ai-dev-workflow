"use client";

import { CopilotKit } from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";
import { A2UIProvider } from "@copilotkit/a2ui-renderer";
import type { ReactNode } from "react";
import { catalog } from "@/a2ui/catalog";

export function WorkspaceProviders({ children }: { children: ReactNode }) {
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" a2ui={{ catalog }}>
      <A2UIProvider catalog={catalog}>{children}</A2UIProvider>
    </CopilotKit>
  );
}
