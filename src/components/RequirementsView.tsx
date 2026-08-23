"use client";

import { useAgent, useAttachments, useCopilotKit } from "@copilotkit/react-core/v2";
import type { InputContent } from "@ag-ui/core";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { ViewContainer } from "@/components/ViewContainer";
import { useOpenInterrupt } from "@/lib/interrupt-context";
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
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [submitting, setSubmitting] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const syncedRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const {
    attachments,
    containerRef,
    fileInputRef,
    handleFileUpload,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    removeAttachment,
    consumeAttachments,
    processFiles,
  } = useAttachments({
    config: {
      enabled: true,
      accept: "image/*,application/pdf,.doc,.docx,.txt,.md",
      maxSize: 20 * 1024 * 1024,
      onUploadFailed: ({ file, message }) => setUploadError(`${file.name}: ${message}`),
    },
  });

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
    const key = `aidw:new-ticket:${threadId}`;
    const pending = sessionStorage.getItem(key);
    if (!pending) return;
    sessionStorage.removeItem(key);
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
  }, [threadId]);

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

  /** Pasted images upload through the normal attachment queue AND insert a markdown ref at the
   * cursor. Each pasted file is renamed to a unique name first -- clipboard images all arrive as
   * "image.png", and the `attachment:` ref resolves by filename, so uniqueness is what keeps two
   * pastes from resolving to the same image. Non-image pastes (text) proceed untouched. */
  function handlePaste(event: React.ClipboardEvent<HTMLTextAreaElement>) {
    const images = Array.from(event.clipboardData?.files ?? []).filter((f) => f.type.startsWith("image/"));
    if (images.length === 0) return;
    event.preventDefault();
    const renamed = images.map(
      (f, i) => new File([f], `pasted-${Date.now()}-${i + 1}.${(f.type.split("/")[1] ?? "png").split("+")[0]}`, { type: f.type }),
    );
    void processFiles(renamed);
    const refs = renamed.map((f) => `![screenshot](attachment:${f.name})`).join("\n");
    const el = textareaRef.current;
    const start = el?.selectionStart ?? text.length;
    const end = el?.selectionEnd ?? text.length;
    const next = `${text.slice(0, start)}${refs}${text.slice(end)}`;
    setText(next);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(start + refs.length, start + refs.length);
    });
  }

  /** Resolves attachment:<filename> refs in the preview to a renderable URL from the live
   * attachment queue. Refs whose attachment is gone (e.g. after submit -- consumeAttachments
   * clears the queue) render as the alt text, which is acceptable: the image itself already
   * reached the pipeline as an InputContent part. */
  function resolveUrl(url: string): string {
    if (!url.startsWith("attachment:")) return url;
    const name = decodeURIComponent(url.slice("attachment:".length));
    const att = attachments.find((a) => a.filename === name);
    if (!att) return "";
    if (att.thumbnail) return att.thumbnail;
    if (att.source.type === "data") return `data:${att.source.mimeType};base64,${att.source.value}`;
    return att.source.value;
  }

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

      <div
        ref={containerRef}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className="flex min-h-0 flex-1 flex-col rounded-lg border border-neutral-300 focus-within:ring-1 focus-within:ring-neutral-400"
      >
        <div className="flex items-center gap-1 border-b border-neutral-200 px-2 py-1">
          <ModeButton label="Edit" active={mode === "edit"} onClick={() => setMode("edit")} />
          <ModeButton label="Preview" active={mode === "preview"} onClick={() => setMode("preview")} />
        </div>

        {mode === "edit" ? (
          <textarea
            ref={textareaRef}
            className="min-h-[240px] w-full flex-1 resize-none p-3 text-sm outline-none"
            placeholder="Describe your software idea... (markdown supported; paste or drag screenshots in)"
            value={text}
            onChange={(event) => setText(event.target.value)}
            onPaste={handlePaste}
            disabled={agent.isRunning || submitting}
          />
        ) : (
          <div className="prose prose-sm min-h-[240px] max-w-none flex-1 overflow-y-auto p-3 [&_img]:max-h-80 [&_img]:rounded-md [&_img]:border">
            {text.trim() ? (
              <ReactMarkdown urlTransform={resolveUrl}>{text}</ReactMarkdown>
            ) : (
              <p className="text-sm text-neutral-400">Nothing to preview yet.</p>
            )}
          </div>
        )}

        {attachments.length > 0 && (
          <ul className="flex flex-wrap gap-2 border-t border-neutral-200 p-2">
            {attachments.map((att) => (
              <li
                key={att.id}
                className="flex items-center gap-1.5 rounded-full border border-neutral-200 bg-neutral-50 px-2.5 py-1 text-xs text-neutral-700"
              >
                <span>{att.filename ?? "attachment"}</span>
                <span className="text-neutral-400">({att.status})</span>
                <button
                  type="button"
                  className="ml-0.5 text-neutral-400 hover:text-neutral-700"
                  onClick={() => removeAttachment(att.id)}
                  aria-label={`Remove ${att.filename ?? "attachment"}`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="flex items-center justify-between gap-2 border-t border-neutral-200 p-2">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleFileUpload}
          />
          <button
            type="button"
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700 hover:bg-neutral-100"
            onClick={() => fileInputRef.current?.click()}
            disabled={agent.isRunning || submitting}
          >
            Attach screenshot/document
          </button>
          {uploadError && <span className="text-xs text-red-600">{uploadError}</span>}
        </div>
      </div>

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

function ModeButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className={[
        "rounded px-2 py-0.5 text-xs font-medium",
        active ? "bg-neutral-900 text-white" : "text-neutral-500 hover:bg-neutral-100",
      ].join(" ")}
      onClick={onClick}
    >
      {label}
    </button>
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
