"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type { ProjectListResponse, ProjectSummary } from "@/app/api/projects/route";
import { STAGE_KEYS_IN_ORDER, type Session } from "@/lib/session-types";

// Ruling 5 (this Part's own plan): plain polling, no CopilotKit/AG-UI live subscription.
const POLL_INTERVAL_MS = 15_000;

// Sentinel column key for the one terminal bucket the brief asks for on top of the 8 real
// StageSpec keys -- deliberately not a real stage key (STAGE_KEYS_IN_ORDER stays exactly what
// graph.py's STAGES declares, nothing invented added to it).
const DONE_COLUMN = "__done__";

// Cosmetic display labels only -- the column SET is still exactly STAGE_KEYS_IN_ORDER (graph.py's
// real 8 StageSpec keys). This is not the wireframe's TS/SP/PL/GF/BD/RM/AR/RR/DN legend (that one
// folds metrics-exit into Adversarial Review and adds two non-pipeline columns that don't
// correspond to any real StageSpec) -- just a readable title for each of the same 8 real keys.
const STAGE_LABELS: Record<(typeof STAGE_KEYS_IN_ORDER)[number], string> = {
  "tech-stack": "Tech Stack",
  specification: "Specification",
  plan: "Plan",
  "ac-to-tests": "AC → Tests",
  "minimal-code-to-green": "Minimal Code to Green",
  remediation: "Remediation",
  "adversarial-compliance": "Adversarial Compliance",
  "metrics-exit": "Metrics Exit",
};

// Same 4-status palette the workflow page's own SessionHistory-adjacent progress UI uses
// (src/app/workflow/.../page.tsx's STATUS_BADGE) -- duplicated rather than imported so this page
// doesn't reach into another route's page module for a 4-line constant.
const STATUS_BADGE: Record<Session["status"], string> = {
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  rejected: "bg-amber-100 text-amber-800",
  in_progress: "bg-blue-100 text-blue-800",
};

type ProjectState =
  | { kind: "loading" }
  | { kind: "not-found" }
  | { kind: "error"; detail: string }
  | { kind: "ready"; project: ProjectSummary };

/** Which column a session/ticket card belongs in: the one terminal bucket for a finished run,
 * else wherever `current_stage` says it is right now -- failed/rejected sessions are NOT split
 * into a separate pipeline column (that would be inventing a 9th "stage" that doesn't correspond
 * to any real StageSpec, the exact mistake the plan's own wireframe legend made). They stay in the
 * real stage column they died in, with the status badge below doing the "this one didn't make it"
 * visible treatment instead. `current_stage` is only ever null before a session's first stage
 * approval (still drafting tech-stack, or it never got that far) -- the one real position that
 * gap can actually mean, so it falls into the first column rather than a fabricated "unstarted"
 * bucket. */
function columnFor(session: Session): string {
  if (session.status === "completed") return DONE_COLUMN;
  return session.current_stage ?? STAGE_KEYS_IN_ORDER[0];
}

/** Re-reads the project list and finds this one -- no `GET /api/projects/:id` exists (same
 * technique src/app/(boxed)/tickets/new/page.tsx's own fetchProject already uses). */
async function fetchProject(projectId: string): Promise<ProjectSummary | null> {
  const res = await fetch("/api/projects");
  if (!res.ok) throw new Error(`Failed to load projects (${res.status})`);
  const data = (await res.json()) as ProjectListResponse;
  return data.projects.find((p) => p.project_id === projectId) ?? null;
}

/**
 * Project-scoped Board (Part 3 Task 9): one card per session/ticket, grouped by pipeline stage.
 * Route is `/projects/[projectId]/board` (the plan's own suggested shape) rather than a `/board`
 * page with an in-page project picker -- this codebase already scopes every other per-resource
 * page this way (`/settings/[owner]/[repo]`, `/workflow/[owner]/[repo]/[sessionId]/[...branch]`),
 * so a bookmarkable, shareable URL per project fits the existing convention better than inventing
 * a second "pick a thing" UI alongside /select's repo picker.
 */
