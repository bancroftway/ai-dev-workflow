import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";

/**
 * Disconnect GitHub for the signed-in user: revoke the OAuth grant on GitHub's side (best-effort,
 * so the authorization is genuinely gone, not just forgotten), then delete the token stored in the
 * org vault. The client calls `update({ github: "disconnect" })` afterward to drop the claims from
 * its own JWT (the route can't mutate the cookie). Deletion happens ONLY here, never on a refresh
 * failure — see the jwt callback's refreshGithubIfNeeded.
 */
export async function POST() {
  const token = await getServerAuthToken();
  if (!token?.entraAccessToken) {
    return NextResponse.json({ detail: "not signed in" }, { status: 401 });
  }

  const clientId = process.env.AUTH_GITHUB_ID;
  const clientSecret = process.env.AUTH_GITHUB_SECRET;
  if (token.accessToken && clientId && clientSecret) {
    try {
      const basic = Buffer.from(`${clientId}:${clientSecret}`).toString("base64");
      await fetch(`https://api.github.com/applications/${clientId}/grant`, {
        method: "DELETE",
        headers: { Authorization: `Basic ${basic}`, Accept: "application/vnd.github+json" },
        body: JSON.stringify({ access_token: token.accessToken }),
      });
    } catch {
      // Best-effort: a GitHub-side revocation failure must not block clearing our own copy.
    }
  }

  const res = await agentFetch("github-link/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entra_assertion: token.entraAccessToken }),
  });
  if (!res.ok) {
    return NextResponse.json({ detail: `disconnect failed (${res.status})` }, { status: 502 });
  }
  return NextResponse.json({ ok: true });
}
