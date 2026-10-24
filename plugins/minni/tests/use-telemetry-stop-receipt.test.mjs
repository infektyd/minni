// Recall-to-use S1b (§4.5 "Stop receipt"): with the ledger on, the Stop
// receipt reads the session's use counts and appends ` Use: <n> surfaced, <c>
// cited, <a> applied.` to its line. The counts stand as of the Stop's start:
// that turn's own judgment lands after delivery, so it shows on the next Stop.
// With the ledger off, the receipt line is byte-identical to the line without
// use counts, and the reader is never loaded.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { ensureVault, formatSessionReceiptLine, sessionReceipt } from "../dist/vault.js";
import { finalJudged, writeDfSnapshot } from "./fixtures/use-detector-fixtures.mjs";
import { SESSION_ID, seedStopTurn } from "./use-stop-fixture.mjs";
import { makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

async function seeded(t, label) {
  const fixture = await makeFixture(t, label);
  await startFakeDaemon(fixture);
  await ensureVault(fixture.vault);
  // Every title token distinctive, so the reply's title citation counts.
  await writeDfSnapshot(fixture.vault);
  return { fixture, ...(await seedStopTurn(fixture)) };
}

test("ledger on: the Stop receipt carries the use counts, one Stop behind the detector", { timeout: 60_000 }, async (t) => {
  const { fixture, payload, surfaced } = await seeded(t, "stop-receipt-on");
  const first = await runHook(fixture, "Stop", payload);
  assert.equal(first.code, 0);
  assert.match(first.output.systemMessage, /^Minni session receipt: .*\. Use: 1 surfaced, 0 cited, 0 applied\.$/);

  // The first Stop's detector run judged the reply's title citation.
  assert.equal(finalJudged(fixture.vault, surfaced.sid).outcome, "cited");
  const second = await runHook(fixture, "Stop", payload);
  assert.match(second.output.systemMessage, / Use: 1 surfaced, 1 cited, 0 applied\.$/);
});

test("ledger off (env or flags.json): the receipt line is byte-identical to the line without use counts", { timeout: 60_000 }, async (t) => {
  for (const [label, env, flags] of [
    ["env", { MINNI_USE_LEDGER: "off" }, null],
    ["file", {}, '{"MINNI_USE_LEDGER":"off"}'],
  ]) {
    const { fixture, payload } = await seeded(t, `stop-receipt-off-${label}`);
    if (flags) {
      await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
      await writeFile(path.join(fixture.vault, ".runtime", "flags.json"), flags, "utf8");
    }
    // What today's Stop prints: the receipt without use counts, tallied before
    // the Stop's own row lands, as handleStopCore tallies it.
    const expected = formatSessionReceiptLine(await sessionReceipt(fixture.vault, SESSION_ID, 500, { useCounts: false }));
    const { code, output } = await runHook(fixture, "Stop", payload, { env });
    assert.equal(code, 0);
    assert.equal(output.systemMessage, expected, `${label}: byte-identical`);
    assert.doesNotMatch(output.systemMessage, / Use: /);
  }
});
