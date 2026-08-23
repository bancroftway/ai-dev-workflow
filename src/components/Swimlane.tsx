"use client";

import { useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { ViewContainer } from "@/components/ViewContainer";
import { formatDuration, toolNameOf, useRunEvents, type RunLogEvent } from "@/lib/use-run-events";

/**
 * Wall-clock swimlane -- Part 2 Task 9. Two lanes over one real time axis, built from the same
 * event stream EventLogView.tsx renders as a log (useRunEvents(), src/lib/use-run-events.ts): node
 * execution (draft/audit/verify) on top, tool calls below. Absolutely-positioned divs, no chart
 * library (this Part's own Global Constraint). Standalone/reusable like EventLogView.tsx -- Task
 * 13 decides which tab this lives in.
 *
 * Real-timing investigation (task-9-report.md has the full writeup): before this task, graph.py's
 * draft/audit/verify nodes emitted only NODE_FINISHED -- a single end-of-node point, not a span --
 * and neither provider's tool-call translator (claude_chat_model.py / copilot_chat_model.py) keeps
 * any per-call timestamp in a TOOL_CALL event's payload (confirmed by reading both functions; the
 * one real timestamp that exists anywhere in Claude's own captured sample, on the tool_result
 * line, is read by neither translator). Even if it were kept, RunEvent.ts is DB-assigned at
 * append_event's INSERT time, and every TOOL_CALL a turn produces is appended in one tight loop
 * right after that whole CLI turn's subprocess has already exited -- so per-call timestamps would
 * still cluster within DB round-trip time of each other, not spread across the turn's real
 * duration. This task added real NODE_STARTED emission (graph.py) so the node lane has a genuine
 * measured start+end span; TOOL_CALL events still carry only a single real timestamp, so the tool
 * lane renders them as instantaneous ticks, never a fabricated-width bar.
 *
 * GATE_PAUSED/GATE_RESOLVED are deliberately not drawn here -- the brief asks for exactly two
 * lanes (model/node time, tool time); gate pauses are human wait time, a third kind of segment,
 * and already visible in EventLogView's log. Out of scope by omission, not an oversight.
 */

/** One node's real execution span. `startTs`/`endTs` are epoch-ms, both from real DB-assigned
 * `ts` values -- never fabricated. Either end can be missing:
 *  - `startTs == null`: a NODE_FINISHED with no matching NODE_STARTED -- real for every event
 *    recorded before this task's graph.py change (or a future event from an untouched node, e.g.
 *    verify_fix_node/"fix", which still emits no RunEvent of any kind -- see task-9-report.md).
 *    Rendered as a point marker, not a bar with a guessed start.
 *  - `endTs == null`: a NODE_STARTED with no NODE_FINISHED yet -- the node is either still running
 *    or crashed before reaching it (a real, legitimate outcome, e.g. draft_node's infra-exhausted
 *    escalate branch, which returns before its own NODE_FINISHED). Rendered as an open-ended bar,
 *    not silently dropped. */
interface NodeSpan {
  key: string;
  stage: string | null;
  node: string | null;
  startTs: number | null;
  endTs: number | null;
  summary: string | null;
}

interface ToolTick {
  key: string;
  ts: number;
  name: string | null;
  stage: string | null;
  summary: string | null;
}

/** Same pairing key/algorithm as EventLogView.tsx's own computeDurations (run_id|stage|node) --
 * kept independent rather than imported, since that function returns only a duration map shaped
 * for log rows, not the raw start/end pair this lane needs to actually position a bar. */
function buildNodeSpans(events: RunLogEvent[]): NodeSpan[] {
  const spans: NodeSpan[] = [];
  const openStarts = new Map<string, RunLogEvent>();
  for (const e of events) {
    if (e.type !== "node_started" && e.type !== "node_finished") continue;
    const key = `${e.run_id}|${e.stage ?? ""}|${e.node ?? ""}`;
    if (e.type === "node_started") {
      openStarts.set(key, e);
    } else {
      const start = openStarts.get(key);
      spans.push({
        key: `${key}|${e.seq}`,
        stage: e.stage,
        node: e.node,
        startTs: start ? new Date(start.ts).getTime() : null,
        endTs: new Date(e.ts).getTime(),
        summary: e.summary,
      });
      if (start) openStarts.delete(key);
    }
  }
  // Every NODE_STARTED left open (no closing NODE_FINISHED seen) is a real "started, not (yet)
  // finished" span, not a bug -- rendered open-ended below rather than dropped.
  for (const [key, start] of openStarts) {
    spans.push({
      key: `${key}|${start.seq}|open`,
      stage: start.stage,
      node: start.node,
      startTs: new Date(start.ts).getTime(),
      endTs: null,
      summary: start.summary,
    });
  }
  return spans;
}

function buildToolTicks(events: RunLogEvent[]): ToolTick[] {
  return events
    .filter((e) => e.type === "tool_call")
    .map((e) => ({
      key: `${e.run_id}|${e.seq}`,
      ts: new Date(e.ts).getTime(),
      name: toolNameOf(e),
      stage: e.stage,
      summary: e.summary,
    }));
}

// Same identity colors as AppShell.tsx's DOT_CLASS/EventLogView.tsx's TYPE_DOT extended with the
// same border-300/bg-50 shading MetricsBar.tsx's CHIP_CLASS already uses for green/amber/red --
// "blue" here follows that identical formula rather than inventing a new one; this app's real
// palette (grep-confirmed) never uses anything outside red/blue/amber/emerald/neutral/green.
const NODE_COLOR: Record<string, { bg: string; border: string; text: string }> = {
  draft: { bg: "bg-blue-100", border: "border-blue-400", text: "text-blue-900" },
  audit: { bg: "bg-amber-100", border: "border-amber-400", text: "text-amber-900" },
  verify: { bg: "bg-emerald-100", border: "border-emerald-400", text: "text-emerald-900" },
};
const DEFAULT_NODE_COLOR = { bg: "bg-neutral-200", border: "border-neutral-400", text: "text-neutral-900" };

const MIN_BAR_PX = 4;
const TICK_PX = 6;
const ZOOM_DRAG_THRESHOLD_PX = 4;

function formatClock(ts: number): string {
  const d = new Date(ts);
  const base = d.toLocaleTimeString([], { hour12: false });
  return `${base}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

/** Full [min, max] epoch-ms range across every event, padded 5% on each side so edge markers
 * aren't clipped against the container border. Falls back to an arbitrary recent minute-wide
 * window when there is no data yet, and pads a single-instant range (one event, or several sharing
 * one ts), so scaleX below never divides by zero. */
function computeFullRange(events: RunLogEvent[]): [number, number] {
  const timestamps = events.map((e) => new Date(e.ts).getTime()).filter((t) => !Number.isNaN(t));
  if (timestamps.length === 0) {
    const now = Date.now();
    return [now - 60_000, now];
  }
  const min = Math.min(...timestamps);
  const max = Math.max(...timestamps);
  if (min === max) return [min - 30_000, max + 30_000];
  const pad = (max - min) * 0.05;
  return [min - pad, max + pad];
}

export function Swimlane({ onSeek }: { onSeek?: (ts: Date) => void }) {
  const events = useRunEvents();
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [viewRange, setViewRange] = useState<[number, number] | null>(null);
  const [dragBox, setDragBox] = useState<{ startX: number; endX: number } | null>(null);
  const [selected, setSelected] = useState<{ label: string; detail: string; ts: number } | null>(null);

  // ResizeObserver's own contract fires its callback once immediately on observe() with the
  // current size, so the initial measurement comes from that first (async) invocation rather than
  // a synchronous getBoundingClientRect() call in the effect body itself.
  const setContainerRef = (el: HTMLDivElement | null) => {
    containerRef.current = el;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(w);
    });
    observer.observe(el);
  };

  const fullRange = computeFullRange(events);
  const effectiveRange = viewRange ?? fullRange;
  const [viewStart, viewEnd] = effectiveRange;
  const span = Math.max(viewEnd - viewStart, 1);

  const scaleX = (ts: number) => ((ts - viewStart) / span) * width;

  function xToTs(x: number): number {
    return viewStart + (x / Math.max(width, 1)) * span;
  }

  function handleMouseDown(e: ReactMouseEvent<HTMLDivElement>) {
    if (width === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const startX = e.clientX - rect.left;
    setDragBox({ startX, endX: startX });

    function onMove(moveEvent: MouseEvent) {
      const x = Math.max(0, Math.min(width, moveEvent.clientX - rect.left));
      setDragBox((prev) => (prev ? { startX: prev.startX, endX: x } : prev));
    }
    function onUp(upEvent: MouseEvent) {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      const endX = Math.max(0, Math.min(width, upEvent.clientX - rect.left));
      setDragBox(null);
      if (Math.abs(endX - startX) > ZOOM_DRAG_THRESHOLD_PX) {
        const [a, b] = [xToTs(startX), xToTs(endX)].sort((p, q) => p - q);
        setViewRange([a, b]);
        setSelected(null);
      } else {
        const ts = xToTs(startX);
        setSelected({ label: "Seeked", detail: formatClock(ts), ts });
        onSeek?.(new Date(ts));
      }
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function selectSpan(s: NodeSpan) {
    const label = `${s.node ?? "node"}${s.stage ? ` (${s.stage})` : ""}`;
    if (s.startTs != null && s.endTs != null) {
      const detail = `${formatClock(s.startTs)} -> ${formatClock(s.endTs)} (${formatDuration(s.endTs - s.startTs)})`;
      setSelected({ label, detail, ts: s.startTs });
      onSeek?.(new Date(s.startTs));
    } else if (s.endTs == null && s.startTs != null) {
      setSelected({ label, detail: `${formatClock(s.startTs)} -> still open (no NODE_FINISHED yet)`, ts: s.startTs });
      onSeek?.(new Date(s.startTs));
    } else if (s.startTs == null && s.endTs != null) {
      setSelected({ label, detail: `${formatClock(s.endTs)} (no NODE_STARTED recorded -- point only)`, ts: s.endTs });
      onSeek?.(new Date(s.endTs));
    }
  }

  function selectTick(t: ToolTick) {
    setSelected({ label: `tool: ${t.name ?? "unknown"}${t.stage ? ` (${t.stage})` : ""}`, detail: formatClock(t.ts), ts: t.ts });
    onSeek?.(new Date(t.ts));
  }

  const nodeSpans = buildNodeSpans(events);
  const toolTicks = buildToolTicks(events);
  // An open span (endTs == null) has no real end yet, so it must not be hidden just because
  // viewEnd falls before some fabricated cutoff -- treated as extending indefinitely rightward
  // (Infinity) for this overlap test only; rendering below still clips its drawn width to the
  // current viewEnd, which is a display concern, not a visibility one.
  const visibleSpans = nodeSpans.filter((s) => {
    const effectiveEnd = s.endTs ?? Infinity;
    const effectiveStart = s.startTs ?? s.endTs ?? -Infinity;
    return effectiveEnd >= viewStart && effectiveStart <= viewEnd;
  });
  const visibleTicks = toolTicks.filter((t) => t.ts >= viewStart && t.ts <= viewEnd);

  const ticks = 6;
  const gridlines = Array.from({ length: ticks + 1 }, (_, i) => viewStart + (span * i) / ticks);

  return (
    <ViewContainer>
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-lg font-semibold">Timeline</h1>
          <p className="text-sm text-neutral-500">
            Node execution and tool calls over real wall-clock time. Drag to zoom, click to seek.
          </p>
        </div>
        {viewRange && (
          <button
            type="button"
            onClick={() => setViewRange(null)}
            className="shrink-0 rounded-full border border-neutral-300 bg-white px-2.5 py-0.5 text-xs text-neutral-600 hover:bg-neutral-50"
          >
            Reset zoom
          </button>
        )}
      </div>

      {events.length === 0 ? (
        <p className="text-sm text-neutral-400">No events yet.</p>
      ) : (
        <div ref={setContainerRef} className="relative select-none overflow-hidden rounded-lg border border-neutral-200 bg-white">
          {/* Ruler */}
          <div className="relative h-6 border-b border-neutral-100 text-[10px] text-neutral-400">
            {width > 0 &&
              gridlines.map((t, i) => (
                <span key={i} className="absolute top-1 -translate-x-1/2 whitespace-nowrap" style={{ left: scaleX(t) }}>
                  {formatClock(t).slice(0, 8)}
                </span>
              ))}
          </div>

          {/* Drag/seek surface spans both lanes -- mousedown here starts either a zoom-brush or a
              seek click, decided in handleMouseDown by how far the mouse moved before mouseup. */}
          <div className="relative cursor-crosshair" onMouseDown={handleMouseDown}>
            {/* Lane 1: node execution */}
            <div className="relative h-14 border-b border-neutral-100">
              <span className="absolute left-1 top-1 z-10 text-[10px] font-medium text-neutral-400">Node execution</span>
              {width > 0 &&
                visibleSpans.map((s) => {
                  const color = (s.node && NODE_COLOR[s.node]) || DEFAULT_NODE_COLOR;
                  if (s.startTs == null && s.endTs != null) {
                    // Finish-only (legacy/pre-Task-9 data, or any node this task didn't
                    // instrument) -- a real point, not a fabricated span.
                    const left = scaleX(s.endTs) - TICK_PX / 2;
                    return (
                      <button
                        key={s.key}
                        type="button"
                        title={`${s.node ?? "node"} finished at ${formatClock(s.endTs)} (no start recorded)`}
                        onClick={(e) => {
                          e.stopPropagation();
                          selectSpan(s);
                        }}
                        className={`absolute top-6 h-3 w-3 rotate-45 border ${color.border} ${color.bg} opacity-60`}
                        style={{ left }}
                      />
                    );
                  }
                  const startTs = s.startTs ?? viewStart;
                  const rawLeft = scaleX(startTs);
                  const rawRight = scaleX(s.endTs ?? viewEnd);
                  const left = Math.max(0, rawLeft);
                  const barWidth = Math.max(rawRight - left, MIN_BAR_PX);
                  const open = s.endTs == null;
                  return (
                    <button
                      key={s.key}
                      type="button"
                      title={`${s.node ?? "node"}${s.stage ? ` (${s.stage})` : ""}: ${
                        open ? "still open" : formatDuration((s.endTs as number) - startTs)
                      }`}
                      onClick={(e) => {
                        e.stopPropagation();
                        selectSpan(s);
                      }}
                      className={`absolute top-5 h-6 overflow-hidden rounded border px-1 text-left text-[10px] leading-6 ${color.border} ${color.bg} ${color.text} ${open ? "border-dashed opacity-70" : ""}`}
                      style={{ left, width: barWidth }}
                    >
                      <span className="whitespace-nowrap">{s.node}</span>
                    </button>
                  );
                })}
            </div>

            {/* Lane 2: tool calls -- ticks only, never a fabricated-width bar (see module docstring
                for why no real per-call duration exists today). */}
            <div className="relative h-10">
              <span className="absolute left-1 top-1 z-10 text-[10px] font-medium text-neutral-400">Tool calls</span>
              {width > 0 &&
                visibleTicks.map((t) => (
                  <button
                    key={t.key}
                    type="button"
                    title={`${t.name ?? "tool"} at ${formatClock(t.ts)}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      selectTick(t);
                    }}
                    className="absolute top-5 h-3 w-1.5 rounded-full border border-neutral-500 bg-neutral-400"
                    style={{ left: scaleX(t.ts) - TICK_PX / 2 }}
                  />
                ))}
            </div>

            {/* Seek playhead */}
            {selected && width > 0 && selected.ts >= viewStart && selected.ts <= viewEnd && (
              <div className="pointer-events-none absolute inset-y-0 w-px bg-neutral-900/50" style={{ left: scaleX(selected.ts) }} />
            )}

            {/* Live drag-to-zoom selection box */}
            {dragBox && (
              <div
                className="absolute inset-y-0 border-x border-blue-400 bg-blue-400/10"
                style={{
                  left: Math.min(dragBox.startX, dragBox.endX),
                  width: Math.abs(dragBox.endX - dragBox.startX),
                }}
              />
            )}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 text-xs text-neutral-500">
        <LegendSwatch className="border-blue-400 bg-blue-100" label="draft" />
        <LegendSwatch className="border-amber-400 bg-amber-100" label="audit" />
        <LegendSwatch className="border-emerald-400 bg-emerald-100" label="verify" />
        <LegendSwatch className="rounded-full border-neutral-500 bg-neutral-400" label="tool call" />
        <span className="text-neutral-400">dashed = still running/no finish recorded &middot; diamond = point only, no start recorded</span>
      </div>

      {selected && <p className="text-sm text-neutral-600">{selected.label}: {selected.detail}</p>}
    </ViewContainer>
  );
}

function LegendSwatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`inline-block h-3 w-3 border ${className}`} />
      {label}
    </span>
  );
}
