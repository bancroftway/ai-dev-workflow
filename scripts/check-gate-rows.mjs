// Self-check for src/lib/gate-rows.ts (the gate dialog's row states, plan §6). Zero dependencies:
//   node --experimental-strip-types scripts/check-gate-rows.mjs
import assert from "node:assert/strict";
import { deriveGateRows } from "../src/lib/gate-rows.ts";

const chk = (id, mode = "blocking", needs_audit = false) => ({ id, label: id, description: "", mode, condition: "always", needs_audit });
const checks = [chk("a"), chk("b"), chk("c", "collected"), chk("aud", "collected", true)];
const wrapperChecks = [chk("wrapper.sandbox")];
const base = { checks, wrapperChecks, verdict: null, policy: "blocking", modeLabel: "Yolo", auditOn: true, stageStatus: "not_started", lap: 1 };
const states = (over) => Object.fromEntries(deriveGateRows({ ...base, ...over }).map((r) => [r.id, r.state]));
const rep = (id, status) => ({ id, status, detail: null, source: "x" });

// Nothing yet / policy off / audit off / approved with no verdict.
assert.deepEqual(states({}), { a: "will_run", b: "will_run", c: "will_run", aud: "will_run", "wrapper.sandbox": "will_run" });
assert.equal(states({ policy: "off" }).a, "policy_off");
assert.match(deriveGateRows({ ...base, policy: "off" })[0].text, /not enforced in Yolo/);
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
assert.match(deriveGateRows({ ...base, verdict: { passed: true } })[0].text, /^Passed, no per-check detail/);
assert.equal(states({ verdict: { passed: false, cannot_verify: true, checks: [] } }).a, "no_sandbox");
// Audit off with a verdict.
assert.equal(states({ verdict: { passed: true, checks: [] }, auditOn: false }).aud, "audit_off");
// Redraft in progress labels every row with the lap.
const redraft = deriveGateRows({ ...base, verdict: failedB, stageStatus: "drafting", lap: 2 });
assert.ok(redraft.every((r) => r.lapNote === "lap 2 (redraft in progress)"));
// Unknown reported id -> its own uncatalogued row; advisory-mode failure is amber.
const extra = deriveGateRows({ ...base, checks: [chk("adv", "advisory")], verdict: { passed: true, checks: [rep("adv", "failed"), rep("new.one", "failed")] } });
assert.deepEqual(extra.find((r) => r.id === "new.one"), { ...extra.find((r) => r.id === "new.one"), group: "uncatalogued", uncatalogued: true });
assert.equal(extra.find((r) => r.id === "adv").tone, "warn");

console.log("gate-rows: all row states OK");
