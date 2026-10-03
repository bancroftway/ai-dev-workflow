"use client";

import { useSession } from "next-auth/react";
import { useEffect, useRef, useState } from "react";

// Any HTTP response at all means the app server is up -- only a network failure (fetch's
// TypeError) means it isn't. The favicon is the cheapest route the server answers, and the
// sign-in proxy skips it.
const PROBE_URL = "/favicon.ico";
const SESSION_PATH = "/api/auth/session";
const UP_PROBE_MS = 30_000;
const DOWN_PROBE_MS = 3_000;

async function serverReachable(realFetch: typeof fetch): Promise<boolean> {
  try {
    await realFetch(PROBE_URL, { method: "HEAD", cache: "no-store" });
    return true;
  } catch {
    return false;
  }
}

function requestUrl(input: RequestInfo | URL): URL | null {
  try {
    return new URL(input instanceof Request ? input.url : String(input), window.location.href);
  } catch {
    return null;
  }
}

/**
 * Shown while the browser can't reach the app server (stopped, restarting, redeploying), instead
 * of the errors that used to be the only sign of it.
 *
 * Detection: any same-origin fetch failing at the network level (TypeError), plus a probe for the
 * quiet case. The fetch wrapper also answers next-auth's session re-read -- fired on every window
 * focus -- with the session this page already holds while the server is unreachable: a rejected
 * read made next-auth log a ClientFetchError (an "Issue" in Next's dev overlay; its logger keeps
 * its own console.error reference, so console can't be filtered) and drop the session to null,
 * reading as signed out until a reload. Every other failed request still rejects as before.
 *
 * Copy is the frontend's own, not server-built: this exists exactly for when there is no server.
 */
export function ServerConnectionBanner() {
  const [down, setDown] = useState(false);
  const { data: session } = useSession();
  const sessionRef = useRef(session);
  const realFetchRef = useRef<typeof fetch | null>(null);

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  useEffect(() => {
    const realFetch = window.fetch;
    realFetchRef.current = realFetch;
    window.fetch = async (input, init) => {
      try {
        return await realFetch(input, init);
      } catch (error) {
        const url = requestUrl(input);
        if (!(error instanceof TypeError) || url?.origin !== window.location.origin) throw error;
        setDown(true);
        if (url.pathname === SESSION_PATH && (init?.method ?? "GET").toUpperCase() === "GET") {
          return Response.json(sessionRef.current ?? null);
        }
        throw error;
      }
    };
    return () => {
      window.fetch = realFetch;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const probe = () => {
      const realFetch = realFetchRef.current;
      if (!realFetch) return;
      void serverReachable(realFetch).then((up) => {
        if (!cancelled) setDown(!up);
      });
    };
    const timer = setInterval(probe, down ? DOWN_PROBE_MS : UP_PROBE_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") probe();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [down]);

  if (!down) return null;
  return (
    <div
      role="status"
      className="fixed inset-x-0 top-0 z-50 border-b border-amber-200 bg-amber-50 px-4 py-2 text-center text-sm text-amber-900"
    >
      Can&apos;t reach the app server right now — it may be restarting. Nothing is lost; this page
      reconnects on its own as soon as the server is back.
    </div>
  );
}
