"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";

type Parser = "int" | "float" | "str" | "bool" | "csv" | "csv_int" | "csv_float" | "frozenset_csv";

type RuntimeSetting = {
  name: string;
  category: string;
  purpose: string;
  effect: string;
  value_range: string | null;
  parser: Parser;
  env_var: string;
  default_value: unknown;
  current_value: unknown;
  is_overridden: boolean;
  updated_by: string | null;
  updated_at: string | null;
};

// Display order/labels for config.py's `_Setting.category` values (agent/src/config.py) -- the
// strongest-justified groups (dated production incidents behind every value) lead, the smallest/
// newest groups trail. Any category this list doesn't name still renders, alphabetically, after
// every named one -- so a future config.py category addition is never silently dropped from the
// page, just unlabeled until this list catches up.
const CATEGORY_ORDER = [
  "verify_cycles",
  "clarification_cycles",
  "e2e",
  "truncation",
  "coverage_gate",
  "repo_scan",
  "ac_coverage",
  "sandbox",
  "runtime_misc",
  "misc",
];

const CATEGORY_LABELS: Record<string, string> = {
  verify_cycles: "Verify-cycle budgets",
  clarification_cycles: "Clarification-cycle caps",
  e2e: "End-to-end testing",
  truncation: "Output truncation",
  coverage_gate: "Test-coverage gate",
  repo_scan: "Repo scan & health score",
  ac_coverage: "AC coverage",
  sandbox: "Sandbox / Docker",
  runtime_misc: "Runtime & misc",
  misc: "Other",
};

type FieldState = {
  text: string; // the input's raw text -- parsed on save, never on every keystroke
  saving: boolean;
  error: string | null;
};

// isArrayParser: the 4 collection-shaped kinds round-trip through a single comma-separated text
// input client-side; the backend's own formatter (config.py's _FORMATTERS) does the real
// type-specific join/sort, this only has to split on commas and trim before sending JSON.
function isArrayParser(parser: Parser): boolean {
  return parser === "csv" || parser === "csv_int" || parser === "csv_float" || parser === "frozenset_csv";
}

function valueToText(value: unknown, parser: Parser): string {
  if (isArrayParser(parser)) return Array.isArray(value) ? value.join(", ") : String(value ?? "");
  if (parser === "bool") return value ? "true" : "false";
  return value === null || value === undefined ? "" : String(value);
}

// Parses the row's text input back into the JSON-typed value the PUT endpoint expects (its own
// `value` field -- the backend's config.format_setting/parse_setting do the real validation and
// round-trip check; this is just "what shape of JSON value does this parser kind want").
function textToValue(text: string, parser: Parser): unknown {
  if (isArrayParser(parser)) {
    return text
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }
  if (parser === "bool") return text.trim().toLowerCase() === "true";
  if (parser === "int") return Number.parseInt(text, 10);
  if (parser === "float") return Number.parseFloat(text);
  return text;
}

