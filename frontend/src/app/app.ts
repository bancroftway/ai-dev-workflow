import { Component, inject } from '@angular/core';
import { MatTabsModule } from '@angular/material/tabs';
import { CopilotSidebar } from '@copilotkit/angular';
import { SurfaceComponent } from '@a2ui/angular/v0_9';
import { RequirementsTab } from './requirements-tab';
import { WorkflowGateStateService } from './workflow-gate-state.service';

@Component({
  selector: 'app-root',
  imports: [CopilotSidebar, SurfaceComponent, MatTabsModule, RequirementsTab],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  protected readonly gateState = inject(WorkflowGateStateService);
}
