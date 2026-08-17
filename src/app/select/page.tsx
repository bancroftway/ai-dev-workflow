"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { RepoSummary } from "@/app/api/github/repos/route";
import type { BranchSummary } from "@/app/api/github/branches/route";
import { SessionHistory } from "@/components/SessionHistory";

type OnboardedStatus = "checking" | "onboarded" | "not-onboarded" | "error";

export default function SelectPage() {
  const [repos, setRepos] = useState<RepoSummary[] | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);
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
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load repositories (${res.status})`);
        return res.json();
      })
      .then((data: { repos: RepoSummary[] }) => setRepos(data.repos))
      .catch((err: Error) => setReposError(err.message));
  }, []);

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-6 p-6">
      <div>
        <h1 className="text-lg font-semibold">Select a repository</h1>
        <p className="text-sm text-neutral-500">
          Choose the GitHub repository and branch to work in.
        </p>
      </div>

      {reposError && <p className="text-sm text-red-600">{reposError}</p>}

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-neutral-700">Repository</span>
        <input
          type="text"
          className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
          placeholder="Filter repositories…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          disabled={!repos}
        />
        <select
          className="mt-1 rounded-md border border-neutral-300 px-3 py-2 text-sm"
          value={selectedFullName}
          onChange={(event) => setSelectedFullName(event.target.value)}
          disabled={!repos}
          size={Math.min(filteredRepos?.length ?? 1, 8) || 1}
        >
          {!repos && <option disabled>Loading repositories…</option>}
          {repos && filteredRepos?.length === 0 && <option disabled>No matches</option>}
          {filteredRepos?.map((r) => (
            <option key={r.fullName} value={r.fullName}>
              {r.fullName}
              {r.private ? " (private)" : ""}
            </option>
          ))}
        </select>
      </label>

      {/* Keyed by repo so switching repositories always starts this section's state fresh,
          rather than manually resetting branch/onboarding state via an effect. */}
      {selectedRepo && <RepoBranchSection key={selectedRepo.fullName} repo={selectedRepo} />}
    </div>
  );
}

function RepoBranchSection({ repo }: { repo: RepoSummary }) {
  const router = useRouter();
  const [branches, setBranches] = useState<BranchSummary[] | null>(null);
  const [branchesError, setBranchesError] = useState<string | null>(null);
  const [selectedBranch, setSelectedBranch] = useState<string>("");

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

  function startNewSession() {
    if (!selectedBranch) return;
    const sessionId = crypto.randomUUID();
    router.push(`/workflow/${repo.owner}/${repo.repo}/${sessionId}/${selectedBranch}`);
  }

  return (
    <>
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
        <OnboardingStatusSection key={selectedBranch} owner={repo.owner} repo={repo.repo} branch={selectedBranch}>
          {(status) => (
            <button
              type="button"
              className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
              onClick={startNewSession}
            >
              {status === "onboarded" ? "Start new session" : "Onboard & start new session"}
            </button>
          )}
        </OnboardingStatusSection>
      )}

      {/* Sessions are branch-scoped now (each gets its own ai-dev-workflow/<session_id> branch),
          so the list is too -- shown once both repo and branch are chosen, keyed the same way. */}
      {selectedBranch && (
        <SessionHistory key={selectedBranch} owner={repo.owner} repo={repo.repo} sourceBranch={selectedBranch} />
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
