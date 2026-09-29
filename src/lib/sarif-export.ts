import type { RemediationFinding } from "@/lib/workflow-types";

/**
 * repo_scan.py's `findings` are already a normalized, tool-agnostic shape (severity, category,
 * rule_id, file, line, message) -- converting THAT to SARIF, rather than trying to merge three
 * tools' own raw SARIF outputs (inconsistent per-tool shapes, and would require re-invoking each
 * tool with a `--sarif`/equivalent flag), is the smaller, more honest diff. Minimal valid SARIF
 * 2.1.0: one run, one synthetic driver, one result per finding. Client-side only -- no server
 * round-trip, no new dependency (hand-rolling this is simpler than a library for a schema this
 * small).
 */

const SEVERITY_TO_LEVEL: Record<string, "error" | "warning" | "note" | "none"> = {
  critical: "error",
  high: "error",
  medium: "warning",
  low: "note",
  info: "note",
  none: "none",
};

export function findingsToSarif(findings: RemediationFinding[]): object {
  const ruleIds = new Set<string>();
  const rules: { id: string }[] = [];
  for (const f of findings) {
    const ruleId = f.rule_id || f.id || "unknown";
    if (!ruleIds.has(ruleId)) {
      ruleIds.add(ruleId);
      rules.push({ id: ruleId });
    }
  }

  return {
    $schema: "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
    version: "2.1.0",
    runs: [
      {
        tool: {
          driver: {
            name: "ai-dev-workflow-health-report",
            informationUri: "https://github.com",
            rules,
          },
        },
        results: findings.map((f) => ({
          ruleId: f.rule_id || f.id || "unknown",
          level: SEVERITY_TO_LEVEL[f.severity ?? ""] ?? "warning",
          message: { text: f.description || f.title || "(no description)" },
          locations: f.location?.path
            ? [
                {
                  physicalLocation: {
                    artifactLocation: { uri: f.location.path },
                    ...(f.location.start_line != null
                      ? { region: { startLine: f.location.start_line, endLine: f.location.end_line ?? f.location.start_line } }
                      : {}),
                  },
                },
              ]
            : [],
        })),
      },
    ],
  };
}
