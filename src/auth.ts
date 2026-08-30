import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";
import MicrosoftEntraID from "next-auth/providers/microsoft-entra-id";
import { getToken, type JWT } from "next-auth/jwt";
import { cookies } from "next/headers";
import { agentFetch } from "@/lib/agent-client";

const GITHUB_CLIENT_ID = process.env.AUTH_GITHUB_ID;
const GITHUB_CLIENT_SECRET = process.env.AUTH_GITHUB_SECRET;

declare module "next-auth" {
  interface Session {
    githubId?: string;
    /** GitHub login (handle), captured when the GitHub account is linked. Advisory only
     * downstream (e.g. session_index `user` field on the agent side is not re-verified there) --
     * never used as an identity key, that's what githubId is for. */
    login?: string;
    /** True once a GitHub account has been linked (its token lives server-side only). */
    githubConnected: boolean;
    /** Set when the silent Entra token refresh failed -- UI should prompt a re-login. */
    entraAuthError?: string;
    /** True when the Entra ID token carried the "Admin" App Role. Display-gating only (hide the
     * Org settings link, 404 the page) -- the API routes re-check the JWT's roles server-side. */
    isAdmin: boolean;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    /** GitHub OAuth access token (linked account). Server-side only -- never copied onto the
     * session object; read it via getServerAuthToken(). */
    accessToken?: string;
    githubId?: string;
    login?: string;
    /** GitHub refresh token (present only when the OAuth app has token expiration enabled).
     * Server-side only; persisted to the org vault so the link survives cookie loss. */
    githubRefreshToken?: string;
    /** Epoch seconds; 0/undefined means a non-expiring classic token (no refresh needed). */
    githubExpiresAt?: number;
    githubRefreshTokenExpiresAt?: number;
    /** Entra access token for the agent API (the OBO assertion). Server-side only. */
    entraAccessToken?: string;
    entraRefreshToken?: string;
    /** Epoch seconds. */
    entraExpiresAt?: number;
    /** Entra object id -- the immutable user key in the tenant. */
    oid?: string;
    /** Entra App Roles ("Admin" | "Member") from the ID token's roles claim. Captured at sign-in
     * only -- refreshEntraIfNeeded never re-reads claims, so a role change takes effect at the
     * next login (documented limitation, CI/CD plan Phase 6). */
    roles?: string[];
    error?: "EntraRefreshFailed";
  }
}

// ONE app registration serves sign-in, the exposed API, and the agent's OBO exchange (user
// decision: fewest moving parts for a single-tenant internal tool). Three env values total:
// AZURE_TENANT_ID, AIDW_AGENT_APP_ID, AIDW_AGENT_CLIENT_SECRET -- shared verbatim with the
// Python agent (agent/src/keyvault.py reads the same names).
const ENTRA_APP_ID = process.env.AIDW_AGENT_APP_ID;
const ENTRA_CLIENT_SECRET = process.env.AIDW_AGENT_CLIENT_SECRET;
const ENTRA_AUTHORITY = `https://login.microsoftonline.com/${process.env.AZURE_TENANT_ID}`;

// The app requests its OWN exposed scope: the resulting access token (aud == this app) is the
// assertion the agent exchanges on-behalf-of for Key Vault access -- same-app OBO is a
// documented, supported pattern. offline_access is what makes Entra return a refresh token.
const ENTRA_SCOPES = `openid profile email offline_access api://${ENTRA_APP_ID}/access_as_user`;

/**
 * Decrypt the CURRENT session JWT straight from the request's cookies.
 *
 * Two jobs:
 * - During a sign-in callback, Auth.js seeds a brand-new JWT, so this is the only way to see the
 *   claims of the session being replaced (how GitHub linking survives an Entra re-login and how
 *   the Entra claims survive the GitHub link sign-in).
 * - For API routes / server components that need the raw tokens, which are deliberately NOT on
 *   the session object (the session is serialized into the RSC payload and returned by
 *   /api/auth/session -- putting a repo-scoped or vault-capable token there hands it to the
 *   browser).
 *
 * Request context only (route handlers, RSC, server actions) -- next/headers cookies() throws in
 * edge middleware, which is fine: middleware only checks session presence.
 */
