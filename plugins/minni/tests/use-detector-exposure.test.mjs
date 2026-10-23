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

test("credits ignore dismissal: named every turn and named once credit identical turn-3 evidence", async (t) => {
  const credits = [];
  for (const renamed of [true, false]) {
    const s = await setup(t);
    await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: ["Looking."] });
    await turn(s, 2, { prompt: "Keep going.", rows: (p) => (renamed ? [pointer(s, p)] : []), replies: ["Still."] });
    await turn(s, 3, { prompt: "Any progress?", rows: (p) => (renamed ? [pointer(s, p)] : []), replies: ["That isn't relevant to the stall.", CITE] });
    closeSession(s);
    credits.push(ledgerRows(s.vault, "exposure").map((exposure) => exposure.credits));
  }
  assert.deepEqual(credits[0], credits[1], "before (re-named) and after (named once) credit alike");
  assert.deepEqual(credits[0], [[3]]);
});

test("a SessionStart reset written after its boot rows still opens the epoch before them", async (t) => {
  const s = await setup(t);
  const boot = surfacedRow({ channel: "boot_recall", mkey: MKEY, title: TITLE, ts: s.clock.next(10) });
  const reset = resetRow({ ts: s.clock.next(10), event: "SessionStart" });
  append(s.vault, [boot, reset]);
  await turn(s, 1, { prompt: "Look at the nightly stall.", replies: [CITE] });
  closeSession(s);
  assert.equal(finalJudged(s.vault, boot.sid).outcome, "cited");
  const exposures = ledgerRows(s.vault, "exposure");
  assert.equal(exposures.length, 1);
  assert.equal(exposures[0].reset_id, reset.reset_id, "the boot exposure lives in the SessionStart epoch");
  assert.deepEqual(exposures[0].credits, [1]);
});

test("control: a SessionStart reset written before its boot rows", async (t) => {
  const s = await setup(t);
  const reset = resetRow({ ts: s.clock.next(10), event: "SessionStart" });
  const boot = surfacedRow({ channel: "boot_recall", mkey: MKEY, title: TITLE, ts: s.clock.next(10) });
  append(s.vault, [reset, boot]);
  await turn(s, 1, { prompt: "Look at the nightly stall.", replies: [CITE] });
  closeSession(s);
  assert.equal(finalJudged(s.vault, boot.sid).outcome, "cited");
  assert.deepEqual(ledgerRows(s.vault, "exposure").map((exposure) => exposure.credits), [[1]]);
});

test("a reset for another agent key does not close main's epoch", async (t) => {
  const s = await setup(t);
  await turn(s, 1, { prompt: "Look at the nightly stall.", rows: (p) => [pointer(s, p)], replies: ["Looking into it."] });
  append(s.vault, [resetRow({ ts: s.clock.next(50), event: "PreCompact", agentKey: "a-0123456789ab" })]);
  await turn(s, 2, { prompt: "Any progress?", replies: [CITE] });
  closeSession(s);
  const exposures = ledgerRows(s.vault, "exposure");
  assert.equal(exposures.length, 1);
  assert.deepEqual(exposures[0].credits, [2]);
});

test("a missed Stop: the turn-1 window closes at the next prompt and takes no turn-2 evidence", async (t) => {
  const s = await setup(t);
  s.tx.prompt("Look at the nightly stall.", "p1");
  const [first] = [pointer(s, "p1")];
  append(s.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: s.clock.next(5) }), first]);
  s.tx.say("Looking into it.");
  await s.tx.flush(); // no Stop for turn 1
  await turn(s, 2, { prompt: "Any progress?", replies: [CITE] });
  closeSession(s);
  assert.equal(finalJudged(s.vault, first.sid).outcome, "ignored");
  assert.deepEqual(ledgerRows(s.vault, "exposure")[0].credits, [2]);
});

test("a turn with no turn row is credited under its own id, never colliding with a real turn_ix", async (t) => {
  const s = await setup(t);
  const promptTurn = async (promptId, prompt, rows, reply, turnIx) => {
    s.tx.prompt(prompt, promptId);
    append(s.vault, [...(turnIx === undefined ? [] : [turnRow({ turnIx, promptId, ts: s.clock.next(5) })]), ...rows]);
    s.tx.say(reply);
    await s.tx.flush();
    runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: reply }));
  };
  // The detector starts mid-session: turn_ix 10, then a prompt whose turn row
  // never landed, then turn_ix 11.
  await promptTurn("p1", "Look at the nightly stall.", [pointer(s, "p1")], "Looking into it.", 10);
  await promptTurn("p2", "Any progress?", [], CITE, undefined);
  await promptTurn("p3", "And now?", [], CITE, 11);
  closeSession(s);
  const [exposure] = ledgerRows(s.vault, "exposure");
  assert.equal(exposure.credits.length, 2, `two credited turns stay two: ${JSON.stringify(exposure.credits)}`);
  assert.ok(exposure.credits.includes(11));
  assert.ok(exposure.credits.some((ix) => ix < 0), "the row-less turn has a detector-local (negative) id");
});
