// Recall-to-use S1b round 4b: the detector and the report never block on a FIFO.
//
// Under the Stop wiring the host waits for the hook to exit (up to its 600 s
// Stop timeout). An open(2) of a FIFO without O_NONBLOCK blocks until a
// writer appears, so every file the detector or the report opens must be
// opened non-blocking and refused unless the opened descriptor is a regular
// file. Each case runs in a child process the test kills at 5 s, so a
// regression fails the test instead of hanging it.
//
// Two shapes per target:
//   - plain: the path is a FIFO;
//   - race: fs.statSync reports the FIFO as a regular file (the file was
//     swapped after a stat), so only a guard on the opened descriptor holds.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, realpath, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  append,
  Clock,
  detectorArgs,
  FIXTURE_SESSION,
  makeVault,
  Transcript,
  turnRow,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const WORKER = fileURLToPath(new URL("./fixtures/use-detector-fifo-worker.mjs", import.meta.url));
const KILL_MS = 5000;
const BUDGET_MS = 2000;

/** A session with one quiet turn; returns the env and the detector args. */
async function quiet(t) {
  const env = await makeVault(t, "minni-fifo-");
  const clock = new Clock();
  const tx = new Transcript(env.transcriptPath, clock);
  tx.prompt("Rename the build target.", "p1");
  append(env.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: clock.next(5) })]);
  tx.say("Renamed.");
  await tx.flush();
  const regularFile = path.join(env.root, "regular.json");
  await writeFile(regularFile, "{}\n", "utf8");
  return { ...env, clock, regularFile, args: detectorArgs(env.vault, env.transcriptPath, clock, { lastAssistantMessage: "Renamed." }) };
}

async function fifoAt(file) {
  await mkdir(path.dirname(file), { recursive: true });
  execFileSync("mkfifo", [file]);
  return file;
}

/** Run the worker; resolves {ms, killed, out}. */
function runWorker(payload) {
  return new Promise((resolve) => {
    const started = performance.now();
    execFile(process.execPath, [WORKER, JSON.stringify(payload)], { timeout: KILL_MS, env: { ...process.env, MINNI_USE_LEDGER: "on" } }, (error, stdout) => {
      const ms = performance.now() - started;
      resolve({ ms, killed: Boolean(error && (error.killed || error.signal)), error, out: stdout ? JSON.parse(stdout) : null });
    });
  });
}

async function bounded(payload, label) {
  const run = await runWorker(payload);
  assert.equal(run.killed, false, `${label}: blocked on the FIFO until killed at ${KILL_MS} ms`);
  assert.ifError(run.error);
  assert.ok(run.ms < BUDGET_MS, `${label}: took ${run.ms.toFixed(0)} ms`);
  return run.out;
}

test("a FIFO as the transcript path: no hang, no transcript (path none)", async (t) => {
  const s = await quiet(t);
  const fifo = await fifoAt(path.join(s.root, "projects", "synthetic-project", "fifo-transcript.jsonl"));
  const out = await bounded({ detector: { ...s.args, transcriptPath: fifo } }, "transcript");
  assert.equal(out.status, "ran");
  assert.equal(out.path, "none");
  assert.equal(out.transcript_lines_parsed, 0);
});

for (const race of [false, true]) {
  const shape = race ? "race" : "plain";

  test(`${shape}: a FIFO in place of the cursor file`, async (t) => {
    const s = await quiet(t);
    const key = createHash("sha256").update(await realpath(s.transcriptPath), "utf8").digest("hex").slice(0, 16);
    const fifo = await fifoAt(path.join(s.vault, ".runtime", "use-detector", "cursors", `${key}.json`));
    const out = await bounded({ detector: s.args, ...(race ? { raceStat: fifo, regularFile: s.regularFile } : {}) }, `cursor ${shape}`);
    assert.equal(out.status, "ran", "the cursor is treated as absent");
  });

  test(`${shape}: a FIFO in place of windows.json`, async (t) => {
    const s = await quiet(t);
    const fifo = await fifoAt(path.join(s.vault, ".runtime", "use-detector", "sessions", FIXTURE_SESSION, "windows.json"));
    const out = await bounded({ detector: s.args, ...(race ? { raceStat: fifo, regularFile: s.regularFile } : {}) }, `windows.json ${shape}`);
    assert.equal(out.status, "ran", "the state is treated as fresh");
  });

  test(`${shape}: a FIFO in place of the DF snapshot`, async (t) => {
    const s = await quiet(t);
    const fifo = await fifoAt(path.join(s.vault, ".runtime", "df-snapshot.json"));
    const out = await bounded({ detector: s.args, ...(race ? { raceStat: fifo, regularFile: s.regularFile } : {}) }, `df ${shape}`);
    assert.equal(out.status, "ran");
    assert.equal(out.title_rule, "off", "no snapshot: the title rule is off");
  });

  test(`${shape}: a FIFO in place of a daily audit log read by the ledger-off listing`, async (t) => {
    const s = await quiet(t);
    const fifo = await fifoAt(path.join(s.vault, "logs", `${new Date().toISOString().slice(0, 10)}.md`));
    const out = await bounded({ report: true, vaultPath: s.vault, ...(race ? { raceStat: fifo, regularFile: s.regularFile } : {}) }, `audit log ${shape}`);
    assert.equal(out.session_starts, 0);
    assert.ok(out.days_without_log >= 1, "the unreadable day is counted, not read");
  });
}
