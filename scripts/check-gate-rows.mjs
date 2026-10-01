// Self-check for src/lib/gate-rows.ts (the gate dialog's row states, plan §6). The Status-column copy
// is the backend's real GATE_TEXT (agent/src/pipeline_layout.py), read the way
// check-no-stage-literals.mjs reads stage keys, so a missing/renamed key fails here too:
//   node --experimental-strip-types scripts/check-gate-rows.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { deriveGateRows, fmt } from "../src/lib/gate-rows.ts";

const GATE_TEXT = JSON.parse(
  execFileSync("uv", ["run", "python", "-c", "import json;from src.pipeline_layout import GATE_TEXT;print(json.dumps(GATE_TEXT))"], {
    cwd: join(import.meta.dirname, "..", "agent"),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }),
);
const T = GATE_TEXT.row_status;

const chk = (id, mode = "blocking", needs_audit = false) => ({ id, label: id, description: "", mode, condition: "always", needs_audit });
const checks = [chk("a"), chk("b"), chk("c", "collected"), chk("aud", "collected", true)];
const wrapperChecks = [chk("wrapper.sandbox")];
const base = { checks, wrapperChecks, verdict: null, policy: "blocking", modeLabel: "Yolo", auditOn: true, stageStatus: "not_started", lap: 1, text: T };
const all = [];
const derive = (over) => {
  const rows = deriveGateRows({ ...base, ...over });
  all.push(...rows);
  return rows;
};
const states = (over) => Object.fromEntries(derive(over).map((r) => [r.id, r.state]));
const rep = (id, status) => ({ id, status, detail: null, source: "x" });

// Nothing yet / policy off / audit off / approved with no verdict.
assert.deepEqual(states({}), { a: "will_run", b: "will_run", c: "will_run", aud: "will_run", "wrapper.sandbox": "will_run" });
assert.equal(states({ policy: "off" }).a, "policy_off");
assert.equal(derive({ policy: "off" })[0].text, fmt(T.policy_off, { mode: "Yolo" }));
assert.ok(derive({ policy: "off" })[0].text.includes("Yolo"));
assert.equal(derive({ policy: "off", modeLabel: null })[0].text, fmt(T.policy_off, { mode: T.policy_off_mode_fallback }));
assert.equal(states({ auditOn: false }).aud, "audit_off");
assert.equal(states({ stageStatus: "approved" }).a, "approved_earlier");

// Reported rows win (even over policy off -- auto-approve); a blocking failure -> later rows not reached.
const failedB = { passed: false, checks: [rep("wrapper.sandbox", "passed"), rep("a", "passed"), rep("b", "failed")] };
assert.deepEqual(states({ verdict: failedB, policy: "off" }), { a: "passed", b: "failed", c: "not_reached", aud: "not_reached", "wrapper.sandbox": "passed" });
// Reported rows only cover part of the catalog, no blocking failure -> not recorded.
assert.deepEqual(states({ verdict: { passed: true, checks: [rep("b", "passed")] } }), { a: "not_recorded", b: "passed", c: "not_recorded", aud: "not_recorded", "wrapper.sandbox": "not_recorded" });
// A blocking wrapper failure stops every stage row.
assert.equal(states({ verdict: { passed: false, checks: [rep("wrapper.sandbox", "failed")] } }).a, "not_reached");
// A collected (non-blocking) failure does not stop the chain.
assert.equal(states({ verdict: { passed: false, checks: [rep("c", "failed")] } }).a, "not_recorded");
// Pre-feature verdict (no checks key), cannot_verify.
assert.equal(states({ verdict: { passed: false } }).a, "no_detail");
assert.equal(derive({ verdict: { passed: true } })[0].text, T.no_detail_passed);
assert.equal(derive({ verdict: { passed: false } })[0].text, T.no_detail_failed);
assert.equal(states({ verdict: { passed: false, cannot_verify: true, checks: [] } }).a, "no_sandbox");
// Audit off with a verdict.
assert.equal(states({ verdict: { passed: true, checks: [] }, auditOn: false }).aud, "audit_off");
// Redraft in progress labels every row with the lap.
const redraft = derive({ verdict: failedB, stageStatus: "drafting", lap: 2 });
assert.ok(redraft.every((r) => r.lapNote === fmt(T.lap_note, { lap: 2 }) && r.lapNote.includes("2")));
// Unknown reported id -> its own uncatalogued row; advisory-mode failure is amber.
const extra = derive({
  checks: [chk("adv", "advisory"), chk("i"), chk("s"), chk("h")],
  verdict: { passed: true, checks: [rep("adv", "failed"), rep("new.one", "failed"), rep("i", "infra"), rep("s", "skipped"), rep("h", "advisory")] },
});
assert.deepEqual(extra.find((r) => r.id === "new.one"), { ...extra.find((r) => r.id === "new.one"), group: "uncatalogued", uncatalogued: true });
const adv = extra.find((r) => r.id === "adv");
assert.deepEqual([adv.tone, adv.text], ["warn", T.advisory_failed]);
for (const [id, s] of [["i", "infra"], ["s", "skipped"], ["h", "advisory"]]) assert.equal(extra.find((r) => r.id === id).text, T[s]);

// Every row any state produced shows real backend copy: present, non-empty, placeholders filled.
for (const r of all) assert.ok(typeof r.text === "string" && r.text && !/\{\w+\}/.test(r.text), `row ${r.id} (${r.state}) text: ${r.text}`);
const seen = new Set(all.map((r) => r.state));
for (const s of ["will_run", "policy_off", "audit_off", "approved_earlier", "passed", "failed", "infra", "skipped", "advisory", "not_reached", "not_recorded", "no_detail", "no_sandbox"])
  assert.ok(seen.has(s), `state ${s} never exercised`);

console.log("gate-rows: all row states OK");
