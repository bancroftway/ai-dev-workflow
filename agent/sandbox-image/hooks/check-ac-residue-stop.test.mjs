// Regression test for check-ac-residue-stop.mjs's run() fail-open fix (final-review Fix Round 2,
// Item 7): run() used to swallow ANY command failure into "" (execSync's own catch block), which
// is indistinguishable from "the command ran and genuinely produced no output" -- for the UNCAPPED
// residue listing specifically, an empty result feeds completed_ac_protection_violations (an
// ABSENCE-IMPLIES-VIOLATION check), so a swallowed failure read as "every completed AC's regression
// test was deleted," a mass false block. The fix makes run() return `null` (a distinct failure
// sentinel) and every call site skip its dependent check rather than proceed with an empty result.
//
// This sandbox has neither `python3` (so the two shelled-out check-hooks, ac_residue_checks.py/
// write_scope_checks.py, can never actually run here) nor, for this test, a git repository at the
// fixture root -- so EVERY run() call site (`git ls-files`/`git diff`) fails simultaneously. Before
// the fix, the capped/uncapped listings would have silently become `""` and the 5th call site
// (changedPaths' own diffOut, inside `if (baselineCommit)`) would have thrown
// (`null.split is not a function`) once run() started returning `null` at all -- a real crash risk
// this test catches directly: the hook must still exit cleanly, never crash, and must report every
// failed data source via reportFailOpen (lib/report-fail-open.mjs's own append-only log).
//
// Run: `node --test agent/sandbox-image/hooks/check-ac-residue-stop.test.mjs`
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const HOOK_PATH = join(HERE, "check-ac-residue-stop.mjs");
const FAIL_OPEN_LOG = ".ai-dev-workflow/hook-fail-opens.jsonl";

function writeFixture(root, relPath, content) {
  const full = join(root, relPath);
  mkdirSync(dirname(full), { recursive: true });
  writeFileSync(full, content);
}

test("check-ac-residue-stop.mjs never crashes when every git/python data source fails, and reports each one", () => {
  const root = mkdtempSync(join(tmpdir(), "aidw-ac-residue-"));
  try {
    // Deliberately NOT a git repo -- every `git ls-files`/`git diff` call site fails, exercising
    // all 5 run() call sites' failure path at once (including the 5th, changedPaths' own diffOut,
    // which the brief's own citation didn't enumerate but which this hook still has).
    writeFixture(root, ".ai-dev-workflow/spec/ledger.json", JSON.stringify({ entries: [] }));

    const env = { ...process.env, AIDW_STAGE: "ac-to-tests", AIDW_BASELINE_COMMIT: "a".repeat(40) };
    const result = spawnSync(process.execPath, [HOOK_PATH], {
      cwd: root,
      env,
      input: JSON.stringify({ cwd: root }),
      encoding: "utf8",
      timeout: 20000,
    });

    assert.equal(result.signal, null, `hook must not be killed/hang; stderr=${result.stderr}`);
    assert.equal(
      result.status, 0,
      `hook must exit cleanly (0 -- no problems found, since every check-hook subprocess also ` +
        `fails without python3) even when every git command fails; got status=${result.status} ` +
        `stderr=${result.stderr}`,
    );
    // The historical crash shape this regression guards against: `TypeError:
    // Cannot read properties of null` / `.split is not a function`, from treating a failed run()
    // result as if it were a genuine string.
    assert.ok(
      !/TypeError|is not a function|Cannot read propert/i.test(result.stderr || ""),
      `hook must not crash on a failed git command; stderr=${result.stderr}`,
    );

    const failOpenPath = join(root, FAIL_OPEN_LOG);
    assert.ok(existsSync(failOpenPath), "every failed data source must be recorded via reportFailOpen");
    const lines = readFileSync(failOpenPath, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    const reasons = lines.map((l) => l.reason).join(" | ");
    // At minimum: the uncapped residue listing and the changed-paths diff both failed here (a
    // non-git cwd) -- the two call sites whose failure previously risked a false block / a crash.
    assert.ok(/uncapped test-file listing/i.test(reasons), reasons);
    assert.ok(/changed-paths diff/i.test(reasons), reasons);
  } finally {
    rmSync(root, { recursive: true, force: true, maxRetries: 3 });
  }
});

test("check-ac-residue-stop.mjs exits 0 immediately on a stage it doesn't cover (unaffected by this fix)", () => {
  const root = mkdtempSync(join(tmpdir(), "aidw-ac-residue-"));
  try {
    const env = { ...process.env, AIDW_STAGE: "plan" };
    const result = spawnSync(process.execPath, [HOOK_PATH], {
      cwd: root,
      env,
      input: "",
      encoding: "utf8",
      timeout: 5000,
    });
    assert.equal(result.status, 0);
    assert.equal(result.signal, null);
  } finally {
    rmSync(root, { recursive: true, force: true, maxRetries: 3 });
  }
});
