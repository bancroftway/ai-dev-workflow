"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { signIn, useSession } from "next-auth/react";
import { checkKeyVault, runSettingsChecks, type MissingSetting } from "@/lib/settings-checks";

/**
 * Amber banners for missing settings (settings-checks.ts) -- one per finding, nothing when all
 * pass. Renders nothing without an authenticated session, which also keeps it out of the way in
 * E2E-bypass mode (no session exists there).
 *
 * onReadyChange (I-2b, whole-branch review): fires with whether the org-credential check passed,
 * every time this banner's own single runSettingsChecks call resolves -- lets a page (tickets/new)
 * fold the SAME signal it's already displaying into its own submit-gating logic, without a second
 * fetch of /api/settings/organization. Optional and unused by every other mount site
 * (select/page.tsx, the board page) -- this banner's own fetch-and-render behavior is unchanged
 * whether or not a caller passes it.
 */
export function SettingsBanner({
  owner,
  repo,
  onReadyChange,
}: {
  owner?: string;
  repo?: string;
  onReadyChange?: (ready: boolean) => void;
}) {
  const { data: session, status } = useSession();
  const [orgMissing, setOrgMissing] = useState<MissingSetting[]>([]);
  const [vaultMissing, setVaultMissing] = useState<MissingSetting[]>([]);
  const githubConnected = session?.githubConnected;
  const entraAuthError = session?.entraAuthError;

  // Session/org-scoped checks (entra-session, github-connected, org-credential) -- deliberately
  // NOT keyed on owner/repo: none of them depend on which repo is picked, and keying them there
  // re-ran the org-credential backend round trip (DB + Key Vault) on every repo click. Calling
  // runSettingsChecks without owner/repo skips its repo-scoped key-vault block (its own guard);
  // that check lives in the separate owner/repo-keyed effect below.
  useEffect(() => {
    // Signed-out is handled by the render guard, never by resetting state here
    // (react-hooks/set-state-in-effect).
    if (status !== "authenticated") return;
    let cancelled = false;
    runSettingsChecks({ session }).then((found) => {
      if (cancelled) return;
      setOrgMissing(found);
      onReadyChange?.(!found.some((item) => item.id === "org-credential"));
    });
    return () => {
      cancelled = true;
    };
    // session object identity churns per render -- the fields the checks read are the real deps.
    // onReadyChange deliberately excluded too: an inline arrow function prop (the expected usage)
    // would otherwise re-create this effect every render for no reason -- this only ever needs to
    // fire after a genuine re-check, not after the caller merely re-rendered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, githubConnected, entraAuthError]);

  // Repo-scoped key-vault check, keyed on the repo it's about.
  useEffect(() => {
    if (status !== "authenticated" || !owner || !repo || !githubConnected) return;
    let cancelled = false;
    checkKeyVault(owner, repo)
      .then((finding) => {
        if (!cancelled) setVaultMissing(finding ? [finding] : []);
      })
      .catch(() => {
        // Same outcome as the composed runSettingsChecks call on a thrown vault fetch: no state
        // update (the banner call site never had a .catch either).
      });
    return () => {
      cancelled = true;
    };
  }, [status, githubConnected, owner, repo]);

  // Same combined shape/order runSettingsChecks returned (org findings first, key-vault last).
  // The owner/repo guard drops a stale vault finding at render time when the repo is deselected,
  // instead of a synchronous state reset in the effect (react-hooks/set-state-in-effect).
  const missing = owner && repo && githubConnected ? [...orgMissing, ...vaultMissing] : orgMissing;

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
