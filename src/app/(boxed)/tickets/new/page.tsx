"use client";

import { useAttachments } from "@copilotkit/react-core/v2";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AttachmentEditor, SHARED_ATTACHMENTS_CONFIG } from "@/components/AttachmentEditor";
import { SettingsBanner } from "@/components/SettingsBanner";
import { stashHandoffAttachments } from "@/lib/new-ticket-attachment-handoff";
import type { ProjectListResponse, ProjectSummary } from "@/app/api/projects/route";
import type { CannedTechStack, TechStackCatalogResponse } from "@/lib/workflow-types";

const NEW_PROJECT_VALUE = "__new__";
const FREE_TEXT_STACK_VALUE = "__freetext__";

// Fallback only -- correct for a freshly-scaffolded repo (repo_scaffold's initial commit always
// lands on "main") or a pre-migration project row. A connected repo's real default branch (Task 5:
// dbo.projects.default_branch, populated at connect time from GitHub's own API) takes priority
// whenever it's set -- see resolveBranch below.
const FALLBACK_BRANCH = "main";

function resolveBranch(project: ProjectSummary): string {
  return project.default_branch ?? FALLBACK_BRANCH;
}

type SubmitState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; detail: string };

/** Re-reads one project's current row via the list route (no GET /api/projects/:id exists) --
 * used both to reuse an already-created "+ New Project" row on retry and to learn the owner/repo
 * provision_session just scaffolded, since neither is echoed back by the calls that trigger them. */
async function fetchProject(projectId: string): Promise<ProjectSummary | null> {
  const res = await fetch("/api/projects");
  if (!res.ok) throw new Error(`Failed to load projects (${res.status})`);
  const data = (await res.json()) as ProjectListResponse;
  return data.projects.find((p) => p.project_id === projectId) ?? null;
}

/**
 * The single New Ticket intake path (Part 3 Task 4): pick a project (or create one inline) and
 * describe the work, then Assign. Submitting calls the same provisioning flow
 * SandboxSessionBoot.tsx uses for every other session, just invoked directly here (awaited, not
 * deferred to a mounted component) so a "+ New Project" submission can learn the repo it just
 * scaffolded before navigating -- see task-4-report.md for why.
 */
