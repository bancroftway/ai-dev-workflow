"use client";

import { createContext, useContext, useState, type ReactNode } from "react";

export type SandboxStatus = "provisioning" | "ready" | "error";

const SandboxStatusContext = createContext<[SandboxStatus, (s: SandboxStatus) => void] | null>(null);

export function SandboxStatusProvider({ children }: { children: ReactNode }) {
  const state = useState<SandboxStatus>("provisioning");
  return <SandboxStatusContext.Provider value={state}>{children}</SandboxStatusContext.Provider>;
}

/** Readiness signal AppShell's auto-trigger effect gates on -- SandboxSessionBoot writes it. */
export function useSandboxStatus() {
  const ctx = useContext(SandboxStatusContext);
  if (!ctx) throw new Error("useSandboxStatus must be used within SandboxStatusProvider");
  return ctx;
}
