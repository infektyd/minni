// Recall-to-use S1b (§4.4 "Single writer", §4.6): the detector holds an O_EXCL
// lock, windows.lock, per session. A lock older than 30 s is broken; one it
// cannot take within 200 ms makes the run skip. Two concurrent Stops for one
// session leave a valid windows.json and no duplicate judged rows.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, readFile, stat, utimes, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  ledgerRows,
  makeVault,
  FIXTURE_SESSION,
  surfacedRow,
  Transcript,
  turnRow,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const WORKER = fileURLToPath(new URL("./fixtures/use-detector-worker.mjs", import.meta.url));

function sessionDir(vault) {
  return path.join(vault, ".runtime", "use-detector", "sessions", FIXTURE_SESSION);
}

async function threeTurns(t) {
  const env = await makeVault(t);
  const clock = new Clock();
  const tx = new Transcript(env.transcriptPath, clock);
  const sids = [];
  for (let ix = 1; ix <= 3; ix += 1) {
    tx.prompt(`Synthetic prompt number ${ix}.`, `p${ix}`);
    const row = surfacedRow({ mkey: `vault:wiki/sessions/note-${ix}.md`, title: `Note ${ix}`, promptId: `p${ix}`, ts: clock.next(10) });
    append(env.vault, [turnRow({ turnIx: ix, promptId: `p${ix}`, ts: row.ts }), row]);
    sids.push(row.sid);
    tx.say(`Reply number ${ix}.`);
  }
  await tx.flush();
  return { ...env, clock, sids };
}

function runWorker(args, rounds) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [WORKER, JSON.stringify(args), String(rounds)], {
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, MINNI_USE_LEDGER: "on" },
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (chunk) => (out += chunk));
    child.stderr.on("data", (chunk) => (err += chunk));
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve(JSON.parse(out)) : reject(new Error(`worker exit ${code}: ${err}`))));
  });
}

test("two concurrent Stops for one session: a valid windows.json and no duplicate judged rows", async (t) => {
  const s = await threeTurns(t);
  const args = detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Reply number 3." });
  const [a, b] = await Promise.all([runWorker(args, 6), runWorker(args, 6)]);
  const statuses = [...a, ...b];
  assert.ok(statuses.includes("ran"), statuses.join(","));
  for (const status of statuses) assert.ok(["ran", "locked"].includes(status), status);

  const state = JSON.parse(await readFile(path.join(sessionDir(s.vault), "windows.json"), "utf8"));
  assert.equal(state.session_key, FIXTURE_SESSION);
  await assert.rejects(stat(path.join(sessionDir(s.vault), "windows.lock")), { code: "ENOENT" }, "the lock was released");

  const judged = ledgerRows(s.vault, "judged");
  const keys = judged.map((row) => `${row.sid}:${row.final}`);
  assert.equal(new Set(keys).size, keys.length, "no (sid, final) pair written twice");
  for (const sid of s.sids) {
    assert.equal(judged.filter((row) => row.sid === sid && row.final).length, 1, `one final row for ${sid}`);
  }
});

test("a lock older than 30 s is broken; a fresh one makes the run skip within the wait budget", async (t) => {
  const s = await threeTurns(t);
  await mkdir(sessionDir(s.vault), { recursive: true });
  const lock = path.join(sessionDir(s.vault), "windows.lock");

  await writeFile(lock, "held by a synthetic peer\n", "utf8");
  const started = Date.now();
  const skipped = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Reply number 3." }));
  const waited = Date.now() - started;
  assert.equal(skipped.status, "locked");
  assert.ok(waited < 2000, `gave up promptly (${waited} ms)`);
  assert.equal(ledgerRows(s.vault, "judged").length, 0, "a skipped run writes nothing");
  assert.ok((await stat(lock)).isFile(), "someone else's lock is left alone");

  const old = new Date(Date.now() - 60_000);
  await utimes(lock, old, old);
  const ran = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage: "Reply number 3." }));
  assert.equal(ran.status, "ran");
  assert.equal(ledgerRows(s.vault, "judged").filter((row) => row.final).length, 3);
  await assert.rejects(stat(lock), { code: "ENOENT" });
});
