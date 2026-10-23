// Recall-to-use S1b round 4 (F3): the detector's text regexes stay linear.
//
// The detector runs inside a Stop hook with a 1.5 s cap, over assistant text
// it does not control. A regex that re-scans a long token from every start
// position is quadratic: one pasted 40k-char blob (a base64 dump, a minified
// line) used to cost ~1.7 s in runDetector and ~20 s per regex at 200k. Each
// pattern is timed in isolation at 200k on a single unbroken token and on
// pattern-specific adversarial shapes, and runDetector is timed end to end.
import assert from "node:assert/strict";
import test from "node:test";

import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  finalJudged,
  makeVault,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const LONG = 200_000;
/** Generous for a slow machine: the linear patterns take a few ms here; the quadratic ones took seconds. */
const PER_REGEX_MS = 300;

const repeatTo = (unit, length) => unit.repeat(Math.ceil(length / unit.length)).slice(0, length);

/** Inputs that make a backtracking pattern re-scan from every position. */
const ADVERSARIAL = {
  "single token": repeatTo("a", LONG),
  "class mix": repeatTo("ab.cd-ef_gh~ij0", LONG),
  "base64ish": Array.from({ length: LONG }, (_, i) => "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[i % 62]).join(""),
  "open brackets": repeatTo("[[a", LONG),
  "brackets only": repeatTo("[", LONG),
  "d handles": repeatTo("d:a", LONG),
  "m handles": repeatTo("m:", LONG),
  "learning refs": `learning${repeatTo(" ", LONG)}`,
  "evidence tags": repeatTo('<EVIDENCE source="', LONG),
  "score parens": repeatTo("(score=", LONG),
  "slashes": repeatTo("/", LONG),
  "md dots": repeatTo("a.md.", LONG),
  "joined": repeatTo("a-", LONG),
  "spaces": repeatTo(" ", LONG),
  "newlines": repeatTo("\n", LONG),
  "dots and spaces": repeatTo(". ", LONG),
};

test("every text regex the detector runs is linear at 200k chars", async () => {
  const mod = await import("../dist/use-detector.js");
  const patterns = mod.DETECTOR_TEXT_PATTERNS;
  assert.ok(patterns && typeof patterns === "object", "the detector exports its text patterns for this test");
  const names = Object.keys(patterns);
  assert.ok(names.length >= 10, `all of them: ${names.join(", ")}`);
  const slow = [];
  for (const name of names) {
    const source = patterns[name];
    for (const [shape, input] of Object.entries(ADVERSARIAL)) {
      const re = new RegExp(source.source, source.flags.includes("g") ? source.flags : `${source.flags}g`);
      const started = performance.now();
      let count = 0;
      for (const _ of input.matchAll(re)) count += 1;
      const ms = performance.now() - started;
      if (ms > PER_REGEX_MS) slow.push(`${name} on ${shape}: ${ms.toFixed(0)} ms (${count} matches)`);
    }
  }
  assert.deepEqual(slow, [], "no pattern takes longer than the budget on any shape");
});

test("runDetector on one assistant text holding a 40,000-char unbroken token finishes under the 1.5 s cap", async (t) => {
  for (const token of [repeatTo("x", 40_000), repeatTo("ab.cd-ef_gh~ij0", 40_000)]) {
    const env = await makeVault(t);
    await writeDfSnapshot(env.vault, { the: 90 });
    const clock = new Clock();
    const tx = new Transcript(env.transcriptPath, clock);
    tx.prompt("Look at the nightly stall.", "p1");
    const row = surfacedRow({ mkey: "vault:wiki/sessions/beryl-cadence.md", title: "Beryl harbor checkpoint cadence", promptId: "p1", ts: clock.next(10) });
    append(env.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: clock.next(5) }), row]);
    const reply = `Here is the dump: ${token} done.`;
    tx.say(reply);
    await tx.flush();
    const started = performance.now();
    const result = runDetector(detectorArgs(env.vault, env.transcriptPath, clock, { lastAssistantMessage: reply }));
    const ms = performance.now() - started;
    assert.equal(result.status, "ran");
    assert.ok(ms < 1500, `runDetector took ${ms.toFixed(0)} ms`);
    assert.equal(finalJudged(env.vault, row.sid).outcome, "ignored", "and the turn was still judged");
  }
});
