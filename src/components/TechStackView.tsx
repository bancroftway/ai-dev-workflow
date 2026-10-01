"use client";

import { UseAgentUpdate, useAgent, useAttachments } from "@copilotkit/react-core/v2";
import { memo, useEffect, useRef, useState } from "react";
import { AttachmentEditor, SHARED_ATTACHMENTS_CONFIG } from "@/components/AttachmentEditor";
import { Spinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { useReview, type ReviewVerification } from "@/lib/review-context";
import { useRunActivity } from "@/lib/run-activity-context";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import { usePipeline } from "@/lib/pipeline";
import type { CannedTechStack, TechStackCatalogResponse, WorkflowState } from "@/lib/workflow-types";

/**
 * First tab in the workflow, before Requirements. Replaces the old chat-sidebar greenfield picker
 * and app-discovery's silent auto-approval -- every repository, empty or not, gets reviewed here
 * before the rest of the pipeline runs.
 *
 * Load/edit/submit shape mirrors RequirementsView, but the gate itself is real (tech-stack's
 * StageSpec is `requires_human_gate=True` now): Submit posts the edited markdown to the open review
 * (POST /api/sessions/{id}/review, action "submit") -- the agent resumes the gate with it, and
 * agent/src/graph.py's make_gate_node/resolve_tech_stack_submission save, extract into structured
 * JSON, and commit it. The review itself (markdown, picker, last verdict, copy) is the server's
 * view model (agent/src/review_view.py).
 */
function TechStackViewImpl() {
  // agentId only -- AppShell already registered this proxied agent (see RequirementsView.tsx).
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  const { review, submitting: reviewSubmitting, error: reviewError, submit } = useReview();
  const [sandboxStatus] = useSandboxStatus();
  const [runActivity] = useRunActivity();
  const { stageOrderIndex } = usePipeline();

  const techStack = review.open && review.kind === "tech_stack" ? review.tech_stack : null;
  const isOpen = techStack != null;
  const showDropdown = techStack?.show_catalog === true;
  const submitAction = review.actions?.find((a) => a.id === "submit");

  const [text, setText] = useState("");
  const [catalog, setCatalog] = useState<CannedTechStack[]>([]);
  const [selectedStackId, setSelectedStackId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const syncedRef = useRef(false);

  // Same editor stack as RequirementsView (user requirement 2026-08-31: identical look & feel,
  // paste-inline-screenshots everywhere). Note: tech-stack Submit resolves with markdown only,
  // so pasted images preview here but are not carried into the committed tech-stack.md.
  const attachmentsApi = useAttachments({
    config: {
      enabled: true,
      ...SHARED_ATTACHMENTS_CONFIG,
      onUploadFailed: ({ file, message }) => setUploadError(`${file.name}: ${message}`),
    },
  });

  useEffect(() => {
    if (!showDropdown) return;
    fetch("/api/tech-stack-catalog")
      .then((res) => (res.ok ? (res.json() as Promise<TechStackCatalogResponse>) : { stacks: [] }))
      .then((body) => setCatalog(body.stacks ?? []))
      .catch(() => setCatalog([]));
  }, [showDropdown]);

  // A rejected submission (Part 2 Task 10) reopens this SAME gate a second time once the redraft
  // is ready -- before Task 10, Submit's only outcome (implicit approval) always advanced the
  // pipeline past tech-stack, so this gate could never reopen within one mount and the one-shot
  // guard below never needed resetting. Without this, a reject would leave the just-rejected text
  // sitting in the editor forever instead of showing the fresh redraft. Keyed by the review's id:
  // each gate opening is a new one (a failed after-submit verify re-opens with a new id too).
  const openReviewId = isOpen ? review.id : null;
  useEffect(() => {
    if (openReviewId != null) syncedRef.current = false;
  }, [openReviewId]);

  // Prefill exactly once per gate occurrence from whatever the gate is showing -- never clobber
  // an active edit. A per-session draft copy (saved on every change below) takes precedence over
  // the gate's own markdown: any remount (tab switch, hot reload) resets this component's state,
  // and prefilling back to the gate's stub silently REPLACED a picked/edited stack -- observed
  // live 2026-08-31: the greenfield stub got submitted and approved instead of the user's
  // Angular+.NET pick. Same sessionStorage-degrades-silently rules as RequirementsView.
  useEffect(() => {
    if (syncedRef.current || techStack == null) return;
    let saved: string | null = null;
    try {
      saved = sessionStorage.getItem(`aidw:techstack-draft:${threadId}`);
    } catch {
      saved = null;
    }
    setText(saved || techStack.markdown);
    syncedRef.current = true;
  }, [techStack, threadId]);

  function updateText(value: string) {
    setText(value);
    try {
      sessionStorage.setItem(`aidw:techstack-draft:${threadId}`, value);
    } catch {
      // Storage blocked -- the remount safety net is lost, editing still works.
    }
  }

  function pickStack(id: string) {
    setSelectedStackId(id);
    const found = catalog.find((s) => s.id === id);
    if (found) updateText(found.markdown); // overwrites the editor; still hand-editable after
  }

  async function handleSubmit() {
    setSubmitting(true);
    try {
      // The submitted text is the stack of record now -- a stale draft copy must not resurrect
      // on the next session/gate against this thread. Kept when the agent refused the submit.
      if (await submit("submit", text)) {
        try {
          sessionStorage.removeItem(`aidw:techstack-draft:${threadId}`);
        } catch {
          // ignore
        }
      }
    } finally {
      setSubmitting(false);
    }
  }

  // Reject + feedback REMOVED (user decision 2026-08-31): unlike spec/plan, this stage's whole
  // artifact sits in the editable textarea below -- "reject with feedback so the LLM redrafts"
  // is strictly worse than the user just editing the text and submitting. The gate's server-side
  // {decision, feedback} contract (graph.py make_gate_node) is untouched; this tab simply never
  // sends it.
  const disabled = !submitAction?.enabled || text.trim().length === 0 || submitting || reviewSubmitting;

  const state = (agent.state ?? {}) as WorkflowState;
  const stage = state.stages?.["tech-stack"]; // stage-literal-ok: Tech Stack's own bespoke view

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Tech Stack</h1>
        <p className="text-sm text-neutral-500">
          {techStack?.subtitle ?? "The technology stack this session builds against."}
        </p>
      </div>

      {/* Two different waits share this slot: before the gate the pipeline is detecting; after
          Submit it is saving (structured extraction + commit) -- calling the second one
          "Detecting" read as the app having lost the submission (user, 2026-08-31). ready_for_review
          with no open interrupt can only be the post-submit phase. */}
      {/* sandboxStatus check: a spinner with no failure signal of its own spun forever on a
          provisioning failure (AppShell's "Sandbox provisioning failed" banner is the actual
          error surface) or a stale reload of a terminated session -- neither is "detecting".
          Empty-tabs fix (root-caused 2026-09-12): none of this ever checked durable truth, so a
          session long past tech-stack showed "Detecting…" forever whenever the live snapshot
          hadn't (re)arrived -- now the only way most sessions show anything at all, since nothing
          auto-fires a live snapshot anymore (this session's pivot). */}
      {!isOpen && stage?.status !== "approved" && (sandboxStatus === "provisioning" || sandboxStatus === "ready") && (
        stageOrderIndex(runActivity?.currentStage) > stageOrderIndex("tech-stack") ? ( // stage-literal-ok: Tech Stack's own bespoke view
          <p className="text-sm text-neutral-500">Approved — waiting for full detail to sync…</p>
        ) : (
          <p className="flex items-center gap-2 text-sm text-neutral-500">
            <Spinner />
            {stage?.status === "ready_for_review"
              ? "Saving your tech stack — extracting the structured details every later stage builds on…"
              : "Detecting your tech stack…"}
          </p>
        )
      )}

      {!isOpen && stage?.status === "approved" && (
        <ConfirmedTechStackSummary content={stage.approved_content} threadId={threadId} />
      )}

      {isOpen && (
        <>
          {showDropdown && (
            <label className="flex flex-col gap-1">
              <span className="text-sm font-medium text-neutral-700">Or start from a canned stack</span>
              <select
                className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
                value={selectedStackId}
                onChange={(event) => pickStack(event.target.value)}
                disabled={catalog.length === 0}
              >
                <option value="" disabled>
                  {catalog.length === 0 ? "Loading stacks…" : "Choose a starting stack…"}
                </option>
                {catalog.map((stack) => (
                  <option key={stack.id} value={stack.id}>
                    {stack.title}
                  </option>
                ))}
              </select>
            </label>
          )}

          {techStack.verification && <SubmissionVerification verification={techStack.verification} />}

          <AttachmentEditor
            value={text}
            onChange={updateText}
            attachmentsApi={attachmentsApi}
            disabled={submitting}
            minHeightClassName="h-[63vh]"
            uploadError={uploadError}
          />

          <div className="flex items-center justify-end gap-3">
            {(reviewError ?? review.blocked ?? review.note) && (
              <span className="text-xs text-neutral-500">{reviewError ?? review.blocked ?? review.note}</span>
            )}
            <button
              className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
              disabled={disabled}
              onClick={handleSubmit}
            >
              {submitting || reviewSubmitting ? "Saving…" : (submitAction?.label ?? "Submit")}
            </button>
          </div>
        </>
      )}
    </ViewContainer>
  );
}

// No props -- memoized so AppShell's unrelated local-state re-renders don't also force this
// while it's the hidden tab.
export const TechStackView = memo(TechStackViewImpl);

/** The after-submit gate sent the submission back: what failed (server-built rows), so the human
 * can fix the text. */
function SubmissionVerification({ verification }: { verification: ReviewVerification }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-red-300 bg-red-50 p-3 text-sm">
      <p className="font-medium text-red-900">{verification.title}</p>
      {verification.items.length > 0 ? (
        <ul className="flex flex-col gap-1 text-xs">
          {verification.items.map((c) => (
            <li key={c.key} className={c.tone === "fail" ? "text-red-800" : "text-amber-800"}>
              <span className="font-medium">{c.label}</span>
              {c.detail && <> — {c.detail}</>}
            </li>
          ))}
        </ul>
      ) : (
        verification.feedback && <p className="text-xs whitespace-pre-wrap text-red-800">{verification.feedback}</p>
      )}
    </div>
  );
}

