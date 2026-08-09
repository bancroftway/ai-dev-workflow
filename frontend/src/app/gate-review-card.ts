import { ChangeDetectionStrategy, Component, Injector, computed, effect, inject, input } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import type { HumanInTheLoopToolCall, HumanInTheLoopToolRenderer } from '@copilotkit/angular';
import { A2uiBridgeService } from './a2ui-bridge.service';
import { GateReviewRequestArgs, GateReviewResponse, unwrapGateReviewRequest } from './gate-review';
import { WorkflowGateStateService } from './workflow-gate-state.service';

/**
 * The sidebar's HITL decision card for the "SpecGate"/"PlanGate" tool calls. This is the *decision*
 * surface — Approve only; requesting a change is done by editing the Requirements tab and
 * submitting, not from here. The spec/plan content itself renders in the main area (A2UI-rendered,
 * fed by the same tool call's already-validated `a2ui` — see A2uiBridgeService).
 */
@Component({
  selector: 'app-gate-review-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatButtonModule, MatCardModule],
  template: `
    @if (args(); as args) {
      <mat-card appearance="outlined">
        <mat-card-header>
          <mat-card-title>{{ args.gateId }} review</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          @if (args.readyForApproval) {
            <p>Review the content on the main panel, then approve — or edit the requirements and submit again.</p>
          } @else {
            <p>More info needed — see the Requirements tab.</p>
          }
        </mat-card-content>
        @if (args.readyForApproval && !resolved()) {
          <mat-card-actions align="end">
            <button matButton="filled" (click)="approve()">Approve</button>
          </mat-card-actions>
        }
      </mat-card>
    }
  `,
})
export class GateReviewCard implements HumanInTheLoopToolRenderer<GateReviewRequestArgs> {
  private readonly bridge = inject(A2uiBridgeService);
  private readonly gateState = inject(WorkflowGateStateService);
  private readonly injector = inject(Injector);

  readonly toolCall = input.required<HumanInTheLoopToolCall<GateReviewRequestArgs>>();

  // "executing" — args complete, awaiting our response, no result yet — is the correct
  // ready-for-review state (confirmed via a raw AG-UI SSE capture: our backend's RequestPort-based
  // gate has no TOOL_CALL_RESULT and RunFinished reports a plain "success" outcome, not the newer
  // interrupt outcome variant). "complete" means a result already exists, i.e. already resolved —
  // gating on that would mean the card only appears after it's too late to respond.
  protected readonly args = computed(() => {
    const call = this.toolCall();
    return call.status === 'in-progress' ? null : unwrapGateReviewRequest(call.args);
  });

  protected readonly resolved = computed(() => this.toolCall().status === 'complete');

  constructor() {
    effect(
      () => {
        const args = this.args();
        if (!args) {
          return;
        }

        this.bridge.process(args.a2ui as never);
        this.gateState.reportGate(args, this.resolved(), (response) => this.toolCall().respond(response));
      },
      { injector: this.injector },
    );
  }

  protected approve(): void {
    const args = this.args();
    if (!args || this.resolved()) {
      return;
    }

    const response: GateReviewResponse = {
      decision: 'Approve',
      outputJson: args.outputJson,
      updatedRawRequirementsText: null,
    };
    this.toolCall().respond(response);
  }
}
