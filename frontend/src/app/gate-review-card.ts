import { ChangeDetectionStrategy, Component, Injector, computed, effect, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import type { HumanInTheLoopToolCall, HumanInTheLoopToolRenderer } from '@copilotkit/angular';
import { A2uiBridgeService } from './a2ui-bridge.service';
import { GateReviewRequestArgs, GateReviewResponse, unwrapGateReviewRequest } from './gate-review';

/**
 * The sidebar's HITL decision card for the "SpecGate"/"PlanGate" tool calls. This is the *decision*
 * surface — Approve or request a revision; the spec/plan content itself is edited in the main area
 * (A2UI-rendered, fed by the same tool call's already-validated `a2ui` — see A2uiBridgeService).
 */
@Component({
  selector: 'app-gate-review-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, MatButtonModule, MatCardModule, MatFormFieldModule, MatInputModule],
  template: `
    @if (args(); as args) {
      <mat-card appearance="outlined">
        <mat-card-header>
          <mat-card-title>{{ args.gateId }} review</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          <p>Review the content on the main panel, then approve or request changes.</p>
          @if (args.clarifyingQuestions.length > 0) {
            <p>{{ args.clarifyingQuestions.length }} clarifying question(s) pending — answer them on the main panel.</p>
          }
          <mat-form-field appearance="outline" style="width: 100%">
            <mat-label>Revision notes (optional)</mat-label>
            <textarea matInput [(ngModel)]="freeformNote" rows="2"></textarea>
          </mat-form-field>
        </mat-card-content>
        <mat-card-actions align="end">
          <button matButton (click)="respond('Continue')" [disabled]="responded()">Continue</button>
          <button matButton="filled" (click)="respond('Approve')" [disabled]="responded()">Approve</button>
        </mat-card-actions>
      </mat-card>
    }
  `,
})
export class GateReviewCard implements HumanInTheLoopToolRenderer<GateReviewRequestArgs> {
  private readonly bridge = inject(A2uiBridgeService);
  private readonly injector = inject(Injector);

  readonly toolCall = input.required<HumanInTheLoopToolCall<GateReviewRequestArgs>>();

  protected readonly freeformNote = signal('');
  protected readonly responded = signal(false);

  // "executing" — args complete, awaiting our response, no result yet — is the correct
  // ready-for-review state (confirmed via a raw AG-UI SSE capture: our backend's RequestPort-based
  // gate has no TOOL_CALL_RESULT and RunFinished reports a plain "success" outcome, not the newer
  // interrupt outcome variant). "complete" means a result already exists, i.e. already resolved —
  // gating on that would mean the card only appears after it's too late to respond.
  protected readonly args = computed(() => {
    const call = this.toolCall();
    return call.status === 'in-progress' ? null : unwrapGateReviewRequest(call.args);
  });

  constructor() {
    effect(
      () => {
        const args = this.args();
        if (args) {
          this.bridge.process(args.a2ui as never);
        }
      },
      { injector: this.injector },
    );
  }

  protected respond(decision: 'Continue' | 'Approve'): void {
    const call = this.toolCall();
    if (call.status === 'in-progress') {
      return;
    }

    const response: GateReviewResponse = {
      decision,
      questionAnswers: null,
      freeformNote: this.freeformNote().trim() || null,
    };
    this.responded.set(true);
    call.respond(response);
  }
}
