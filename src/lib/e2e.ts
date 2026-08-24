import "server-only";
import type { JWT } from "next-auth/jwt";

/**
 * E2E test mode (automated Playwright testing without GitHub OAuth+MFA).
 *
 * Active only when AIDW_E2E_MODE=1 AND not a production build -- the guard is deliberately
 * conjunctive so the bypass can never ship enabled. In this mode the middleware (src/proxy.ts)
 * stops enforcing sign-in, and every consumer of the session's GitHub identity/token falls back
 * to E2E_GITHUB_TOKEN (a PAT with `repo` read on the target repos) and a fixed synthetic user id.
 */
export const E2E_MODE = process.env.AIDW_E2E_MODE === "1" && process.env.NODE_ENV !== "production";

export const E2E_GITHUB_TOKEN = process.env.E2E_GITHUB_TOKEN;

/** Stable synthetic identity used wherever a githubId is needed (auth checks, hasRepoAccess) --
 * must not vary between requests within one E2E run. */
export const E2E_GITHUB_ID = "e2e-user";

/** The session's GitHub access token, falling back to the E2E PAT in bypass mode (which has no
 * authenticated session to read one from). undefined when neither is available. */
export function githubAccessToken(token: JWT | null): string | undefined {
  return token?.accessToken ?? (E2E_MODE ? E2E_GITHUB_TOKEN : undefined);
}

if (E2E_MODE) {
  console.warn(
    "[ai-dev-workflow] AIDW_E2E_MODE is ACTIVE: authentication is bypassed and GitHub API calls " +
      "use E2E_GITHUB_TOKEN. Never enable this outside local end-to-end testing.",
  );
}
