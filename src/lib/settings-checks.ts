import type { Session } from "next-auth";

/**
 * Detects missing settings the user should fix, for SettingsBanner to render. Deliberately a
 * flat list of checks, not a framework: adding a check = one more block in runSettingsChecks
 * returning a MissingSetting with whatever fix action applies.
 */
export type MissingSetting = {
  id: "entra-session" | "github-connected" | "key-vault";
  label: string;
  description: string;
  fix:
    | { kind: "link"; href: string; label: string }
    | { kind: "connect-github" }
    | { kind: "signin-entra" };
};

export async function runSettingsChecks(ctx: {
  session: Session | null;
  owner?: string;
  repo?: string;
}): Promise<MissingSetting[]> {
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

  // Repo-scoped: only meaningful once a repo is selected and GitHub is linked (the vault lookup
  // is keyed by the GitHub login).
  if (ctx.owner && ctx.repo && ctx.session.githubConnected) {
    const params = new URLSearchParams({ owner: ctx.owner, repo: ctx.repo });
    const res = await fetch(`/api/repos/vault?${params}`);
    if (res.status === 404) {
      missing.push({
        id: "key-vault",
        label: "No Key Vault configured",
        description: `Sessions on ${ctx.owner}/${ctx.repo} will run the app without secrets. Point this repo at your Azure Key Vault to inject them automatically.`,
        fix: {
          kind: "link",
          href: `/settings/${encodeURIComponent(ctx.owner)}/${encodeURIComponent(ctx.repo)}`,
          label: "Configure",
        },
      });
    }
  }

  return missing;
}
