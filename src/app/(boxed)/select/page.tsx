"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { RepoSummary } from "@/app/api/github/repos/route";
import type { BranchSummary } from "@/app/api/github/branches/route";
import { SessionHistory } from "@/components/SessionHistory";
import { SettingsBanner } from "@/components/SettingsBanner";

type OnboardedStatus = "checking" | "onboarded" | "not-onboarded" | "error";

/** Get-or-create a project row for (owner, repo) via the Connect-Repository route -- shared by
 * RepoBranchSection's "Connect repository" button AND its "start new session" action (Task 5):
 * provisioning a session now requires a real project_id (agent/src/sessions_api.py's
 * ProvisionRequest), and this is this task's own single resolve-a-project step, same call either
 * way. Throws with the server's own detail message on failure. */
async function connectProject(owner: string, repo: string): Promise<string> {
  const res = await fetch("/api/projects/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo }),
  });
  const body = (await res.json().catch(() => null)) as { project_id?: string; detail?: string } | null;
  if (!res.ok || !body?.project_id) {
    throw new Error(body?.detail ?? `couldn't connect repository (${res.status})`);
  }
  return body.project_id;
}

export default function SelectPage() {
  const [repos, setRepos] = useState<RepoSummary[] | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);
  // Distinct from reposError: "not connected yet" is expected, quiet state (the settings banner
  // already prompts the fix) -- a red error box would just be a second, redundant alarm.
  const [githubNotConnected, setGithubNotConnected] = useState(false);
  const [selectedFullName, setSelectedFullName] = useState<string>("");
  const [filter, setFilter] = useState("");

  const selectedRepo = useMemo(
    () => repos?.find((r) => r.fullName === selectedFullName) ?? null,
    [repos, selectedFullName],
  );

  // Already sorted updated-desc by the API's own octokit query -- filter only, no re-sort needed.
  const filteredRepos = useMemo(() => {
    if (!repos) return repos;
    const q = filter.trim().toLowerCase();
    return q ? repos.filter((r) => r.fullName.toLowerCase().includes(q)) : repos;
  }, [repos, filter]);

  useEffect(() => {
    fetch("/api/github/repos")
      .then(async (res) => {
        if (res.status === 401) {
          const body = (await res.json().catch(() => null)) as { error?: string } | null;
          if (body?.error === "github_not_connected") {
            setGithubNotConnected(true);
            return null;
          }
        }
        if (!res.ok) throw new Error(`Failed to load repositories (${res.status})`);
        return res.json();
      })
      .then((data: { repos: RepoSummary[] } | null) => {
        if (data) setRepos(data.repos);
      })
      .catch((err: Error) => setReposError(err.message));
  }, []);

  return (
    <div className="flex h-full w-full flex-col gap-4 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Select a repository</h1>
          <p className="text-sm text-neutral-500">
            Choose the GitHub repository and branch to work in.
          </p>
        </div>
        {/* Part 3's project/ticket intake path (no existing page to extend it from) -- kept
            reachable from here since this is where "start new session" has always lived. */}
        <Link
          href="/tickets/new"
          className="shrink-0 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-800"
        >
          + New Ticket
        </Link>
      </div>

      {/* Missing-settings banners: session-scoped always; the key-vault check joins once a repo
          is selected. */}
      <SettingsBanner owner={selectedRepo?.owner} repo={selectedRepo?.repo} />

      {reposError && <p className="text-sm text-red-600">{reposError}</p>}

      {/* Two independently-scrolling panels -- the repo list and the branch/session list were
          fighting each other for a single shared scroll region before, cutting sessions off once
          the repo list grew past a fixed height. */}
      <div className="flex min-h-0 flex-1 gap-6">
        <div className="flex w-[380px] shrink-0 flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Repository</span>
          {githubNotConnected ? (
            <p className="text-sm text-neutral-500">Connect your GitHub account above to browse repositories.</p>
          ) : (
            <>
              <input
                type="text"
                className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
                placeholder="Filter repositories…"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                disabled={!repos}
              />
              <div className="mt-1 min-h-0 flex-1 overflow-y-auto rounded-md border border-neutral-300">
                {!repos && <p className="px-3 py-2 text-sm text-neutral-500">Loading repositories…</p>}
                {repos && filteredRepos?.length === 0 && (
                  <p className="px-3 py-2 text-sm text-neutral-500">No matches</p>
                )}
                {filteredRepos?.map((r) => (
                  <RepoRow
                    key={r.fullName}
                    repo={r}
                    selected={r.fullName === selectedFullName}
                    onSelect={() => setSelectedFullName(r.fullName)}
                  />
                ))}
              </div>
            </>
          )}
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-4 overflow-y-auto border-l border-neutral-200 pl-6">
          {/* Keyed by repo so switching repositories always starts this section's state fresh,
              rather than manually resetting branch/onboarding state via an effect. */}
          {selectedRepo ? (
            <RepoBranchSection key={selectedRepo.fullName} repo={selectedRepo} />
          ) : (
            <p className="text-sm text-neutral-500">Select a repository to see its branches and sessions.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function RepoRow({
  repo,
  selected,
  onSelect,
}: {
  repo: RepoSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <div
      className={`flex w-full cursor-pointer items-center gap-3 border-b border-neutral-100 px-3 py-2 text-left last:border-b-0 ${
        selected ? "bg-neutral-900 text-white" : "hover:bg-neutral-50"
      }`}
      onClick={onSelect}
      role="option"
      aria-selected={selected}
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{repo.fullName}</span>
        <span className={`text-xs ${selected ? "text-neutral-300" : "text-neutral-500"}`}>
          {repo.private ? "private" : "public"}
          {repo.updatedAt ? ` · updated ${timeAgo(repo.updatedAt)}` : ""}
        </span>
      </div>
      <Link
        href={`/settings/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.repo)}`}
        onClick={(event) => event.stopPropagation()}
        className={`shrink-0 rounded p-1 ${
          selected ? "text-neutral-300 hover:bg-neutral-700" : "text-neutral-400 hover:bg-neutral-200"
        }`}
        title={`Settings for ${repo.fullName}`}
        aria-label={`Settings for ${repo.fullName}`}
      >
        <GearIcon className="h-4 w-4" />
      </Link>
    </div>
  );
}

function GearIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" className={className} fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M7.84 1.804A1 1 0 0 1 8.82 1h2.36a1 1 0 0 1 .98.804l.331 1.652a6.993 6.993 0 0 1 1.929 1.115l1.598-.54a1 1 0 0 1 1.186.447l1.18 2.044a1 1 0 0 1-.205 1.251l-1.267 1.113a7.047 7.047 0 0 1 0 2.228l1.267 1.113a1 1 0 0 1 .206 1.25l-1.18 2.045a1 1 0 0 1-1.187.447l-1.598-.54a6.993 6.993 0 0 1-1.929 1.115l-.33 1.652a1 1 0 0 1-.98.804H8.82a1 1 0 0 1-.98-.804l-.331-1.652a6.993 6.993 0 0 1-1.929-1.115l-1.598.54a1 1 0 0 1-1.186-.447l-1.18-2.044a1 1 0 0 1 .205-1.251l1.267-1.114a7.05 7.05 0 0 1 0-2.227L1.821 7.773a1 1 0 0 1-.206-1.25l1.18-2.045a1 1 0 0 1 1.187-.447l1.598.54A6.992 6.992 0 0 1 7.51 3.456l.33-1.652ZM10 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function RepoBranchSection({ repo }: { repo: RepoSummary }) {
  const router = useRouter();
  const [branches, setBranches] = useState<BranchSummary[] | null>(null);
  const [branchesError, setBranchesError] = useState<string | null>(null);
  const [selectedBranch, setSelectedBranch] = useState<string>("");
  // Shared between "Connect repository" and "start new session" -- both resolve a project via
  // the same connectProject() call, so one error/busy slot covers either.
  const [actionError, setActionError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    fetch(`/api/github/branches?owner=${repo.owner}&repo=${repo.repo}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load branches (${res.status})`);
        return res.json();
      })
      .then((data: { branches: BranchSummary[] }) => {
        setBranches(data.branches);
        setSelectedBranch(repo.defaultBranch);
      })
      .catch((err: Error) => setBranchesError(err.message));
  }, [repo]);

  async function startNewSession() {
    if (!selectedBranch) return;
    setActionError(null);
    setStarting(true);
    try {
      // Task 2 made project_id required on session provisioning; this repo/branch flow had no
      // notion of a project until now, which is exactly what left it broken (task-5-brief.md).
      // Resolving one here -- the same connect-or-find-existing-project step Connect Repository
      // itself uses -- is this flow's fix rather than a second, parallel path to keep in sync with
      // the New Ticket form. Carried into the already-existing SandboxSessionBoot provisioning
      // call via ?projectId= (same mechanism ?resume=1 already uses below/in SessionHistory).
      const projectId = await connectProject(repo.owner, repo.repo);
      const sessionId = crypto.randomUUID();
      router.push(`/workflow/${repo.owner}/${repo.repo}/${sessionId}/${selectedBranch}?projectId=${projectId}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setStarting(false);
    }
  }

  async function connectRepository() {
    setActionError(null);
    setConnecting(true);
    try {
      await connectProject(repo.owner, repo.repo);
      // ponytail: no ?project= preselect on the New Ticket form -- the just-connected project
      // sorts newest-first in its picker, so it's already on top. Add a preselect if that's ever
      // not enough (e.g. reconnecting an old project buried in the list).
      router.push("/tickets/new");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setConnecting(false);
    }
  }

  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-neutral-500">
          Connecting registers this repo as a project so tickets can be filed against it --
          no session starts until the first ticket does.
        </p>
        <button
          type="button"
          className="shrink-0 self-start rounded-lg border border-neutral-300 px-4 py-2 text-sm font-medium text-neutral-700 hover:bg-neutral-50 disabled:opacity-40"
          onClick={connectRepository}
          disabled={connecting}
        >
          {connecting ? "Connecting…" : "Connect repository"}
        </button>
      </div>

      {actionError && <p className="text-sm text-red-600">{actionError}</p>}

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-neutral-700">Branch</span>
        {branchesError && <p className="text-sm text-red-600">{branchesError}</p>}
        <select
          className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
          value={selectedBranch}
          onChange={(event) => setSelectedBranch(event.target.value)}
          disabled={!branches}
        >
          <option value="" disabled>
            {branches ? "Choose a branch…" : "Loading branches…"}
          </option>
          {branches?.map((b) => (
            <option key={b.name} value={b.name}>
              {b.name}
            </option>
          ))}
        </select>
      </label>

      {/* Keyed by branch so switching branches re-checks onboarding status and re-fetches the
          session list from scratch. */}
      {selectedBranch && (
        <OnboardingStatusSection key={`onboarding-${selectedBranch}`} owner={repo.owner} repo={repo.repo} branch={selectedBranch}>
          {(status) => (
            <button
              type="button"
              className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
              onClick={startNewSession}
              disabled={starting}
            >
              {starting ? "Starting…" : status === "onboarded" ? "Start new session" : "Onboard & start new session"}
            </button>
          )}
        </OnboardingStatusSection>
      )}

      {/* Sessions are branch-scoped now (each gets its own ai-dev-workflow/<session_id> branch),
          so the list is too -- shown once both repo and branch are chosen, keyed the same way. */}
      {selectedBranch && (
        <SessionHistory key={`history-${selectedBranch}`} owner={repo.owner} repo={repo.repo} sourceBranch={selectedBranch} />
      )}
    </>
  );
}

function OnboardingStatusSection({
  owner,
  repo,
  branch,
  children,
}: {
  owner: string;
  repo: string;
  branch: string;
  children: (status: OnboardedStatus) => ReactNode;
}) {
  const [status, setStatus] = useState<OnboardedStatus>("checking");

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({ owner, repo, branch });
    fetch(`/api/github/onboarding-status?${params}`)
      .then((res) => {
        if (!res.ok) throw new Error(`status ${res.status}`);
        return res.json();
      })
      .then((data: { onboarded: boolean }) => {
        if (!cancelled) setStatus(data.onboarded ? "onboarded" : "not-onboarded");
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [owner, repo, branch]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 text-sm">
        <OnboardedBadge status={status} />
      </div>
      {children(status)}
    </div>
  );
}

function OnboardedBadge({ status }: { status: OnboardedStatus }) {
  switch (status) {
    case "checking":
      return <span className="text-neutral-500">Checking onboarding status…</span>;
    case "onboarded":
      return (
        <span className="rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800">
          Onboarded
        </span>
      );
    case "not-onboarded":
      return (
        <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">
          Not yet onboarded
        </span>
      );
    case "error":
      return <span className="text-red-600">Couldn&apos;t check onboarding status</span>;
  }
}
