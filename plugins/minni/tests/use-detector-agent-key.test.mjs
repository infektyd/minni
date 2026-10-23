// Recall-to-use S1b r4d: the detector accepts only capture's agent keys.
//
// Every live producer (agentKeyFrom) emits `main` or `a-<12 lowercase hex>`,
// and capture validates exactly that form. The detector used a looser rule,
// and its main-thread checks compare with "main" exactly, so a crafted
// `Main` would have been judged as a subagent (and filtered differently).
// Now any other spelling is refused, by runDetector and in the report.
import assert from "node:assert/strict";
import test from "node:test";

import { appendLedgerRows, buildSurfacedRow, sessionKeyFrom } from "../dist/use-ledger.js";
import { runDetector } from "../dist/use-detector.js";
import { auditReport } from "../dist/vault.js";
import { Clock, detectorArgs, makeVault, Transcript } from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

test("runDetector accepts main and a-<12 hex>, and refuses any other agent key", async (t) => {
  const { vault, transcriptPath } = await makeVault(t, "minni-agent-key-");
  const clock = new Clock();
  const tx = new Transcript(transcriptPath, clock);
  tx.prompt("Rename the build target.", "p1");
  tx.say("Renamed.");
  await tx.flush();
  for (const [agentKey, status] of [
    ["main", "ran"],
    ["a-0123456789ab", "ran"],
    ["Main", "invalid"],
    ["MAIN", "invalid"],
    ["a-ABCDEF012345", "invalid"],
    ["a-0123456789a", "invalid"],
    ["a-0123456789abc", "invalid"],
    ["agent-0123456789ab", "invalid"],
    ["subagent", "invalid"],
  ]) {
    const result = runDetector(detectorArgs(vault, transcriptPath, clock, { agent_key: agentKey, lastAssistantMessage: "Renamed." }));
    assert.equal(result.status, status, agentKey);
  }
});

test("the report counts no row whose agent key is not capture's form", async (t) => {
  const { vault } = await makeVault(t, "minni-agent-key-report-");
  const at = new Date(Date.now() - 3600_000).toISOString();
  const ctx = (agent_key) => ({ session_key: sessionKeyFrom({ session_id: "synthetic-agent-report" }), agent_key, wire: "claude-code", principal: "claude-code", features: [] });
  const rows = [
    buildSurfacedRow(ctx("main"), { channel: "prompt_pointer", mkey: "vault:wiki/a.md", title: "A", rank: 0, visible: true, delivered: true }, at),
    buildSurfacedRow(ctx("a-0123456789ab"), { channel: "subagent_brief", mkey: "vault:wiki/b.md", title: "B", rank: 0, visible: true, delivered: true }, at),
    buildSurfacedRow(ctx("Main"), { channel: "prompt_pointer", mkey: "vault:wiki/c.md", title: "C", rank: 0, visible: true, delivered: true }, at),
  ];
  assert.equal(appendLedgerRows(vault, rows).written, 3);
  const { use } = await auditReport(vault, 100, { useDays: 7 });
  assert.equal(use.surfacings.total, 2, "the crafted Main row is not counted");
  assert.equal(use.surfacings.by_agent.main.total, 1);
  assert.deepEqual(Object.keys(use.surfacings.by_agent.subagents), ["unknown"], "and is never a subagent");
});
