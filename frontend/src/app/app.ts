import { Component } from '@angular/core';
import { CopilotSidebar } from '@copilotkit/angular';
import { SurfaceComponent } from '@a2ui/angular/v0_9';

@Component({
  selector: 'app-root',
  imports: [CopilotSidebar, SurfaceComponent],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {}
