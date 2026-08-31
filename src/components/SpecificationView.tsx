"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { parseSpecification, SpecificationSurfaceRenderer } from "@/a2ui/catalog";
import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { ViewContainer } from "@/components/ViewContainer";
import { SPECIFICATION_SURFACE_ID } from "@/lib/a2ui-surface-ids";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { WorkflowState } from "@/lib/workflow-types";

export function SpecificationView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { interrupt } = useOpenInterrupt();
  const state = (agent.state ?? {}) as WorkflowState;
  const stage = state.stages?.specification;

  // Pre-approval fallback: the A2UI surface message only exists after deterministic verify, and
  // vanishes on reload -- render the streamed draft (current review target; approved_content is
  // the PREVIOUS run's content) so the gate is never a blind approve. Last resort: the draft
  // carried inside a re-emitted gate interrupt (the only data available after a reload while the
  // gate is open).
  const draft =
    parseSpecification(stage?.draft) ??
    parseSpecification(stage?.approved_content) ??
    (interrupt.stage === "specification" ? parseSpecification(interrupt.draft) : null);

  // Same shell as the Tech Stack / Requirements tabs (user requirement 2026-08-31): header block
  // on top, content in one bounded 65vh box that scrolls internally -- the document itself is
  // read-only (approve/reject happens in the gate card above the tabs), so there is no editor
  // chrome, but the frame and height match.
  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Specification</h1>
        <p className="text-sm text-neutral-500">
          User stories and acceptance criteria drafted from your requirements, adversarially audited by a
          second model before reaching you. Review here; approve in the card above — or revise the
          requirements document on the Requirements tab and resubmit to redraft from it.
        </p>
      </div>

      {(stage?.audit_findings?.length ?? 0) > 0 && (
        <details className="rounded-lg border border-neutral-200 px-3 py-2 text-sm">
          <summary className="cursor-pointer text-neutral-700">
            Adversarial audit revised this draft — {stage!.audit_findings.length} finding(s) addressed
          </summary>
          <ul className="mt-1 list-inside list-disc text-xs text-neutral-600">
            {stage!.audit_findings.map((finding, index) => (
              <li key={index}>{finding}</li>
            ))}
          </ul>
        </details>
      )}
      <ClarifyingQuestions
        stageKey="specification"
        questions={stage?.clarifying_questions ?? []}
        hint="Answer by editing the requirements text on the Requirements tab, then resubmit."
      />
      <div className="h-[75vh] overflow-y-auto rounded-lg border border-neutral-300 p-3">
        <A2UISurfaceView
          surfaceId={SPECIFICATION_SURFACE_ID}
          fallback={
            draft ? (
              <SpecificationSurfaceRenderer specification={draft} />
            ) : (
              <p className="text-sm text-neutral-500">No Specification draft yet.</p>
            )
          }
        />
      </div>
    </ViewContainer>
  );
}
