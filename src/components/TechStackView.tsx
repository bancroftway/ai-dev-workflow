"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ViewContainer } from "@/components/ViewContainer";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { CannedTechStack, TechStackCatalogResponse, WorkflowState } from "@/lib/workflow-types";

/**
 * First tab in the workflow, before Requirements. Replaces the old chat-sidebar greenfield picker
 * and app-discovery's silent auto-approval -- every repository, empty or not, gets reviewed here
 * before the rest of the pipeline runs.
 *
 * Load/edit/submit shape mirrors RequirementsView, but the gate itself is real (tech-stack's
 * StageSpec is `requires_human_gate=True` now): Submit resolves the open interrupt with the
 * edited markdown via `useOpenInterrupt().resolve` rather than posting a chat message -- that
 * resolve is what agent/src/graph.py's make_gate_node/resolve_tech_stack_submission actually save,
 * extract into structured JSON, and commit.
 */
export function TechStackView() {
  // agentId only -- AppShell already registered this proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { interrupt } = useOpenInterrupt();
  const [sandboxStatus] = useSandboxStatus();

  const isOpen = interrupt.open && interrupt.stage === "tech-stack";
  const showDropdown = isOpen && interrupt.fileExisted === false;

  const [text, setText] = useState("");
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [catalog, setCatalog] = useState<CannedTechStack[]>([]);
  const [selectedStackId, setSelectedStackId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState("");
  const syncedRef = useRef(false);

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
  // sitting in the editor forever instead of showing the fresh redraft.
  useEffect(() => {
    if (isOpen) syncedRef.current = false;
  }, [isOpen]);

  // Prefill exactly once per gate occurrence from whatever the gate is showing -- never clobber
  // an active edit.
  useEffect(() => {
    if (syncedRef.current || !isOpen || typeof interrupt.draftMarkdown !== "string") return;
    setText(interrupt.draftMarkdown);
    syncedRef.current = true;
  }, [isOpen, interrupt.draftMarkdown]);

  function pickStack(id: string) {
    setSelectedStackId(id);
    const found = catalog.find((s) => s.id === id);
    if (found) setText(found.markdown); // overwrites the editor; still hand-editable after
  }

  async function handleSubmit() {
    setSubmitting(true);
    try {
      interrupt.resolve?.({ markdown: text });
    } finally {
      setSubmitting(false);
    }
  }

  // Same {decision, feedback} contract as the generic InterruptCard's Reject button
  // (graph.py make_gate_node) -- consistency across every gated stage's UI, per the plan's text.
  async function handleReject() {
    setSubmitting(true);
    try {
      interrupt.resolve?.({ decision: "rejected", feedback: feedback.trim() });
    } finally {
      setSubmitting(false);
    }
  }

  const disabled = !isOpen || text.trim().length === 0 || submitting || sandboxStatus !== "ready";
  const rejectDisabled = !isOpen || feedback.trim().length === 0 || submitting || sandboxStatus !== "ready";

  const state = (agent.state ?? {}) as WorkflowState;
  const stage = state.stages?.["tech-stack"];

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Tech Stack</h1>
        <p className="text-sm text-neutral-500">
          {isOpen
            ? "Review the detected tech stack below. Edit it directly, or pick a starting stack, then submit."
            : "The technology stack this session builds against."}
        </p>
      </div>

      {!isOpen && stage?.status !== "approved" && (
        <p className="text-sm text-neutral-500">Detecting your tech stack…</p>
      )}

      {!isOpen && stage?.status === "approved" && (
        <ConfirmedTechStackSummary content={stage.approved_content} />
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

          <div className="flex min-h-0 flex-1 flex-col rounded-lg border border-neutral-300 focus-within:ring-1 focus-within:ring-neutral-400">
            <div className="flex items-center gap-1 border-b border-neutral-200 px-2 py-1">
              <ModeButton label="Edit" active={mode === "edit"} onClick={() => setMode("edit")} />
              <ModeButton label="Preview" active={mode === "preview"} onClick={() => setMode("preview")} />
            </div>

            {mode === "edit" ? (
              <textarea
                className="min-h-[240px] w-full flex-1 resize-none p-3 font-mono text-xs outline-none"
                value={text}
                onChange={(event) => setText(event.target.value)}
                disabled={submitting}
              />
            ) : (
              // Default sanitizer, no urlTransform override: this text may carry arbitrary human
              // edits (or came from a canned catalog file), so raw HTML/script must never render.
              <div className="prose prose-sm min-h-[240px] max-w-none flex-1 overflow-y-auto p-3">
                <ReactMarkdown>{text}</ReactMarkdown>
              </div>
            )}
          </div>

          <label className="flex flex-col gap-1">
            <span className="text-sm font-medium text-neutral-700">Feedback (required to reject)</span>
            <textarea
              className="min-h-[60px] w-full resize-none rounded-md border border-neutral-300 p-2 text-sm outline-none"
              rows={2}
              placeholder="What should change before this is approved?"
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              disabled={submitting}
            />
          </label>

          <div className="flex items-center justify-end gap-3">
            {sandboxStatus !== "ready" && (
              <span className="text-xs text-neutral-500">Waiting for the dev-tool sandbox to finish starting…</span>
            )}
            <button
              className="rounded-lg border border-red-300 bg-white px-4 py-2 text-sm font-medium text-red-700 disabled:cursor-not-allowed disabled:opacity-40"
              disabled={rejectDisabled}
              onClick={handleReject}
            >
              Reject
            </button>
            <button
              className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
              disabled={disabled}
              onClick={handleSubmit}
            >
              {submitting ? "Saving…" : "Submit"}
            </button>
          </div>
        </>
      )}
    </ViewContainer>
  );
}

function ConfirmedTechStackSummary({ content }: { content: unknown }) {
  const c = (content ?? {}) as {
    summary?: string;
    languages?: string[];
    frameworks?: string[];
  };
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-neutral-200 p-4">
      <div className="flex items-center gap-2">
        <span className="rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800">
          Confirmed
        </span>
      </div>
      {c.summary && <p className="text-sm text-neutral-700">{c.summary}</p>}
      {(c.languages?.length || c.frameworks?.length) && (
        <p className="text-xs text-neutral-500">
          {[...(c.languages ?? []), ...(c.frameworks ?? [])].join(" · ")}
        </p>
      )}
    </div>
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
