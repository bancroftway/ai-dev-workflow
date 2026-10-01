"use client";

import { UseAgentUpdate, useAgent } from "@copilotkit/react-core/v2";
import { useEffect, useMemo, useState } from "react";
import { useRunActivity } from "@/lib/run-activity-context";
import { useStructuralRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";

// The verification gates are built server-side (agent/src/gate_view.py, GET
// /sessions/{id}/gates[/{tabId}]). This file only renders what it receives: it knows no stage,
// check or status -- the one thing it maps is a tone/style name to CSS classes.

type IconTone = "passed" | "failed" | "warn" | "verifying" | "not_run" | "off" | "unknown";
type CellTone = "pass" | "fail" | "warn" | "muted";

/** One entry of GET /sessions/{id}/gates. */
export interface GateSummary {
  tab_id: string;
  icon: { tone: IconTone; badge: string; label: string };
}

interface GateCell {
  text: string;
  sub: string | null;
  badge: string | null;
  collapsible: boolean;
  tone: CellTone | null;
}

/** GET /sessions/{id}/gates/{tabId}. */
interface GateScreen {
  title: string;
  subtitle: string;
  legend: { text: string; bold: boolean }[];
  columns: { key: string; label: string; style: string }[];
  sections: {
    key: string;
    heading: string;
    facts: string[];
    feedback: string | null;
    attempts: { label: string; disabled: boolean; options: { id: string; label: string; time: string | null; selected: boolean }[] };
    groups: { heading: string | null; rows: { key: string; cells: Record<string, GateCell> }[] }[];
  }[];
}

/** View id of a tab's gate in AppShell's tab strip. */
export const gateViewId = (tabId: string) => `gate:${tabId}`;

const ICON_CLASS: Record<IconTone, string> = {
  off: "text-neutral-400 opacity-50",
  unknown: "text-neutral-500",
  not_run: "text-neutral-400",
  verifying: "text-blue-600 animate-pulse",
  passed: "text-green-600",
  failed: "text-red-600",
  warn: "text-amber-600",
};

const TONE_CLASS: Record<CellTone, string> = {
  pass: "text-green-700",
  fail: "text-red-700 font-medium",
  warn: "text-amber-700",
  muted: "text-neutral-500",
};

const COLUMN_CLASS: Record<string, string> = {
  dim: "text-neutral-400",
  strong: "font-medium text-neutral-800",
  muted: "text-neutral-600",
  nowrap: "whitespace-nowrap",
  wide: "max-w-xs text-neutral-700",
};

/** Coalesces a burst of state/event changes into one refetch. */
const REFETCH_DEBOUNCE_MS = 300;

/** Fetches `url` (JSON) and refetches, debounced, whenever the run changes: the AG-UI state
 * object, run status, run-event count or run activity. Only identities/counts are used as change
 * signals -- nothing here reads what the state contains. Keeps the last good response. */
function useGateFetch<T>(url: string): T | null {
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  const eventCount = useStructuralRunEvents().length;
  const [runActivity] = useRunActivity();
  const runActive = runActivity?.runActive;
  const { state, isRunning } = agent;
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      fetch(url, { cache: "no-store" })
        .then((r) => (r.ok ? (r.json() as Promise<T>) : null))
        .catch(() => null)
        .then((body) => {
          if (!cancelled && body != null) setData(body);
        });
    }, REFETCH_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [url, state, isRunning, eventCount, runActive]);
  return data;
}

/** Every gate's tab-strip icon, keyed by tab id; a tab missing from the map gates nothing. */
export function useGateSummaries(): Map<string, GateSummary> {
  const { threadId } = useWorkflowThread();
  const body = useGateFetch<{ gates: GateSummary[] }>(`/api/sessions/${encodeURIComponent(threadId)}/gates`);
  return useMemo(() => new Map((body?.gates ?? []).map((g) => [g.tab_id, g])), [body]);
}

/** An arrow passing through a slatted gate -- the work passing a stage's checks. Stroke uses
 * currentColor, so the tone classes above tint it. */
function GateArrowIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 38 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" className={className} aria-hidden>
      {/* arrow: shaft into a head */}
      <path d="M1 8h23V4l13 6-13 6v-4H1z" />
      {/* gate: a slanted frame with vertical slats, drawn over the shaft */}
      <path d="M11 4.5 17 1.5v16L11 19.5z" />
      <path d="M13 3.5v15M15 2.5v15" />
    </svg>
  );
}

