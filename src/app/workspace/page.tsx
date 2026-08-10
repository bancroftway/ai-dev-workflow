import { AppShell } from "@/components/AppShell";
import { WorkspaceHeader } from "@/components/WorkspaceHeader";
import { WorkspaceProviders } from "./providers";

export default function WorkspacePage() {
  return (
    <WorkspaceProviders>
      <div className="flex min-h-full flex-1 flex-col">
        <WorkspaceHeader />
        <AppShell />
      </div>
    </WorkspaceProviders>
  );
}
