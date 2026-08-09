"use client";

import { UseAgentUpdate, useAgent } from "@copilotkit/react-core/v2";
import type { ReactNode } from "react";
import { SURFACE_COMPONENT_MAP } from "@/a2ui/catalog";

/**
 * Renders the A2UI fixed-schema envelope this system's drafting nodes emit
 * (agent/src/a2ui_tools.py) by reading it straight out of the tool message
 * `useAgent` already parsed, and dispatching to the matching catalog
 * component — generically, by the `component` name the backend stamped,
 * mirroring what CopilotKit's own A2UI renderer would do with a live
 * TOOL_CALL_RESULT stream (Section 3.3). See src/a2ui/catalog.tsx for why
 * this bypasses that renderer's auto-detection specifically.
 */

interface ToolMessageLike {
  role: string;
  toolCallId?: string;
  content?: string;
}

interface A2UIOperation {
  createSurface?: { surfaceId: string; catalogId: string };
  updateComponents?: { surfaceId: string; components: Array<{ id: string; component: string }> };
  updateDataModel?: { surfaceId: string; path?: string; value: unknown };
}

function findSurfaceData(
  messages: ToolMessageLike[],
  surfaceId: string,
): { component: string; data: unknown } | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role !== "tool" || typeof message.content !== "string") continue;

    let parsed: { a2ui_operations?: A2UIOperation[] } | null = null;
    try {
      parsed = JSON.parse(message.content);
    } catch {
      continue;
    }
    const operations = parsed?.a2ui_operations;
    if (!operations) continue;

    const belongsToSurface = operations.some(
      (op) =>
        op.createSurface?.surfaceId === surfaceId ||
        op.updateComponents?.surfaceId === surfaceId ||
        op.updateDataModel?.surfaceId === surfaceId,
    );
    if (!belongsToSurface) continue;

    const componentOp = operations.find((op) => op.updateComponents?.surfaceId === surfaceId);
    const dataOp = operations.find((op) => op.updateDataModel?.surfaceId === surfaceId);
    const rootComponent = componentOp?.updateComponents?.components?.[0]?.component;
    if (!rootComponent || dataOp?.updateDataModel?.value === undefined) continue;

    return { component: rootComponent, data: dataOp.updateDataModel.value };
  }
  return null;
}

export function A2UISurfaceView({ surfaceId, fallback }: { surfaceId: string; fallback: ReactNode }) {
  const { agent } = useAgent({
    agentId: "workflow",
    updates: [UseAgentUpdate.OnMessagesChanged],
  });

  const surface = findSurfaceData(agent.messages as unknown as ToolMessageLike[], surfaceId);
  if (!surface) return <>{fallback}</>;

  const renderer = SURFACE_COMPONENT_MAP[surface.component];
  if (!renderer) {
    return <p className="text-sm text-red-600">Unrecognized surface component &quot;{surface.component}&quot;.</p>;
  }

  return renderer(surface.data);
}
