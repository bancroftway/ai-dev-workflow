"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { signIn, useSession } from "next-auth/react";
import { runSettingsChecks, type MissingSetting } from "@/lib/settings-checks";

/**
 * Amber banners for missing settings (settings-checks.ts) -- one per finding, nothing when all
 * pass. Renders nothing without an authenticated session, which also keeps it out of the way in
 * E2E-bypass mode (no session exists there).
 */
export function SettingsBanner({ owner, repo }: { owner?: string; repo?: string }) {
  const { data: session, status } = useSession();
  const [missing, setMissing] = useState<MissingSetting[]>([]);
  const githubConnected = session?.githubConnected;
  const entraAuthError = session?.entraAuthError;

  useEffect(() => {
    // Signed-out is handled by the render guard, never by resetting state here
    // (react-hooks/set-state-in-effect).
    if (status !== "authenticated") return;
    let cancelled = false;
    runSettingsChecks({ session, owner, repo }).then((found) => {
      if (!cancelled) setMissing(found);
    });
    return () => {
      cancelled = true;
    };
    // session object identity churns per render -- the fields the checks read are the real deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, githubConnected, entraAuthError, owner, repo]);

  if (status !== "authenticated" || missing.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      {missing.map((item) => (
        <div
          key={item.id}
          className="flex items-start justify-between gap-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-3"
        >
          <div>
            <p className="text-sm font-medium text-amber-900">{item.label}</p>
            <p className="mt-0.5 text-sm text-amber-800">{item.description}</p>
          </div>
          <FixAction fix={item.fix} />
        </div>
      ))}
    </div>
  );
}

function FixAction({ fix }: { fix: MissingSetting["fix"] }) {
  const buttonClass =
    "shrink-0 rounded-md bg-amber-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-800";
  if (fix.kind === "link") {
    return (
      <Link href={fix.href} className={buttonClass}>
        {fix.label} →
      </Link>
    );
  }
  if (fix.kind === "connect-github") {
    return (
      <button type="button" className={buttonClass} onClick={() => signIn("github", { redirectTo: "/select" })}>
        Connect GitHub
      </button>
    );
  }
  return (
    <button
      type="button"
      className={buttonClass}
      onClick={() => signIn("microsoft-entra-id", { redirectTo: "/select" })}
    >
      Sign in again
    </button>
  );
}