export async function getServerAuthToken(): Promise<JWT | null> {
  const store = await cookies();
  const header = store
    .getAll()
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");
  if (!header) return null;
  // Cookie name differs by scheme (__Secure- prefix on https) -- try both rather than guessing
  // from env. getToken reassembles chunked (.0/.1) cookies itself.
  for (const secureCookie of [true, false]) {
    const token = await getToken({
      req: { headers: { cookie: header } },
      secret: process.env.AUTH_SECRET!,
      secureCookie,
    });
    if (token) return token as JWT;
  }
  return null;
}

/**
 * Server-derived audit-trail identity (updated_by / created_by fields) -- must come from the
 * session JWT, never from a client body. Entra is the primary sign-in and GitHub only a linked
 * credential, so token.login (GitHub handle, only present once GitHub is linked) isn't the right
 * first choice -- an Entra-only user who never linked GitHub must still be able to save. Prefer
 * the Entra profile fields every signed-in session actually carries (email, then name), falling
 * back to token.login, then to the immutable Entra object id so this is never empty for anyone
 * who actually made it past src/proxy.ts's sign-in gate.
 */
export function auditIdentity(token: JWT | null): string | undefined {
  return token?.email ?? token?.name ?? token?.login ?? token?.oid;
}

/**
 * Server-side Admin gate for org-level surfaces (Entra App Role "Admin", CI/CD plan Phase 6).
 * Reads the JWT directly -- the session object's isAdmin is display-gating only. Request context
 * only (same constraint as getServerAuthToken).
 */
export async function isAdminRequest(): Promise<boolean> {
  const token = await getServerAuthToken();
  return token?.roles?.includes("Admin") ?? false;
}

