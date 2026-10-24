// Recall-to-use S1b (§8.2 "Ledger-off stretches stay visible"), end to end:
// the producer (every `<prefix>_session_start` audit row carries
// details.use_ledger, written by the real SessionStart hook) and the reader
// (use-report's ledger_off, built from the daily audit logs) proven together.
// Nothing here hand-writes an audit row: every row comes from a real hook run.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { buildUseReport } from "../dist/use-report.js";
import { makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("real SessionStart hooks, then use-report: ledger-off stretches are listed from their audit rows", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "ledger-off-e2e");
  await startFakeDaemon(fixture);
  const since = Date.now() - 60_000;
  const flags = path.join(fixture.vault, ".runtime", "flags.json");
  // on, off (env), off (env), on, off (flags.json): two stretches, of 2 and 1.
  const boots = [
    ["on", {}],
    ["off-env-1", { MINNI_USE_LEDGER: "off" }],
    ["off-env-2", { MINNI_USE_LEDGER: "off" }],
    ["on-again", {}],
    ["off-file", {}],
  ];
  for (const [label, env] of boots) {
    if (label === "off-file") {
      await mkdir(path.dirname(flags), { recursive: true });
      await writeFile(flags, '{"MINNI_USE_LEDGER":"off"}', "utf8");
    }
    // The hook fixture bypasses the 5 s audit throttle, so every boot keeps its row.
    const { code } = await runHook(fixture, "SessionStart", { session_id: `e2e-${label}`, source: "startup" }, { env });
    assert.equal(code, 0, label);
  }
  const report = buildUseReport(fixture.vault, { since, until: Date.now() + 60_000 });
  const off = report.ledger_off;
  assert.equal(off.session_starts, 5, JSON.stringify(off));
  assert.equal(off.unknown, 0, "every real boot row carries use_ledger");
  assert.equal(off.off, 3);
  assert.deepEqual(
    off.stretches.map((stretch) => stretch.count),
    [2, 1],
    JSON.stringify(off.stretches),
  );
  for (const stretch of off.stretches) {
    assert.ok(Date.parse(stretch.first) <= Date.parse(stretch.last));
    assert.ok(Date.parse(stretch.first) >= since);
  }
});
