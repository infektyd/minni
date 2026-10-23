// Recall-to-use S1a-2, the hard rule: capture only RECORDS. For SessionStart,
// UserPromptSubmit (strong, weak and envelope prompts) and a PreToolUse deny,
// the hook output is byte-identical with MINNI_USE_LEDGER on and off. The
// positive control (rows were written with it on) keeps this from passing
// vacuously on a build that records nothing.
import assert from "node:assert/strict";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { runOutputScenarios } from "./use-telemetry-hook-fixture.mjs";

test("hook output is identical with the use ledger on and off", { timeout: 240_000 }, async () => {
  const root = path.join(tmpdir(), `minni-output-unchanged-${process.pid}`);
  const on = await runOutputScenarios({ root, env: { MINNI_USE_LEDGER: "on" } });
  const off = await runOutputScenarios({ root, env: { MINNI_USE_LEDGER: "off" } });
  for (const name of Object.keys(on)) {
    assert.equal(on[name].code, 0, `${name} exit code (on)`);
    assert.equal(off[name].code, 0, `${name} exit code (off)`);
    assert.equal(on[name].raw, off[name].raw, `${name}: output bytes differ between ledger on and off`);
    assert.deepEqual(JSON.parse(on[name].raw), JSON.parse(off[name].raw));
    assert.equal(off[name].rows, 0, `${name}: the ledger is off, so nothing is written`);
    assert.ok(on[name].rows > 0, `${name}: with the ledger on, capture must have written rows`);
  }
  // The deny scenario must really be a deny, or it pins nothing.
  assert.equal(JSON.parse(on.pretooluse_deny.raw).hookSpecificOutput?.permissionDecision, "deny");
  assert.match(JSON.parse(on.ups_strong.raw).hookSpecificOutput?.additionalContext ?? "", /recall_pointer/);
});
