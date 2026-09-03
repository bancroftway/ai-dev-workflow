"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type { ProjectSummary } from "@/app/api/projects/route";
import { fetchProject } from "@/lib/agent-client";
import { SettingsBanner } from "@/components/SettingsBanner";
import { inProgressLabel, STATUS_BADGE } from "@/components/SessionHistory";
import { RunningSpinner } from "@/components/Spinner";
import { STAGE_KEYS_IN_ORDER, type Session } from "@/lib/session-types";
import { providerLabel, useOrgProvider } from "@/lib/use-org-provider";

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

type ProjectState =
  | { kind: "loading" }
  | { kind: "not-found" }
  | { kind: "error"; detail: string }
  | { kind: "ready"; project: ProjectSummary };

/** Which column a session/ticket card belongs in: the one terminal bucket for a finished run,
 * else wherever the ticket is actually being worked right now -- failed/rejected sessions are NOT
 * split into a separate pipeline column (that would be inventing a 9th "stage" that doesn't
 * correspond to any real StageSpec, the exact mistake the plan's own wireframe legend made). They
 * stay in the real stage column they died in, with the status badge below doing the "this one
 * didn't make it" visible treatment instead.
 *
 * Phase E audit finding 2 (off-by-one): `current_stage` is NOT "the stage this ticket is in" --
 * it's the LAST APPROVED stage. Its single writer, `session_store.update_current_stage`, is only
 * ever called post-approval (agent/src/graph.py's `_run_post_approve_hook`), and that writer's own
 * docstring says so verbatim (session_store.py): "current_stage on its own only ever advances
 * post-approval, so it cannot distinguish 'still drafting stage X' from 'paused at stage X's
 * gate'." So a ticket drafting `plan`, or paused at `plan`'s gate awaiting approval, both still
 * report `current_stage: "specification"` -- one stage behind reality either way.
 *
 * Coordinator ruling: fix HERE, at the display layer, rather than changing current_stage's backend
 * semantics -- those are documented as deliberate elsewhere and resume/persistence code may depend
 * on them. A non-completed card's column is therefore the stage AFTER current_stage in
 * STAGE_KEYS_IN_ORDER, clamped at the last real stage (an all-8-approved ticket that hasn't
 * flipped to status "completed" yet has nowhere further to go but Done, and Done is reserved for
 * that status specifically -- see the branch above). `current_stage === null` means "before the
 * first stage's own approval" (still drafting tech-stack, or never got that far) and keeps landing
 * in the first column, same result "shift index -1 by one" would already give. */
function columnFor(session: Session): string {
  if (session.status === "completed") return DONE_COLUMN;
  const stage = session.current_stage;
  if (stage == null) return STAGE_KEYS_IN_ORDER[0];
  const index = (STAGE_KEYS_IN_ORDER as readonly string[]).indexOf(stage);
  // Defensive (Task 10 sweep item #14): a non-null value that isn't one of the 8 real keys can't
  // be shifted by one -- same first-column fallback as the null case above. Without this, such a
  // session would land in a `grouped` bucket the render loop below never iterates (it only maps
  // over the fixed `columns` list), silently vanishing from the board instead of degrading
  // gracefully the way SessionHistory.tsx's own ProgressIndicator already does for this same case.
  if (index === -1) return STAGE_KEYS_IN_ORDER[0];
  return STAGE_KEYS_IN_ORDER[Math.min(index + 1, STAGE_KEYS_IN_ORDER.length - 1)];
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
  // Minor 10: "shown once in the board header, not repeated per card" -- SessionCard renders no
  // provider string of its own (finding B.2, still correct), this is the one positive affordance
  // for the whole board.
  const provider = useOrgProvider();

  // Reset both to a loading/empty state the moment projectId itself changes (Task 10 sweep item
  // #13): without this, direct board-to-board navigation (browser back/forward, a typed URL) kept
  // rendering the PREVIOUS project's already-`ready` state and session cards until the effects
  // below re-fetched. Deliberately done HERE, during render, not with a synchronous setState
  // inside the effects below -- that shape is exactly what react-hooks/set-state-in-effect (an
  // enforced lint error in this repo) flags as a cascading-render risk. This is the sanctioned
  // "adjusting state when a prop changes" pattern instead: React re-renders once, synchronously,
  // before committing, rather than committing stale state and scheduling a second render via an
  // effect.
  const [resetForProjectId, setResetForProjectId] = useState<string | null>(null);
  if (projectId !== resetForProjectId) {
    setResetForProjectId(projectId);
    setProjectState({ kind: "loading" });
    setSessions(null);
  }

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
    // sessions is already reset to null for the new projectId by the render-phase block above
    // (Task 10 sweep item #13) before this effect ever runs -- nothing to clear here.
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
        {provider && (
          <p className="text-xs text-neutral-400">
            <span aria-hidden>ⓘ</span> Runs on {providerLabel(provider)}
          </p>
        )}
      </div>

      {/* I-2(a), whole-branch review: the Spec named the board explicitly as a persistent-banner
          site (tickets live here) -- previously the only two mounts were New Ticket and /select.
          No owner/repo (matches tickets/new's own bare mount): those only feed the unrelated
          per-repo key-vault check, out of this finding's scope. */}
      <SettingsBanner />

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
  // Done cards go to the existing read-only report route instead of the workflow route -- fix
  // round 1 (review finding): the workflow route's SandboxSessionBoot unconditionally POSTs
  // /sessions/provision on mount, no ?resume=1 needed to trigger it. provision_session's own 409
  // guard against resuming a completed session only fires `if body.resume`, so a plain card link
  // sails past it into `provider.provision(...)`, which only short-circuits cheaply while this
  // exact agent process still has the container warm in its in-memory registry -- gone after a
  // restart/manual stop/enough time, a "Done" click would silently re-clone a work branch that
  // may not even exist anymore post-merge. `/sessions/{owner}/{repo}/{sessionId}/{runId}/report`
  // is this codebase's own existing target for exactly this case (SessionHistory.tsx's "View
  // report" button, same route/param shape copied verbatim) -- no side effects, matches what
  // "Done" actually implies. Every other status is unaffected: still the workflow route,
  // unchanged, still deliberately without ?resume=1 (same reasoning, now only relevant to them).
  // Workflow Liveness Fix: an interrupted in_progress session's process is dead -- a plain
  // reattach reconnects to nothing, same reasoning as SessionHistory's Open-vs-Resume split.
  // ?resume=1 is the same query param AppShell already reads to fire a blank runAgent() on mount.
  const workflowHref = `/workflow/${owner}/${repo}/${session.session_id}/${session.source_branch}${
    session.status === "in_progress" && session.interrupted ? "?resume=1" : ""
  }`;
  const href = session.status === "completed"
    ? `/sessions/${owner}/${repo}/${session.session_id}/${session.run_id}/report`
    : workflowHref;
  return (
    <Link
      href={href}
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
      <span className={`flex items-center gap-1.5 self-start rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_BADGE[session.status]}`}>
        {session.status === "in_progress" && session.run_active && <RunningSpinner className="h-3 w-3" />}
        {session.status === "in_progress" ? inProgressLabel(session) : session.status.replace("_", " ")}
      </span>
      {(session.status === "failed" || session.status === "rejected") && session.failure_message && (
        <p className="truncate text-xs text-red-700" title={session.failure_message}>
          {session.failure_message}
        </p>
      )}
    </Link>
  );
}
