// Recall-to-use S1b (§4.4 "Closing at Stop(N)", §4.6): the final turn.
//
// At Stop, `last_assistant_message` is evidence for the final reply. When the
// transcript already holds that reply (matched by hash) the judgment is final;
// otherwise it is provisional, and a later run that reaches the end of the
// turn writes the final row. A window no run reaches is finalized after 24 h
// with final_reason "unflushed_tail". Each (sid, final) pair is written once.
import assert from "node:assert/strict";
import test from "node:test";

import { finalizeIdleSessions, runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  judgedFor,
  ledgerRows,
  makeVault,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const REPLY = "Per the beryl harbor checkpoint cadence, I moved the job earlier.";

async function oneTurnWithoutReply(t) {
  const env = await makeVault(t);
  await writeDfSnapshot(env.vault);
  const clock = new Clock();
  const tx = new Transcript(env.transcriptPath, clock);
  tx.prompt("Look at the nightly stall.", "p1");
  const row = surfacedRow({
    mkey: "vault:wiki/sessions/beryl-cadence.md",
    title: "Beryl harbor checkpoint cadence",
    promptId: "p1",
    ts: clock.next(10),
  });
  append(env.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: row.ts }), row]);
  const toolId = tx.tool("Bash", { command: "ls jobs/" });
  tx.result(toolId, "nightly.sh");
  await tx.flush(); // the final reply is NOT in the transcript yet
  return { ...env, clock, tx, row };
}

test("a reply missing from the transcript is judged from last_assistant_message: provisional, then final", async (t) => {
  const s = await oneTurnWithoutReply(t);

  const first = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: REPLY }));
  assert.equal(first.status, "ran");
  assert.equal(first.provisional, 1);
  let rows = judgedFor(s.vault, s.row.sid);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].final, false);
  assert.equal(rows[0].outcome, "cited", "judged from the final message");

  // The host flushes the reply; the turn's Stop runs again.
  s.tx.say(REPLY);
  await s.tx.flush();
  const second = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: REPLY }));
  assert.equal(second.status, "ran");
  rows = judgedFor(s.vault, s.row.sid);
  assert.deepEqual(rows.map((row) => row.final), [false, true], "provisional, then final");
  assert.equal(rows[1].outcome, "cited");

  // A third run writes nothing more for this sid.
  runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: REPLY }));
  assert.equal(judgedFor(s.vault, s.row.sid).length, 2, "never written twice");
});

test("a provisional window no run reaches is finalized after 24 h as unflushed_tail", async (t) => {
  const s = await oneTurnWithoutReply(t);
  runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: REPLY }));
  assert.deepEqual(judgedFor(s.vault, s.row.sid).map((row) => row.final), [false]);

  // Not yet idle: nothing is finalized.
  finalizeIdleSessions(s.vault, { now: s.clock.now + 60 * 60 * 1000 });
  assert.equal(judgedFor(s.vault, s.row.sid).length, 1);

  finalizeIdleSessions(s.vault, { now: s.clock.now + 25 * 60 * 60 * 1000 });
  const rows = judgedFor(s.vault, s.row.sid);
  assert.deepEqual(rows.map((row) => row.final), [false, true]);
  assert.equal(rows[1].outcome, "cited", "the provisional evidence stands");
  assert.equal(rows[1].final_reason, "unflushed_tail");

  // The idle close also closed the epoch, once.
  const exposures = ledgerRows(s.vault, "exposure");
  assert.equal(exposures.length, 1);
  assert.deepEqual(exposures[0].credits, [1]);
  finalizeIdleSessions(s.vault, { now: s.clock.now + 50 * 60 * 60 * 1000 });
  assert.equal(judgedFor(s.vault, s.row.sid).length, 2);
  assert.equal(ledgerRows(s.vault, "exposure").length, 1);
});