async function refreshEntraIfNeeded(token: JWT): Promise<JWT> {
  if (!token.entraRefreshToken || !token.entraExpiresAt) return token;
  const msLeft = token.entraExpiresAt * 1000 - Date.now();
  if (msLeft > 5 * 60_000) return token;
  try {
    const res = await fetch(`${ENTRA_AUTHORITY}/oauth2/v2.0/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: ENTRA_APP_ID!,
        client_secret: ENTRA_CLIENT_SECRET!,
        grant_type: "refresh_token",
        refresh_token: token.entraRefreshToken,
        scope: ENTRA_SCOPES,
      }),
    });
    const data = (await res.json()) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
      error?: string;
      error_description?: string;
    };
    if (!res.ok || !data.access_token) {
      throw new Error(data.error_description ?? data.error ?? `refresh failed (${res.status})`);
    }
    token.entraAccessToken = data.access_token;
    token.entraExpiresAt = Math.floor(Date.now() / 1000) + Number(data.expires_in ?? 3600);
    // Entra rotates refresh tokens; keep the old one if a rotation wasn't issued.
    if (data.refresh_token) token.entraRefreshToken = data.refresh_token;
    delete token.error;
  } catch {
    // Leave the (dead) tokens in place and flag it -- session callback surfaces entraAuthError
    // and the UI prompts a re-login. Recovery is always "sign in again", never a retry loop here.
    token.error = "EntraRefreshFailed";
  }
  return token;
}

// --- GitHub link persistence (org vault, keyed by Entra oid) ----------------------------------
// The agent verifies the entra_assertion via tenant JWKS and derives the oid itself -- we never
// send a bare oid. All three calls are best-effort: any failure degrades to today's cookie-only
// link, never breaks sign-in.

type StoredGithubLink = {
  access_token?: string;
  refresh_token?: string;
  expires_at?: number;
  refresh_token_expires_at?: number;
  github_id?: number;
  login?: string;
};

async function readGithubLink(entraAssertion: string): Promise<StoredGithubLink | null> {
  try {
    const res = await agentFetch("github-link/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entra_assertion: entraAssertion }),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { linked?: boolean } & StoredGithubLink;
    return data.linked ? data : null;
  } catch {
    return null;
  }
}

async function writeGithubLink(entraAssertion: string, token: JWT): Promise<void> {
  if (!token.accessToken) return;
  try {
    await agentFetch("github-link", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entra_assertion: entraAssertion,
        access_token: token.accessToken,
        refresh_token: token.githubRefreshToken ?? null,
        expires_at: token.githubExpiresAt ?? null,
        refresh_token_expires_at: token.githubRefreshTokenExpiresAt ?? null,
        github_id: token.githubId ? Number(token.githubId) : null,
        login: token.login ?? null,
      }),
    });
  } catch {
    // Non-fatal: the link still works for this session (JWT-only), same as before this feature.
  }
}

function applyStoredLink(token: JWT, link: StoredGithubLink): void {
  token.accessToken = link.access_token;
  token.githubRefreshToken = link.refresh_token;
  token.githubExpiresAt = link.expires_at;
  token.githubRefreshTokenExpiresAt = link.refresh_token_expires_at;
  token.githubId = link.github_id != null ? String(link.github_id) : undefined;
  token.login = link.login;
}

async function refreshGithubIfNeeded(token: JWT): Promise<JWT> {
  // Only apps with token expiration enabled hand out refresh tokens + a real expiry; a classic
  // non-expiring token has no githubExpiresAt and needs nothing here.
  if (!token.githubRefreshToken || !token.githubExpiresAt) return token;
  if (token.githubExpiresAt * 1000 - Date.now() > 5 * 60_000) return token;

  async function attemptRefresh(refreshToken: string) {
    const res = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: new URLSearchParams({
        client_id: GITHUB_CLIENT_ID!,
        client_secret: GITHUB_CLIENT_SECRET!,
        grant_type: "refresh_token",
        refresh_token: refreshToken,
      }),
    });
    return (await res.json()) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
      refresh_token_expires_in?: number;
      error?: string;
    };
  }

  const applyRefreshed = (d: Awaited<ReturnType<typeof attemptRefresh>>) => {
    const now = Math.floor(Date.now() / 1000);
    token.accessToken = d.access_token;
    token.githubExpiresAt = now + Number(d.expires_in ?? 28800);
    if (d.refresh_token) token.githubRefreshToken = d.refresh_token;
    if (d.refresh_token_expires_in) token.githubRefreshTokenExpiresAt = now + Number(d.refresh_token_expires_in);
  };

  try {
    let data = await attemptRefresh(token.githubRefreshToken);
    if (data.error === "bad_refresh_token" && token.entraAccessToken) {
      // Rotation race / another device already refreshed: NEVER delete. Re-read the vault; if it
      // holds a different (newer) pair, adopt it and retry once. Only if it still matches the dead
      // token do we drop the claims (unlinked); the stored copy self-heals on the next real link.
      const stored = await readGithubLink(token.entraAccessToken);
      if (stored?.refresh_token && stored.refresh_token !== token.githubRefreshToken) {
        applyStoredLink(token, stored);
        return token; // adopted a live pair; no retry needed
      }
      data = await attemptRefresh(token.githubRefreshToken);
    }
    if (!data.access_token) {
      // Dead link: drop the GitHub claims for this session (banner offers re-link). Do NOT delete
      // the vault secret -- that's Disconnect's job only.
      token.accessToken = undefined;
      token.githubRefreshToken = undefined;
      token.githubExpiresAt = undefined;
      return token;
    }
    applyRefreshed(data);
    if (token.entraAccessToken) await writeGithubLink(token.entraAccessToken, token);
  } catch {
    // Network hiccup: leave the (possibly stale) token; next request retries. Don't unlink.
  }
  return token;
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    MicrosoftEntraID({
      clientId: ENTRA_APP_ID,
      clientSecret: ENTRA_CLIENT_SECRET,
      issuer: `${ENTRA_AUTHORITY}/v2.0`,
      // No prompt=consent needed: the app pre-authorizes ITSELF for api://<app>/access_as_user
      // (api.preAuthorizedApplications, set 2026-08-30), because this tenant's "recommended"
      // user-consent policy silently blocks user consent for custom API scopes -- sign-ins never
      // showed a consent screen and the missing grant surfaced only as AADSTS65001 at the OBO
      // exchange. Pre-authorization needs app ownership only, not tenant admin.
      authorization: { params: { scope: ENTRA_SCOPES } },
    }),
    GitHub({
      // Link-only provider (see signIn callback). Default scope (read:user user:email) can't
      // list private repos, list branches, or read repo contents -- all required for the
      // repo/branch picker and onboarding-detection (architecture plan Section A). "repo" is
      // broad; narrowing this to a GitHub App with per-repo installation tokens is the
      // documented follow-up once this grows past a small internal tool.
      authorization: { params: { scope: "read:user user:email repo" } },
    }),
  ],
  session: { strategy: "jwt" },
  callbacks: {
    async signIn({ account }) {
      // GitHub is a linked credential, not a sign-in: without an existing Entra session there
      // would be no assertion for the agent's on-behalf-of flow, so refuse the bare login.
      if (account?.provider === "github") {
        const prev = await getServerAuthToken();
        if (!prev?.entraAccessToken) return false;
      }
      return true;
    },
    async jwt({ token, account, profile, trigger, session }) {
      // Disconnect (client calls update({ github: "disconnect" }) after the route revokes the
      // vault copy): drop the GitHub claims from this session's JWT.
      if (trigger === "update" && (session as { github?: string } | undefined)?.github === "disconnect") {
        token.accessToken = undefined;
        token.githubRefreshToken = undefined;
        token.githubExpiresAt = undefined;
        token.githubRefreshTokenExpiresAt = undefined;
        token.githubId = undefined;
        token.login = undefined;
        return token;
      }
      if (account?.provider === "microsoft-entra-id") {
        // Fresh Entra sign-in. Auth.js seeded `token` from scratch, so carry an existing GitHub
        // link over from the session being replaced (re-logins must not force re-linking).
        const prev = await getServerAuthToken();
        token.entraAccessToken = account.access_token;
        token.entraRefreshToken = account.refresh_token;
        token.entraExpiresAt = account.expires_at;
        token.oid = (profile as { oid?: string } | undefined)?.oid;
        // App Roles arrive in the ID token once assigned in the tenant (enterprise app has
        // assignment-required, so an unassigned user never gets this far).
        token.roles = (profile as { roles?: string[] } | undefined)?.roles ?? [];
        if (prev?.accessToken) {
          token.accessToken = prev.accessToken;
          token.githubId = prev.githubId;
          token.login = prev.login;
          token.githubRefreshToken = prev.githubRefreshToken;
          token.githubExpiresAt = prev.githubExpiresAt;
          token.githubRefreshTokenExpiresAt = prev.githubRefreshTokenExpiresAt;
        } else if (token.entraAccessToken && token.oid) {
          // No GitHub link in the cookie being replaced (sign-out, new browser, expired cookie) --
          // recover it from the org vault so the user links only once, ever. Sign-in path only.
          const stored = await readGithubLink(token.entraAccessToken);
          if (stored) applyStoredLink(token, stored);
        }
        return token;
      }
      if (account?.provider === "github") {
        // Linking: this sign-in also seeded a fresh JWT -- restore the Entra claims from the
        // session being replaced, then merge the new GitHub credentials in.
        const prev = await getServerAuthToken();
        if (prev) {
          token.entraAccessToken = prev.entraAccessToken;
          token.entraRefreshToken = prev.entraRefreshToken;
          token.entraExpiresAt = prev.entraExpiresAt;
          token.oid = prev.oid;
          // Same carry-over rule as oid: the GitHub link sign-in seeds a fresh JWT, and losing
          // the roles claim here would silently demote an admin until their next Entra login.
          token.roles = prev.roles;
        }
        token.accessToken = account.access_token;
        token.githubId = account.providerAccountId;
        token.login = (profile as { login?: string } | undefined)?.login;
        // account.expires_at/refresh_token present only when the OAuth app has token expiration on.
        token.githubRefreshToken = account.refresh_token;
        token.githubExpiresAt = account.expires_at;
        token.githubRefreshTokenExpiresAt = (account as { refresh_token_expires_at?: number } | undefined)?.refresh_token_expires_at;
        // Persist the link so it survives this cookie. Best-effort; needs a live assertion.
        if (token.entraAccessToken) await writeGithubLink(token.entraAccessToken, token);
        return token;
      }
      // Routine read (no sign-in event): keep both access tokens live for provision/actions.
      return refreshGithubIfNeeded(await refreshEntraIfNeeded(token));
    },
    async session({ session, token }) {
      // Identity/display fields only -- tokens stay on the JWT (getServerAuthToken).
      session.githubId = token.githubId;
      session.login = token.login;
      session.githubConnected = Boolean(token.accessToken);
      session.entraAuthError = token.error;
      session.isAdmin = token.roles?.includes("Admin") ?? false;
      return session;
    },
  },
});
