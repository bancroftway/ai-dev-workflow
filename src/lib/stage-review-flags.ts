/**
 * Shared derivation for SpecificationView/PlanView's isFinal/isProvisional split -- both views
 * duplicated this by hand and it's already caused two live bugs (see history below), so it's
 * pulled out once rather than re-copied a third time.
 *
 * isFinal: the ONLY authoritative "this is final and actionable" signal is THIS stage's own gate
 * interrupt being open -- anything shown before that (draft mid-audit, mid-verify, or a redraft in
 * flight after a rejection) is provisional.
 *
 * isProvisional: agent.isRunning OR'd with the durable run_active signal (Workflow Liveness Fix)
 * -- isRunning is stream-attachment only and resets to false on reload while the server may still
 * genuinely be auditing/redrafting this stage. stageStatus !== "approved" is required too (found
 * live: once a stage is approved and the next stage starts drafting, agent.isRunning stays true
 * with this stage's own interrupt closed, and without this check an already-approved stage would
 * blur again).
 */
export function deriveStageReviewFlags(opts: {
  stageKey: string;
  stageStatus: string | undefined;
  interruptOpen: boolean;
  interruptStage: string | undefined;
  agentIsRunning: boolean;
  runActive: boolean | undefined;
}): { isFinal: boolean; isProvisional: boolean } {
  const isFinal = opts.interruptOpen && opts.interruptStage === opts.stageKey;
  const isProvisional = (opts.agentIsRunning || opts.runActive === true) && opts.stageStatus !== "approved" && !isFinal;
  return { isFinal, isProvisional };
}
