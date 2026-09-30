"use client";

import { UseAgentUpdate, useAgent, useAttachments, useCopilotKit } from "@copilotkit/react-core/v2";
import type { InputContent } from "@ag-ui/core";
import { memo, useEffect, useRef, useState } from "react";
import { AttachmentEditor, SHARED_ATTACHMENTS_CONFIG } from "@/components/AttachmentEditor";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { ViewContainer } from "@/components/ViewContainer";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { takeHandoffAttachments } from "@/lib/new-ticket-attachment-handoff";
import { rawProxyUrl } from "@/lib/raw-proxy";
import { useRunActivity } from "@/lib/run-activity-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import { usePipeline } from "@/lib/pipeline";
import { anyStageDrafting, buildStarted, runEnded, type WorkflowState } from "@/lib/workflow-types";

interface RequirementsViewProps {
  owner: string;
  repo: string;
  workBranch: string;
}

function RequirementsViewImpl({ owner, repo, workBranch }: RequirementsViewProps) {
  // agentId only, not the full {agentId, runtimeAgentId, threadId} triple: AppShell (always
  // mounted above this) already registers the proxied agent once -- registerProxiedAgent throws
  // "already registered" if a second call site re-registers the same agentId (confirmed live),
  // so every other consumer just binds to the existing registration by id.
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  const { copilotkit } = useCopilotKit();
  const [runActivity] = useRunActivity();
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const syncedRef = useRef(false);

  const attachmentsApi = useAttachments({
    config: {
      enabled: true,
      ...SHARED_ATTACHMENTS_CONFIG,
      onUploadFailed: ({ file, message }) => setUploadError(`${file.name}: ${message}`),
    },
  });
  const { consumeAttachments, processFiles } = attachmentsApi;

  const state = (agent.state ?? {}) as WorkflowState;
  const rawRequirements = state.stages?.["raw-requirements"]; // stage-literal-ok: Requirements' own bespoke view
  // Requirements-delta pivot: at least one submission has ever been recorded for this thread, so
  // the maintained PRD (01-requirements-prd.md) exists to view/download -- see the panel below.
  const hasRequirementsPrd = rawRequirements?.status === "approved";

  // One-shot handoff from the New Ticket form (src/app/(boxed)/tickets/new/page.tsx): title +
  // description typed there before this session's sandbox even existed, stashed in sessionStorage
  // (same-tab client navigation preserves it) since a brand-new session has no server-side draft
  // yet for the rehydrate effect below to find. Runs first so its syncedRef write, if any, short-
  // circuits that effect on this same mount; removed immediately so it can never reapply after the
  // human clears the box. A session opened any other way (e.g. /select) never had this key set, so
  // this is a no-op for every session that isn't ticket-created.
  useEffect(() => {
    if (syncedRef.current) return;
    // sessionStorage access itself (not just the payload's shape) can throw -- a browser/policy
    // that blocks Web Storage outright (private mode variants, some lockdown policies) throws on
    // .getItem itself, and this app has no error boundary anywhere to catch that for us. Degrade
    // exactly like "no handoff was ever set" on any such failure.
    let pending: string | null;
    try {
      const key = `aidw:new-ticket:${threadId}`;
      pending = sessionStorage.getItem(key);
      if (pending) sessionStorage.removeItem(key);
    } catch {
      return;
    }
    if (!pending) return;
    // Attachments queued on the New Ticket form travel via an in-memory handoff, not
    // sessionStorage (new-ticket-attachment-handoff.ts explains why) -- re-fed through the same
    // processFiles path a real file-picker/paste/drop selection uses, so they re-validate and land
    // in this session's attachment queue exactly as if selected here. Independent of whether the
    // text below parses: a malformed text payload shouldn't also drop attachments the user added.
    const handoffFiles = takeHandoffAttachments(threadId);
    if (handoffFiles.length > 0) void processFiles(handoffFiles);
    const combined = parseNewTicketHandoff(pending);
    if (combined) {
      // One-time seed from an external store (sessionStorage) into component state on mount --
      // there's no dependency this could "react" to instead (sessionStorage isn't observable), so
      // this doesn't fit the rule's "derive state from a changed dependency" shape it otherwise
      // checks for. Guarded by syncedRef the same way the server-state rehydrate effect below is,
      // so this never re-fires or clobbers text the human is actively editing.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setText(combined);
      syncedRef.current = true;
    }
  }, [threadId, processFiles]);

  // Requirements-delta pivot: no server-state rehydrate here on purpose. This tab is now a blank
  // entry box for "what do you want to do this session" -- pre-filling it with the prior
  // approved/draft raw-requirements content (the old "the document is the single source of truth,
  // keep editing it in place" contract) would force the human to delete stale text before typing a
  // delta. The maintained PRD is still one click away via the view/download panel below.

  // Last-resort rehydrate from this tab's own draft copy (saved on every keystroke below).
  // Mid-run, agent state doesn't reach a reloaded client until the run next pauses (the
  // reattach gap) -- so a reload right after Submit showed an EMPTY editor ("my requirements
  // vanished", observed live 2026-08-31). Same sessionStorage-degrades-silently rules as the
  // new-ticket handoff above. syncedRef is set so late-arriving server state never clobbers.
  useEffect(() => {
    if (syncedRef.current) return;
    let saved: string | null = null;
    try {
      saved = sessionStorage.getItem(`aidw:req-draft:${threadId}`);
    } catch {
      return;
    }
    if (saved) {
      // One-time seed from an external store on mount, same shape as the handoff effect above.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setText(saved);
      syncedRef.current = true;
    }
  }, [threadId]);

  function updateText(value: string) {
    setText(value);
    try {
      sessionStorage.setItem(`aidw:req-draft:${threadId}`, value);
    } catch {
      // Storage blocked -- the reload safety net is lost, typing still works.
    }
  }

  // A run submitted while an interrupt is pending is silently dropped server-side (the endpoint
  // re-emits the stored interrupt and never starts the graph), so Submit must go down while a
  // review is open -- an enabled button there is a lie.
  const { interrupt: openInterrupt } = useOpenInterrupt();
  // State-derived run lock: agent.isRunning is stream attachment, which resets to false on a
  // page reload while the run keeps going server-side -- observed live: Submit sat enabled all
  // through ac-to-tests. Locked from build-start until the run ends (failure recorded or exit
  // approved -- resubmitting after THAT is the supported requirements-delta flow), and while any
  // stage is actively drafting pre-build.
  const firstBuildStageKey = usePipeline().tabs.find((t) => t.view === "build")?.stages[0]?.key;
  const runLocked = (buildStarted(state, firstBuildStageKey) && !runEnded(state)) || anyStageDrafting(state);
  // Requirements-as-single-source-of-truth (user requirement 2026-08-31, extended to Plan
  // 2026-08-31): while the SPECIFICATION or PLAN gate is open, this tab stays live -- submitting
  // resolves whichever gate is open with the full revised document (graph.py make_gate_node's
  // revised_requirements contract). For Plan, the SAME resolve shape also trips
  // GraphState.restart_from_specification server-side (make_gate_node detects stage_spec.key ==
  // "plan" on its own -- no extra field needed here) so the redraft cascades through
  // Specification first rather than redrafting Plan against its now-stale approved spec.
  const sourceOfTruthGateOpen =
    openInterrupt.open && (openInterrupt.stage === "specification" || openInterrupt.stage === "plan"); // stage-literal-ok: source-of-truth resubmit flow (spec/plan gates)
  // Requirements-delta into an already-finished session is a supported flow (see runLocked's own
  // comment above) but must never fire silently from a stale tab that doesn't know the session
  // already finished elsewhere -- handleSubmit below confirms with the user first and tells the
  // agent via POST /api/sessions/actions {action: "confirm-reopen"} before submitting, which
  // graph.py's intake_node requires (GraphState.reopen_blocked) before it will let this thread
  // reopen. Deliberately does NOT feed into `disabled`: the action must stay available, just
  // confirmed.
  //
  // finishedWithVerdict, not `status === "completed"` (root-caused 2026-09-12): a run that reached
  // the whole pipeline's end and wrote a real report, but scored merge_ready=false, is durably
  // "failed" -- the SAME status value a genuine mid-pipeline crash gets. Gating only on
  // "completed" let a resubmit against a finished-but-not-merge-ready session skip this
  // confirmation and silently reopen it. See session_store.is_finished_with_verdict's own
  // docstring for the full reasoning.
  const needsReopenConfirm = runActivity?.finishedWithVerdict ?? false;
  const disabled =
    text.trim().length === 0 ||
    agent.isRunning ||
    submitting ||
    (openInterrupt.open && !sourceOfTruthGateOpen) ||
    runLocked;

  async function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed) return;
    setSubmitting(true);
    if (sourceOfTruthGateOpen) {
      try {
        const feedback =
          openInterrupt.stage === "plan" // stage-literal-ok: source-of-truth resubmit copy
            ? "Requirements revised by the reviewer while reviewing the Plan — the Specification redrafts first, strictly from this correction; once it is re-approved, the Plan will redraft from it. " +
              "This correction is a DELTA, not the whole specification: only include what it actually adds or changes — a genuinely new story/criterion, or one you're revising (cite its existing id) or retiring (retired_us_ids/retired_ac_ids). Never re-emit anything this correction doesn't touch; leaving it out does not remove it."
            : "Requirements revised by the reviewer — redraft the Specification strictly from this correction. " +
              "This correction is a DELTA, not the whole specification: only include what it actually adds or changes — a genuinely new story/criterion, or one you're revising (cite its existing id) or retiring (retired_us_ids/retired_ac_ids). Never re-emit anything this correction doesn't touch; leaving it out does not remove it.";
        openInterrupt.resolve?.({ decision: "rejected", feedback, revised_requirements: trimmed });
      } finally {
        setSubmitting(false);
      }
      return;
    }
    if (needsReopenConfirm) {
      if (!window.confirm("This session already finished. Continue working on it anyway?")) {
        setSubmitting(false);
        return;
      }
      try {
        await fetch("/api/sessions/actions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sessionId: threadId, action: "confirm-reopen" }),
        });
      } catch {
        // Best-effort: if this fails, intake_node's own confirm_reopen check just refuses the
        // submission server-side (fails safe, never silently reopens) -- the run below still
        // fires, it'll just no-op with a clear server-side log instead of a client error here.
      }
    }
    try {
      const ready = consumeAttachments();
      const content: string | InputContent[] =
        ready.length === 0
          ? trimmed
          : [
              { type: "text", text: trimmed },
              ...ready.map(
                (att) =>
                  ({
                    type: att.type,
                    source: att.source,
                    metadata: { ...(att.filename ? { filename: att.filename } : {}), ...att.metadata },
                  }) as InputContent,
              ),
            ];
      agent.addMessage({ id: crypto.randomUUID(), role: "user", content });
      await copilotkit.runAgent({ agent });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <ViewContainer>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Requirements</h1>
          <p className="text-sm text-neutral-500">
            Describe what you want to add, change, or remove this session — just the delta, not
            the whole product. Paste screenshots directly into the text.
          </p>
        </div>
        {/* Requirements-delta pivot: the maintained PRD (every round's delta merged into one
            document, in a standard PRD structure) replaces the old "Start from PRD template"
            button -- this tab no longer asks the human to author or re-author the whole document
            themselves. */}
        {hasRequirementsPrd && (
          <div className="flex shrink-0 items-center gap-2 text-xs">
            <span className="text-neutral-500">Current PRD:</span>
            <a
              href={rawProxyUrl(owner, repo, ".ai-dev-workflow/01-requirements-prd.md", workBranch)}
              target="_blank"
              rel="noreferrer"
              className="rounded-md border border-neutral-300 px-3 py-1.5 font-medium text-neutral-600 hover:bg-neutral-100"
            >
              View
            </a>
            <a
              href={rawProxyUrl(owner, repo, ".ai-dev-workflow/01-requirements-prd.md", workBranch)}
              download="requirements-prd.md"
              className="rounded-md border border-neutral-300 px-3 py-1.5 font-medium text-neutral-600 hover:bg-neutral-100"
            >
              Download
            </a>
          </div>
        )}
      </div>

      <ClarifyingQuestions
        stageKey="raw-requirements" // stage-literal-ok: Requirements' own bespoke view
        questions={rawRequirements?.clarifying_questions ?? []}
        hint="Answer by editing the requirements text below, then resubmit."
      />

      <AttachmentEditor
        value={text}
        onChange={updateText}
        attachmentsApi={attachmentsApi}
        disabled={agent.isRunning || submitting}
        minHeightClassName="h-[63vh]"
        placeholder="Describe your software idea... (markdown supported; paste or drag screenshots in)"
        uploadError={uploadError}
      />

      <div className="flex items-center justify-end gap-3">
        {/* Workflow Liveness Fix: `runLocked` is pure persisted state (anyStageDrafting survives a
            reload on purpose) -- it stays a lock either way, but a genuinely dead run needs
            different copy and an actual way out, not an indefinite "in progress". */}
        {runLocked && !openInterrupt.open && runActivity?.interrupted && (
          <span className="flex items-center gap-2 text-xs text-amber-700">
            This run appears to have stopped — Resume before submitting new requirements.
            <button
              type="button"
              className="rounded-md bg-neutral-900 px-2 py-1 text-xs font-medium text-white disabled:opacity-40"
              disabled={agent.isRunning}
              onClick={() => void copilotkit.runAgent({ agent })}
            >
              {agent.isRunning ? "Resuming…" : "Resume"}
            </button>
          </span>
        )}
        {runLocked && !openInterrupt.open && !runActivity?.interrupted && (
          <span className="text-xs text-neutral-500">
            A run is in progress — requirements are locked until it ends (resubmit afterwards for a delta).
          </span>
        )}
        {openInterrupt.open && (
          <span className="text-xs text-neutral-500">
            {openInterrupt.stage === "tech-stack" // stage-literal-ok: bespoke interrupt copy
              ? "Finish the Tech Stack tab first, then resubmit."
              : openInterrupt.stage === "specification" // stage-literal-ok: bespoke interrupt copy
                ? "The Specification is awaiting review — submitting here revises the requirements and redrafts it from the updated document."
                : openInterrupt.stage === "plan" // stage-literal-ok: bespoke interrupt copy
                  ? "The Plan is awaiting review — submitting here revises the requirements and redrafts the Specification first, then the Plan."
                  : "A review is waiting — approve or reject it first, then edit and resubmit."}
          </span>
        )}
        <button
          className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          disabled={disabled}
          onClick={handleSubmit}
        >
          {/* "Submitting…" only during the actual submit POST -- it used to stay up for the
              whole multi-minute run (isRunning), which made the Requirements green dot (that
              stage IS done seconds in) look contradictory. The global spinner in the tab row
              now owns "the pipeline is working". */}
          {submitting ? "Submitting…" : "Submit"}
        </button>
      </div>
    </ViewContainer>
  );
}

// Memoized (shallow prop comparison) so AppShell's unrelated local-state re-renders don't also
// force this while it's the hidden tab -- owner/repo/workBranch are stable per session.
export const RequirementsView = memo(RequirementsViewImpl);

/** Parses the New Ticket form's sessionStorage handoff payload (see the rehydrate effect above)
 * into the combined requirements text, or null for a missing/malformed/empty payload -- kept
 * outside the effect body so that one stays a flat, single-branch setState-from-external-state
 * read. */
function parseNewTicketHandoff(raw: string): string | null {
  try {
    const { title, description } = JSON.parse(raw) as { title: string; description: string };
    const combined = description ? `${title}\n\n${description}` : title;
    return combined.trim() ? combined : null;
  } catch {
    // Malformed handoff payload -- ignore, fall through to the normal server-state rehydrate.
    return null;
  }
}
