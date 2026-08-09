import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { CopilotKit, injectAgentStore } from '@copilotkit/angular';
import { WorkflowGateStateService } from './workflow-gate-state.service';

/**
 * The only editable surface in the app: one evergreen requirements textbox. Submitting either
 * resolves whatever gate is currently pending (Continue, carrying this text — see
 * WorkflowGateStateService) or, if nothing is pending, sends a plain new message on the existing
 * AG-UI thread (the same mechanism CopilotKit's own chat input uses internally — see
 * submitInput/CopilotChatInput — reused directly here rather than driving the sidebar's text box,
 * since this tab is the actual input surface). Both paths converge on a fresh SpecAgent turn with
 * the full prior conversation available to it.
 */
@Component({
  selector: 'app-requirements-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, MatButtonModule, MatFormFieldModule, MatInputModule],
  template: `
    <mat-form-field appearance="outline" style="width: 100%">
      <mat-label>Project requirements</mat-label>
      <textarea
        matInput
        rows="14"
        [ngModel]="requirementsText()"
        (ngModelChange)="requirementsText.set($event)"
      ></textarea>
    </mat-form-field>

    @if (pendingQuestions(); as pending) {
      <div>
        <p>{{ pending.gateId }} needs more information before continuing:</p>
        <ul>
          @for (q of pending.clarifyingQuestions; track q.id) {
            <li>
              {{ q.question }}
              @if (q.choices && q.choices.length > 0) {
                <em> ({{ q.choices.join(', ') }})</em>
              }
            </li>
          }
        </ul>
      </div>
    }

    <button matButton="filled" (click)="submit()" [disabled]="submitting() || !requirementsText().trim()">Submit</button>
  `,
})
export class RequirementsTab {
  private readonly copilotKit = inject(CopilotKit);
  private readonly gateState = inject(WorkflowGateStateService);
  private readonly agentStore = injectAgentStore('default');

  protected readonly requirementsText = signal('');
  protected readonly submitting = signal(false);

  protected readonly pendingQuestions = computed(() => {
    const pending = this.gateState.pendingGate();
    if (!pending || pending.args.readyForApproval || pending.args.clarifyingQuestions.length === 0) {
      return null;
    }

    return { gateId: pending.args.gateId, clarifyingQuestions: pending.args.clarifyingQuestions };
  });

  protected submit(): void {
    const text = this.requirementsText().trim();
    if (!text || this.submitting()) {
      return;
    }

    // Deliberately not awaited: core.runAgent()'s promise only settles once the entire agentic
    // loop completes, including waiting for whatever HITL tool call it produces to be resolved —
    // which for a fresh submit is the very thing the human hasn't done yet. Awaiting it here would
    // leave the button (and this whole tab) disabled until Approve is eventually clicked. Dispatch
    // and move on; WorkflowGateStateService/A2uiBridgeService react to the stream as it arrives.
    this.submitting.set(true);
    try {
      const pending = this.gateState.pendingGate();
      if (pending) {
        pending.respond({ decision: 'Continue', outputJson: pending.args.outputJson, updatedRawRequirementsText: text });
        return;
      }

      const agent = this.agentStore().agent;
      agent.addMessage({ id: crypto.randomUUID(), role: 'user', content: text });
      void this.copilotKit.core.runAgent({ agent }).catch((error: unknown) => console.error('Agent run failed:', error));
    } finally {
      this.submitting.set(false);
    }
  }
}
