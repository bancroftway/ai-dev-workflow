import { ApplicationConfig, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideCopilotKit } from '@copilotkit/angular';
import { HttpAgent } from '@ag-ui/client';
import { provideA2Ui, BasicCatalog, provideMarkdownRenderer } from '@a2ui/angular/v0_9';
import { environment } from '../environments/environment';
import { GateReviewCard } from './gate-review-card';
import { GateReviewRequestSchema } from './gate-review';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideCopilotKit({
      agents: {
        default: new HttpAgent({ url: environment.apiBaseUrl }),
      },
      // SpecGate/PlanGate are RequestPort tool calls emitted by the backend workflow (see
      // WorkflowPorts.cs), not agent-declared tools — this just tells the sidebar how to render and
      // resolve them, using the same content the backend's A2UiSchemaValidator has already validated.
      humanInTheLoop: [
        {
          name: 'SpecGate',
          description: 'Human review of the draft specification.',
          parameters: GateReviewRequestSchema,
          component: GateReviewCard,
        },
        {
          name: 'PlanGate',
          description: 'Human review of the draft implementation plan.',
          parameters: GateReviewRequestSchema,
          component: GateReviewCard,
        },
      ],
    }),
    // M6 scaffold uses the stock BasicCatalog (plain HTML rendering) to prove the pipeline end to
    // end; M7 replaces this with a Material-backed AngularCatalog implementing the same component
    // Apis (TextApi, ButtonApi, CardApi, ...), swapped in here without touching anything else.
    provideA2Ui(() => ({ catalogs: [new BasicCatalog()] })),
    // BasicCatalog's Text component renders its `text` property as Markdown and requires this
    // provider to be present, even with the default renderer.
    provideMarkdownRenderer(),
  ],
};
