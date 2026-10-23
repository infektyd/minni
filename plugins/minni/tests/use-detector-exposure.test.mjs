// Recall-to-use S1b (§4.4, §4.6): exposures, the unit of the before/after
// comparison.
//
// An exposure is (session_key, agent_key, reset_id, mkey), opened by the
// memory's first visible surfacing in the reset epoch. It stays creditable
// whether or not the memory is named again, so today's pointer (re-naming a
// memory every turn) and cross-turn suppression (naming it once) give the same
// denominator. Each turn's used_ii evidence gives one credit per (exposure,
// turn), and a reset closes the epoch.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  finalJudged,
  ledgerRows,
  makeVault,
  resetRow,
  FIXTURE_SESSION,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const MKEY = "vault:wiki/sessions/beryl-cadence.md";
const TITLE = "Beryl harbor checkpoint cadence";
const CITE = "Per the beryl harbor checkpoint cadence, I moved the job earlier.";

const sha16 = (parts) => createHash("sha256").update(JSON.stringify(parts), "utf8").digest("hex").slice(0, 16);

async function setup(t) {
  const env = await makeVault(t);
  await writeDfSnapshot(env.vault);
  const clock = new Clock();
  return { ...env, clock, tx: new Transcript(env.transcriptPath, clock) };
}

/** One prompt turn ending in a Stop. `rows(promptId)` builds the turn's surfacings. */
async function turn(s, ix, { prompt, rows = () => [], replies }) {
  const promptId = `p${ix}`;
  s.tx.prompt(prompt, promptId);
  const surfaced = rows(promptId);
  append(s.vault, [turnRow({ turnIx: ix, promptId, ts: s.clock.next(5) }), ...surfaced]);
  for (const reply of replies) s.tx.say(reply);
  await s.tx.flush();
  const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: replies.at(-1) }));
  assert.equal(result.status, "ran", JSON.stringify(result));
  return surfaced;
}

function pointer(s, promptId) {
  return surfacedRow({ mkey: MKEY, title: TITLE, promptId, ts: s.clock.next(10) });
}

function closeSession(s) {
  const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { endsTurn: false, sessionEnd: true }));
  assert.equal(result.status, "ran", JSON.stringify(result));
}

test("named on turn 1, suppressed on turns 2-3, cited on turn 3: one exposure, one credit on turn 3", async (t) => {
  const s = await setup(t);
  const [first] = await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: ["Looking into it."] });
  await turn(s, 2, { prompt: "Keep going.", replies: ["Still checking the logs."] });
  await turn(s, 3, { prompt: "Any progress?", replies: [CITE] });
  closeSession(s);

  const exposures = ledgerRows(s.vault, "exposure");
  assert.equal(exposures.length, 1);
  const [exposure] = exposures;
  assert.equal(exposure.mkey, MKEY);
  assert.equal(exposure.first_sid, first.sid);
  assert.equal(exposure.channel, "prompt_pointer");
  assert.equal(exposure.visible, true);
  assert.equal(exposure.outcome, "cited");
  assert.deepEqual(exposure.credits, [3], "used_ii on turn 3, credited although not named that turn");
  assert.equal(exposure.final, true);
  assert.equal(exposure.exposure_id, sha16([FIXTURE_SESSION, "main", exposure.reset_id, MKEY]));
  assert.equal(finalJudged(s.vault, first.sid).outcome, "ignored", "the turn-1 window itself saw no use");
});

test("the same memory re-named on turns 1-3 (today's pointer) is still one exposure", async (t) => {
  const s = await setup(t);
  const [first] = await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: ["Looking into it."] });
  const [second] = await turn(s, 2, { prompt: "Keep going.", rows: (p) => [pointer(s, p)], replies: ["Still checking the logs."] });
  const [third] = await turn(s, 3, { prompt: "Any progress?", rows: (p) => [pointer(s, p)], replies: [CITE] });
  closeSession(s);

  const exposures = ledgerRows(s.vault, "exposure");
  assert.equal(exposures.length, 1, "one exposure, not one per surfacing");
  assert.equal(exposures[0].first_sid, first.sid);
  assert.deepEqual(exposures[0].credits, [3]);
  assert.equal(finalJudged(s.vault, first.sid).outcome, "ignored");
  assert.equal(finalJudged(s.vault, second.sid).outcome, "ignored");
  assert.equal(finalJudged(s.vault, third.sid).outcome, "cited", "per-surfacing judgments stay as diagnostics");
});

test("a reset between turns 1 and 3 closes the first exposure: the turn-3 citation is not credited to it", async (t) => {
  const s = await setup(t);
  await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: ["Looking into it."] });
  await turn(s, 2, { prompt: "Keep going.", replies: ["Still checking the logs."] });
  const reset = resetRow({ ts: s.clock.next(50), event: "PreCompact" });
  append(s.vault, [reset]);
  await turn(s, 3, { prompt: "Any progress?", replies: [CITE] });

  const closedByReset = ledgerRows(s.vault, "exposure");
  assert.equal(closedByReset.length, 1, "the reset closed the epoch and wrote its exposure");
  assert.notEqual(closedByReset[0].reset_id, reset.reset_id, "it belongs to the epoch before the reset");
  assert.deepEqual(closedByReset[0].credits, []);
  assert.equal(closedByReset[0].final, true);

  closeSession(s);
  for (const exposure of ledgerRows(s.vault, "exposure")) {
    assert.ok(!exposure.credits.includes(3), "no exposure is credited with the post-reset citation");
  }
});

test("boot plus pointer for one mkey on one turn give one credit", async (t) => {
  const s = await setup(t);
  const boot = surfacedRow({ channel: "boot_recall", mkey: MKEY, title: TITLE, ts: s.clock.next(10) });
  append(s.vault, [boot]);
  const [ptr] = await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: [CITE] });
  closeSession(s);

  const exposures = ledgerRows(s.vault, "exposure");
  assert.equal(exposures.length, 1);
  assert.equal(exposures[0].channel, "boot_recall", "the channel of the first surfacing");
  assert.equal(exposures[0].first_sid, boot.sid);
  assert.deepEqual(exposures[0].credits, [1], "two surfacings on one turn share one credit");
  assert.equal(finalJudged(s.vault, boot.sid).outcome, "cited");
  assert.equal(finalJudged(s.vault, ptr.sid).outcome, "cited");
});

test("an exposure stays creditable for 50 human prompt turns after its latest visible surfacing, then stops", async (t) => {
  const s = await setup(t);
  await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: ["Looking into it."] });
  for (let ix = 2; ix <= 50; ix += 1) await turn(s, ix, { prompt: `Synthetic follow-up number ${ix}.`, replies: ["Still on it."] });
  await turn(s, 51, { prompt: "Any progress now?", replies: [CITE] });
  await turn(s, 52, { prompt: "And after that?", replies: [CITE] });
  closeSession(s);
  const [exposure] = ledgerRows(s.vault, "exposure");
  assert.deepEqual(exposure.credits, [51], "turn 51 is the 50th human turn after the surfacing; turn 52 is past it");
});
