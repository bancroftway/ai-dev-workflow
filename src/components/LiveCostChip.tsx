"use client";

import { Chip } from "@/components/MetricsBar";
import { useRunEvents } from "@/lib/use-run-events";

/**
 * Live per-run cost/token chip -- Part 2 Task 11. Same value/tooltip shape as MetricsBar.tsx's own
 * `costChip` (grep `costChip`) -- reuses `Chip` directly rather than inventing new chrome -- but
 * summed from useRunEvents()'s real event list instead of agent state, so it updates on every
 * NODE_FINISHED the event log already streams in (draft/audit/fix), not just once at Metrics Exit.
 *
 * draft/audit/fix's NODE_FINISHED events each carry a real `token_usage`
 * (claude_chat_model.py / copilot_chat_model.py's `_last_usage`: {model, input_tokens,
 * output_tokens, cost}); verify_node's NODE_FINISHED has none (not an LLM call), and no
 * NODE_STARTED/TOOL_CALL/gate event ever sets one either -- confirmed by grepping every
 * `token_usage=` RunEvent call site in graph.py. Summing over every event that HAS one is
 * therefore safe: nothing double-counts. `cost` can be null on an individual event even when
 * token counts are real (the CLI turn didn't report `total_cost_usd`) -- treated as 0 for that
 * event rather than poisoning the whole run's total to null, the same rule metrics_nodes.py's own
 * `_sum_token_usage` already applies server-side (`usage.get("cost") or 0.0`) to this identical
 * shape.
 *
 * Standalone/reusable like EventLogView.tsx/Swimlane.tsx -- Task 13 places this in AppShell's
 * layout later. Renders nothing until the first usage-bearing event lands, same "hide until there
 * is real data" rule MetricsBar's costChip already applies.
 */
export function LiveCostChip() {
  const events = useRunEvents();

  let inputTokens = 0;
  let outputTokens = 0;
  let cost = 0;
  let sawUsage = false;

  for (const e of events) {
    if (!e.token_usage) continue;
    sawUsage = true;
    inputTokens += Number(e.token_usage.input_tokens) || 0;
    outputTokens += Number(e.token_usage.output_tokens) || 0;
    cost += Number(e.token_usage.cost) || 0;
  }

  if (!sawUsage) return null;

  return (
    <Chip
      label="Cost"
      value={`$${cost.toFixed(2)}`}
      tone="gray"
      title={`LLM spend this run so far: ${inputTokens.toLocaleString()} tokens in / ${outputTokens.toLocaleString()} out. Live from the event log -- updates as each draft/audit/fix node finishes.`}
    />
  );
}
