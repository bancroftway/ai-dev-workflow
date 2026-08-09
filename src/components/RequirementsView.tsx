"use client";

import { useAgent, useAttachments, useCopilotKit } from "@copilotkit/react-core/v2";
import type { InputContent } from "@ag-ui/core";
import { useEffect, useRef, useState } from "react";
import type { WorkflowState } from "@/lib/workflow-types";

export function RequirementsView() {
  const { agent } = useAgent({ agentId: "workflow" });
  const { copilotkit } = useCopilotKit();
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const syncedRef = useRef(false);

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
  } = useAttachments({
    config: {
      enabled: true,
      accept: "image/*,application/pdf,.doc,.docx,.txt,.md",
      maxSize: 20 * 1024 * 1024,
      onUploadFailed: ({ file, message }) => setUploadError(`${file.name}: ${message}`),
    },
  });

  const state = (agent.state ?? {}) as WorkflowState;

  // Rehydrate the textarea once from server state (e.g. after a remount),
  // without ever clobbering text the human is actively editing.
  useEffect(() => {
    if (!syncedRef.current && state.raw_requirements_text) {
      setText(state.raw_requirements_text);
      syncedRef.current = true;
    }
  }, [state.raw_requirements_text]);
  // Question ids are only stable "within the turn that produced it" (per the domain model),
  // scoped to their own stage -- concatenating both stages' lists can produce the same id twice
  // (e.g. both stages' latest turn independently minting a "CQ-1"), so the React key is
  // stage-qualified rather than the bare id.
  const questions = [
    ...(state.stages?.specification?.clarifying_questions ?? []).map((q) => ({
      ...q,
      reactKey: `specification-${q.id}`,
    })),
    ...(state.stages?.plan?.clarifying_questions ?? []).map((q) => ({ ...q, reactKey: `plan-${q.id}` })),
  ];

  const disabled = text.trim().length === 0 || agent.isRunning || submitting;

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
    <div className="mx-auto flex max-w-3xl flex-col gap-4 p-6">
      <div>
        <h1 className="text-lg font-semibold">Requirements</h1>
        <p className="text-sm text-neutral-500">
          Describe what you want built. Edit and resubmit at any time — including to answer
          clarifying questions below.
        </p>
      </div>

      {questions.length > 0 && (
        <div className="space-y-3 rounded-lg border border-amber-300 bg-amber-50 p-4">
          <h2 className="text-sm font-medium text-amber-900">Clarifying Questions</h2>
          <ul className="space-y-2">
            {questions.map((q) => (
              <li key={q.reactKey} className="text-sm text-amber-900">
                <span className="mr-1 font-mono text-xs text-amber-700">{q.id}</span>
                {q.question}
                {q.suggested_choices.length > 0 && (
                  <div className="mt-1 text-xs text-amber-700">
                    Suggestions: {q.suggested_choices.join(", ")}
                  </div>
                )}
              </li>
            ))}
          </ul>
          <p className="text-xs text-amber-700">
            Answer by editing the requirements text below, then resubmit.
          </p>
        </div>
      )}

      <div
        ref={containerRef}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className="rounded-lg border border-neutral-300 focus-within:ring-1 focus-within:ring-neutral-400"
      >
        <textarea
          className="min-h-[240px] w-full rounded-t-lg p-3 text-sm outline-none"
          placeholder="Describe your software idea... (or drag screenshots/documents in)"
          value={text}
          onChange={(event) => setText(event.target.value)}
          disabled={agent.isRunning || submitting}
        />

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

      <div className="flex justify-end">
        <button
          className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          disabled={disabled}
          onClick={handleSubmit}
        >
          {agent.isRunning || submitting ? "Submitting…" : "Submit"}
        </button>
      </div>
    </div>
  );
}
