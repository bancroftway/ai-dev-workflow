import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";
import MicrosoftEntraID from "next-auth/providers/microsoft-entra-id";
import { getToken, type JWT } from "next-auth/jwt";
import { cookies } from "next/headers";

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
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    /** GitHub OAuth access token (linked account). Server-side only -- never copied onto the
     * session object; read it via getServerAuthToken(). */
    accessToken?: string;
    githubId?: string;
    login?: string;
    /** Entra access token for the agent API (the OBO assertion). Server-side only. */
    entraAccessToken?: string;
    entraRefreshToken?: string;
    /** Epoch seconds. */
    entraExpiresAt?: number;
    /** Entra object id -- the immutable user key in the tenant. */
    oid?: string;
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
    async jwt({ token, account, profile }) {
      if (account?.provider === "microsoft-entra-id") {
        // Fresh Entra sign-in. Auth.js seeded `token` from scratch, so carry an existing GitHub
        // link over from the session being replaced (re-logins must not force re-linking).
        const prev = await getServerAuthToken();
        token.entraAccessToken = account.access_token;
        token.entraRefreshToken = account.refresh_token;
        token.entraExpiresAt = account.expires_at;
        token.oid = (profile as { oid?: string } | undefined)?.oid;
        if (prev?.accessToken) {
          token.accessToken = prev.accessToken;
          token.githubId = prev.githubId;
          token.login = prev.login;
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
        }
        token.accessToken = account.access_token;
        token.githubId = account.providerAccountId;
        token.login = (profile as { login?: string } | undefined)?.login;
        return token;
      }
      // Routine read (no sign-in event): silently refresh the Entra access token when close to
      // expiry so provision/actions always have a live assertion to forward.
      return refreshEntraIfNeeded(token);
    },
    async session({ session, token }) {
      // Identity/display fields only -- tokens stay on the JWT (getServerAuthToken).
      session.githubId = token.githubId;
      session.login = token.login;
      session.githubConnected = Boolean(token.accessToken);
      session.entraAuthError = token.error;
      return session;
    },
  },
});
