"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { argSummary, toolNameOf, useRunEvents, type RunLogEvent } from "@/lib/use-run-events";

/**
 * Agent Narration Drawer: a right-side, non-modal panel showing the agent's live reasoning
 * narration and tool-call activity, mounted once in AppShell.tsx so it stays visible regardless of
 * which tab is active. Deliberately excludes prompts, private chain-of-thought structure, raw tool
 * arguments/results, and structured JSON -- only a redacted narration summary and a compact
 * per-tool status line, per the feature's own design.
 *
 * Reuses useRunEvents() (already a shared, deduped subscription -- see that hook's own docstring)
 * filtered to reasoning/tool_call events only; node_started/node_finished/gate_* are already
 * surfaced elsewhere (tab-dot spinners, the interrupt/gate banner) and would just be noise here.
 *
 * useStickToBottom below and ReasoningRow's fold-past-N-chars shape are copied (not imported) from
 * the deleted EventLogView.tsx (git show 7a37340^:src/components/EventLogView.tsx) -- recovered,
 * not reinvented, since that component already solved exactly this rendering job. GroupRow's
 * consecutive-tool-call collapsing and ToolCallDetail/DiffView's click-to-expand raw args/results
 * are deliberately NOT recovered here -- the drawer's own requirements exclude raw tool arguments/
 * results entirely, and collapsing a chatty run of tool calls is a separate, un-asked-for polish
 * item (YAGNI for v1).
 */

/** "Close enough to the bottom to count as at the bottom," in px -- a couple of row-heights of
 * slack so sub-pixel scroll math never misses an exact-0 comparison. */
const BOTTOM_THRESHOLD_PX = 24;

/** Pin-to-bottom / live-follow for the drawer's scrollable event list, copied from the deleted
 * EventLogView.tsx (see this file's own module docstring) -- single consumer, so inlined here
 * rather than a new shared-hooks module; extract only if a second consumer ever needs it too. */
function useStickToBottom(events: RunLogEvent[]) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [stuckToBottom, setStuckToBottom] = useState(true);
  const [lastSeenSeq, setLastSeenSeq] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el || !stuckToBottom) return;
    el.scrollTop = el.scrollHeight;
  }, [events, stuckToBottom]);

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD_PX;
    setStuckToBottom(atBottom);
    setLastSeenSeq((prev) => (atBottom ? null : (prev ?? events[events.length - 1]?.seq ?? null)));
  }

  const newCount = stuckToBottom || lastSeenSeq == null ? 0 : events.filter((e) => e.seq > lastSeenSeq).length;

  function jumpToLatest() {
    setStuckToBottom(true);
    setLastSeenSeq(null);
  }

  return { containerRef, onScroll, newCount, jumpToLatest };
}

/** Character-counted (a "thinking"/"text" block is frequently one long unbroken paragraph with no
 * line structure) fold threshold, same value the deleted EventLogView.tsx used for the identical
 * row. */
const REASONING_COLLAPSE_CHARS = 480;

/** Full-width prose, never folded to a one-liner -- the research note's own label for this row
 * ("model-emitted narration, labelled Reasoning summary"). Reads `payload.text` directly (`summary`
 * is only a truncated head, per both providers' own translate functions), falling back to
 * `summary` for the pathological case of a payload that didn't survive redaction/serialization as
 * expected, so this never renders blank. */
function ReasoningRow({ event }: { event: RunLogEvent }) {
  const [expanded, setExpanded] = useState(false);
  const payload = event.payload;
  const text = typeof payload?.text === "string" ? (payload.text as string) : (event.summary ?? "");
  const isLong = text.length > REASONING_COLLAPSE_CHARS;
  const visible = expanded || !isLong ? text : `${text.slice(0, REASONING_COLLAPSE_CHARS)}…`;

  return (
    <div className="bg-neutral-50/70 px-4 py-3">
      <div className="mb-1 flex items-center gap-2 text-[10px] font-medium uppercase tracking-wide text-neutral-400">
        <span>Reasoning summary</span>
        {event.stage && <span>· {event.stage}</span>}
      </div>
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-neutral-800">{visible}</p>
      {isLong && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs font-medium text-neutral-500 hover:text-neutral-700"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

/** Compact tool name/status row -- dot, tool name, a one-line arg preview, the stage it belongs
 * to. No click-to-expand, no raw args/results detail panel: the feature's own requirements
 * explicitly exclude those (contrast the deleted EventLogView.tsx's EventRow/ToolCallDetail, which
 * this deliberately does not recover). */
function CompactToolRow({ event }: { event: RunLogEvent }) {
  const tool = toolNameOf(event);
  const arg = argSummary(event.payload);
  return (
    <div className="flex items-center gap-2 px-4 py-1.5 text-sm">
      <span aria-hidden className="h-2 w-2 shrink-0 rounded-full bg-neutral-400" />
      <span className="truncate text-neutral-700">{event.summary ?? tool ?? "tool"}</span>
      {arg && <span className="truncate font-mono text-xs text-neutral-400">{arg}</span>}
      {event.stage && <span className="ml-auto shrink-0 text-xs text-neutral-400">{event.stage}</span>}
    </div>
  );
}

export function AgentNarrationDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const events = useRunEvents();
  // node_started/node_finished/gate_* already surface elsewhere (tab-dot spinners, the interrupt/
  // gate banner) -- this drawer only ever shows the two event types the feature actually asks for.
  const narrationEvents = useMemo(
    () => events.filter((e) => e.type === "reasoning" || e.type === "tool_call"),
    [events],
  );
  const { containerRef, onScroll, newCount, jumpToLatest } = useStickToBottom(narrationEvents);

  return (
    <aside
      aria-hidden={!open}
      aria-label="Agent activity"
      className={`fixed inset-y-0 right-0 z-30 flex w-full max-w-md flex-col border-l border-neutral-200 bg-white shadow-xl transition-transform duration-200 ${
        open ? "translate-x-0" : "pointer-events-none translate-x-full"
      }`}
    >
      <div className="flex shrink-0 items-center justify-between border-b border-neutral-200 px-4 py-3">
        <h2 className="text-sm font-semibold text-neutral-800">Agent activity</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close agent activity drawer"
          className="rounded p-1 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600"
        >
          ✕
        </button>
      </div>
      {narrationEvents.length === 0 ? (
        <p className="px-4 py-3 text-sm text-neutral-400">No agent activity yet.</p>
      ) : (
        <div className="relative min-h-0 flex-1">
          <div
            ref={containerRef}
            onScroll={onScroll}
            tabIndex={0}
            aria-label="Agent activity log"
            className="flex h-full flex-col divide-y divide-neutral-100 overflow-y-auto"
          >
            {narrationEvents.map((e) =>
              e.type === "reasoning" ? (
                <ReasoningRow key={e.seq} event={e} />
              ) : (
                <CompactToolRow key={e.seq} event={e} />
              ),
            )}
          </div>
          {/* Shown only while auto-follow is disengaged AND real content arrived since -- same
              "there's more" nudge the deleted EventLogView.tsx used for the identical situation. */}
          {newCount > 0 && (
            <button
              type="button"
              onClick={jumpToLatest}
              className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-blue-300 bg-blue-50 px-3 py-1 text-xs font-medium text-blue-800 shadow-sm hover:bg-blue-100"
            >
              {newCount} new event{newCount === 1 ? "" : "s"} ↓
            </button>
          )}
        </div>
      )}
    </aside>
  );
}
