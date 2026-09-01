/** Node-runtime-only half of instrumentation.ts, split out because the Edge bundle statically
 * analyzes everything instrumentation.ts imports and `process.on` is a hard error there
 * ("Ecmascript file had an error"), which also poisoned Turbopack HMR for the whole dev server
 * (observed 2026-08-31: client edits silently stopped hot-reloading). instrumentation.ts loads
 * this via dynamic import strictly inside its NEXT_RUNTIME === "nodejs" branch. */

/** The copilotkit proxy route streams the agent's run over an undici fetch nothing in app code
 * owns (the pump lives inside @copilotkit/runtime) -- when the Python agent dies mid-stream,
 * the socket reset surfaces as a process-level `TypeError: terminated` (cause ECONNRESET)
 * unhandled rejection. Registering ANY handler suppresses Node's default for ALL rejections,
 * so: the known agent-stream shape logs at warn, everything else re-logs loudly at error with
 * its stack -- nothing gets quieter than before. */
export function registerUnhandledRejectionGuard() {
  process.on("unhandledRejection", (reason) => {
    const err = reason instanceof Error ? reason : null;
    const cause = err?.cause instanceof Error ? err.cause : null;
    const agentStreamDeath =
      err?.message === "terminated" ||
      (cause && "code" in cause && (cause as { code?: string }).code === "ECONNRESET");
    if (agentStreamDeath) {
      console.warn("[copilotkit proxy] agent stream terminated (backend down or restarted):", err?.message);
      return;
    }
    console.error("Unhandled promise rejection:", reason);
  });
}