export default function NewTicketPage() {
  const router = useRouter();

  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string>(NEW_PROJECT_VALUE);

  const [newProjectName, setNewProjectName] = useState("");
  const [catalog, setCatalog] = useState<CannedTechStack[] | null>(null);
  const [selectedStackId, setSelectedStackId] = useState<string>(FREE_TEXT_STACK_VALUE);
  const [freeTextStack, setFreeTextStack] = useState("");

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Same shared config RequirementsView.tsx uses -- the New Ticket -> workflow-page handoff
  // (new-ticket-attachment-handoff.ts) re-validates these attachments through that same hook on
  // the other end, which only stays correct if both sides agree on what's acceptable.
  const attachmentsApi = useAttachments({
    config: {
      enabled: true,
      ...SHARED_ATTACHMENTS_CONFIG,
      onUploadFailed: ({ file, message }) => setUploadError(`${file.name}: ${message}`),
    },
  });

  const [submit, setSubmit] = useState<SubmitState>({ kind: "idle" });
  // Set once POST /api/projects succeeds for a "+ New Project" submission -- a retry after a later
  // failure (e.g. provisioning) must reuse this project rather than create a second, duplicate row
  // with the same name (no uniqueness constraint applies until owner/repo are non-null).
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/projects")
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load projects (${res.status})`);
        return res.json();
      })
      .then((data: ProjectListResponse) => setProjects(data.projects))
      .catch((err: Error) => setProjectsError(err.message));
  }, []);

  useEffect(() => {
    // Static, session-independent catalog (agent's load_stack_catalog is @lru_cache'd) -- fine to
    // fetch unconditionally on mount rather than only once "+ New Project" is picked, same
    // "just fetch it, it's cheap" call select/page.tsx makes for repos.
    fetch("/api/tech-stack-catalog")
      .then((res) => (res.ok ? res.json() : { stacks: [] }))
      .then((data: TechStackCatalogResponse) => setCatalog(data.stacks))
      .catch(() => setCatalog([]));
  }, []);

  const isNewProject = selectedProjectId === NEW_PROJECT_VALUE;
  const busy = submit.kind === "submitting";
  const canSubmit =
    !busy && title.trim().length > 0 && (!isNewProject || newProjectName.trim().length > 0);

  async function handleAssign() {
    setSubmit({ kind: "submitting" });
    try {
      let project: ProjectSummary;
      if (isNewProject) {
        if (createdProjectId) {
          // Retrying after a failure past this point (most likely provisioning) -- the project row
          // already exists (and may already carry a scaffolded repo, if that earlier failure
          // happened after scaffolding but before the sandbox came up), so reuse it instead of
          // creating a second row with the same name.
          const existing = await fetchProject(createdProjectId);
          if (!existing) throw new Error("Project was created but can no longer be found — try again");
          project = existing;
        } else {
          const stackText = selectedStackId === FREE_TEXT_STACK_VALUE ? freeTextStack.trim() || null : null;
          const stackId = selectedStackId === FREE_TEXT_STACK_VALUE ? null : selectedStackId;
          const res = await fetch("/api/projects", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: newProjectName.trim(),
              tech_stack_id: stackId,
              tech_stack_text: stackText,
            }),
          });
          const body = (await res.json()) as ProjectSummary & { detail?: string };
          if (!res.ok) throw new Error(body.detail ?? `couldn't create project (${res.status})`);
          project = body;
          setCreatedProjectId(project.project_id);
        }
      } else {
        const found = projects?.find((p) => p.project_id === selectedProjectId);
        if (!found) throw new Error("Select a project");
        project = found;
      }

      // Resolved once, up front, from the project as selected/created -- a "+ New Project" row has
      // no default_branch yet either way (resolveBranch's own "main" fallback), so scaffolding
      // backfilling owner/repo afterward below never changes what this should be.
      const branch = resolveBranch(project);
      const sessionId = crypto.randomUUID();
      const provisionRes = await fetch("/api/sessions/provision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessionId,
          projectId: project.project_id,
          // Placeholders when the project has no repo yet -- provision_session
          // (agent/src/sessions_api.py) ignores owner/repo entirely once it scaffolds a brand-new
          // repo; these just need to be non-empty strings so this BFF route's own required-field
          // check (mirroring the agent's) passes.
          owner: project.owner ?? "pending",
          repo: project.repo ?? "pending",
          branch,
        }),
      });
      const provisionBody = (await provisionRes.json().catch(() => null)) as
        | { error?: string; detail?: string }
        | null;
      if (!provisionRes.ok) {
        throw new Error(
          provisionBody?.error ?? provisionBody?.detail ?? `couldn't provision session (${provisionRes.status})`,
        );
      }

      let owner = project.owner;
      let repo = project.repo;
      if (!owner || !repo) {
        // provision_session backfills dbo.projects with the scaffolded repo BEFORE it returns, but
        // its own response never echoes owner/repo back -- re-reading it is the cheapest way to
        // learn what it picked, no new backend route needed.
        const updated = await fetchProject(project.project_id);
        owner = updated?.owner ?? null;
        repo = updated?.repo ?? null;
      }
      if (!owner || !repo) {
        throw new Error("Project repo was not created — try again");
      }

      // One-shot handoff for RequirementsView.tsx: a brand-new session has no server-side draft
      // yet for its own server-state rehydrate effect to find, so title/description ride along in
      // sessionStorage (same-tab client navigation preserves it) and get consumed there once.
      // Attachments travel separately, in memory (new-ticket-attachment-handoff.ts) rather than
      // through sessionStorage's own much smaller size quota -- consumed last, right before the
      // navigation that's the only thing that can ever collect it on the other end, so an earlier
      // failure in this same try block (still retryable above) never drains the queue the user
      // sees on screen.
      const readyAttachments = attachmentsApi.consumeAttachments();
      if (readyAttachments.length > 0) {
        stashHandoffAttachments(sessionId, readyAttachments);
      }
      sessionStorage.setItem(
        `aidw:new-ticket:${sessionId}`,
        JSON.stringify({ title: title.trim(), description: description.trim() }),
      );
      router.push(`/workflow/${owner}/${repo}/${sessionId}/${branch}`);
    } catch (err) {
      setSubmit({ kind: "error", detail: err instanceof Error ? err.message : String(err) });
    }
  }

  return (
    <div className="flex h-full w-full flex-col gap-6 p-6">
      <div>
        <Link href="/select" className="text-sm text-neutral-500 hover:text-neutral-800">
          ← Back to repositories
        </Link>
        <h1 className="mt-2 text-lg font-semibold">New Ticket</h1>
        <p className="text-sm text-neutral-500">
          File a ticket against a project. A brand-new project scaffolds its own private GitHub
          repo the moment this ticket provisions.
        </p>
      </div>

      <SettingsBanner />

      <section className="flex max-w-2xl flex-col gap-4 rounded-lg border border-neutral-200 p-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Project</span>
          {projectsError && <p className="text-sm text-red-600">{projectsError}</p>}
          <select
            className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
            value={selectedProjectId}
            onChange={(event) => setSelectedProjectId(event.target.value)}
            disabled={!projects || busy}
          >
            <option value={NEW_PROJECT_VALUE}>+ New Project</option>
            {projects?.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.name}
                {p.owner && p.repo ? ` (${p.owner}/${p.repo})` : " (repo not yet created)"}
              </option>
            ))}
          </select>
          {/* Task 9's only entry point into the Board -- nothing else in the app links to it yet. */}
          {!isNewProject && (
            <Link
              href={`/projects/${selectedProjectId}/board`}
              className="self-start text-xs text-neutral-500 underline hover:text-neutral-800"
            >
              View board →
            </Link>
          )}
        </label>

        {isNewProject && (
          <div className="flex flex-col gap-4 rounded-md border border-neutral-200 bg-neutral-50 p-3">
            <label className="flex flex-col gap-1">
              <span className="text-sm font-medium text-neutral-700">Project name</span>
              <input
                type="text"
                className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
                placeholder="e.g. customer-portal"
                value={newProjectName}
                onChange={(event) => setNewProjectName(event.target.value)}
                disabled={busy}
              />
              <span className="text-xs text-neutral-500">
                Also becomes the new GitHub repo&apos;s name, created under your own account.
              </span>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-sm font-medium text-neutral-700">Tech stack</span>
              <select
                className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
                value={selectedStackId}
                onChange={(event) => setSelectedStackId(event.target.value)}
                disabled={!catalog || busy}
              >
                <option value={FREE_TEXT_STACK_VALUE}>Describe it myself</option>
                {catalog?.map((stack) => (
                  <option key={stack.id} value={stack.id}>
                    {stack.title}
                  </option>
                ))}
              </select>
            </label>

            {selectedStackId === FREE_TEXT_STACK_VALUE && (
              <label className="flex flex-col gap-1">
                <span className="text-sm font-medium text-neutral-700">
                  Describe the tech stack (optional)
                </span>
                <textarea
                  className="min-h-[80px] rounded-md border border-neutral-300 px-3 py-2 text-sm"
                  placeholder="e.g. Next.js frontend, FastAPI backend, Postgres"
                  value={freeTextStack}
                  onChange={(event) => setFreeTextStack(event.target.value)}
                  disabled={busy}
                />
              </label>
            )}
          </div>
        )}

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Title</span>
          <input
            type="text"
            className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
            placeholder="Short summary of what this ticket does"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            disabled={busy}
          />
        </label>

        <div className="flex flex-col gap-1">
          {/* A plain <div>, not a <label> wrapping the whole multi-control editor (Task 10 sweep
              item #16): AttachmentEditor nests its own buttons (mode toggle, attach, remove), and
              a <label> wrapping several unrelated interactive elements risks the browser's default
              click-forwards-to-labeled-control behavior firing on the wrong one. The explicit
              htmlFor/id pair below restores the programmatic association without that risk. */}
          <label htmlFor="new-ticket-description" className="text-sm font-medium text-neutral-700">
            Description
          </label>
          <AttachmentEditor
            value={description}
            onChange={setDescription}
            attachmentsApi={attachmentsApi}
            disabled={busy}
            placeholder="Describe what you want built. You can refine this further once the session opens. Paste or drag screenshots in."
            minHeightClassName="min-h-[160px]"
            uploadError={uploadError}
            textareaId="new-ticket-description"
          />
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={handleAssign}
            disabled={!canSubmit}
          >
            {busy ? "Assigning…" : "Assign"}
          </button>
        </div>

        {submit.kind === "error" && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-medium">Couldn&apos;t create this ticket</p>
            <p className="mt-1 break-words">{submit.detail}</p>
          </div>
        )}
      </section>
    </div>
  );
}
