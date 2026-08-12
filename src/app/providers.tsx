"use client";

import { SessionProvider } from "next-auth/react";
import type { Session } from "next-auth";
import type { ReactNode } from "react";

// CopilotKit/A2UIProvider deliberately live in workflow/providers.tsx, not
// here: mounting them app-wide made the client fetch runtime info from
// /api/copilotkit on every page, including the public, unauthenticated
// homepage -- which now correctly 401s that request, surfacing as a
// runtime_info_fetch_failed error where no CopilotKit UI is even rendered.
export function Providers({
  children,
  session,
}: {
  children: ReactNode;
  session?: Session | null;
}) {
  return <SessionProvider session={session}>{children}</SessionProvider>;
}