export default function ProjectBoardPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;

  const [projectState, setProjectState] = useState<ProjectState>({ kind: "loading" });
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [sessionsError, setSessionsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProject(projectId)
      .then((project) => {
        if (cancelled) return;
        setProjectState(project ? { kind: "ready", project } : { kind: "not-found" });
      })
      .catch((err: Error) => {
        if (!cancelled) setProjectState({ kind: "error", detail: err.message });
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const owner = projectState.kind === "ready" ? projectState.project.owner : null;
  const repo = projectState.kind === "ready" ? projectState.project.repo : null;

  useEffect(() => {
    // No repo yet (a brand-new "+ New Project" that hasn't taken its first ticket) -- provisioning
    // a ticket is exactly what scaffolds owner/repo, so nothing could possibly have a session row
    // yet, and GET /sessions requires owner/repo. Nothing to poll; `effectiveSessions` below
    // renders this as an empty board directly from `owner`/`repo` being null, no state update
    // needed here (setState synchronously in an effect body just to represent this is exactly the
    // cascading-render pattern react-hooks/set-state-in-effect flags).
    if (!owner || !repo) return;
    // Rebind narrowed to plain `string` -- TS doesn't carry the `!owner || !repo` narrowing above
    // into `load`'s closure (a nested function capturing outer `const`s), so `owner`/`repo` still
    // type as `string | null` inside its body without this.
    const ownerId = owner;
    const repoId = repo;
    let cancelled = false;
    function load() {
      const query = new URLSearchParams({ owner: ownerId, repo: repoId, project_id: projectId });
      fetch(`/api/sessions/list?${query}`)
        .then((res) => {
          if (!res.ok) throw new Error(`Failed to load sessions (${res.status})`);
          return res.json();
        })
        .then((data: { sessions: Session[] }) => {
          if (!cancelled) setSessions(data.sessions);
        })
        .catch((err: Error) => {
          if (!cancelled) setSessionsError(err.message);
        });
    }
    load();
    // Fixed-interval polling + refetch-on-focus (Ruling 5) -- deliberately no CopilotKit/AG-UI
    // subscription; that question is Part 2's own, explicitly deferred elsewhere.
    const interval = setInterval(load, POLL_INTERVAL_MS);
    window.addEventListener("focus", load);
    return () => {
      cancelled = true;
      clearInterval(interval);
      window.removeEventListener("focus", load);
    };
  }, [owner, repo, projectId]);

  if (projectState.kind === "loading") {
    return <p className="p-6 text-sm text-neutral-500">Loading project…</p>;
  }
  if (projectState.kind === "not-found") {
    return (
      <div className="flex flex-col gap-2 p-6">
        <p className="text-sm text-red-600">Project not found.</p>
        <Link href="/select" className="text-sm text-neutral-500 hover:text-neutral-800">
          ← Back to repositories
        </Link>
      </div>
    );
  }
  if (projectState.kind === "error") {
    return <p className="p-6 text-sm text-red-600">{projectState.detail}</p>;
  }

  const project = projectState.project;
  // No repo yet -> definitionally empty (see the effect above); otherwise whatever the last
  // successful poll returned, or still-loading (null) on the very first render.
  const effectiveSessions: Session[] | null = !owner || !repo ? [] : sessions;
  const columns: string[] = [...STAGE_KEYS_IN_ORDER, DONE_COLUMN];
  const grouped: Record<string, Session[]> = Object.fromEntries(columns.map((key) => [key, []]));
  for (const session of effectiveSessions ?? []) {
    (grouped[columnFor(session)] ??= []).push(session);
  }

  return (
    <div className="flex h-full w-full flex-col gap-4 p-6">
      <div>
        <Link href="/select" className="text-sm text-neutral-500 hover:text-neutral-800">
          ← Back to repositories
        </Link>
        <h1 className="mt-2 text-lg font-semibold">{project.name} — Board</h1>
        <p className="text-sm text-neutral-500">
          {project.owner && project.repo ? `${project.owner}/${project.repo}` : "Repository not yet created"}
        </p>
      </div>

      {sessionsError && <p className="text-sm text-red-600">{sessionsError}</p>}
      {!owner || !repo ? (
        <p className="text-sm text-neutral-500">
          This project has no repository yet.{" "}
          <Link href="/tickets/new" className="text-neutral-700 underline hover:text-neutral-900">
            File a ticket
          </Link>{" "}
          against it to scaffold one.
        </p>
      ) : (
        sessions === null && !sessionsError && <p className="text-sm text-neutral-500">Loading tickets…</p>
      )}

      <div className="flex min-h-0 flex-1 gap-4 overflow-x-auto">
        {columns.map((key) => (
          <div key={key} className="flex w-64 shrink-0 flex-col gap-2 rounded-lg bg-neutral-50 p-3">
            <h2 className="flex items-center justify-between text-sm font-semibold text-neutral-700">
              <span>{key === DONE_COLUMN ? "Done" : STAGE_LABELS[key as keyof typeof STAGE_LABELS]}</span>
              <span className="text-xs font-normal text-neutral-400">{grouped[key].length}</span>
            </h2>
            <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
              {grouped[key].length === 0 && <p className="text-xs text-neutral-400">No tickets</p>}
              {grouped[key].map((session) => (
                <SessionCard key={session.session_id} session={session} owner={owner ?? ""} repo={repo ?? ""} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SessionCard({ session, owner, repo }: { session: Session; owner: string; repo: string }) {
  return (
    // Links to the existing workflow page, unchanged -- no ?resume=1: that flag makes
    // provision_session 409 on an already-completed session (agent/src/sessions_api.py), and this
    // one link has to work for every column including Done, so it deliberately never sends it
    // (plain reload semantics, same as landing on a bookmarked workflow URL).
    <Link
      href={`/workflow/${owner}/${repo}/${session.session_id}/${session.source_branch}`}
      className="flex flex-col gap-1.5 rounded-md border border-neutral-200 bg-white px-3 py-2 text-sm shadow-sm hover:border-neutral-400"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="truncate font-medium text-neutral-900">{session.title}</span>
        {session.awaiting_gate && (
          <span title="Awaiting your approval" aria-label="Awaiting your approval">
            ⏸
          </span>
        )}
      </div>
      <span className={`self-start rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_BADGE[session.status]}`}>
        {session.status.replace("_", " ")}
      </span>
      {(session.status === "failed" || session.status === "rejected") && session.failure_message && (
        <p className="truncate text-xs text-red-700" title={session.failure_message}>
          {session.failure_message}
        </p>
      )}
    </Link>
  );
}
