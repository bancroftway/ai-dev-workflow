import type { ScanSummary } from "@/lib/workflow-types";

/** Human labels for repo_scan.py's health subscore keys, in weight order. */
export const HEALTH_SUBSCORE_LABELS: ReadonlyArray<[key: string, label: string]> = [
  ["security", "Security"],
  ["coverage", "Coverage"],
  ["dependencies", "Dependencies"],
  ["ac_verification", "AC verification"],
  ["accessibility", "Accessibility"],
  ["complexity", "Complexity"],
  ["performance", "Performance"],
  ["duplication", "Duplication"],
  ["maintainability", "Maintainability"],
];

/** Continuous red(0) -> yellow(~50) -> green(100) hue sweep. Lightness 45%: no single lightness
 * clears WCAG 1.4.11's 3:1 on BOTH the light strip and a dark page (green@55% is 1.9:1 on
 * neutral-50), so the hue is deliberately redundant encoding -- the score is always also
 * rendered as text -- and 45% favors the light surfaces this app actually styles for. */
export function healthColor(score: number): string {
  const clamped = Math.max(0, Math.min(100, score));
  return `hsl(${Math.round(clamped * 1.2)}, 70%, 45%)`;
}

function ringTitle(score: number, baseline: number | null | undefined, comparable: boolean | undefined): string {
  const base =
    baseline != null && baseline !== score
      ? ` (baseline ${baseline}${comparable === false ? ", not directly comparable" : ""})`
      : "";
  return `Application health score ${score}/100 — weighted blend of security, coverage, dependencies, AC verification, accessibility, complexity, performance, duplication and maintainability. See the Quality tab for the full breakdown.${base}`;
}

/** Annular health-score ring: sweep = score% of the circle, color = red->green gradient.
 * The r=15.915 circle has circumference ~100, so strokeDasharray works in score units directly.
 * The accessible subscore BREAKDOWN lives in QualityView as a real list -- this ring carries only
 * the headline number (visible text + aria-label), with `title` as supplementary hover text. */
export function HealthRing({
  score,
  baseline,
  comparable,
  size = 28,
}: {
  score: number;
  baseline?: number | null;
  comparable?: boolean;
  size?: number;
}) {
  const color = healthColor(score);
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 36 36"
      role="img"
      aria-label={`Health score ${score} out of 100`}
      className="shrink-0"
    >
      <title>{ringTitle(score, baseline, comparable)}</title>
      {/* currentColor at low opacity: inherits the surrounding text color, so the track and
          numeral stay legible on dark surfaces too (hardcoded #e5e7eb was invisible there). */}
      <circle cx="18" cy="18" r="15.915" fill="none" stroke="currentColor" opacity="0.15" strokeWidth="3.5" />
      {score > 0 && (
        <circle
          cx="18"
          cy="18"
          r="15.915"
          fill="none"
          stroke={color}
          strokeWidth="3.5"
          strokeLinecap="round"
          strokeDasharray={`${score} ${100 - score}`}
          transform="rotate(-90 18 18)"
        />
      )}
      <text
        x="18"
        y="18"
        textAnchor="middle"
        dominantBaseline="central"
        fill="currentColor"
        fontSize="12"
        fontWeight="600"
      >
        {score}
      </text>
    </svg>
  );
}

/** The accessible breakdown list the ring links to -- one row per subscore, weight alongside,
 * unmeasured subscores named rather than hidden (their weight was redistributed). */
export function HealthBreakdown({
  summary,
  baseline,
  label = "Health",
}: {
  summary: ScanSummary;
  baseline?: ScanSummary | null;
  /** "Baseline health" when the latest scan hasn't landed yet -- presenting the pre-build
   * baseline as the current score sends a reader chasing a number no code has earned. */
  label?: string;
}) {
  const subscores = summary.health_subscores;
  if (!subscores || summary.health_score == null) return null;
  const weights = summary.health_weights_used ?? {};
  const unmeasured = HEALTH_SUBSCORE_LABELS.filter(([key]) => subscores[key] == null).map(([, label]) => label);
  return (
    <div className="flex items-start gap-4">
      <HealthRing
        score={summary.health_score}
        baseline={baseline?.health_score}
        comparable={summary.health_score_comparable}
        size={56}
      />
      <div className="text-sm text-neutral-700">
        <p>
          {label} {summary.health_score} / 100
          {baseline?.health_score != null && baseline.health_score !== summary.health_score && (
            <span className="text-neutral-500">
              {" "}(baseline {baseline.health_score}
              {summary.health_score_comparable === false ? ", scored under a different measured set" : ""})
            </span>
          )}
        </p>
        {summary.health_coverage_multiplier != null && summary.health_coverage_multiplier < 1 && (
          // v3: subscores x weights no longer sum to the ring when security tools failed --
          // name the haircut or the breakdown reads as broken arithmetic.
          <p className="text-xs text-amber-700">
            coverage ×{summary.health_coverage_multiplier}
            {summary.health_coverage_fraction != null &&
              ` (${Math.round(summary.health_coverage_fraction * 100)}% of security tools completed)`}
            {summary.health_raw != null && ` — raw ${summary.health_raw}`}
          </p>
        )}
        <ul className="mt-1 grid grid-cols-1 gap-x-6 sm:grid-cols-3">
          {HEALTH_SUBSCORE_LABELS.map(([key, label]) => {
            const value = subscores[key];
            if (value == null) return null;
            const weight = weights[key];
            return (
              <li key={key} className="flex justify-between gap-2 text-xs text-neutral-600">
                <span>{label}</span>
                <span>
                  <span style={{ color: healthColor(value) }} className="font-medium">{Math.round(value)}</span>
                  {weight != null && <span className="text-neutral-400"> ×{Math.round(weight * 100)}%</span>}
                </span>
              </li>
            );
          })}
        </ul>
        {unmeasured.length > 0 && (
          <p className="mt-1 text-xs text-neutral-400">
            Not measured this scan (weight redistributed): {unmeasured.join(", ")}
          </p>
        )}
      </div>
    </div>
  );
}
