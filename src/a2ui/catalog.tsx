import { createCatalog } from "@copilotkit/a2ui-renderer";
import type { ReactElement } from "react";
import { z } from "zod";
import { CATALOG_ID } from "@/lib/a2ui-surface-ids";

export { CATALOG_ID };

// Mirrors agent/src/schemas.py field-for-field (SPECIFICATION.md Section 4).
// Field names are snake_case to match the Pydantic JSON dump verbatim.
const AcceptanceCriterionSchema = z.object({
  id: z.string(),
  description: z.string(),
});

const UserStorySchema = z.object({
  id: z.string(),
  title: z.string(),
  narrative: z.string(),
  acceptance_criteria: z.array(AcceptanceCriterionSchema),
});

const SpecificationSchema = z.object({
  title: z.string(),
  summary: z.string(),
  user_stories: z.array(UserStorySchema),
  assumptions: z.array(z.string()),
  out_of_scope: z.array(z.string()),
});

const PlanStepSchema = z.object({
  id: z.string(),
  description: z.string(),
});

const ImplementationPlanSchema = z.object({
  overview: z.string(),
  plan_steps: z.array(PlanStepSchema),
  risk_notes: z.array(z.string()),
});

type Specification = z.infer<typeof SpecificationSchema>;
type ImplementationPlan = z.infer<typeof ImplementationPlanSchema>;

function AuditNotes({ findings }: { findings: string[] }) {
  if (findings.length === 0) return null;
  return (
    <div className="rounded-lg border border-sky-200 bg-sky-50 p-4">
      <h4 className="text-sm font-medium text-sky-900">Audit Notes</h4>
      <p className="mt-0.5 text-xs text-sky-700">
        Gaps a second, independently-configured model found and revised before this draft reached you.
      </p>
      <ul className="mt-2 list-inside list-disc space-y-1 text-sm text-sky-900">
        {findings.map((finding, index) => (
          <li key={index}>{finding}</li>
        ))}
      </ul>
    </div>
  );
}

export function SpecificationSurfaceRenderer({
  specification: spec,
  auditFindings = [],
}: {
  specification: Specification;
  auditFindings?: string[];
}) {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">{spec.title}</h2>
        <p className="mt-1 text-neutral-600">{spec.summary}</p>
      </div>

      <AuditNotes findings={auditFindings} />

      <div className="space-y-4">
        {spec.user_stories.map((story) => (
          <div key={story.id} className="rounded-lg border border-neutral-200 p-4">
            <div className="font-mono text-xs text-neutral-500">{story.id}</div>
            <h3 className="font-medium">{story.title}</h3>
            <p className="mt-1 text-sm text-neutral-700">{story.narrative}</p>
            {story.acceptance_criteria.length > 0 && (
              <ul className="mt-2 space-y-1">
                {story.acceptance_criteria.map((ac) => (
                  <li key={ac.id} className="text-sm">
                    <span className="mr-1 font-mono text-xs text-neutral-500">{ac.id}</span>
                    {ac.description}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>

      {spec.assumptions.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Assumptions</h4>
          <ul className="list-inside list-disc text-sm text-neutral-700">
            {spec.assumptions.map((assumption, index) => (
              <li key={index}>{assumption}</li>
            ))}
          </ul>
        </div>
      )}

      {spec.out_of_scope.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Out of Scope</h4>
          <ul className="list-inside list-disc text-sm text-neutral-700">
            {spec.out_of_scope.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function PlanSurfaceRenderer({
  plan,
  auditFindings = [],
}: {
  plan: ImplementationPlan;
  auditFindings?: string[];
}) {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">Implementation Plan</h2>
        <p className="mt-1 text-neutral-600">{plan.overview}</p>
      </div>

      <AuditNotes findings={auditFindings} />

      <ol className="space-y-3">
        {plan.plan_steps.map((step) => (
          <li key={step.id} className="rounded-lg border border-neutral-200 p-4">
            <div className="font-mono text-xs text-neutral-500">{step.id}</div>
            <p className="mt-1 text-sm text-neutral-700">{step.description}</p>
          </li>
        ))}
      </ol>

      {plan.risk_notes.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Risk Notes</h4>
          <ul className="list-inside list-disc text-sm text-neutral-700">
            {plan.risk_notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

const definitions = {
  SpecificationSurface: {
    description: "Read-only rendering of the current Specification draft.",
    props: z.object({ specification: SpecificationSchema, audit_findings: z.array(z.string()) }),
  },
  PlanSurface: {
    description: "Read-only rendering of the current Implementation Plan draft.",
    props: z.object({ plan: ImplementationPlanSchema, audit_findings: z.array(z.string()) }),
  },
};

// Registered with CopilotKit's A2UI provider/catalog machinery for protocol
// compliance (Section 3.1/3.3). Live rendering currently goes through
// `SURFACE_COMPONENT_MAP` below — see src/components/A2UISurfaceView.tsx for
// why: the installed ag-ui-langgraph/CopilotRuntime version delivers this
// system's programmatically-appended tool result as a MESSAGES_SNAPSHOT
// rather than a live TOOL_CALL_RESULT stream event, which
// @ag-ui/a2ui-middleware's event-stream-only detection does not scan. Both
// paths render the exact same component functions.
export const catalog = createCatalog(
  definitions,
  {
    SpecificationSurface: ({ props }) => (
      <SpecificationSurfaceRenderer specification={props.specification} auditFindings={props.audit_findings} />
    ),
    PlanSurface: ({ props }) => <PlanSurfaceRenderer plan={props.plan} auditFindings={props.audit_findings} />,
  },
  { catalogId: CATALOG_ID, includeBasicCatalog: false },
);

interface SpecificationSurfaceData {
  specification: Specification;
  audit_findings: string[];
}
interface PlanSurfaceData {
  plan: ImplementationPlan;
  audit_findings: string[];
}

export const SURFACE_COMPONENT_MAP: Record<string, (data: unknown) => ReactElement> = {
  SpecificationSurface: (data) => {
    const { specification, audit_findings } = data as SpecificationSurfaceData;
    return <SpecificationSurfaceRenderer specification={specification} auditFindings={audit_findings} />;
  },
  PlanSurface: (data) => {
    const { plan, audit_findings } = data as PlanSurfaceData;
    return <PlanSurfaceRenderer plan={plan} auditFindings={audit_findings} />;
  },
};
