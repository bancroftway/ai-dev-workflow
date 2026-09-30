// Regression test for the "AIDW_STAGE read but never gated on" bug class (final-review Fix Round
// 2, Item 1): check-narrative-format-stop.mjs, check-ledger-sync-stop.mjs,
// check-plan-schema-stop.mjs, check-plan-citations-stop.mjs, check-diagram-staleness-stop.mjs, and
// check-diagram-render-stop.mjs used to gate ONLY on a scratch file's existence, so once that file
// was written they kept firing on every later stage's Stop event for the rest of the run -- see
// check-citation-drop-stop.mjs's own header for the original 2026-09-19 incident this class of bug
// is named after.
//
// Each case below writes the SAME on-disk fixture (the scratch file the hook gates on, left behind
// "from an earlier stage") and runs the hook twice, once per AIDW_STAGE: the stage it covers, and a
// LATER stage that does not own it. The fixture is deliberately built so that if the hook reaches
// its file-existence check at all, it always proceeds to a downstream, observable side effect --
// either a `.ai-dev-workflow/hook-fail-opens.jsonl` write (this sandbox has no `python3`/no real
// `/opt/aidw-hooks` scripts, and a `git`-less temp dir, so every downstream subprocess/`git log`
// call fails and self-reports via reportFailOpen; see lib/report-fail-open.mjs) or a non-empty
// stderr (a genuine block). This makes "nothing observable happened" the ONLY way a run can pass at
// the covering stage AND at the later stage both -- so the assertion that the later-stage run does
// nothing while the covering-stage run does something is a real behavioral proof, not a tautology.
//
// Run: `node --test agent/sandbox-image/hooks/check-stage-gating.test.mjs`
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const FAIL_OPEN_LOG = ".ai-dev-workflow/hook-fail-opens.jsonl";

function makeTmpRepo() {
  return mkdtempSync(join(tmpdir(), "aidw-stage-gate-"));
}

function writeFixture(root, relPath, content) {
  const full = join(root, relPath);
  mkdirSync(dirname(full), { recursive: true });
  writeFileSync(full, content);
}

/** Runs `file` (an absolute hook path) against `root` with the given AIDW_STAGE, closed stdin (the
 * hook must not block waiting on it), and returns { status, stderr, didSomething }. `didSomething`
 * is true if the hook produced ANY observable effect beyond a silent `exit(0)` -- a fail-open log
 * write, or non-empty stderr. */
function runHook(file, root, stage) {
  const env = { ...process.env };
  if (stage === undefined) delete env.AIDW_STAGE;
  else env.AIDW_STAGE = stage;
  const result = spawnSync(process.execPath, [file], {
    cwd: root,
    env,
    input: JSON.stringify({ cwd: root }),
    encoding: "utf8",
    timeout: 20000,
  });
  const didSomething = existsSync(join(root, FAIL_OPEN_LOG)) || (result.stderr || "").trim() !== "";
  return { status: result.status, signal: result.signal, stderr: result.stderr, didSomething };
}

const CASES = [
  {
    file: "check-narrative-format-stop.mjs",
    coveringStage: "specification",
    laterStage: "plan",
    setup: (root) => writeFixture(root, ".ai-dev-workflow/spec/draft-specification.json", JSON.stringify({ user_stories: [] })),
  },
  {
    file: "check-ledger-sync-stop.mjs",
    coveringStage: "specification",
    laterStage: "plan",
    setup: (root) => writeFixture(root, ".ai-dev-workflow/spec/draft-specification.json", JSON.stringify({})),
  },
  {
    file: "check-plan-schema-stop.mjs",
    coveringStage: "plan",
    laterStage: "metrics-exit",
    setup: (root) => writeFixture(root, ".ai-dev-workflow/plan/_draft/manifest.json", JSON.stringify({})),
  },
  {
    file: "check-plan-citations-stop.mjs",
    coveringStage: "plan",
    laterStage: "metrics-exit",
    setup: (root) => writeFixture(root, ".ai-dev-workflow/plan/_draft/manifest.json", JSON.stringify({})),
  },
  {
    file: "check-diagram-staleness-stop.mjs",
    coveringStage: "plan",
    laterStage: "metrics-exit",
    setup: (root) => writeFixture(root, ".ai-dev-workflow/plan/_draft/manifest.json", JSON.stringify({})),
  },
  {
    file: "check-diagram-render-stop.mjs",
    coveringStage: "plan",
    laterStage: "metrics-exit",
    setup: (root) => {
      writeFixture(root, ".ai-dev-workflow/plan/_draft/manifest.json", JSON.stringify({ diagrams: [{ name: "foo" }] }));
      writeFixture(root, ".ai-dev-workflow/plan/_draft/diagrams/foo.mmd", "graph TD;\nA-->B;\n");
    },
  },
];

for (const { file, coveringStage, laterStage, setup } of CASES) {
  const hookPath = join(HERE, file);

  test(`${file}: same fixture reaches downstream at its own stage (${coveringStage}) but not at a later stage (${laterStage})`, () => {
    const root = makeTmpRepo();
    try {
      setup(root);

      const covering = runHook(hookPath, root, coveringStage);
      assert.equal(covering.status, 0, `${file} at stage=${coveringStage} should still exit 0 (fail-open/nudge, not a hard crash), got ${covering.status}`);
      assert.ok(
        covering.didSomething,
        `${file} at its own stage (${coveringStage}) should reach the file-existence check and produce SOME observable effect ` +
          `(fail-open log or stderr) given this fixture -- got none, so this fixture doesn't prove anything below`,
      );

      // Fresh temp dir for the later-stage run -- a fail-open log left behind by the covering-stage
      // run above must never leak into this assertion.
      rmSync(root, { recursive: true, force: true });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }

    const root2 = makeTmpRepo();
    try {
      setup(root2);
      const later = runHook(hookPath, root2, laterStage);
      assert.equal(later.status, 0, `${file} at a later stage (${laterStage}) should exit 0`);
      assert.equal(later.signal, null, `${file} should not hang/be killed at a later stage`);
      assert.equal(
        later.didSomething,
        false,
        `${file} at a later stage (${laterStage}) should exit before ever reaching the file-existence check -- ` +
          `it must not touch the leftover file from an earlier stage at all`,
      );
    } finally {
      rmSync(root2, { recursive: true, force: true, maxRetries: 3 });
    }
  });
}
