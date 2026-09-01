import { NextResponse } from "next/server";
import { auth } from "@/auth";

const PUBLIC_PATHS = new Set(["/"]);

// Duplicated (not imported from lib/e2e.ts) on purpose: this runs in the edge middleware bundle,
// which should stay dependency-free. Same conjunctive guard -- E2E mode can never be production.
const E2E_MODE = process.env.AIDW_E2E_MODE === "1" && process.env.NODE_ENV !== "production";

export default auth((req) => {
  if (E2E_MODE) return;
  const { pathname } = req.nextUrl;
  if (PUBLIC_PATHS.has(pathname) || pathname.startsWith("/api/auth")) return;

  if (!req.auth) {
    if (pathname.startsWith("/api/")) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    return NextResponse.redirect(new URL("/", req.nextUrl.origin));
  }
});

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)"],
};
