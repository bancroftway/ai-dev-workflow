import { z } from 'zod';

/** Mirrors AiDev.Workflow.Application.Common.Contracts.ClarifyingQuestion. */
export const ClarifyingQuestionSchema = z.object({
  id: z.string(),
  question: z.string(),
  choices: z.array(z.string()).nullable().optional(),
});

/**
 * Mirrors AiDev.Workflow.Application.Common.Contracts.GateReviewRequest — the *unwrapped* shape.
 * a2ui here is already validated/fallback-corrected by A2UiSchemaValidator server-side, unlike the
 * raw streamed assistant text.
 */
export const GateReviewRequestSchema = z
  .object({
    gateId: z.string(),
    contentSnapshot: z.string(),
    clarifyingQuestions: z.array(ClarifyingQuestionSchema),
    a2ui: z.array(z.record(z.string(), z.unknown())),
    readyForApproval: z.boolean(),
    outputJson: z.string(),
  })
  .catchall(z.unknown());

export type GateReviewRequestArgs = z.infer<typeof GateReviewRequestSchema>;

/**
 * The Microsoft Agent Framework wraps a RequestPort's typed request payload in this envelope when it
 * surfaces as a tool call's arguments — confirmed via a raw AG-UI SSE capture. Shape:
 * `{"data":{"typeId":{"assemblyName":...,"typeName":...},"value":{...the DTO...}}}`. This only applies
 * to the request side: the response side's RequestPort is typed `string` server-side (see
 * WorkflowPorts.cs) precisely because MAF's AG-UI hosting layer never deserializes a "tool" message's
 * content into a PortableValue on the way in, so a plain, unwrapped GateReviewResponse JSON object is
 * what the server actually expects back — see GateReviewResponse below.
 */
const PortableValueSchema = z.object({
  data: z.object({
    typeId: z.object({ assemblyName: z.string(), typeName: z.string() }).optional(),
    value: z.record(z.string(), z.unknown()),
  }),
});

export function unwrapGateReviewRequest(args: unknown): GateReviewRequestArgs | null {
  const wrapped = PortableValueSchema.safeParse(args);
  if (!wrapped.success) {
    return null;
  }

  const inner = GateReviewRequestSchema.safeParse(wrapped.data.data.value);
  return inner.success ? inner.data : null;
}

/** Mirrors AiDev.Workflow.Domain.Enums.GateDecision. */
export type GateDecision = 'Continue' | 'Approve';

/**
 * Mirrors AiDev.Workflow.Application.Common.Contracts.GateReviewResponse. outputJson must be
 * GateReviewRequestArgs.outputJson echoed back unchanged — the client never parses or constructs
 * it. updatedRawRequirementsText carries the current evergreen requirements text on Continue, and
 * is null on Approve.
 */
export interface GateReviewResponse {
  decision: GateDecision;
  outputJson: string;
  updatedRawRequirementsText: string | null;
}
