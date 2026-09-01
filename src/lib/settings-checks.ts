import type { Session } from "next-auth";

/**
 * Detects missing settings the user should fix, for SettingsBanner to render. Deliberately a
 * flat list of checks, not a framework: adding a check = one more block in runSettingsChecks
 * returning a MissingSetting with whatever fix action applies.
 */
export type MissingSetting = {
  id: "entra-session" | "github-connected" | "key-vault" | "org-credential";
  label: string;
  description: string;
  fix:
    | { kind: "link"; href: string; label: string }
    | { kind: "connect-github" }
    | { kind: "signin-entra" };
};

// Shared across the "not ready", "non-2xx", and "fetch threw" branches below (M-9 fix: all three
// now push this same finding -- fail CLOSED, not silently fail-open on the latter two).
const ORG_CREDENTIAL_FINDING: MissingSetting = {
  id: "org-credential",
  label: "No usable coding-agent credential",
  description: "No provider credential is configured or currently usable. Sessions can't run until an admin sets one for the organization.",
  fix: { kind: "link", href: "/settings/organization", label: "Open Settings" },
};

export async function runSettingsChecks(ctx: { session: Session | null }): Promise<MissingSetting[]> {
  const missing: MissingSetting[] = [];
  if (!ctx.session) return missing;

  if (ctx.session.entraAuthError) {
    missing.push({
      id: "entra-session",
      label: "Microsoft session expired",
      description: "The silent sign-in renewal failed. Sign in again to keep Key Vault access working.",
      fix: { kind: "signin-entra" },
    });
  }

  if (!ctx.session.githubConnected) {
    missing.push({
      id: "github-connected",
      label: "GitHub not connected",
      description: "Connect your GitHub account to list repositories, read branches, and push work.",
      fix: { kind: "connect-github" },
    });
  }

  // Session-scoped, org-wide: no owner/repo needed, unlike the key-vault check below. This
  // codebase has no admin/role concept (checked src/lib/session-access.ts and grepped the rest of
  // src/ -- confirmed again while wiring this in): every signed-in user already has the same
  // access to /settings/organization, so there is no "admin vs. non-admin" copy to branch on here
  // -- everyone gets the same actionable link.
  //
  // Keyed off `session_ready`, deliberately NOT the same field
  // src/app/(boxed)/settings/organization/page.tsx keys its own UI off (`credential_configured`).
  // That's not a bug, it's two different questions answered from the same response: the settings
  // page asks "is a credential saved in the vault" (so it knows whether to show the masked-dots
  // display or an empty input); this banner asks "would a session provisioned right now actually
  // get a usable credential" (agent/src/sessions_api.py's own `_org_settings_response()` docstring
  // spells out why they're deliberately different signals -- an env-var-only deployment is
  // credential_configured=false but session_ready=true, and keying this banner off
  // credential_configured would make it permanently, falsely warn on every such deployment).
  //
  // try/catch scoped to just this check: unlike the key-vault fetch below (only reachable once a
  // repo is picked), this one runs unconditionally on every call, so a network failure here must
  // not reject the whole runSettingsChecks promise -- that would also discard the two synchronous,
  // unrelated findings above at the SettingsBanner call site, which has no .catch().
  //
  // Fails CLOSED, on both a thrown fetch and a non-2xx response: this banner exists specifically
  // to catch "sessions can't actually run" before a user wastes container-minutes discovering that
  // themselves (sessions_api.py's own provision-time 409 is the backstop; this is the advisory
  // version of the same check) -- staying silent on "couldn't tell" is exactly the wrong default
  // for that job. A transient blip reaching our own same-origin BFF route is rare enough that an
  // occasional false-positive banner is the cheaper mistake here, not the reverse.
  try {
    const orgRes = await fetch("/api/settings/organization");
    if (orgRes.ok) {
      const orgSettings = (await orgRes.json()) as { session_ready?: boolean };
      if (!orgSettings.session_ready) {
        missing.push(ORG_CREDENTIAL_FINDING);
      }
    } else {
      missing.push(ORG_CREDENTIAL_FINDING);
    }
  } catch {
    missing.push(ORG_CREDENTIAL_FINDING);
  }

  return missing;
}

/** Repo-scoped key-vault check, separate from runSettingsChecks so SettingsBanner can key it on
 * the selected repo without re-running the org-credential round trip. Only a 404 is a finding;
 * a thrown fetch rejects and the caller keeps its previous state (same as the old composed call,
 * whose call site had no .catch). */
export async function checkKeyVault(owner: string, repo: string): Promise<MissingSetting | null> {
  const params = new URLSearchParams({ owner, repo });
  const res = await fetch(`/api/repos/vault?${params}`);
  if (res.status !== 404) return null;
  return {
    id: "key-vault",
    label: "No Key Vault configured",
    description: `Sessions on ${owner}/${repo} will run the app without secrets. Point this repo at your Azure Key Vault to inject them automatically.`,
    fix: {
      kind: "link",
      href: `/settings/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`,
      label: "Configure",
    },
  };
}
