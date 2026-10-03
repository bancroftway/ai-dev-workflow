import { z } from "zod";
import { ChangeBadge } from "@/a2ui/catalog";

// Mirrors agent/src/test_inventory.py's build_test_inventory output. The server decides grouping,
// order, change stamps and which cards start collapsed; this file only paints. Lenient optional
// fields so an inventory from an older agent build still parses.
const InventoryTestSchema = z.object({ path: z.string(), name: z.string(), change: z.string() });

const InventoryCriterionSchema = z.object({
  id: z.string(),
  description: z.string().optional().default(""),
  change: z.string().optional().default("unchanged"),
  deferred: z.boolean().optional().default(false),
  retired: z.boolean().optional().default(false),
  tests: z.array(InventoryTestSchema).optional().default([]),
});

const InventoryStorySchema = z.object({
  id: z.string(),
  title: z.string().optional().default(""),
  change: z.string().optional().default("unchanged"),
  deferred: z.boolean().optional().default(false),
  retired: z.boolean().optional().default(false),
  collapsed: z.boolean().optional().default(false),
  criteria: z.array(InventoryCriterionSchema).optional().default([]),
});

const TestInventorySchema = z.object({
  stage_key: z.string(),
  as_of_label: z.string().optional().default(""),
  counts: z.object({ new: z.number(), modified: z.number(), deleted: z.number(), unchanged: z.number() }),
  files_scanned: z.number().optional().default(0),
  files_without_recognised_tests: z.array(z.string()).optional().default([]),
  stories: z.array(InventoryStorySchema).optional().default([]),
  unattributed: z.array(InventoryTestSchema).optional().default([]),
  unattributed_collapsed: z.boolean().optional().default(true),
});

export type TestInventory = z.infer<typeof TestInventorySchema>;

/** Zod-validated, not cast: anything malformed renders nothing instead of crashing the tab. */
export function parseTestInventory(data: unknown): TestInventory | null {
  const result = TestInventorySchema.safeParse(data);
  return result.success ? result.data : null;
}

function TestRow({ test }: { test: z.infer<typeof InventoryTestSchema> }) {
  const gone = test.change === "deleted";
  return (
    <li className="text-sm">
      <span className={gone ? "text-neutral-400 line-through" : "text-neutral-800"}>{test.name}</span>
      <ChangeBadge change={test.change} />
      <span className={`block font-mono text-xs ${gone ? "text-neutral-300 line-through" : "text-neutral-400"}`}>{test.path}</span>
    </li>
  );
}

/** The Tests tab's inventory, laid out like the Specification tab: a card per story, a row per
 * criterion, and the tests naming that criterion beneath it -- each with the same change chip the
 * Spec/Plan tabs use. Removed criteria and deleted tests are crossed out. */
export function TestInventoryView({ inventory }: { inventory: TestInventory }) {
  const { counts } = inventory;
  return (
    <div className="space-y-3">
      <p className="text-sm text-neutral-600">
        {counts.new} new · {counts.modified} updated · {counts.deleted} deleted · {counts.unchanged} unchanged
        {inventory.as_of_label && <span className="text-neutral-400"> — {inventory.as_of_label}</span>}
      </p>

      <div className="space-y-2">
        {inventory.stories.map((story) => (
          <details
            key={story.id}
            open={!story.collapsed}
            className={`rounded-lg border px-3 py-2 ${
              story.retired
                ? "border-dashed border-neutral-300 opacity-70"
                : story.deferred
                  ? "border-slate-200 bg-slate-50"
                  : "border-neutral-200"
            }`}
          >
            <summary className="cursor-pointer font-medium">
              <span className="mr-2 font-mono text-xs font-normal text-neutral-500">{story.id}</span>
              <span className={story.retired ? "text-neutral-400 line-through" : story.deferred ? "text-neutral-500" : ""}>
                {story.title}
              </span>
              {!story.retired && <ChangeBadge change={story.change} deferred={story.deferred} />}
            </summary>
            <ul className="mt-1 space-y-1.5">
              {story.criteria.map((ac) => (
                <li key={ac.id} className="text-sm">
                  <div className={ac.retired ? "text-neutral-400 line-through" : ac.deferred ? "text-neutral-500" : ""}>
                    <span className="mr-1 font-mono text-xs text-neutral-500">{ac.id}</span>
                    {ac.description}
                    {!ac.retired && <ChangeBadge change={ac.change} deferred={ac.deferred && !story.deferred} />}
                  </div>
                  {ac.tests.length > 0 ? (
                    <ul className="mt-0.5 space-y-0.5 border-l border-neutral-100 pl-3">
                      {ac.tests.map((test, i) => (
                        <TestRow key={`${test.path}:${test.name}:${i}`} test={test} />
                      ))}
                    </ul>
                  ) : (
                    !ac.retired && !ac.deferred && <p className="border-l border-neutral-100 pl-3 text-xs text-neutral-400">No test names this criterion.</p>
                  )}
                </li>
              ))}
            </ul>
          </details>
        ))}
      </div>

      {inventory.unattributed.length > 0 && (
        <details open={!inventory.unattributed_collapsed} className="rounded-lg border border-neutral-200 px-3 py-2">
          <summary className="cursor-pointer font-medium text-neutral-700">
            Tests that name no criterion ({inventory.unattributed.length})
          </summary>
          <ul className="mt-1 space-y-0.5">
            {inventory.unattributed.map((test, i) => (
              <TestRow key={`${test.path}:${test.name}:${i}`} test={test} />
            ))}
          </ul>
        </details>
      )}

      <div className="text-xs text-neutral-400">
        {inventory.files_scanned} test file{inventory.files_scanned === 1 ? "" : "s"} scanned
        {inventory.files_without_recognised_tests.length > 0 && (
          <>
            {" "}·{" "}
            <details className="inline">
              <summary className="inline cursor-pointer">
                {inventory.files_without_recognised_tests.length} with no recognised test declarations
              </summary>
              <span className="block font-mono">{inventory.files_without_recognised_tests.join(", ")}</span>
            </details>
          </>
        )}
      </div>
    </div>
  );
}
