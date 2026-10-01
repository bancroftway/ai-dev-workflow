"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { createContext, useCallback, useContext, useState } from "react";
import { useGateFetch } from "@/components/GateButton";
import { useWorkflowThread } from "@/lib/workflow-thread-context";

// The open human review is server state (agent/src/review_view.py): GET /api/sessions/{id}/review
// builds it from the checkpoint's pending interrupt, POST resolves it and resumes the graph
// server-side. Nothing here depends on a live AG-UI stream, so a reload, an agent restart or a
// second tab all see the same review. This file knows no stage, copy or rule -- it fetches, posts
// an action id, and tracks the click.

export interface ReviewSegment {
  text: string;
  bold: boolean;
}

export interface ReviewAction {
  id: string;
  label: string;
  style: "primary" | "danger";
  /** The action posts the review's text field (reject feedback / tech-stack markdown). */
  needs_text: boolean;
  hint: string | null;
  enabled: boolean;
}

export interface ReviewVerification {
  title: string;
  items: { key: string; tone: "fail" | "warn"; label: string; detail: string | null }[];
  feedback: string | null;
}

/** GET /sessions/{id}/review. Closed reviews carry only open/id/stage/kind/requirements. */
export interface ReviewView {
  open: boolean;
  id: string | null;
  stage: string | null;
  kind: "approval" | "tech_stack" | "escalation" | null;
  /** Render the generic card above the tabs (false: the stage's own tab renders the review). */
  card?: boolean;
  tone?: "review" | "error";
  title?: string | null;
  body?: ReviewSegment[];
  details?: string | null;
  input?: { placeholder: string } | null;
  /** Why the actions are disabled right now (a run owns the thread, or no sandbox yet). */
  blocked?: string | null;
  actions?: ReviewAction[];
  tech_stack?: { subtitle: string; markdown: string; show_catalog: boolean; verification: ReviewVerification | null } | null;
  /** The Requirements tab while this review is open: the action its Submit posts (with the revised
   * document) to resolve this review -- null when Submit stays locked -- and the line it shows. */
  requirements: { resubmit_action: string | null; note: string } | null;
}

const CLOSED: ReviewView = { open: false, id: null, stage: null, kind: null, requirements: null };

export interface ReviewState {
  review: ReviewView;
  /** From the click until the server's review moves on (closes, or a new one opens). */
  submitting: boolean;
  /** The server's refusal of the last action on THIS review, verbatim. */
  error: string | null;
  /** POSTs an action; true when the agent accepted it (202). */
  submit: (actionId: string, text?: string) => Promise<boolean>;
}

/** Fetch + submit for the open review. Called once (AppShell) and shared via ReviewProvider. */
export function useReviewModel(): ReviewState {
  const { threadId, localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { copilotkit } = useCopilotKit();
  const url = `/api/sessions/${encodeURIComponent(threadId)}/review`;
  const review = useGateFetch<ReviewView>(url) ?? CLOSED;
  const [inFlight, setInFlight] = useState(false);
  const [acceptedFor, setAcceptedFor] = useState<string | null>(null);
  const [error, setError] = useState<{ reviewId: string | null; message: string } | null>(null);

  const submit = useCallback(
    async (actionId: string, text?: string) => {
      if (!review.open || !review.stage) return false;
      setInFlight(true);
      setError(null);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stage: review.stage, action_id: actionId, text }),
        });
        if (res.status !== 202) {
          const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
          setError({ reviewId: review.id, message: typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}` });
          return false;
        }
        setAcceptedFor(review.id);
        // Watch the run the resolve just started. attachOnly: the agent never STARTS a run for
        // this call (main.py run()) -- if that run already ended it just hands back state.
        copilotkit.runAgent({ agent, forwardedProps: { attachOnly: true } }).catch(() => {});
        return true;
      } catch (err) {
        setError({ reviewId: review.id, message: err instanceof Error ? err.message : String(err) });
        return false;
      } finally {
        setInFlight(false);
      }
    },
    [review.open, review.stage, review.id, url, copilotkit, agent],
  );

  return {
    review,
    submitting: inFlight || (review.open && acceptedFor != null && acceptedFor === review.id),
    error: error != null && error.reviewId === review.id ? error.message : null,
    submit,
  };
}

const ReviewContext = createContext<ReviewState>({ review: CLOSED, submitting: false, error: null, submit: async () => false });

export const ReviewProvider = ReviewContext.Provider;

export function useReview(): ReviewState {
  return useContext(ReviewContext);
}
