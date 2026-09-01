"use client";

import { useAgent, useAttachments, useCopilotKit } from "@copilotkit/react-core/v2";
import type { InputContent } from "@ag-ui/core";
import { useEffect, useRef, useState } from "react";
import { AttachmentEditor, SHARED_ATTACHMENTS_CONFIG } from "@/components/AttachmentEditor";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { ViewContainer } from "@/components/ViewContainer";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { takeHandoffAttachments } from "@/lib/new-ticket-attachment-handoff";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import { anyStageDrafting, buildStarted, runEnded, type WorkflowState } from "@/lib/workflow-types";

export function RequirementsView() {
  // agentId only, not the full {agentId, runtimeAgentId, threadId} triple: AppShell (always
  // mounted above this) already registers the proxied agent once -- registerProxiedAgent throws
  // "already registered" if a second call site re-registers the same agentId (confirmed live),
  // so every other consumer just binds to the existing registration by id.
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { copilotkit } = useCopilotKit();
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
  const rawRequirements = state.stages?.["raw-requirements"];
  // The document as ai-dev-workflow's own P1 stage sees it: its approved content once approved,
  // otherwise its latest draft (the same "approved-else-draft" precedence workflow_persistence.py
  // uses for the .md render) -- falls back to the raw seed text only before P1 has ever drafted
  // anything (a brand new thread's very first paint).
  const rawRequirementsContent =
    ((rawRequirements?.approved_content ?? rawRequirements?.draft) as { content?: string } | null)?.content ??
    state.raw_requirements_text;

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

  // Rehydrate the textarea once from server state (e.g. after a remount),
  // without ever clobbering text the human is actively editing.
  useEffect(() => {
    if (!syncedRef.current && rawRequirementsContent) {
      setText(rawRequirementsContent);
      syncedRef.current = true;
    }
  }, [rawRequirementsContent]);

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
  const runLocked = (buildStarted(state) && !runEnded(state)) || anyStageDrafting(state);
  // Requirements-as-single-source-of-truth (user requirement 2026-08-31, extended to Plan
  // 2026-08-31): while the SPECIFICATION or PLAN gate is open, this tab stays live -- submitting
  // resolves whichever gate is open with the full revised document (graph.py make_gate_node's
  // revised_requirements contract). For Plan, the SAME resolve shape also trips
  // GraphState.restart_from_specification server-side (make_gate_node detects stage_spec.key ==
  // "plan" on its own -- no extra field needed here) so the redraft cascades through
  // Specification first rather than redrafting Plan against its now-stale approved spec.
  const sourceOfTruthGateOpen =
    openInterrupt.open && (openInterrupt.stage === "specification" || openInterrupt.stage === "plan");
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
          openInterrupt.stage === "plan"
            ? "Requirements revised by the reviewer while reviewing the Plan — the Specification redrafts first, strictly from the updated requirements document; once it is re-approved, the Plan will redraft from it. " +
              "Emit the COMPLETE specification: every still-applicable user story and acceptance criterion re-appears citing its existing id — never just the changed ones. " +
              "Features REMOVED from the document must be explicitly retired via retired_us_ids/retired_ac_ids (citing their existing ids), never silently dropped. " +
              "Features the document marks for a LATER phase are specified with deferred=true, never retired; features moved INTO the build-now scope re-appear with deferred=false. " +
              "A previously deferred feature that no longer appears ANYWHERE in the document has been removed — retire it; the current document alone decides what exists."
            : "Requirements revised by the reviewer — redraft the Specification strictly from the updated requirements document. " +
              "Emit the COMPLETE specification: every still-applicable user story and acceptance criterion re-appears citing its existing id — never just the changed ones. " +
              "Features REMOVED from the document must be explicitly retired via retired_us_ids/retired_ac_ids (citing their existing ids), never silently dropped. " +
              "Features the document marks for a LATER phase are specified with deferred=true, never retired; features moved INTO the build-now scope re-appear with deferred=false. " +
              "A previously deferred feature that no longer appears ANYWHERE in the document has been removed — retire it; the current document alone decides what exists.";
        openInterrupt.resolve?.({ decision: "rejected", feedback, revised_requirements: trimmed });
      } finally {
        setSubmitting(false);
      }
      return;
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
            Describe what you want built. Edit and resubmit at any time — including to answer
            clarifying questions below. Paste screenshots directly into the text.
          </p>
        </div>
        {/* Teaches the scoping convention by example (user, 2026-08-31): a full PRD with explicit
            "Build now" vs "Later (deferred)" sections -- deferred items are specified and shown,
            never built until moved up. */}
        <button
          type="button"
          className="shrink-0 rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-600 hover:bg-neutral-100 disabled:opacity-40"
          disabled={agent.isRunning || submitting || runLocked}
          onClick={() => {
            if (text.trim() && !window.confirm("Replace the current requirements text with the PRD template?")) return;
            updateText(PRD_TEMPLATE);
          }}
        >
          Start from PRD template
        </button>
      </div>

      <ClarifyingQuestions
        stageKey="raw-requirements"
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
        {runLocked && !openInterrupt.open && (
          <span className="text-xs text-neutral-500">
            A run is in progress — requirements are locked until it ends (resubmit afterwards for a delta).
          </span>
        )}
        {openInterrupt.open && (
          <span className="text-xs text-neutral-500">
            {openInterrupt.stage === "tech-stack"
              ? "Finish the Tech Stack tab first, then resubmit."
              : openInterrupt.stage === "specification"
                ? "The Specification is awaiting review — submitting here revises the requirements and redrafts it from the updated document."
                : openInterrupt.stage === "plan"
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

/** The Requirements document is the single source of truth; this skeleton teaches the full-PRD
 * convention: keep EVERYTHING the product needs in one document, scope with "Build now" vs
 * "Later (deferred)" sections, and promote work by moving items up and resubmitting. */
const PRD_TEMPLATE = `# <Product name>

## Goal
One or two sentences: what this product does and for whom.

## Build now
List the features to build in this pass. Be concrete — each becomes user stories with testable
acceptance criteria.
- Feature A — what the user can do and what they see
- Feature B — ...

## Later (deferred)
Features that belong to the product but NOT this pass. They are specified and reviewed now, shown
as "deferred", and no code or tests are written for them until you move them into "Build now" and
resubmit.
- Feature C (deferred: planned for a later phase — do not build yet)

## Tech stack
Confirmed on the Tech Stack tab; note anything extra here (libraries, hosting, integrations).

## Constraints & non-goals
- Keep it as simple as possible.
- No auth / no persistence / no ... (delete what doesn't apply)
`;

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
