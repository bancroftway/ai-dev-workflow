"use client";

import { useSession, signOut } from "next-auth/react";

export function WorkspaceHeader() {
  const { data: session } = useSession();
  return (
    <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2 text-sm">
      <span className="font-medium text-neutral-700">AI-Assisted Specification &amp; Planning</span>
      {session?.user && (
        <div className="flex items-center gap-3">
          <span className="text-neutral-600">{session.user.name ?? session.user.email}</span>
          <button
            onClick={() => signOut({ redirectTo: "/" })}
            className="rounded-md px-2 py-1 text-neutral-500 hover:bg-neutral-100"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
