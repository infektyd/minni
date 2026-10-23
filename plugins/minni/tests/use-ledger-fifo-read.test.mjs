// Recall-to-use S1b: the ledger reader never blocks on a FIFO.
//
// Stop reaches readSessionLedger through the receipt's use counts
// (sessionUseCounts), and the host waits for the hook to exit. An open(2) of
// a FIFO without O_NONBLOCK blocks until a writer appears, so a day file must
// be opened non-blocking and refused unless the OPENED descriptor is a regular
// file. Each case runs in a child process this test kills at 5 s, so a
// regression fails instead of hanging.
//
// Two shapes:
//   - plain: the day file is a FIFO;
//   - race: fs.statSync reports the FIFO as a regular file (as if it was
//     swapped in after a stat), so only a guard on the opened descriptor holds.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { appendLedgerRows, buildTurnRow, sessionKeyFrom } from "../dist/use-ledger.js";

delete process.env.MINNI_USE_LEDGER;

const KILL_MS = 5000;
const DIST = new URL("../dist/", import.meta.url);
const SESSION_ID = "fifo-read-session";

async function vaultWithFifoDay(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-fifo-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(vault, { recursive: true });
  const key = sessionKeyFrom({ session_id: SESSION_ID });
  // One real day file, so the reader has something to return, and a FIFO in a day file's name.
  const context = { session_key: key, agent_key: "main", wire: "claude-code", principal: "claude-code", features: ["MINNI_USE_LEDGER"] };
  const written = appendLedgerRows(vault, [
    buildTurnRow(context, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "strong" }, "2026-10-22T10:00:00.000Z"),
  ]);
  assert.equal(written.written, 1, "fixture row lands");
  const fifo = path.join(vault, ".runtime", "use-ledger", key, "2026-10-23.jsonl");
  execFileSync("mkfifo", [fifo]);
  const regularFile = path.join(root, "regular.jsonl");
  await writeFile(regularFile, "\n", "utf8");
  const worker = path.join(root, "worker.mjs");
  await writeFile(
    worker,
    [
      'import fs from "node:fs";',
      `const { readSessionLedger } = await import(${JSON.stringify(new URL("use-ledger.js", DIST).href)});`,
      `const { sessionUseCounts } = await import(${JSON.stringify(new URL("use-report.js", DIST).href)});`,
      "const args = JSON.parse(process.argv[2]);",
      "if (args.raceStat) {",
      "  const realStat = fs.statSync;",
      "  const regular = realStat(args.regularFile);",
      "  fs.statSync = function statSync(target, ...rest) {",
      "    return String(target) === args.raceStat ? regular : realStat.call(fs, target, ...rest);",
      "  };",
      "}",
      "const ledger = readSessionLedger(args.vault, args.key);",
      "const counts = sessionUseCounts(args.vault, args.sessionId);",
      "process.stdout.write(JSON.stringify({ rows: ledger.rows.length, refused: ledger.refused, counts }));",
      "",
    ].join("\n"),
    "utf8",
  );
  return { vault, key, fifo, regularFile, worker };
}

function runWorker(worker, args) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = execFile(process.execPath, [worker, JSON.stringify(args)], { timeout: KILL_MS, killSignal: "SIGKILL" }, (error, stdout) => {
      resolve({ ms: Date.now() - started, killed: Boolean(error && error.killed), error, out: stdout });
    });
    child.stdin?.end();
  });
}

for (const shape of ["plain", "race"]) {
  test(`readSessionLedger and the receipt's counts never block on a FIFO day file (${shape})`, { timeout: 30_000 }, async (t) => {
    const env = await vaultWithFifoDay(t);
    const result = await runWorker(env.worker, {
      vault: env.vault,
      key: env.key,
      sessionId: SESSION_ID,
      ...(shape === "race" ? { raceStat: env.fifo, regularFile: env.regularFile } : {}),
    });
    assert.equal(result.killed, false, `the reader was still blocked on the FIFO after ${result.ms} ms`);
    assert.ifError(result.error);
    const parsed = JSON.parse(result.out);
    assert.equal(parsed.rows, 1, "the regular day file is still read; the FIFO contributes nothing");
    assert.equal(parsed.refused, false);
  });
}
