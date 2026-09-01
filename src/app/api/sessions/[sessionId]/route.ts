import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { E2E_MODE } from "@/lib/e2e";
import { getAuthorizedSession } from "@/lib/session-access";

/**
 * Single-session lookup. Two callers: the workflow page's ownership check (a session id is a
 * random UUID now, not derived from (owner, repo, user), so landing on someone else's session id
 * is no longer structurally impossible -- see session-access.ts) and the report page's
 * work_branch resolution.
 *
 * Returns 404 (never a distinguishable "exists but you can't see it") for both "no such session"
 * and "session exists but you lack GitHub access to its repo" -- getAuthorizedSession is the one
 * place both checks happen, so this route does no authorization of its own.
 */
export async function GET(_request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  const session = await auth();
  if (!session && !E2E_MODE) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { sessionId } = await params;
  const sessionRow = await getAuthorizedSession(sessionId);
  if (!sessionRow) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  return NextResponse.json(sessionRow);
}
