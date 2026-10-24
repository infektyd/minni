// Recall-to-use S1b round 4b: per-act work is done once per act, not once per
// open window.
//
// The detector judges every open window and creditable exposure against each
// assistant action, and the per-entry deadline cannot interrupt one entry. So
// anything derived from the action alone (its tokenisation, its shingles, a
// recall query's words) must be computed once per action: recomputing it per
// window made one 200k-char minni_recall query with the title rule on cost
// ~2.4 s at 40 open memories and ~7.4 s at 120.
//
// A warm-up turn saves the cursor first, so the measured turn's long line is
// parsed (a fresh cursor starts 64 KB before the end of the transcript).
import assert from "node:assert/strict";
import test from "node:test";

import { echoHashes, shingleHashes } from "../dist/use-ledger.js";
import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  judgedFor,
  makeVault,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const CAP_MS = 1500;
const K = 40;
const repeatTo = (unit, n) => unit.repeat(Math.ceil(n / unit.length)).slice(0, n);
const PROSE = repeatTo("The nightly harbor job moved ahead of the checkpoint flush so the cadence holds. ", 200_000);
const WORDS = ["amber", "basalt", "cobalt", "dune", "ember", "fjord", "garnet", "heron", "indigo", "jasper", "kelp", "lumen", "mica", "nectar", "ochre", "pumice", "quartz", "rune", "sable", "tundra"];
/** Distinct, assessable titles: 4 distinctive words plus a tag. */
const titleOf = (i) => `${WORDS[i % 20]} ${WORDS[(i * 7 + 3) % 20]} ${WORDS[(i * 11 + 5) % 20]} ${WORDS[(i * 13 + 9) % 20]} ledger ${i}`;

/** Warm the cursor, then surface `rows(promptId, clock)` and run `actions` in one turn; returns the measured run. */
async function measured(t, { rows, actions }) {
  const env = await makeVault(t, "minni-act-cost-");
  await writeDfSnapshot(env.vault, { the: 90 });
  const clock = new Clock();
  const tx = new Transcript(env.transcriptPath, clock);
  tx.prompt("Hello.", "p0");
  append(env.vault, [turnRow({ turnIx: 0, promptId: "p0", ts: clock.next(5) })]);
  tx.say("Hi.");
  await tx.flush();
  assert.equal(runDetector(detectorArgs(env.vault, env.transcriptPath, clock, { lastAssistantMessage: "Hi." })).status, "ran");

  tx.prompt("Look at the nightly stall.", "p1");
  const surfaced = rows("p1", clock);
  append(env.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: clock.next(5) }), ...surfaced]);
  let last = null;
  for (const action of actions) {
    if (typeof action === "string") {
      tx.say(action);
      last = action;
    } else {
      tx.tool(action[0], action[1]);
    }
  }
  await tx.flush();
  const started = performance.now();
  const result = runDetector(detectorArgs(env.vault, env.transcriptPath, clock, { lastAssistantMessage: last }));
  const ms = performance.now() - started;
  assert.equal(result.status, "ran");
  assert.equal(result.path, "slow");
  assert.equal(result.carried, false, "the whole turn was read");
  return { env, surfaced, ms };
}

const pointersOf = (count) => (promptId, clock) =>
  Array.from({ length: count }, (_, i) =>
    surfacedRow({ mkey: `vault:wiki/sessions/note-${i}.md`, title: titleOf(i), promptId, rank: i, ts: clock.next(1) }),
  );
const pointers = pointersOf(K);

// 40 is the review's headline size; 120 pins the once-per-act memo itself
// (tokenising the query per window instead stays under the cap at 40 only).
for (const count of [K, 120]) {
  test(`a 200k-char minni_recall query, title rule on, ${count} assessable memories open: under the cap`, async (t) => {
    const { ms } = await measured(t, { rows: pointersOf(count), actions: [["mcp__minni__minni_recall", { query: PROSE }], "Done."] });
    assert.ok(ms < CAP_MS, `runDetector took ${ms.toFixed(0)} ms`);
  });
}

test("control: the same shape with a query naming one title still opens it (the long line was parsed)", async (t) => {
  const query = `${PROSE.slice(0, 150_000)} ${titleOf(0)}`;
  const { env, surfaced, ms } = await measured(t, { rows: pointers, actions: [["mcp__minni__minni_recall", { query }], "Done."] });
  assert.ok(ms < CAP_MS, `runDetector took ${ms.toFixed(0)} ms`);
  const standing = judgedFor(env.vault, surfaced[0].sid).find((row) => row.final === true);
  assert.equal(standing?.outcome, "opened");
  assert.equal(judgedFor(env.vault, surfaced[1].sid).find((row) => row.final === true)?.outcome, "ignored");
});

test("a 200k-char reply and a 200k-char Edit input, 40 memories open: under the cap", async (t) => {
  for (const actions of [[`Here: ${PROSE}`], [["Edit", { file_path: "/synthetic/x.ts", old_string: "a", new_string: PROSE }], "Done."]]) {
    const { ms } = await measured(t, { rows: pointers, actions });
    assert.ok(ms < CAP_MS, `runDetector took ${ms.toFixed(0)} ms`);
  }
});

test("40 boot learnings and a 200k-char reply holding one recital: under the cap", async (t) => {
  // Cost only: the echo-masking pass runs per boot window; its correctness is
  // pinned in use-detector-rules.test.mjs.
  const line = (i) => `Run the ${titleOf(i)} migration scripts in numeric order, never in parallel batches.`;
  const boots = (promptId, clock) =>
    Array.from({ length: K }, (_, i) =>
      surfacedRow({
        channel: "boot_recent_learnings",
        mkey: `learning:${i + 1}`,
        title: line(i),
        rank: i,
        ts: clock.next(1),
        shingles: shingleHashes(line(i)),
        echo: echoHashes(line(i)),
      }),
    );
  const { env, surfaced, ms } = await measured(t, { rows: boots, actions: [`Noted: ${line(0)} ${PROSE}`] });
  assert.ok(ms < CAP_MS, `runDetector took ${ms.toFixed(0)} ms`);
  assert.equal(judgedFor(env.vault, surfaced[0].sid).length, 0, "boot windows stay open past their first turn");
});
