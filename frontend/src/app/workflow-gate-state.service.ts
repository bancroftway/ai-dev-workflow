import { Injectable, computed, signal } from '@angular/core';
import { GateReviewRequestArgs, GateReviewResponse } from './gate-review';

export interface PendingGate {
  readonly args: GateReviewRequestArgs;
  readonly respond: (response: GateReviewResponse) => void;
}

/**
 * Shared state bridging the sidebar's per-tool-call GateReviewCard instances with the Requirements/
 * Spec/Plan tabs, which have no direct access to CopilotKit's tool-call objects. `specGate`/
 * `planGate` are "sticky" — they hold the latest args for that gate even after it resolves, so the
 * Spec/Plan tabs keep showing content once approved. `pendingGate` holds whichever one (if any) is
 * still awaiting a response — the Requirements tab's Submit action resolves it directly.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowGateStateService {
  readonly specGate = signal<GateReviewRequestArgs | null>(null);
  readonly planGate = signal<GateReviewRequestArgs | null>(null);
  readonly pendingGate = signal<PendingGate | null>(null);

  readonly specReady = computed(() => this.specGate()?.readyForApproval === true);
  readonly planReady = computed(() => this.planGate()?.readyForApproval === true);

  // outputJson values that have already resolved. CopilotKit can render a second
  // GateReviewCard instance carrying byte-identical args for a call that was already resolved
  // (observed live: an already-'complete' card coexists briefly with a fresh 'executing' one that
  // reports the exact same content) — without this guard, that ghost instance's "not resolved"
  // report would clobber pendingGate right after the genuinely new call sets it.
  private readonly resolvedOutputs = new Set<string>();

  reportGate(args: GateReviewRequestArgs, isResolved: boolean, respond: (response: GateReviewResponse) => void): void {
    if (isResolved) {
      this.resolvedOutputs.add(args.outputJson);
      this.setStickyGate(args);
      // A card's effect re-fires once its own toolCall.status flips to 'complete', which happens
      // asynchronously and can land *after* the next gate call already became pending (e.g. this
      // gate's own Continue loop-back produced a new call before CopilotKit got around to marking
      // this one 'complete'). Only clear pendingGate if it's still pointing at *this* call —
      // outputJson is unique per turn (it's that turn's full serialized LLM output) — so an old,
      // late-resolving card can never clobber a newer one that's already pending.
      if (this.pendingGate()?.args.outputJson === args.outputJson) {
        this.pendingGate.set(null);
      }
      return;
    }

    if (this.resolvedOutputs.has(args.outputJson)) {
      // A ghost/duplicate render of an already-resolved call (see class doc). Previously this still
      // fell through to overwrite the sticky specGate/planGate signal below even though it was
      // correctly excluded from pendingGate — observed live: the sidebar card showed the real,
      // ready-for-approval content (its own local args()), but the Spec/Plan tab stayed disabled,
      // because a stale ghost's "not ready" report clobbered the tab-enablement signal *after* the
      // real one had already set it. Must bail out completely, before touching either signal.
      return;
    }

    this.setStickyGate(args);
    this.pendingGate.set({ args, respond });
  }

  private setStickyGate(args: GateReviewRequestArgs): void {
    if (args.gateId === 'SpecGate') {
      this.specGate.set(args);
    } else if (args.gateId === 'PlanGate') {
      this.planGate.set(args);
    }
  }
}
