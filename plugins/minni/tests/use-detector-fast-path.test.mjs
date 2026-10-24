// Recall-to-use S1b (§4.4 "Reading the transcript", §4.6): the fast path.
//
// With no window open, no new surfacing row, and a transcript delta that names
// neither minni_recall nor minni_drill, a Stop is a byte scan: no transcript
// line goes through JSON.parse. The per-transcript cursor still advances.
import assert from "node:assert/strict";
import { mkdir, readdir, readFile, stat, symlink } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  ledgerRows,
  makeVault,
  surfacedRow,
  Transcript,
  turnRow,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

/** Run `fn` with JSON.parse spied; returns [result, every string it parsed]. */
function withParseSpy(fn) {
  const original = JSON.parse;
  const parsed = [];
  JSON.parse = function spy(text, reviver) {
    parsed.push(String(text));
    return original.call(JSON, text, reviver);
  };
  try {
    return [fn(), parsed];
  } finally {
    JSON.parse = original;
  }
}

const isTranscriptLine = (text) => /"type":"(?:user|assistant)"/.test(text);

async function quietSession(t) {
  const env = await makeVault(t);
  const clock = new Clock();
  const tx = new Transcript(env.transcriptPath, clock);
  tx.prompt("Rename the build target.", "p1");
  append(env.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: clock.next(5) })]);
  tx.tool("Bash", { command: "git mv build.sh make.sh" });
  tx.say("Renamed.");
  await tx.flush();
  return { ...env, clock, tx };
}

test("a Stop with no open windows parses no transcript JSON and advances the cursor", async (t) => {
  const s = await quietSession(t);
  runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Renamed." }));

  s.tx.prompt("Now update the docs.", "p2");
  append(s.vault, [turnRow({ turnIx: 2, promptId: "p2", ts: s.clock.next(5) })]);
  s.tx.tool("Edit", { file_path: "/synthetic/README.md", old_string: "build.sh", new_string: "make.sh" });
  s.tx.say("Docs updated.");
  await s.tx.flush();

  const [result, parsed] = withParseSpy(() =>
    runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Docs updated." })),
  );
  assert.equal(result.status, "ran");
  assert.equal(result.path, "fast");
  assert.equal(result.transcript_lines_parsed, 0);
  assert.deepEqual(parsed.filter(isTranscriptLine), [], "no transcript line was parsed");

  const cursors = path.join(s.vault, ".runtime", "use-detector", "cursors");
  const [cursorFile] = await readdir(cursors);
  const cursor = JSON.parse(await readFile(path.join(cursors, cursorFile), "utf8"));
  assert.equal(cursor.offset, (await stat(s.transcriptPath)).size, "the cursor is at the end of the transcript");
});

test("a delta that names minni_recall takes the slow path", async (t) => {
  const s = await quietSession(t);
  runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Renamed." }));

  s.tx.prompt("What do we know about the build?", "p2");
  const id = s.tx.tool("mcp__minni__minni_recall", { query: "build target" });
  s.tx.result(id, "# Minni Recall\n\nNo vault wiki matches.");
  s.tx.say("Nothing on record.");
  await s.tx.flush();
  const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Nothing on record." }));
  assert.equal(result.path, "slow");
  assert.ok(result.transcript_lines_parsed > 0);
});

test("a new surfacing row takes the slow path", async (t) => {
  const s = await quietSession(t);
  runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Renamed." }));

  s.tx.prompt("Check the nightly stall.", "p2");
  append(s.vault, [surfacedRow({ mkey: "vault:wiki/sessions/stall.md", title: "Stall note", promptId: "p2", ts: s.clock.next(10) })]);
  s.tx.say("Looking.");
  await s.tx.flush();
  const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Looking." }));
  assert.equal(result.path, "slow");
});

test("MINNI_USE_LEDGER=off: the detector does nothing", async (t) => {
  const s = await quietSession(t);
  process.env.MINNI_USE_LEDGER = "off";
  try {
    const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Renamed." }));
    assert.equal(result.status, "disabled");
    await assert.rejects(stat(path.join(s.vault, ".runtime", "use-detector")), { code: "ENOENT" });
  } finally {
    delete process.env.MINNI_USE_LEDGER;
  }
});

test("hostile arguments never throw", () => {
  for (const args of [undefined, null, {}, { vaultPath: 7 }, { vaultPath: "/nonexistent-synthetic", session_key: "../x", agent_key: "main" }]) {
    const result = runDetector(args);
    assert.notEqual(result.status, "ran");
  }
});

test("a symlinked detector directory that escapes the vault is refused; nothing is written outside", async (t) => {
  const s = await quietSession(t);
  const outside = path.join(s.root, "outside");
  await mkdir(outside, { recursive: true });
  await mkdir(path.join(s.vault, ".runtime"), { recursive: true });
  await symlink(outside, path.join(s.vault, ".runtime", "use-detector"));
  const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Renamed." }));
  assert.equal(result.status, "refused");
  assert.deepEqual(await readdir(outside), []);
});

test("under a small byte cap the slow path carries work forward and finishes without duplicates", async (t) => {
  const s = await quietSession(t);
  s.tx.prompt("Check the nightly stall.", "p2");
  const row = surfacedRow({ mkey: "vault:wiki/sessions/stall.md", title: "Stall note", promptId: "p2", ts: s.clock.next(10) });
  append(s.vault, [row]);
  for (let i = 0; i < 6; i += 1) s.tx.tool("Bash", { command: `echo step ${i} of a long synthetic turn` });
  s.tx.say("Looked at the stall.");
  await s.tx.flush();

  const statuses = [];
  for (let i = 0; i < 40; i += 1) {
    const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Looked at the stall.", maxReadBytes: 700 }));
    statuses.push(result.carried);
    if (!result.carried) break;
  }
  assert.ok(statuses.includes(true), "the cap made at least one run carry");
  assert.equal(statuses.at(-1), false, "a later run reached the end");
  const judged = ledgerRows(s.vault, "judged").filter((j) => j.sid === row.sid);
  assert.deepEqual(judged.map((j) => j.final), [true], "one final judgment, written once");
});
