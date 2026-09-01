"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useSession, signOut } from "next-auth/react";

/** Minimal app-wide header: brand (doubles as "back to repositories" nav) and account controls.
 * Mounted on every page. The sandbox connection indicator moved into AppShell's tab row: this
 * header mounts in root layout, OUTSIDE the workflow page's SandboxStatusProvider, so a pill
 * here always read null context and rendered nothing. */
export function WorkspaceHeader() {
  const { data: session } = useSession();
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-neutral-200 px-4 py-2 text-sm">
      <Link href="/select" className="shrink-0 font-medium text-neutral-700 hover:text-neutral-900">
        AI-Assisted Specification &amp; Planning
      </Link>
      <div className="flex flex-wrap items-center justify-end gap-3">
        <RefreshSecretsButton />
        {session?.user && (
          <>
            {/* Admin-only surface (Entra App Role): hiding the link is courtesy -- the page's
                layout 404s and the API routes 403 non-admins server-side regardless. */}
            {session.isAdmin && (
              <Link
                href="/settings/organization"
                className="rounded-md px-2 py-1 text-neutral-500 hover:bg-neutral-100"
              >
                Org settings
              </Link>
            )}
            <span className="text-neutral-600">{session.user.name ?? session.user.email}</span>
            <DisconnectGithubButton />
            <button
              onClick={() => signOut({ redirectTo: "/" })}
              className="rounded-md px-2 py-1 text-neutral-500 hover:bg-neutral-100"
            >
              Sign out
            </button>
          </>
        )}
      </div>
    </div>
  );
}

/** Disconnect GitHub: revoke + delete the stored link, then drop the JWT claims via update().
 * Shown only when a link exists. "Signing out keeps GitHub linked for your next sign-in;
 * Disconnect removes it" -- users can't otherwise tell signout from unlink. */
function DisconnectGithubButton() {
  const { data: session, update } = useSession();
  const [busy, setBusy] = useState(false);
  if (!session?.githubConnected) return null;
  return (
    <button
      type="button"
      disabled={busy}
      title="Removes the stored GitHub authorization. Signing out alone keeps it linked for next time."
      onClick={async () => {
        if (!window.confirm("Disconnect GitHub? You'll need to reconnect to browse repositories. (Signing out alone keeps it linked.)")) return;
        setBusy(true);
        try {
          const res = await fetch("/api/github/disconnect", { method: "POST" });
          if (res.ok) await update({ github: "disconnect" });
        } finally {
          setBusy(false);
        }
      }}
      className="rounded-md px-2 py-1 text-neutral-500 hover:bg-neutral-100 disabled:opacity-40"
    >
      {busy ? "Disconnecting…" : "Disconnect GitHub"}
    </button>
  );
}

type RefreshState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; secretCount: number }
  | { kind: "error"; detail: string };

/**
 * Mid-session "the user just added/rotated a vault secret" escape hatch -- re-fetches secrets
 * on-behalf-of the user (fresh assertion minted server-side per click) and re-writes the env
 * file inside the sandbox, no session restart. Rendered only on a workflow route whose repo has
 * a vault configured; invisible everywhere else (including E2E-bypass mode, which has no
 * authenticated session).
 */
function RefreshSecretsButton() {
  const { status } = useSession();
  const params = useParams<{ owner?: string; repo?: string; sessionId?: string }>();
  const [vaultConfigured, setVaultConfigured] = useState(false);
  const [state, setState] = useState<RefreshState>({ kind: "idle" });
  const { owner, repo, sessionId } = params ?? {};
  // Off-route / signed-out is handled by the render guard below, never by resetting state in the
  // effect (react-hooks/set-state-in-effect).
  const eligible = status === "authenticated" && Boolean(owner && repo && sessionId);

  useEffect(() => {
    if (!eligible || !owner || !repo) return;
    let cancelled = false;
    const query = new URLSearchParams({ owner, repo });
    fetch(`/api/repos/vault?${query}`)
      .then((res) => {
        if (!cancelled) setVaultConfigured(res.ok);
      })
      .catch(() => {
        if (!cancelled) setVaultConfigured(false);
      });
    return () => {
      cancelled = true;
    };
  }, [eligible, owner, repo]);

  if (!eligible || !vaultConfigured) return null;

  async function refresh() {
    setState({ kind: "running" });
    try {
      const res = await fetch("/api/sessions/actions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, action: "refresh-secrets" }),
      });
      const body = (await res.json()) as { secret_count?: number; detail?: string };
      if (res.ok) {
        setState({ kind: "done", secretCount: body.secret_count ?? 0 });
      } else {
        setState({ kind: "error", detail: body.detail ?? `failed (${res.status})` });
      }
    } catch {
      setState({ kind: "error", detail: "network error" });
    }
  }

  return (
    <span className="flex items-center gap-2">
      {state.kind === "done" && (
        <span className="text-xs text-green-700">✓ {state.secretCount} secrets</span>
      )}
      {state.kind === "error" && (
        <span className="max-w-64 truncate text-xs text-red-600" title={state.detail}>
          {state.detail}
        </span>
      )}
      <button
        onClick={refresh}
        disabled={state.kind === "running"}
        className="rounded-md border border-neutral-300 px-2 py-1 text-neutral-600 hover:bg-neutral-100 disabled:opacity-40"
        title="Re-fetch this repo's Key Vault secrets into the running session"
      >
        {state.kind === "running" ? "Refreshing…" : "Refresh Key Vault secrets"}
      </button>
    </span>
  );
}
