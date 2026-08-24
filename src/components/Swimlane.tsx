"use client";

import { useCallback, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import { ViewContainer } from "@/components/ViewContainer";
import { formatDuration, parseEventTs, toolNameOf, useRunEvents, type RunLogEvent } from "@/lib/use-run-events";

/**
 * Wall-clock swimlane -- Part 2 Task 9. Two lanes over one real time axis, built from the same
 * event stream EventLogView.tsx renders as a log (useRunEvents(), src/lib/use-run-events.ts): node
 * execution (draft/audit/verify) on top, tool calls below. Absolutely-positioned divs, no chart
 * library (this Part's own Global Constraint). Standalone/reusable like EventLogView.tsx -- Task
 * 13 decides which tab this lives in.
 *
 * Real-timing investigation (task-9-report.md has the full writeup): before Part 2 Task 9,
 * graph.py's draft/audit/verify nodes emitted only NODE_FINISHED -- a single end-of-node point,
 * not a span -- and neither provider's tool-call translator kept any per-call timestamp in a
 * TOOL_CALL event's payload. Task 9 added real NODE_STARTED emission (graph.py) so lane 1 has a
 * genuine measured start+end span; it is node execution (draft/audit/verify/fix), NOT
 * model-thinking-time -- it includes whatever tool time happens inside that node, since neither
 * translator could separate the two at the time. Labelled "Node execution" below, honestly, not
 * "model thinking" (Phase E audit finding 6b): the Spec's own model-vs-tool split is a real,
 * documented, unmet commitment (see task-9-report.md), not something this lane's label should
 * imply it delivers.
 *
 * Phase E audit finding 6 (rendering half): two things changed once E-3a's capture-half fix
 * landed (fix-e3a-report.md):
 *
 * 1. TOOL_CALL ticks now position at a real per-call timestamp when one exists -- Claude's payload
 *    carries `result_ts` (the tool_result envelope's own timestamp), Copilot's carries
 *    `envelope_ts` on every event -- rather than always falling back to RunEvent.ts (DB INSERT
 *    time). That fallback is still real and still necessary: `ts` is the only thing older data (or
 *    a tool_use with no matching tool_result) has, but every TOOL_CALL in one turn appended via it
 *    alone would cluster within DB round-trip time of each other regardless of when the real call
 *    happened. Still never a fabricated-width bar -- no per-call *duration* exists either way, so
 *    the tool lane renders instantaneous ticks, just more truthfully placed ones.
 * 2. GATE_PAUSED/GATE_RESOLVED now have real producers (graph.py's make_gate_node). The comment
 *    that used to live here -- justifying the lack of a gate lane by claiming a pause was "already
 *    visible in EventLogView's log" -- was false when written (nothing emitted either event, so
 *    there was nothing to be visible) and is only true now, by coincidence of this same fix
 *    landing on both views. Being visible as one more dense log line was never the point anyway:
 *    a gate pause is the single most user-relevant wait in the whole pipeline (a run can sit there
 *    for hours), so it gets lane 3 below, a visually distinct band, not just a line in a list.
 */

/** One node's real execution span. `startTs`/`endTs` are epoch-ms, both from real DB-assigned
 * `ts` values -- never fabricated. Either end can be missing:
 *  - `startTs == null`: a NODE_FINISHED with no matching NODE_STARTED -- real for every event
 *    recorded before this task's graph.py change (draft/audit/verify/fix all emit both now, per
 *    Ruling 12). Rendered as a point marker, not a bar with a guessed start.
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
  // Finding 6d: true when `ts` came from the payload's own result_ts/envelope_ts (a real per-call
  // instant), false when it fell back to RunEvent.ts (DB INSERT time -- see realToolTs below).
  realTs: boolean;
}

/** One human-review wait, GATE_PAUSED -> GATE_RESOLVED. `startTs`/`endTs` are epoch-ms from real
 * DB-assigned `ts` values, same honesty rule as NodeSpan above:
 *  - `startTs == null`: a GATE_RESOLVED with no matching GATE_PAUSED -- shouldn't happen for data
 *    written after this fix (graph.py's make_gate_node fires both from the same code path), but
 *    handled the same defensive way an unmatched NODE_FINISHED is: a point marker, not a fabricated
 *    start.
 *  - `endTs == null`: a GATE_PAUSED with no GATE_RESOLVED yet -- the run is sitting at this gate
 *    RIGHT NOW. Rendered open-ended, same convention as an open node span.
 * `decision` ("approved"/"rejected", from GATE_RESOLVED's own payload) is null while still open. */
interface GateSpan {
  key: string;
  stage: string | null;
  startTs: number | null;
  endTs: number | null;
  decision: string | null;
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
        startTs: start ? parseEventTs(start.ts) : null,
        endTs: parseEventTs(e.ts),
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
      startTs: parseEventTs(start.ts),
      endTs: null,
      summary: start.summary,
    });
  }
  return spans;
}

