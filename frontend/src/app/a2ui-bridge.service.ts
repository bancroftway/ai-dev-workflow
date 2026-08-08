import { Injectable, inject } from '@angular/core';
import { A2uiRendererService } from '@a2ui/angular/v0_9';
import type { A2uiMessage } from '@a2ui/web_core/v0_9';

/**
 * Forwards validated A2UI envelopes to the renderer. Content only reaches here from the
 * "SpecGate"/"PlanGate" tool-call arguments (see GateReviewCard) — that's the content
 * A2UiSchemaValidator has already validated or replaced with the fallback card server-side. The
 * raw streamed assistant text is deliberately NOT used as a source: it's the model's unvalidated
 * output, and A2UI's own client-side MessageProcessor throws (not just logs) on malformed content.
 *
 * Each turn resends a full self-contained snapshot (including its own `createSurface`), and this
 * can be called more than once for the same content (e.g. on change-detection re-runs) — the A2UI
 * client throws on a `createSurface` for a surface that already exists, so this tracks which
 * surfaceIds have already been created and strips redundant `createSurface` entries.
 */
@Injectable({ providedIn: 'root' })
export class A2uiBridgeService {
  private readonly renderer = inject(A2uiRendererService);
  private readonly createdSurfaceIds = new Set<string>();

  process(messages: readonly A2uiMessage[]): void {
    this.renderer.processMessages(this.dedupeCreateSurface(messages));
  }

  private dedupeCreateSurface(messages: readonly A2uiMessage[]): A2uiMessage[] {
    return messages.filter((message) => {
      if (!('createSurface' in message)) {
        return true;
      }

      const { surfaceId } = message.createSurface;
      if (this.createdSurfaceIds.has(surfaceId)) {
        return false;
      }

      this.createdSurfaceIds.add(surfaceId);
      return true;
    });
  }
}
