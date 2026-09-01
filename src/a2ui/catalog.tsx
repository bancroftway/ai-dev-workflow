import { createCatalog } from "@copilotkit/a2ui-renderer";
import type { ReactElement } from "react";
import { z } from "zod";
import { CATALOG_ID } from "@/lib/a2ui-surface-ids";
import { MermaidDiagram } from "@/components/MermaidDiagram";

export { CATALOG_ID };

// Mirrors agent/src/schemas.py field-for-field (SPECIFICATION.md Section 4).
// Field names are snake_case to match the Pydantic JSON dump verbatim.
// Per-run scope lifecycle, stamped by the spec verify (graph.py) from the id ledger. Optional so
// pre-stamp envelopes still parse.
const ChangeSchema = z.enum(["new", "modified", "deleted", "unchanged", "deferred", "activated"]).optional();

const AcceptanceCriterionSchema = z.object({
  id: z.string(),
  description: z.string(),
  change: ChangeSchema,
  // Parked for a later phase (specified, reviewed, not built this ticket) -- optional so
  // envelopes from before deferred scope existed still parse.
  deferred: z.boolean().optional().default(false),
  // Whether this criterion has a visible user-facing surface (user requirement 2026-09-01) --
  // the Plan gate demands a wireframe cite every live one of these; optional for the same
  // before-this-field-existed reason as `deferred`.
  ui_related: z.boolean().optional().default(false),
});

const UserStorySchema = z.object({
  id: z.string(),
  title: z.string(),
  narrative: z.string(),
  acceptance_criteria: z.array(AcceptanceCriterionSchema),
  change: ChangeSchema,
  deferred: z.boolean().optional().default(false),
});

const RetiredStorySchema = z.object({ id: z.string(), title: z.string() });
const RetiredCriterionSchema = z.object({ id: z.string(), description: z.string(), parent_us_id: z.string() });

const SpecQuestionSchema = z.object({
  id: z.string(),
  question: z.string(),
  status: z.enum(["open", "answered", "assumed"]),
  answer: z.string().optional().default(""),
  suggested_choices: z.array(z.string()).optional().default([]),
});

const SpecificationSchema = z.object({
  title: z.string(),
  summary: z.string(),
  user_stories: z.array(UserStorySchema),
  assumptions: z.array(z.string()),
  out_of_scope: z.array(z.string()),
  // Optional so envelopes from before the question ledger existed still parse.
  questions: z.array(SpecQuestionSchema).optional().default([]),
  retired_user_stories: z.array(RetiredStorySchema).optional().default([]),
  retired_acceptance_criteria: z.array(RetiredCriterionSchema).optional().default([]),
});

const PlanStepSchema = z.object({
  id: z.string(),
  description: z.string(),
  // Optional so envelopes from before plan lifecycle badges existed still parse. `change` is
  // stamped deterministically by the plan verify (strongest change among the criteria the step
  // fulfils, from the approved spec's own per-AC stamps); `removes_ids` is the model's explicit
  // declaration of which retired scope's delivered artifacts this step deletes (gate-validated);
  // `ui_related` is a display-only tag (not gate-enforced) for whether this step has a visible
  // surface.
  kind: z.enum(["feature", "infrastructure"]).optional().default("feature"),
  ac_ids: z.array(z.string()).optional().default([]),
  removes_ids: z.array(z.string()).optional().default([]),
  change: ChangeSchema.nullable().optional(),
  ui_related: z.boolean().optional().default(false),
});

const WireframeSchema = z.object({
  screen: z.string(),
  html_source: z.string(),
  // Which Acceptance Criteria this screen is evidence for (user requirement 2026-08-31) --
  // optional so envelopes from before this field existed still parse.
  ac_ids: z.array(z.string()).optional().default([]),
});

// Rendered client-side from `mermaid_source` (mermaid.js, same engine the backend's mmdc CLI
// uses to validate syntax at verify time) -- self-contained like a wireframe's inline HTML, no
// fetch/proxy/push-state dependency: the diagram always reflects exactly the draft being
// reviewed, committed or not. An earlier version fetched the server-rendered SVG through the
// raw-content proxy instead; that meant a not-yet-pushed commit 404'd (found live 2026-09-01,
// right after this exact feature shipped) and the diagram simply never appeared.
const PlanDiagramSchema = z.object({
  name: z.string(),
  kind: z.string(),
  mermaid_source: z.string().optional().default(""),
});