/** The gate between stage tabs: a tab in its own right -- selecting it shows GateView. */
export function GateButton({ summary, active, onSelect }: { summary: GateSummary; active: boolean; onSelect: () => void }) {
  const { tone, badge, label } = summary.icon;
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      aria-label={label}
      title={label}
      onClick={onSelect}
      className={`relative flex shrink-0 items-center rounded-md p-1 hover:bg-neutral-100 ${ICON_CLASS[tone] ?? ""} ${
        active ? "bg-neutral-100 ring-2 ring-neutral-900" : ""
      }`}
    >
      <GateArrowIcon className="h-[18.4px] w-[35px]" />
      {badge && (
        <span aria-hidden className="absolute -right-0.5 -bottom-0.5 text-[9px] leading-none font-bold">
          {badge}
        </span>
      )}
    </button>
  );
}

/** A gate's screen, exactly as the server built it. */
export function GateView({ tabId }: { tabId: string }) {
  const { threadId } = useWorkflowThread();
  const [attempt, setAttempt] = useState<string | null>(null);
  const query = attempt ? `?attempt=${encodeURIComponent(attempt)}` : "";
  const screen = useGateFetch<GateScreen>(`/api/sessions/${encodeURIComponent(threadId)}/gates/${encodeURIComponent(tabId)}${query}`);
  if (!screen) return null;
  return (
    <div className="flex flex-col gap-6 p-6">
      <header>
        <h2 className="text-lg font-semibold">{screen.title}</h2>
        <p className="text-sm text-neutral-600">{screen.subtitle}</p>
      </header>
      {screen.sections.map((section) => (
        <section key={section.key} className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <h3 className="font-semibold">{section.heading}</h3>
            {section.facts.map((fact, i) => (
              <span key={i} className="text-neutral-600">
                {fact}
              </span>
            ))}
            <label className="ml-auto flex items-center gap-1.5 text-neutral-600">
              {section.attempts.label}
              <select
                className="rounded-md border border-neutral-300 px-2 py-1 text-sm"
                value={section.attempts.options.find((o) => o.selected)?.id ?? ""}
                onChange={(e) => setAttempt(e.target.value || null)}
                disabled={section.attempts.disabled}
              >
                {section.attempts.options.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.time ? `${o.label} · ${new Date(o.time).toLocaleString()}` : o.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="text-xs text-neutral-500">
            {screen.legend.map((seg, i) =>
              seg.bold ? (
                <span key={i} className="font-medium">
                  {seg.text}
                </span>
              ) : (
                seg.text
              ),
            )}
          </p>
          {section.feedback && (
            <p className="rounded-md bg-neutral-50 p-2 text-xs whitespace-pre-wrap text-neutral-700">{section.feedback}</p>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-neutral-200 text-neutral-500">
                <tr>
                  {screen.columns.map((c) => (
                    <th key={c.key} className="px-2 py-1.5 font-medium">
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {section.groups.map((group, g) => [
                  group.heading && (
                    <tr key={`heading-${g}`}>
                      <td colSpan={screen.columns.length} className="px-2 pt-3 pb-1 font-medium text-neutral-600">
                        {group.heading}
                      </td>
                    </tr>
                  ),
                  ...group.rows.map((row) => (
                    <tr key={`${g}-${row.key}`} className="border-b border-neutral-100 align-top">
                      {screen.columns.map((c) => (
                        <Cell key={c.key} cell={row.cells[c.key]} className={COLUMN_CLASS[c.style] ?? ""} />
                      ))}
                    </tr>
                  )),
                ])}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}

function Cell({ cell, className }: { cell: GateCell | undefined; className: string }) {
  return (
    <td className={`px-2 py-1.5 ${className} ${cell?.tone ? TONE_CLASS[cell.tone] : ""}`}>
      {cell?.text &&
        (cell.collapsible ? (
          <details>
            <summary className="cursor-pointer truncate">{cell.text.split("\n")[0]}</summary>
            <pre className="mt-1 whitespace-pre-wrap break-words font-sans">{cell.text}</pre>
          </details>
        ) : (
          <span className="break-words">{cell.text}</span>
        ))}
      {cell?.badge && <span className="mt-0.5 block w-fit rounded bg-amber-100 px-1.5 text-[10px] text-amber-800">{cell.badge}</span>}
      {cell?.sub && <div className="text-[11px] font-normal text-neutral-500">{cell.sub}</div>}
    </td>
  );
}
