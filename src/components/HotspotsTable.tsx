import type { HotspotEntry } from "@/lib/workflow-types";

/**
 * Churn x complexity "hotspots" -- the files everyone keeps editing AND nobody can read
 * (Tornhill's standard heuristic, already computed by repo_scan.py's `_assemble_metrics`).
 * Shared by two consumers: QualityView's "Final metrics" section (live, full-profile sessions)
 * and the standalone health-report page -- both scans include `git-churn` + `lizard`, so both
 * get the same `metrics.churn.hotspots` shape for free.
 */
export function HotspotsTable({ hotspots }: { hotspots: HotspotEntry[] }) {
  if (hotspots.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-left text-xs">
        <thead className="bg-neutral-50 text-neutral-500">
          <tr>
            <th className="px-3 py-1 font-medium">File</th>
            <th className="px-3 py-1 font-medium">Commits</th>
            <th className="px-3 py-1 font-medium">Max CCN</th>
            <th className="px-3 py-1 font-medium">Score</th>
          </tr>
        </thead>
        <tbody>
          {hotspots.map((h) => (
            <tr key={h.path} className="border-t border-neutral-100 align-top">
              <td className="px-3 py-1 font-mono">{h.path}</td>
              <td className="px-3 py-1">{h.commits}</td>
              <td className="px-3 py-1">{h.ccn}</td>
              <td className="px-3 py-1">{h.hotspot_score.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