const ImplementationPlanSchema = z.object({
  overview: z.string(),
  plan_steps: z.array(PlanStepSchema),
  risk_notes: z.array(z.string()),
  // Optional so envelopes from before wireframes/diagrams existed still parse.
  wireframes: z.array(WireframeSchema).optional().default([]),
  diagrams: z.array(PlanDiagramSchema).optional().default([]),
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

/** Real ledger ids only (user, 2026-08-31): a draft mid-verify still carries the model's own
 * placeholder ids ("n-us7", "story-a") -- rendering those read as a labeling scheme. The ledger
 * overwrites them with US-#### ids at verify; until then the id chip is simply omitted. */
function realIdOrNull(id: string): string | null {
  return /^US-\d{4}(\.\d+)?$/.test(id) ? id : null;
}

/** Scope-lifecycle chip (user requirement 2026-08-31): 'new' and 'updated' get loud colors;
 * 'deleted' items render crossed-out via the retired lists instead of a chip. 'deferred'/
 * 'activated' (2026-08-31): parked scope shows a quiet slate chip whether or not it changed this
 * run (its parked-ness is the fact the reviewer needs); a promotion this run gets a loud
 * 'activated'.
 *
 * 'unchanged' USED to render nothing at all ("the common case must not add noise") -- flagged
 * live, twice, as a "critical, persistent bug": on a second-or-later revision, every genuinely
 * untouched story/AC went silent next to the few that changed, and blank space next to a real
 * badge on a sibling item reads as broken tracking, not as "nothing to report." Verified against
 * the raw draft JSON both times: the `change` field was correctly computed as "unchanged" in
 * every case -- this was a rendering choice, not a stamping bug. Every live item now always
 * carries a visible, low-contrast chip so the reviewer never has to wonder whether a blank space
 * means "unchanged" or "the tracking broke." */
function ChangeBadge({
  change,
  deferred,
}: {
  change?: "new" | "modified" | "deleted" | "unchanged" | "deferred" | "activated";
  deferred?: boolean;
}) {
  if (deferred || change === "deferred") {
    return <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 text-xs font-normal text-slate-700">deferred</span>;
  }
  if (change === "activated") {
    return (
      <span className="ml-1.5 rounded-full bg-emerald-100 px-1.5 text-xs font-normal text-emerald-800">activated</span>
    );
  }
  if (change === "new") {
    return <span className="ml-1.5 rounded-full bg-emerald-100 px-1.5 text-xs font-normal text-emerald-800">new</span>;
  }
  if (change === "modified") {
    return <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 text-xs font-normal text-amber-800">updated</span>;
  }
  return <span className="ml-1.5 rounded-full bg-neutral-100 px-1.5 text-xs font-normal text-neutral-400">unchanged</span>;
}

/** Shared "has a visible surface" marker (user requirement 2026-09-01) -- same chip on a
 * Specification AC and the Plan step(s) that fulfil it, so the tag reads as one continuous fact
 * as the reviewer moves between tabs, not two different indicators that happen to say "UI". */
function UiChip() {
  return <span className="ml-1.5 rounded-full bg-indigo-100 px-1.5 font-sans text-xs font-normal text-indigo-800">UI</span>;
}

export function SpecificationSurfaceRenderer({
  specification: spec,
}: {
  specification: Specification;
  auditFindings?: string[];
}) {
  const openQuestions = (spec.questions ?? []).filter((q) => q.status === "open");
  // Split by WHO resolved them (user, 2026-08-31): "Resolved questions" read as if a person had
  // answered -- 'answered' means the requirements document settled it, 'assumed' means the agent
  // picked a default itself (overridable by editing the requirements).
  const answeredQuestions = (spec.questions ?? []).filter((q) => q.status === "answered");
  const assumedQuestions = (spec.questions ?? []).filter((q) => q.status === "assumed");

  // Compact layout (user, 2026-08-31): id + title share one line, halved paddings/gaps -- the
  // airy original spent half the review box on whitespace.
  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-xl font-semibold">{spec.title}</h2>
        <p className="text-neutral-600">{spec.summary}</p>
      </div>

      {/* Question ledger (user requirement 2026-08-31): open questions on top in amber -- these
          pause the pipeline until answered in the requirements document; resolved history stays
          reviewable underneath, each answer tracing back to the doc or an explicit assumption. */}
      {openQuestions.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2">
          <p className="text-sm font-medium text-amber-900">
            Open questions — answer these in the Requirements document, then resubmit:
          </p>
          <ul className="mt-1 space-y-1">
            {openQuestions.map((q) => (
              <li key={q.id} className="text-sm text-amber-900">
                <span className="mr-1 font-mono text-xs text-amber-700">{q.id}</span>
                {q.question}
                {q.suggested_choices.length > 0 && (
                  <span className="text-xs text-amber-700"> (e.g. {q.suggested_choices.join(" / ")})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {answeredQuestions.length > 0 && (
        <details className="rounded-lg border border-neutral-200 px-3 py-2 text-sm">
          <summary className="cursor-pointer text-neutral-700">
            Answered from your requirements ({answeredQuestions.length})
          </summary>
          <ul className="mt-1 space-y-1">
            {answeredQuestions.map((q) => (
              <li key={q.id} className="text-sm text-neutral-700">
                <span className="mr-1 font-mono text-xs text-neutral-500">{q.id}</span>
                {q.question}
                {q.answer && <span className="block pl-4 text-xs text-neutral-500">↳ {q.answer}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {assumedQuestions.length > 0 && (
        <details className="rounded-lg border border-neutral-200 px-3 py-2 text-sm">
          <summary className="cursor-pointer text-neutral-700">
            Assumed by the agent ({assumedQuestions.length}) — override any by stating your choice in the
            Requirements document and resubmitting
          </summary>
          <ul className="mt-1 space-y-1">
            {assumedQuestions.map((q) => (
              <li key={q.id} className="text-sm text-neutral-700">
                <span className="mr-1 font-mono text-xs text-neutral-500">{q.id}</span>
                {q.question}
                {q.answer && <span className="block pl-4 text-xs text-neutral-500">↳ {q.answer}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="space-y-2">
        {spec.user_stories.map((story) => {
          const retiredAcsHere = (spec.retired_acceptance_criteria ?? []).filter(
            (ac) => ac.parent_us_id === story.id,
          );
          return (
            // Deferred stories (parked, NOT removed): muted but fully readable -- deliberately
            // distinct from the retired cards' dashed cross-out below (user, 2026-08-31: a
            // deferred feature rendering as deleted read as data loss).
            <div
              key={story.id}
              className={`rounded-lg border px-3 py-2 ${story.deferred ? "border-slate-200 bg-slate-50" : "border-neutral-200"}`}
            >
              <h3 className={`font-medium ${story.deferred ? "text-neutral-500" : ""}`}>
                {realIdOrNull(story.id) && (
                  <span className="mr-2 font-mono text-xs font-normal text-neutral-500">{story.id}</span>
                )}
                {story.title}
                <ChangeBadge change={story.change} deferred={story.deferred} />
              </h3>
              <p className={`text-sm ${story.deferred ? "text-neutral-500" : "text-neutral-700"}`}>{story.narrative}</p>
              {story.deferred && (
                <p className="text-xs text-slate-500">
                  Deferred — specified now, built in a later phase. To build it, move it into the
                  build-now scope in the Requirements document and resubmit.
                </p>
              )}
              {(story.acceptance_criteria.length > 0 || retiredAcsHere.length > 0) && (
                <ul className="mt-1">
                  {story.acceptance_criteria.map((ac) => (
                    <li key={ac.id} className={`text-sm ${ac.deferred || story.deferred ? "text-neutral-500" : ""}`}>
                      {realIdOrNull(ac.id) && (
                        <span className="mr-1 font-mono text-xs text-neutral-500">{ac.id}</span>
                      )}
                      {ac.description}
                      <ChangeBadge change={ac.change} deferred={ac.deferred && !story.deferred} />
                      {ac.ui_related && <UiChip />}
                    </li>
                  ))}
                  {retiredAcsHere.map((ac) => (
                    <li key={ac.id} className="text-sm text-neutral-400 line-through">
                      <span className="mr-1 font-mono text-xs">{ac.id}</span>
                      {ac.description}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
        {(spec.retired_user_stories ?? []).map((story) => (
          <div key={story.id} className="rounded-lg border border-dashed border-neutral-300 px-3 py-2 opacity-70">
            <h3 className="font-medium text-neutral-400 line-through">
              <span className="mr-2 font-mono text-xs font-normal">{story.id}</span>
              {story.title}
            </h3>
            <p className="text-xs text-neutral-400">Removed from scope — retired in the requirements revision.</p>
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

/** .ai-dev-workflow/plan/diagrams/<name>.svg -- the exact path agent/src/gates/diagram_gate.py's
 * DIAGRAMS_DIR writes to and commits -- but display renders `mermaid_source` directly via
 * MermaidDiagram (client-side, no fetch), so that file only ever matters for someone browsing
 * the repo/branch directly, not for this view. */

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
        {plan.plan_steps.map((step) => {
          const removes = step.removes_ids ?? [];
          return (
            // Removal steps get a red-tinted card (user, 2026-08-31): deleting delivered scope is
            // the change a reviewer most needs to notice before approving.
            <li
              key={step.id}
              className={`rounded-lg border p-4 ${removes.length > 0 ? "border-red-200 bg-red-50/50" : "border-neutral-200"}`}
            >
              <div className="flex items-center font-mono text-xs text-neutral-500">
                {step.id}
                <ChangeBadge change={step.change ?? undefined} />
                {step.ui_related && <UiChip />}
                {removes.length > 0 && (
                  <span className="ml-1.5 rounded-full bg-red-100 px-1.5 font-sans text-xs font-normal text-red-800">
                    removes {removes.join(", ")}
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-neutral-700">{step.description}</p>
              {(step.ac_ids ?? []).length > 0 && (
                <p className="mt-1 font-mono text-xs text-neutral-400">fulfils {(step.ac_ids ?? []).join(", ")}</p>
              )}
            </li>
          );
        })}
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

      {/* Diagrams (user, 2026-08-31, found live: the audit confirmed a Mermaid diagram was
          syntactically valid, but nothing in this view ever rendered `plan.diagrams` at all --
          confirmed via the sandbox that the file genuinely exists, purely a missing surface).
          Rendered client-side from `mermaid_source` (user, 2026-09-01) -- an earlier version
          fetched the backend's rendered SVG through the raw-content proxy, which 404'd for any
          diagram not yet PUSHED to the branch (found live within minutes of shipping it) and
          could lag a commit behind the draft on screen either way. */}
      {(plan.diagrams ?? []).length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Diagrams</h4>
          <div className="mt-2 flex flex-wrap gap-4">
            {(plan.diagrams ?? []).map((d) => (
              <div key={d.name} className="rounded-lg border border-neutral-200 p-2">
                <p className="mb-1 font-mono text-xs text-neutral-500">
                  {d.name} <span className="text-neutral-400">({d.kind})</span>
                </p>
                <MermaidDiagram source={d.mermaid_source} name={d.name} />
              </div>
            ))}
          </div>
        </div>
      )}

      {(plan.wireframes ?? []).length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Wireframes</h4>
          <p className="mt-1 text-xs text-neutral-500">Click a thumbnail to open the full-size wireframe in a new tab.</p>
          <div className="mt-2 flex flex-wrap gap-3">
            {(plan.wireframes ?? []).map((wf) => (
              <WireframeThumbnail key={wf.screen} screen={wf.screen} htmlSource={wf.html_source} acIds={wf.ac_ids} />
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
function WireframeThumbnail({
  screen,
  htmlSource,
  acIds,
}: {
  screen: string;
  htmlSource: string;
  /** Acceptance Criteria this screen is evidence for (user requirement 2026-08-31) -- rendered
   * below the thumbnail, same "fulfils …" convention as a plan step's own ac_ids. */
  acIds?: string[];
}) {
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
    <div className="flex flex-col gap-1">
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
      {(acIds ?? []).length > 0 && (
        <p className="font-mono text-xs text-neutral-400">fulfils {(acIds ?? []).join(", ")}</p>
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
