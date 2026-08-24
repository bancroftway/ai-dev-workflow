import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { githubAccessToken } from "@/lib/e2e";
import { getOctokit } from "@/lib/github";

const CONTENT_TYPE_BY_EXT: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
};

// Applied to every response this route sends, including error responses -- an attacker probing
// with a crafted path gets the same locked-down headers as a legitimate screenshot fetch.
const HARDENED_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Content-Security-Policy": "default-src 'none'; sandbox",
  "Cache-Control": "private, max-age=300",
};

function notFound() {
  return NextResponse.json({ error: "not found" }, { status: 404, headers: HARDENED_HEADERS });
}

/**
 * Hardened raw-content proxy for repo-controlled screenshots (Report tab / past-session report
 * page). `?path=` QUERY STYLE ONLY, never a `[...path]` segment route: a path-suffixed URL ending
 * in `.png` would bypass src/proxy.ts's auth middleware entirely (its matcher excludes image
 * extensions so ordinary static-asset requests aren't needlessly gated) -- this route re-checks
 * auth explicitly instead of trusting the middleware to have run first.
 *
 * Repo-controlled content is treated as untrusted: only .png/.jpg/.jpeg ever leave this route
 * (never SVG/HTML/XML/JSON, which the browser would happily execute/parse as same-origin content
 * -- stored XSS against the NextAuth session otherwise), with nosniff + a locked-down CSP +
 * sandboxed Content-Disposition on every response.
 *
 * `ref` (required) is the git ref to read from -- the caller's own session's work_branch, since
 * screenshots/reports now live on a per-session branch rather than one repo-shared default.
 */
export async function GET(request: Request) {
  const token = await getServerAuthToken();
  const accessToken = githubAccessToken(token);
  if (!accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: HARDENED_HEADERS });
  }

  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  const path = searchParams.get("path");
  const ref = searchParams.get("ref");

  if (!owner || !repo || !path || !ref) {
    return notFound();
  }
  if (path.startsWith("/") || path.includes("..") || path.includes("\\") || !path.startsWith(".ai-dev-workflow/")) {
    return notFound();
  }

  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const contentType = CONTENT_TYPE_BY_EXT[ext];
  if (!contentType) {
    return notFound();
  }

  const octokit = await getOctokit();
  let bytes: Buffer;
  try {
    const content = await octokit.rest.repos.getContent({ owner, repo, path, ref });
    if (Array.isArray(content.data) || content.data.type !== "file") {
      // Symlink/dir/submodule -- never proxied.
      return notFound();
    }
    if (content.data.content) {
      bytes = Buffer.from(content.data.content, "base64");
    } else {
      // getContent omits `content` for files >1MB. Re-request as raw bytes rather than following
      // `download_url` -- that's an unauthenticated, time-limited redirect that would leak the
      // blob to anyone who captured the URL; `mediaType: "raw"` stays on the authenticated call.
      const raw = await octokit.rest.repos.getContent({
        owner, repo, path, ref,
        mediaType: { format: "raw" },
      });
      bytes = Buffer.from(raw.data as unknown as ArrayBuffer);
    }
  } catch (error) {
    if ((error as { status?: number }).status === 404) return notFound();
    throw error;
  }

  const basename = (path.split("/").pop() || "file").replace(/[^A-Za-z0-9._-]/g, "_");
  // Buffer really is a valid fetch BodyInit at runtime (Node/undici accept it directly) -- the
  // cast works around Buffer's `ArrayBufferLike` (SharedArrayBuffer-inclusive) type not
  // structurally matching the DOM lib's stricter `ArrayBuffer`-only BodyInit under this repo's
  // TS/lib versions.
  return new NextResponse(bytes as unknown as BodyInit, {
    status: 200,
    headers: {
      "Content-Type": contentType,
      "Content-Disposition": `inline; filename="${basename}"`,
      ...HARDENED_HEADERS,
    },
  });
}