/** Finding 6c: pairs GATE_PAUSED -> GATE_RESOLVED by stage, same open-map-and-delete-on-match
 * shape as buildNodeSpans above (keyed on run_id|stage rather than run_id|stage|node -- every gate
 * event's own `node` is always the literal "gate", per graph.py's make_gate_node, so it adds no
 * discriminating value). That shape is what makes a reject-then-redraft-then-re-pause cycle on the
 * SAME stage pair correctly into two separate spans rather than one: the first GATE_PAUSED opens,
 * the rejection's GATE_RESOLVED closes it, the re-pause's GATE_PAUSED opens a fresh one under the
 * same key. graph.py's own gate_node self-check asserts exactly this shape (2 genuine pauses, 2
 * resolutions, in order) against a scripted pause/reject/re-pause/approve sequence -- this reads
 * the identical two event types the same way. */
function buildGateSpans(events: RunLogEvent[]): GateSpan[] {
  const spans: GateSpan[] = [];
  const openPauses = new Map<string, RunLogEvent>();
  for (const e of events) {
    if (e.type !== "gate_paused" && e.type !== "gate_resolved") continue;
    const key = `${e.run_id}|${e.stage ?? ""}`;
    if (e.type === "gate_paused") {
      openPauses.set(key, e);
    } else {
      const start = openPauses.get(key);
      const decision = typeof e.payload?.decision === "string" ? (e.payload.decision as string) : null;
      spans.push({
        key: `${key}|${e.seq}`,
        stage: e.stage,
        startTs: start ? parseEventTs(start.ts) : null,
        endTs: parseEventTs(e.ts),
        decision,
      });
      if (start) openPauses.delete(key);
    }
  }
  // A GATE_PAUSED left open is a real, currently-active pause -- the run is waiting on a human
  // right now. Rendered open-ended below, same convention as an open node span.
  for (const [key, start] of openPauses) {
    spans.push({
      key: `${key}|${start.seq}|open`,
      stage: start.stage,
      startTs: parseEventTs(start.ts),
      endTs: null,
      decision: null,
    });
  }
  return spans;
}

/** Finding 6d: Claude's TOOL_CALL payload carries a real `result_ts` (the tool_result envelope's
 * own timestamp, folded in by claude_chat_model.py); Copilot's carries `envelope_ts` on every
 * event (copilot_chat_model.py). Either is a genuine CLI-reported instant for THIS call, unlike
 * RunEvent.ts (DB INSERT time, shared by every event in a turn regardless of when the real call
 * happened -- see the module docstring above). Prefers result_ts arbitrarily when a payload
 * somehow carried both (never happens for either real translator today, both are provider-
 * exclusive keys) rather than picking one and silently ignoring the other. Returns null -- never a
 * guess -- when neither key is present, so the caller can fall back to `ts` and know it did. */
function realToolTs(e: RunLogEvent): number | null {
  const raw = e.payload?.result_ts ?? e.payload?.envelope_ts;
  if (typeof raw !== "string") return null;
  const parsed = parseEventTs(raw);
  return Number.isNaN(parsed) ? null : parsed;
}

function buildToolTicks(events: RunLogEvent[]): ToolTick[] {
  return events
    .filter((e) => e.type === "tool_call")
    .map((e) => {
      const real = realToolTs(e);
      return {
        key: `${e.run_id}|${e.seq}`,
        ts: real ?? parseEventTs(e.ts),
        name: toolNameOf(e),
        stage: e.stage,
        summary: e.summary,
        realTs: real != null,
      };
    });
}

// Same identity colors as AppShell.tsx's DOT_CLASS/EventLogView.tsx's TYPE_DOT extended with the
// same border-300/bg-50 shading MetricsBar.tsx's CHIP_CLASS already uses for green/amber/red --
// "blue" here follows that identical formula rather than inventing a new one; this app's real
// palette (grep-confirmed) never uses anything outside red/blue/amber/emerald/neutral/green.
// "fix" (verify_fix_node, Ruling 12) gets "green" -- a real, separately-used family from
// "emerald" (already claimed by verify) -- rather than "red", which this app's own DOT_CLASS/
// CHIP_CLASS convention reserves for error/failure states verify_fix_node's normal operation is
// not.
const NODE_COLOR: Record<string, { bg: string; border: string; text: string }> = {
  draft: { bg: "bg-blue-100", border: "border-blue-400", text: "text-blue-900" },
  audit: { bg: "bg-amber-100", border: "border-amber-400", text: "text-amber-900" },
  verify: { bg: "bg-emerald-100", border: "border-emerald-400", text: "text-emerald-900" },
  fix: { bg: "bg-green-100", border: "border-green-400", text: "text-green-900" },
};
const DEFAULT_NODE_COLOR = { bg: "bg-neutral-200", border: "border-neutral-400", text: "text-neutral-900" };

