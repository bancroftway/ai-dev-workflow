"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { RequirementsLedger } from "@/components/RequirementsLedger";
import { SessionHistory } from "@/components/SessionHistory";

/**
 * Repo-scoped Tickets view (Task: Tickets View, Scope §1) -- the single-repo replacement for the
 * cross-repo lanes / per-project kanban board designs considered earlier. Entered from `/select`'s
 * "Open Tickets" button (Scope §2). Not project-scoped: owner/repo alone identify the repo, and
 * project_id is resolved transparently server-side wherever it's actually needed (ticket
 * provisioning), never surfaced here.
 */
export default function RepoTicketsPage() {
  const params = useParams<{ owner: string; repo: string }>();
  const owner = decodeURIComponent(params.owner);
  const repo = decodeURIComponent(params.repo);

  return (
    <div className="flex h-full w-full flex-col gap-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link href="/select" className="text-sm text-neutral-500 hover:text-neutral-800">
            ← Back to repositories
          </Link>
          <h1 className="mt-2 text-lg font-semibold">
            {owner}/{repo}
          </h1>
          <p className="text-sm text-neutral-500">Tickets filed against this repository.</p>
        </div>
        <Link
          href={`/tickets/new?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`}
          className="shrink-0 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-800"
        >
          + New Ticket
        </Link>
      </div>

      <SessionHistory owner={owner} repo={repo} />

      <div className="flex flex-col gap-2 border-t border-neutral-200 pt-4">
        <h2 className="text-sm font-medium text-neutral-700">Requirements</h2>
        <RequirementsLedger owner={owner} repo={repo} />
      </div>
    </div>
  );
}
