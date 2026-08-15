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

const WireframeSchema = z.object({
  screen: z.string(),
  html_source: z.string(),
});

const ImplementationPlanSchema = z.object({
  overview: z.string(),
  plan_steps: z.array(PlanStepSchema),
  risk_notes: z.array(z.string()),
  // Optional so envelopes from before wireframes existed still parse.
  wireframes: z.array(WireframeSchema).optional().default([]),
});

export type Specification = z.infer<typeof SpecificationSchema>;
export type ImplementationPlan = z.infer<typeof ImplementationPlanSchema>;

/** Safe parsers for rendering a stage's raw streamed `draft`/`approved_content` dict directly
 * (the pre-approval fallback when no A2UI surface message exists yet). Zod validation, not a
 * cast: a partial or clarifying-questions-only draft returns null instead of crashing `.map`. */
export function parseSpecification(data: unknown): Specification | null {
  const result = SpecificationSchema.safeParse(data);
  return result.success ? result.data : null;
}

export function parseImplementationPlan(data: unknown): ImplementationPlan | null {
  const result = ImplementationPlanSchema.safeParse(data);
  return result.success ? result.data : null;
}

// Audit findings are deliberately never rendered: the pipeline's design intent is that a human
// reviewer only ever sees final, gap-addressed content, never the intermediate list of what the
// audit pass found and fixed (see the plan's "Audit findings hidden from the user" requirement).
// Still accepted in props below (the backend still sends it in every envelope), just unused here.

export function SpecificationSurfaceRenderer({
  specification: spec,
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

      {(plan.wireframes ?? []).length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Wireframes</h4>
          <p className="mt-1 text-xs text-neutral-500">Click a thumbnail to open the full-size wireframe in a new tab.</p>
          <div className="mt-2 flex flex-wrap gap-3">
            {(plan.wireframes ?? []).map((wf) => (
              <WireframeThumbnail key={wf.screen} screen={wf.screen} htmlSource={wf.html_source} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Sandboxed thumbnail of a self-contained HTML wireframe. The empty `sandbox` attribute blocks
 * scripts and same-origin access; pointer-events are disabled on the iframe so the overlay
 * button gets the click. The gate's server-side denylist is hygiene only -- the sandbox
 * attribute here is the actual security boundary. Never rendered with dangerouslySetInnerHTML.
 *
 * Full-size view: a blob URL inherits the app's origin, so the wireframe HTML is NEVER the blob
 * document itself -- markup that slipped the gate would otherwise run app-origin script in the
 * new tab. Instead the blob is a trusted static shell whose only dynamic content is the
 * wireframe entity-escaped into a sandbox="" iframe srcdoc: same confinement as the thumbnail. */
function WireframeThumbnail({ screen, htmlSource }: { screen: string; htmlSource: string }) {
  function openFullSize() {
    const escaped = htmlSource.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    const shell =
      `<!doctype html><html><head><meta charset="utf-8"><title>${screen} wireframe</title>` +
      `<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%}</style></head>` +
      `<body><iframe sandbox="" srcdoc="${escaped}"></iframe></body></html>`;
    const url = URL.createObjectURL(new Blob([shell], { type: "text/html" }));
    window.open(url, "_blank", "noopener");
  }
  return (
    <button
      type="button"
      className="group relative block overflow-hidden rounded-lg border border-neutral-200 text-left hover:border-neutral-400"
      onClick={openFullSize}
      title={`Open ${screen} wireframe full-size`}
    >
      <iframe
        sandbox=""
        srcDoc={htmlSource}
        className="pointer-events-none h-44 w-72 origin-top-left"
        tabIndex={-1}
        aria-hidden
      />
      <span className="absolute inset-x-0 bottom-0 bg-neutral-900/70 px-2 py-1 font-mono text-xs text-white">
        {screen}
      </span>
    </button>
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
