"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { useEffect, useRef, useState } from "react";
import type { WorkflowState } from "@/lib/workflow-types";

export function RequirementsView() {
  const { agent } = useAgent({ agentId: "workflow" });
  const { copilotkit } = useCopilotKit();
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const syncedRef = useRef(false);

  const state = (agent.state ?? {}) as WorkflowState;

  // Rehydrate the textarea once from server state (e.g. after a remount),
  // without ever clobbering text the human is actively editing.
  useEffect(() => {
    if (!syncedRef.current && state.raw_requirements_text) {
      setText(state.raw_requirements_text);
      syncedRef.current = true;
    }
  }, [state.raw_requirements_text]);
  const questions = [
    ...(state.stages?.specification?.clarifying_questions ?? []),
    ...(state.stages?.plan?.clarifying_questions ?? []),
  ];

  const disabled = text.trim().length === 0 || agent.isRunning || submitting;

  async function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed) return;
    setSubmitting(true);
    try {
      agent.addMessage({ id: crypto.randomUUID(), role: "user", content: trimmed });
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
              <li key={q.id} className="text-sm text-amber-900">
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

      <textarea
        className="min-h-[240px] w-full rounded-lg border border-neutral-300 p-3 text-sm"
        placeholder="Describe your software idea..."
        value={text}
        onChange={(event) => setText(event.target.value)}
        disabled={agent.isRunning || submitting}
      />

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
