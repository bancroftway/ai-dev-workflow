"use client";

import { useAgent, useAttachments } from "@copilotkit/react-core/v2";
import { useEffect, useRef, useState } from "react";
import { AttachmentEditor, SHARED_ATTACHMENTS_CONFIG } from "@/components/AttachmentEditor";
import { Spinner } from "@/components/Spinner";
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
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { interrupt } = useOpenInterrupt();
  const [sandboxStatus] = useSandboxStatus();

  const isOpen = interrupt.open && interrupt.stage === "tech-stack";
  const showDropdown = isOpen && interrupt.fileExisted === false;

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
  // sitting in the editor forever instead of showing the fresh redraft.
  useEffect(() => {
    if (isOpen) syncedRef.current = false;
  }, [isOpen]);

  // Prefill exactly once per gate occurrence from whatever the gate is showing -- never clobber
  // an active edit. A per-session draft copy (saved on every change below) takes precedence over
  // the gate's own markdown: any remount (tab switch, hot reload) resets this component's state,
  // and prefilling back to the gate's stub silently REPLACED a picked/edited stack -- observed
  // live 2026-08-31: the greenfield stub got submitted and approved instead of the user's
  // Angular+.NET pick. Same sessionStorage-degrades-silently rules as RequirementsView.
  useEffect(() => {
    if (syncedRef.current || !isOpen || typeof interrupt.draftMarkdown !== "string") return;
    let saved: string | null = null;
    try {
      saved = sessionStorage.getItem(`aidw:techstack-draft:${threadId}`);
    } catch {
      saved = null;
    }
    setText(saved || interrupt.draftMarkdown);
    syncedRef.current = true;
  }, [isOpen, interrupt.draftMarkdown, threadId]);

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
      interrupt.resolve?.({ markdown: text });
      // The submitted text is the stack of record now -- a stale draft copy must not resurrect
      // on the next session/gate against this thread.
      try {
        sessionStorage.removeItem(`aidw:techstack-draft:${threadId}`);
      } catch {
        // ignore
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
  const disabled = !isOpen || text.trim().length === 0 || submitting || sandboxStatus !== "ready";

  const state = (agent.state ?? {}) as WorkflowState;
  const stage = state.stages?.["tech-stack"];

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Tech Stack</h1>
        <p className="text-sm text-neutral-500">
          {isOpen
            ? "Pick a starting stack or review what was detected — the text below is fully editable either way, and whatever you submit becomes the stack of record."
            : "The technology stack this session builds against."}
        </p>
      </div>

      {/* Two different waits share this slot: before the gate the pipeline is detecting; after
          Submit it is saving (structured extraction + commit) -- calling the second one
          "Detecting" read as the app having lost the submission (user, 2026-08-31). ready_for_review
          with no open interrupt can only be the post-submit phase. */}
      {!isOpen && stage?.status !== "approved" && (
        <p className="flex items-center gap-2 text-sm text-neutral-500">
          <Spinner />
          {stage?.status === "ready_for_review"
            ? "Saving your tech stack — extracting the structured details every later stage builds on…"
            : "Detecting your tech stack…"}
        </p>
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

          <AttachmentEditor
            value={text}
            onChange={updateText}
            attachmentsApi={attachmentsApi}
            disabled={submitting}
            minHeightClassName="h-[63vh]"
            uploadError={uploadError}
          />

          <div className="flex items-center justify-end gap-3">
            {sandboxStatus !== "ready" && (
              <span className="text-xs text-neutral-500">Waiting for the dev-tool sandbox to finish starting…</span>
            )}
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
      {((c.languages?.length ?? 0) > 0 || (c.frameworks?.length ?? 0) > 0) && (
        <p className="text-xs text-neutral-500">
          {[...(c.languages ?? []), ...(c.frameworks ?? [])].join(" · ")}
        </p>
      )}
    </div>
  );
}

