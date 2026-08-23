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
import type { WorkflowState } from "@/lib/workflow-types";

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

  // A run submitted while an interrupt is pending is silently dropped server-side (the endpoint
  // re-emits the stored interrupt and never starts the graph), so Submit must go down while a
  // review is open -- an enabled button there is a lie.
  const { interrupt: openInterrupt } = useOpenInterrupt();
  const disabled = text.trim().length === 0 || agent.isRunning || submitting || openInterrupt.open;

  async function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed) return;
    setSubmitting(true);
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
      <div>
        <h1 className="text-lg font-semibold">Requirements</h1>
        <p className="text-sm text-neutral-500">
          Describe what you want built. Edit and resubmit at any time — including to answer
          clarifying questions below. Paste screenshots directly into the text.
        </p>
      </div>

      <ClarifyingQuestions
        stageKey="raw-requirements"
        questions={rawRequirements?.clarifying_questions ?? []}
        hint="Answer by editing the requirements text below, then resubmit."
      />

      <AttachmentEditor
        value={text}
        onChange={setText}
        attachmentsApi={attachmentsApi}
        disabled={agent.isRunning || submitting}
        placeholder="Describe your software idea... (markdown supported; paste or drag screenshots in)"
        uploadError={uploadError}
      />

      <div className="flex items-center justify-end gap-3">
        {openInterrupt.open && (
          <span className="text-xs text-neutral-500">
            {openInterrupt.stage === "tech-stack"
              ? "Finish the Tech Stack tab first, then resubmit."
              : "A review is waiting in the chat sidebar — approve or acknowledge it first, then edit and resubmit."}
          </span>
        )}
        <button
          className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          disabled={disabled}
          onClick={handleSubmit}
        >
          {agent.isRunning || submitting ? "Submitting…" : "Submit"}
        </button>
      </div>
    </ViewContainer>
  );
}

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
