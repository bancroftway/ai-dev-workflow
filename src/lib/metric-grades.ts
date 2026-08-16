// Pure metric-grade computation for the metrics bar: CSV threshold parsing, letter-grade bands,
// and baseline->latest delta/polarity helpers. No React, no env reads (page.tsx does that) so
// this stays unit-testable and reusable from a later ReportView (see MetricsBar.tsx re-exports).

export type Grade = "A" | "B" | "C" | "D" | "E";
export type Tone = "green" | "amber" | "red" | "gray";

export const GRADE_TONE: Record<Grade, Tone> = {
  A: "green",
  B: "green",
  C: "amber",
  D: "red",
  E: "red",
};

/** worst_open_severity -> grade, per controller ruling: "info" buckets with "low" (both B). */
const SECURITY_SEVERITY_GRADE: Record<string, Grade> = {
  none: "A",
  info: "B",
  low: "B",
  medium: "C",
  high: "D",
  critical: "E",
};

/** Unknown/missing severity grades to E (worst), never A -- an unrecognized value must not
 * paint a green Security chip. */
export function securityGrade(worstOpenSeverity: string | null | undefined): Grade {
  return SECURITY_SEVERITY_GRADE[worstOpenSeverity ?? ""] ?? "E";
}

export type Thresholds4 = readonly [number, number, number, number];

/** Strict CSV threshold parsing (4 comma-separated numbers). Rejects NaN, wrong length, or
 * non-monotonic input and falls back to `defaults`, warning once server-side (this is only ever
 * called from page.tsx, a Server Component) so a bad env var never silently mis-grades a chip. */
export function parseThresholds(
  raw: string | undefined,
  defaults: Thresholds4,
  envName: string,
  ascending: boolean,
): Thresholds4 {
  if (!raw) return defaults;
  const parts = raw.split(",").map((s) => Number(s.trim()));
  const valid = parts.length === 4 && parts.every((n) => Number.isFinite(n));
  const monotonic =
    valid &&
    (ascending
      ? parts[0] < parts[1] && parts[1] < parts[2] && parts[2] < parts[3]
      : parts[0] > parts[1] && parts[1] > parts[2] && parts[2] > parts[3]);
  if (!monotonic) {
    console.warn(`metric-grades: invalid ${envName}="${raw}", falling back to defaults ${defaults.join(",")}`);
    return defaults;
  }
  return parts as unknown as Thresholds4;
}

/** Bands a value where LOWER is better (CCN, duplication): value<=t0 -> A ... value<=t3 -> D,
 * else E. NaN (or any non-finite value) grades to E -- ambiguous data must never render green. */
export function gradeLowerIsBetter(value: number, [a, b, c, d]: Thresholds4): Grade {
  if (!Number.isFinite(value)) return "E";
  if (value <= a) return "A";
  if (value <= b) return "B";
  if (value <= c) return "C";
  if (value <= d) return "D";
  return "E";
}

/** Bands a value where HIGHER is better (coverage): value>=t0 -> A ... value>=t3 -> D, else E. */
export function gradeHigherIsBetter(value: number, [a, b, c, d]: Thresholds4): Grade {
  if (!Number.isFinite(value)) return "E";
  if (value >= a) return "A";
  if (value >= b) return "B";
  if (value >= c) return "C";
  if (value >= d) return "D";
  return "E";
}

export type DeltaArrow = "▲" | "▼" | "—";

export interface Delta {
  arrow: DeltaArrow;
  /** Signed, formatted numeric change (e.g. "+4", "-2.5"); empty when the rounded delta is zero
   * ("show numeric delta where meaningful"). */
  text: string;
}

/** Compares baseline -> latest for a metric, `higherIsBetter` setting polarity so the arrow means
 * "improved"/"regressed" rather than raw increase/decrease. Returns null when either side is
 * missing (no baseline yet, or the metric itself is absent) -- never a fabricated arrow. */
export function computeDelta(
  baseline: number | null | undefined,
  latest: number | null | undefined,
  higherIsBetter: boolean,
  decimals = 0,
): Delta | null {
  if (baseline == null || latest == null || !Number.isFinite(baseline) || !Number.isFinite(latest)) return null;
  const diff = Number((latest - baseline).toFixed(decimals));
  if (diff === 0) return { arrow: "—", text: "" };
  const improved = higherIsBetter ? diff > 0 : diff < 0;
  return { arrow: improved ? "▲" : "▼", text: `${diff > 0 ? "+" : ""}${diff}` };
}

/** Total open security findings across all severities (the count the Security chip shows
 * alongside its grade) -- undefined/null-safe for old data missing the `measures` block. */
export function securityOpenCount(bySeverity: Record<string, number> | null | undefined): number {
  if (!bySeverity) return 0;
  return Object.values(bySeverity).reduce((sum, n) => sum + n, 0);
}