function formatDisplayValue(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "(empty)";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/**
 * Org Settings → Advanced: every live-editable `agent/src/config.py` setting (the DB-backed Org
 * Settings migration), grouped by category, with per-field purpose/effect/value-range help and a
 * reset-to-default control. Separate page from ../page.tsx (not a section on it): ~120 settings
 * would dominate that page's 5 short sections, and this one needs its own height-constrained
 * scrolling container (below) rather than making the whole settings area one long scroll.
 *
 * Admin-gated by the parent layout (../layout.tsx, Entra App Role "Admin", 404s non-admins before
 * this ever renders) -- same courtesy-layer-only posture as every other page under
 * settings/organization/**; the API routes underneath re-check on every call.
 *
 * Read-live semantics (agent/src/runtime_settings.py): a saved change takes effect for the NEXT
 * session started, never an already-running one -- stated once here rather than per-field, since
 * it's uniform across all ~120 settings shown.
 */
export default function AdvancedSettingsPage() {
  const [settings, setSettings] = useState<RuntimeSetting[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, FieldState>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [filter, setFilter] = useState("");

  function loadSettings() {
    fetch("/api/settings/runtime")
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail ?? `load failed (${res.status})`);
        return res.json() as Promise<{ settings: RuntimeSetting[] }>;
      })
      .then((data) => {
        setSettings(data.settings);
        setFields(
          Object.fromEntries(
            data.settings.map((s) => [s.name, { text: valueToText(s.current_value, s.parser), saving: false, error: null }]),
          ),
        );
      })
      .catch((err: Error) => setLoadError(err.message));
  }

  useEffect(loadSettings, []);

  const grouped = useMemo(() => {
    const byCategory = new Map<string, RuntimeSetting[]>();
    for (const s of settings ?? []) {
      if (filter && !s.name.toLowerCase().includes(filter.toLowerCase()) && !s.purpose.toLowerCase().includes(filter.toLowerCase())) {
        continue;
      }
      if (!byCategory.has(s.category)) byCategory.set(s.category, []);
      byCategory.get(s.category)!.push(s);
    }
    const orderedCategories = [
      ...CATEGORY_ORDER.filter((c) => byCategory.has(c)),
      ...[...byCategory.keys()].filter((c) => !CATEGORY_ORDER.includes(c)).sort(),
    ];
    return orderedCategories.map((category) => ({
      category,
      label: CATEGORY_LABELS[category] ?? category,
      items: byCategory.get(category)!.sort((a, b) => a.name.localeCompare(b.name)),
    }));
  }, [settings, filter]);

  function updateField(name: string, patch: Partial<FieldState>) {
    setFields((prev) => ({ ...prev, [name]: { ...prev[name], ...patch } }));
  }

  function updateSettingInList(name: string, updated: RuntimeSetting) {
    setSettings((prev) => (prev ? prev.map((s) => (s.name === name ? updated : s)) : prev));
    updateField(name, { text: valueToText(updated.current_value, updated.parser), saving: false, error: null });
  }

  async function saveField(setting: RuntimeSetting) {
    const field = fields[setting.name];
    if (!field) return;
    updateField(setting.name, { saving: true, error: null });
    const res = await fetch(`/api/settings/runtime/${encodeURIComponent(setting.name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: textToValue(field.text, setting.parser) }),
    });
    const body = (await res.json().catch(() => ({}))) as RuntimeSetting & { detail?: string };
    if (res.ok) {
      updateSettingInList(setting.name, body);
    } else {
      updateField(setting.name, { saving: false, error: body.detail ?? `save failed (${res.status})` });
    }
  }

  async function resetField(setting: RuntimeSetting) {
    updateField(setting.name, { saving: true, error: null });
    const res = await fetch(`/api/settings/runtime/${encodeURIComponent(setting.name)}`, { method: "DELETE" });
    const body = (await res.json().catch(() => ({}))) as RuntimeSetting & { detail?: string };
    if (res.ok) {
      updateSettingInList(setting.name, body);
    } else {
      updateField(setting.name, { saving: false, error: body.detail ?? `reset failed (${res.status})` });
    }
  }

  return (
    <div className="flex h-full w-full flex-col gap-6 p-6">
      <div>
        <Link href="/settings/organization" className="text-sm text-neutral-500 hover:text-neutral-800">
          ← Back to Organization Settings
        </Link>
        <h1 className="mt-2 text-lg font-semibold">Advanced Settings</h1>
        <p className="text-sm text-neutral-500">
          Every runtime-tunable pipeline constant (lap/verify-cycle caps, timeouts, truncation
          budgets, thresholds) -- normally set once via deploy-time environment variables, editable
          here without a redeploy. Takes effect for the next session started; an already-running
          session keeps whatever was in effect when it began.
        </p>
      </div>

      {loadError && (
        <div className="max-w-2xl rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          <p className="font-medium">Could not load settings</p>
          <p className="mt-1 break-words">{loadError}</p>
        </div>
      )}

      {settings === null && !loadError && <p className="text-sm text-neutral-500">Loading…</p>}

      {settings !== null && (
        <>
          <input
            type="text"
            placeholder="Filter by name or description…"
            className="max-w-md rounded-md border border-neutral-300 px-3 py-2 text-sm"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />

          {/* Height-constrained, internally scrolling -- ~120 settings would otherwise make this
              the entire page's scroll, burying the filter box and page header off-screen. */}
          <div className="flex max-h-[65vh] max-w-3xl flex-col gap-3 overflow-y-auto rounded-lg border border-neutral-200 p-2">
            {grouped.length === 0 && <p className="p-3 text-sm text-neutral-500">No settings match &quot;{filter}&quot;.</p>}
            {grouped.map(({ category, label, items }) => (
              <details key={category} className="rounded-md border border-neutral-100" open={Boolean(filter)}>
                <summary className="cursor-pointer select-none rounded-md bg-neutral-50 px-3 py-2 text-sm font-medium text-neutral-800">
                  {label} <span className="font-normal text-neutral-400">({items.length})</span>
                </summary>
                <div className="flex flex-col divide-y divide-neutral-100 px-3">
                  {items.map((setting) => {
                    const field = fields[setting.name] ?? { text: "", saving: false, error: null };
                    const dirty = field.text !== valueToText(setting.current_value, setting.parser);
                    return (
                      <div key={setting.name} className="flex flex-col gap-1.5 py-3">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5">
                            <code className="text-xs font-medium text-neutral-800">{setting.name}</code>
                            <button
                              type="button"
                              aria-expanded={Boolean(expanded[setting.name])}
                              aria-label={`About ${setting.name}`}
                              className="flex h-4 w-4 items-center justify-center rounded-full border border-neutral-300 text-[10px] leading-none text-neutral-500 hover:bg-neutral-100"
                              onClick={() => setExpanded((prev) => ({ ...prev, [setting.name]: !prev[setting.name] }))}
                            >
                              i
                            </button>
                            {setting.is_overridden && (
                              <span className="rounded-full bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
                                overridden
                              </span>
                            )}
                          </div>
                          <span className="text-xs text-neutral-400">default: {formatDisplayValue(setting.default_value)}</span>
                        </div>

                        {expanded[setting.name] && (
                          <div className="rounded-md bg-neutral-50 p-2 text-xs text-neutral-600">
                            <p>
                              <span className="font-medium text-neutral-700">Purpose: </span>
                              {setting.purpose}
                            </p>
                            <p className="mt-1">
                              <span className="font-medium text-neutral-700">Effect of changing it: </span>
                              {setting.effect}
                            </p>
                            {setting.value_range && (
                              <p className="mt-1">
                                <span className="font-medium text-neutral-700">Valid range: </span>
                                {setting.value_range}
                              </p>
                            )}
                            <p className="mt-1 text-neutral-400">
                              Env var fallback: <code>{setting.env_var}</code>
                              {setting.updated_by && (
                                <>
                                  {" "}
                                  · last changed by {setting.updated_by}
                                  {setting.updated_at && ` on ${new Date(setting.updated_at).toLocaleString()}`}
                                </>
                              )}
                            </p>
                          </div>
                        )}

                        <div className="flex items-center gap-2">
                          {setting.parser === "bool" ? (
                            <select
                              className="rounded-md border border-neutral-300 px-2 py-1 text-sm"
                              value={field.text}
                              onChange={(event) => updateField(setting.name, { text: event.target.value, error: null })}
                            >
                              <option value="true">true</option>
                              <option value="false">false</option>
                            </select>
                          ) : (
                            <input
                              type="text"
                              className="w-full max-w-sm rounded-md border border-neutral-300 px-2 py-1 text-sm"
                              value={field.text}
                              onChange={(event) => updateField(setting.name, { text: event.target.value, error: null })}
                              placeholder={isArrayParser(setting.parser) ? "comma-separated" : undefined}
                            />
                          )}
                          <button
                            type="button"
                            className="rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
                            disabled={!dirty || field.saving}
                            onClick={() => saveField(setting)}
                          >
                            {field.saving ? "Saving…" : "Save"}
                          </button>
                          {setting.is_overridden && (
                            <button
                              type="button"
                              className="rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-600 hover:bg-neutral-50 disabled:opacity-40"
                              disabled={field.saving}
                              onClick={() => resetField(setting)}
                            >
                              Reset to default
                            </button>
                          )}
                        </div>

                        {field.error && <p className="text-xs text-red-700">{field.error}</p>}
                      </div>
                    );
                  })}
                </div>
              </details>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