function ConfirmedTechStackSummary({ content, threadId }: { content: unknown; threadId: string }) {
  const c = (content ?? {}) as {
    summary?: string;
    languages?: string[];
    frameworks?: string[];
    // B2 (tech-stack startability pivot): brownfield-only, set once by the boot probe
    // (preflight_nodes.probe_tech_stack_startability) that runs alongside tech-stack approval.
    // Absent (undefined) on a greenfield repo or one onboarded before this field existed -- treated
    // the same as `true` everywhere this is read (backend and frontend both default startable to
    // true), so this banner only ever renders when it's explicitly `false`.
    startable?: boolean;
    not_startable_reason?: string | null;
  };
  // Local override: the recheck button's own response is the freshest verdict there is (it just
  // rewrote tech-stack.approved.json), but nothing re-streams `agent.state` outside of an actual
  // graph run/resume -- see preflight_nodes.recheck_tech_stack_startability's own docstring for why
  // the file write alone is still correct for the NEXT resume. A successful recheck (startable:
  // true) self-resolves the banner below with no extra wiring, since `startable` reads the
  // override first. ponytail: does NOT reset if `content` itself later changes out from under a
  // still-mounted tab (a real background resync) -- that's a rare edge case this codebase's
  // stricter react-hooks/refs rules make annoying to guard against during render; add a
  // content-change reset if it's ever actually observed in practice.
  const [recheck, setRecheck] = useState<{ startable: boolean; reason: string | null } | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [recheckError, setRecheckError] = useState<string | null>(null);

  const startable = recheck ? recheck.startable : (c.startable ?? true);
  const notStartableReason = recheck ? recheck.reason : c.not_startable_reason;

  async function handleRecheck() {
    setRechecking(true);
    setRecheckError(null);
    try {
      const res = await fetch("/api/sessions/actions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: threadId, action: "recheck-tech-stack-boot" }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || `recheck failed (${res.status})`);
      }
      const body = (await res.json()) as { startable?: boolean; not_startable_reason?: string | null };
      setRecheck({ startable: body.startable ?? false, reason: body.not_startable_reason ?? null });
    } catch (err) {
      setRecheckError(err instanceof Error ? err.message : "recheck failed");
    } finally {
      setRechecking(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-neutral-200 p-4">
      <div className="flex items-center gap-2">
        <span className="rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800">
          Confirmed
        </span>
      </div>
      {c.summary && <p className="text-sm text-neutral-700">{c.summary}</p>}
      {((c.languages?.length ?? 0) > 0 || (c.frameworks?.length ?? 0) > 0) && (
        <p className="text-xs text-neutral-500">
          {[...(c.languages ?? []), ...(c.frameworks ?? [])].join(" · ")}
        </p>
      )}
      {!startable && (
        <div className="flex flex-col gap-1.5 rounded-md border border-amber-300 bg-amber-50 p-3">
          <p className="text-xs font-medium text-amber-800">
            This application did not start during setup — e2e and App Health are unavailable.
          </p>
          {notStartableReason && <p className="text-xs text-amber-700">{notStartableReason}</p>}
          <div>
            <button
              type="button"
              className="rounded-md border border-amber-400 bg-white px-2.5 py-1 text-xs font-medium text-amber-800 disabled:opacity-50"
              disabled={rechecking}
              onClick={handleRecheck}
            >
              {rechecking ? "Rechecking…" : "Recheck"}
            </button>
          </div>
          {recheckError && <p className="text-xs text-red-700">{recheckError}</p>}
        </div>
      )}
    </div>
  );
}

