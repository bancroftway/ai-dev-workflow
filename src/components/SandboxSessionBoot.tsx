"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CodeGenModePicker, type CodeGenMode } from "@/components/CodeGenModePicker";
import { useRunActivity } from "@/lib/run-activity-context";
import { useSandboxStatus } from "@/lib/sandbox-status-context";

/**
 * Fires sandbox provisioning (architecture plan Section C) in the background when a workflow
 * session opens. Non-blocking: graph.py falls back to local-stdio Copilot execution when no
 * sandbox is registered yet for a thread, so the rest of the page is fully usable while this is
 * still in flight -- this only surfaces a small status banner, it never blocks rendering.
 *
 * The one place every workspace banner lives: preparing, idle-paused (the server's
 * workspace_notice + Reconnect) and a failed provision (its reason + Try again).
 *
 * Status lives in SandboxStatusProvider (not local state) so AppShell's auto-trigger effect can
 * gate on the same readiness signal without prop-drilling.
 */
export function SandboxSessionBoot({
  sessionId,
  owner,
  repo,
  branch,
  resume,
  projectId,
  skip,
}: {
  /** This session's own id -- a UUID minted client-side for a new session, or the historical
   * session being resumed. Forwarded as-is to the provision route/agent; never derived here. */
  sessionId: string;
  owner: string;
  repo: string;
  branch: string;
  /** ?resume=1 from the workflow page's searchParams (set by SessionHistory's Resume button) --
   * forwarded to the provision route so the agent's registry meta carries it into intake_node. */
  resume?: boolean;
  /** ?projectId= from the workflow page's searchParams (set by /select's RepoBranchSection,
   * Task 5) -- only ever needed for a genuinely brand-new session; the agent falls back to an
   * already-existing session's own stored project_id otherwise (resume, or a plain reload of this
   * page), so this is undefined in every other case and simply omitted from the POST body. */
  projectId?: string;
  /** True for a terminal (completed/failed/rejected) session opened WITHOUT ?resume=1 -- there is
   * nothing to provision (the container/branch may not even exist anymore) and nothing here should
   * try. Still the one place that resolves sandboxStatus out of its "provisioning" default --
   * skipping the POST but leaving status unset would strand the header's pill on "Connecting…"
   * forever (observed live: reopening a completed session via its Report link). Set to
   * "terminated" instead: truthful (no live container), and every sandboxStatus-gated check
   * elsewhere (`!== "ready"`, `=== "provisioning"`) already treats it the same as never having
   * provisioned. */
  skip?: boolean;
}) {
  const [status, setStatus] = useSandboxStatus();
  // A failed provision's reason, shown verbatim with a Try again button (e.g. the agent's 404/409
  // resume guards, a docker failure) -- see `provision` below.
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // One-shot latch (root-caused 2026-09-13, "container keeps dying" investigation): this
  // component's own name promises a boot-time decision, but `skip` sat in the effect's dependency
  // array below, so any LATER change to it re-ran this whole effect mid-session -- and a fetch
  // fired from it uses THIS component's own `branch` prop, which is the URL's source/PR-target
  // segment, not `sessionRow.work_branch`. `skip` flips true->false the instant a Session
  // Overview recovery action (rewind/retry/continue) succeeds and the durable row's status leaves
  // its terminal state -- exactly when this fired a SECOND, wrongly-branched provision call.
  // local_docker.py's provision() treats any branch mismatch against the running container as a
  // genuine "PR target changed", stops the container that action's own (correctly-branched)
  // ensureSandboxProvisioned call had just set up, and reprovisions against the wrong branch,
  // which then dies. Session Overview's own recovery handlers already provision correctly before
  // every action now, so this component's ENTIRE job -- skip or fetch -- only needs to happen
  // once per session, at genuine page-open; latched before either branch below, not just the
  // fetch one, so a session that started `skip`-true also never re-decides later.
  const decidedForRef = useRef<string | null>(null);

  // Cosmetic pre-highlight only (CodeGenModePicker's own RadioGroup defaultValue) is unrelated to
  // this -- null here just means "the picker hasn't been answered (or restored from storage) yet".
  const [codeGenMode, setCodeGenMode] = useState<CodeGenMode | null>(null);
  // True only once this mount has checked sessionStorage for an already-answered mode and come up
  // empty. Gates the picker's RENDER, not just the fetch below: `codeGenMode === null` alone is
  // true for one render on every mount (before the effect has had a chance to restore a stored
  // answer), and without this second flag that render would flash the popup on screen for a
  // session that's already answered -- exactly on refresh, the one case this must never happen.
  const [storageChecked, setStorageChecked] = useState(false);
  // The only signal available for "this session has never been asked before": ?projectId= is set
  // in the URL solely for a genuinely brand-new session (see that prop's own doc above), and stays
  // in the URL across a plain refresh of the same page -- sessionStorage (checked below), not this
  // flag, is what actually prevents the popup from reappearing on that refresh.
  const isNewSession = !skip && !resume && projectId !== undefined;
  const [runActivity] = useRunActivity();
  const workspaceNotice = runActivity?.workspaceNotice ?? null;

  /** POST /api/sessions/provision; resolves null on success, else the server's reason -- the
   * agent's own `detail` (every agent error, and agentFetch's "agent unreachable" 502) or the
   * proxy's `error`. Reading only `error` used to hide every agent-side reason behind generic copy. */
  const provision = useCallback(
    async (resumeFlag: boolean): Promise<string | null> => {
      try {
        const res = await fetch("/api/sessions/provision", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            sessionId,
            owner,
            repo,
            branch,
            resume: resumeFlag,
            projectId,
            // Omitted (JSON.stringify drops an `undefined` value) for every session that never
            // showed the picker -- resume, a plain reload, or `skip` -- so the agent falls back to
            // this session's own already-pinned mode instead of overwriting it.
            codeGenMode: codeGenMode ?? undefined,
          }),
        });
        if (res.ok) return null;
        const body = await res.json().catch(() => null);
        const reason = body?.detail ?? body?.error;
        return typeof reason === "string" && reason ? reason : `HTTP ${res.status}`;
      } catch (err) {
        return err instanceof Error ? err.message : String(err);
      }
    },
    [sessionId, owner, repo, branch, projectId, codeGenMode],
  );

  useEffect(() => {
    if (decidedForRef.current === sessionId) return;

    if (skip) {
      decidedForRef.current = sessionId;
      setStatus("terminated");
      return;
    }

    if (isNewSession && codeGenMode === null) {
      // Deliberately NOT latching decidedForRef in this branch: with nothing stored, this effect
      // must run again once the picker's onSelect (below, in the render) sets codeGenMode.
      let stored: string | null = null;
      try {
        stored = sessionStorage.getItem(`aidw:code-gen-mode:${sessionId}`);
      } catch {
        stored = null;
      }
      if (stored === null) {
        // One-time seed from an external, non-reactive source (sessionStorage) -- there's no
        // dependency this could "react" to instead, same shape as RequirementsView.tsx's own
        // sessionStorage-seed effects.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setStorageChecked(true);
        return;
      }
      setCodeGenMode(stored as CodeGenMode);
      return;
    }

    decidedForRef.current = sessionId;
    let cancelled = false;
    void provision(Boolean(resume)).then((error) => {
      if (cancelled) return;
      setErrorMessage(error);
      setStatus(error === null ? "ready" : "error");
    });
    return () => {
      cancelled = true;
    };
  }, [skip, sessionId, resume, codeGenMode, isNewSession, provision, setStatus]);

  // Reconnect (an idle-paused workspace) and Retry (a failed provision) -- the same provision call
  // as page-open, minus `resume`: that one-shot flag belongs to the Resume button's own open.
  async function reconnect() {
    setErrorMessage(null);
    setStatus("provisioning");
    const error = await provision(false);
    setErrorMessage(error);
    setStatus(error === null ? "ready" : "error");
  }

  if (skip || status === "ready" || status === "terminated") return null;

  if (isNewSession && storageChecked && codeGenMode === null) {
    return (
      <CodeGenModePicker
        onSelect={(mode) => {
          try {
            sessionStorage.setItem(`aidw:code-gen-mode:${sessionId}`, mode);
          } catch {
            // Storage blocked -- worst case a future refresh just asks again; this mount still
            // proceeds either way once codeGenMode is set below.
          }
          setCodeGenMode(mode);
        }}
      />
    );
  }

  if (status === "paused") {
    // No server notice = a stopped run whose own notice (AppShell's run notice, Resume) covers it.
    if (workspaceNotice == null) return null;
    return (
      <div className="flex items-center gap-3 border-b border-sky-200 bg-sky-50 px-4 py-1.5 text-xs text-sky-900">
        <span className="flex-1">{workspaceNotice.text}</span>
        <button
          type="button"
          onClick={() => void reconnect()}
          className="shrink-0 rounded-md border border-sky-300 bg-white px-2 py-0.5 font-medium text-sky-900 hover:bg-sky-100"
        >
          {workspaceNotice.action.label}
        </button>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="flex items-center gap-3 border-b border-amber-200 bg-amber-50 px-4 py-1.5 text-xs text-amber-900">
        <span className="flex-1">The workspace couldn&apos;t start: {errorMessage ?? "no reason was given"}</span>
        <button
          type="button"
          onClick={() => void reconnect()}
          className="shrink-0 rounded-md border border-amber-300 bg-white px-2 py-0.5 font-medium text-amber-900 hover:bg-amber-100"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="border-b border-neutral-200 bg-neutral-50 px-4 py-1.5 text-xs text-neutral-500">
      Preparing dev-tool sandbox…
    </div>
  );
}