/** Finding 6c: the gate-wait band's hatched fill -- deliberately not a flat NODE_COLOR-style swatch
 * so "waiting on a human" reads as visually distinct from "a node is running" at a glance, not just
 * a differently-labelled bar in the same style. Same amber family this app already reserves for
 * "needs attention" (TYPE_DOT's gate_paused, EventLogView.tsx; the "audit" node color above) --
 * not a new hue. Plain CSS (repeating-linear-gradient), no chart library, matching this whole
 * component's own constraint. */
const GATE_HATCH_STYLE = {
  backgroundImage:
    "repeating-linear-gradient(135deg, rgba(180,83,9,0.35) 0px, rgba(180,83,9,0.35) 3px, rgba(253,230,138,0.45) 3px, rgba(253,230,138,0.45) 6px)",
};

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
  const timestamps = events.map((e) => parseEventTs(e.ts)).filter((t) => !Number.isNaN(t));
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
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const [width, setWidth] = useState(0);
  const [viewRange, setViewRange] = useState<[number, number] | null>(null);
  const [dragBox, setDragBox] = useState<{ startX: number; endX: number } | null>(null);
  const [selected, setSelected] = useState<{ label: string; detail: string; ts: number } | null>(null);

  // ResizeObserver's own contract fires its callback once immediately on observe() with the
  // current size, so the initial measurement comes from that first (async) invocation rather than
  // a synchronous getBoundingClientRect() call in the effect body itself. useCallback([]) keeps
  // the ref identity stable so React only calls this on attach/detach (never per render -- an
  // inline arrow here re-ran every render, leaking one never-disconnected observer each time);
  // the previous observer is disconnected before a new one is created, and on detach (el = null).
  const setContainerRef = useCallback((el: HTMLDivElement | null) => {
    resizeObserverRef.current?.disconnect();
    resizeObserverRef.current = null;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(w);
    });
    observer.observe(el);
    resizeObserverRef.current = observer;
  }, []);

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
    const suffix = t.realTs ? "" : " (log write time, not a real per-call timestamp)";
    setSelected({ label: `tool: ${t.name ?? "unknown"}${t.stage ? ` (${t.stage})` : ""}`, detail: `${formatClock(t.ts)}${suffix}`, ts: t.ts });
    onSeek?.(new Date(t.ts));
  }

  function selectGateSpan(s: GateSpan) {
    const label = `gate wait${s.stage ? ` (${s.stage})` : ""}`;
    if (s.startTs != null && s.endTs != null) {
      const detail = `${formatClock(s.startTs)} -> ${formatClock(s.endTs)} (${formatDuration(s.endTs - s.startTs)})${s.decision ? `, ${s.decision}` : ""}`;
      setSelected({ label, detail, ts: s.startTs });
      onSeek?.(new Date(s.startTs));
    } else if (s.endTs == null && s.startTs != null) {
      setSelected({ label, detail: `${formatClock(s.startTs)} -> still waiting on a human (no resolution yet)`, ts: s.startTs });
      onSeek?.(new Date(s.startTs));
    } else if (s.startTs == null && s.endTs != null) {
      setSelected({ label, detail: `${formatClock(s.endTs)} (no GATE_PAUSED recorded -- point only)`, ts: s.endTs });
      onSeek?.(new Date(s.endTs));
    }
  }

  // Memoized on [events]: renders fire at mousemove rate during a drag-zoom, and none of these
  // depend on anything but the event list itself.
  const nodeSpans = useMemo(() => buildNodeSpans(events), [events]);
  const toolTicks = useMemo(() => buildToolTicks(events), [events]);
  const gateSpans = useMemo(() => buildGateSpans(events), [events]);
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
  // Same open-ended-extends-rightward overlap rule as visibleSpans above.
  const visibleGateSpans = gateSpans.filter((s) => {
    const effectiveEnd = s.endTs ?? Infinity;
    const effectiveStart = s.startTs ?? s.endTs ?? -Infinity;
    return effectiveEnd >= viewStart && effectiveStart <= viewEnd;
  });

  const ticks = 6;
  const gridlines = Array.from({ length: ticks + 1 }, (_, i) => viewStart + (span * i) / ticks);

  return (
    <ViewContainer>
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-lg font-semibold">Timeline</h1>
          <p className="text-sm text-neutral-500">
            Node execution, tool calls, and gate waits over real wall-clock time. Drag to zoom, click to seek.
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
                for why no real per-call duration exists today). Positioned at the real per-call
                result_ts/envelope_ts when the payload carries one (finding 6d), DB append time
                otherwise -- the tooltip says which. */}
            <div className="relative h-10 border-b border-neutral-100">
              <span className="absolute left-1 top-1 z-10 text-[10px] font-medium text-neutral-400">Tool calls</span>
              {width > 0 &&
                visibleTicks.map((t) => (
                  <button
                    key={t.key}
                    type="button"
                    title={`${t.name ?? "tool"} at ${formatClock(t.ts)}${t.realTs ? "" : " (log write time -- no real per-call timestamp captured)"}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      selectTick(t);
                    }}
                    className={`absolute top-5 h-3 w-1.5 rounded-full border border-neutral-500 bg-neutral-400 ${t.realTs ? "" : "opacity-50"}`}
                    style={{ left: scaleX(t.ts) - TICK_PX / 2 }}
                  />
                ))}
            </div>

            {/* Lane 3: gate wait -- finding 6c. GATE_PAUSED/GATE_RESOLVED (graph.py's
                make_gate_node, added by E-3a) paired by stage via buildGateSpans above, the same
                open-map-and-close-on-match shape lane 1's NODE_STARTED/NODE_FINISHED pairing
                already uses. The backend guards against LangGraph's own replay-from-top-on-resume
                double-firing a pause (graph.py's `already_paused` check before emitting
                GATE_PAUSED), so one PAUSED really does mean one genuine pause -- this is the
                single most user-relevant wait in the whole pipeline (a run can sit here for
                hours), finally derivable from real events instead of an unexplained gap between
                two node spans. */}
            <div className="relative h-10">
              <span className="absolute left-1 top-1 z-10 text-[10px] font-medium text-neutral-400">Gate wait</span>
              {width > 0 &&
                visibleGateSpans.map((s) => {
                  if (s.startTs == null && s.endTs != null) {
                    // No matching GATE_PAUSED -- shouldn't happen for data written after this fix
                    // (both events fire from the same gate_node execution), handled defensively
                    // the same way an unmatched NODE_FINISHED is: a point, not a fabricated start.
                    const left = scaleX(s.endTs) - TICK_PX / 2;
                    return (
                      <button
                        key={s.key}
                        type="button"
                        title={`gate resolved at ${formatClock(s.endTs)} (no matching pause recorded)`}
                        onClick={(e) => {
                          e.stopPropagation();
                          selectGateSpan(s);
                        }}
                        className="absolute top-5 h-3 w-3 rotate-45 border border-amber-600 bg-amber-200 opacity-60"
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
                      title={
                        open
                          ? `waiting on human review${s.stage ? ` (${s.stage})` : ""} since ${formatClock(startTs)} -- still open`
                          : `waited on human review${s.stage ? ` (${s.stage})` : ""}: ${formatDuration((s.endTs as number) - startTs)}${s.decision ? `, ${s.decision}` : ""}`
                      }
                      onClick={(e) => {
                        e.stopPropagation();
                        selectGateSpan(s);
                      }}
                      className={`absolute top-5 h-6 overflow-hidden rounded border border-amber-600 px-1 text-left text-[10px] leading-6 text-amber-900 ${open ? "border-dashed opacity-70" : ""}`}
                      style={{ left, width: barWidth, ...GATE_HATCH_STYLE }}
                    >
                      <span className="whitespace-nowrap">{open ? "waiting…" : (s.decision ?? "gate")}</span>
                    </button>
                  );
                })}
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
        <LegendSwatch className="border-green-400 bg-green-100" label="fix" />
        <LegendSwatch className="rounded-full border-neutral-500 bg-neutral-400" label="tool call" />
        <LegendSwatch className="border-amber-600" style={GATE_HATCH_STYLE} label="gate wait" />
        <span className="text-neutral-400">
          dashed = still running/waiting, no finish recorded &middot; diamond = point only, no start recorded &middot;
          faded tick = no real per-call timestamp, log write time shown instead
        </span>
      </div>

      {selected && <p className="text-sm text-neutral-600">{selected.label}: {selected.detail}</p>}
    </ViewContainer>
  );
}

function LegendSwatch({
  className,
  style,
  label,
}: {
  className: string;
  style?: CSSProperties;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`inline-block h-3 w-3 border ${className}`} style={style} />
      {label}
    </span>
  );
}
