// Recall-to-use S1b: the flags reader never blocks on a FIFO.
//
// Stop reaches flags.ts through flagOn (the detector gate) and
// featuresInEffect, and the host waits for the hook to exit. An open(2) of a
// FIFO without O_NONBLOCK blocks until a writer appears, so `.runtime/
// flags.json` must be opened non-blocking and refused unless the OPENED
// descriptor is a regular file. Each case runs in a child process this test
// kills at 5 s, so a regression fails instead of hanging.
//
// Two shapes:
//   - plain: flags.json is a FIFO;
//   - race: fs.statSync reports the FIFO as a regular file (as if it was
//     swapped in after a stat), so only a guard on the opened descriptor holds.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const KILL_MS = 5000;
const DIST = new URL("../dist/", import.meta.url);

async function vaultWithFifoFlags(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-flags-fifo-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  const fifo = path.join(vault, ".runtime", "flags.json");
  execFileSync("mkfifo", [fifo]);
  const regularFile = path.join(root, "regular.json");
  await writeFile(regularFile, '{"MINNI_USE_LEDGER":"off"}', "utf8");
  const worker = path.join(root, "worker.mjs");
  await writeFile(
    worker,
    [
      'import fs from "node:fs";',
      "const args = JSON.parse(process.argv[2]);",
      "if (args.raceStat) {",
      "  const realStat = fs.statSync;",
      "  const regular = realStat(args.regularFile);",
      "  fs.statSync = function statSync(target, ...rest) {",
      "    return String(target) === args.raceStat ? regular : realStat.call(fs, target, ...rest);",
      "  };",
      "}",
      `const { featuresInEffect, flagOn, loadFlags } = await import(${JSON.stringify(new URL("flags.js", DIST).href)});`,
      "const on = flagOn(args.vault, 'MINNI_USE_LEDGER', {});",
      "const features = featuresInEffect(args.vault, {});",
      "const { notes } = loadFlags(args.vault, {});",
      "process.stdout.write(JSON.stringify({ on, features: features.length, notes }));",
      "",
    ].join("\n"),
    "utf8",
  );
  return { vault, fifo, regularFile, worker };
}

function runWorker(worker, args) {
  return new Promise((resolve) => {
    const started = Date.now();
    execFile(
      process.execPath,
      [worker, JSON.stringify(args)],
      { timeout: KILL_MS, killSignal: "SIGKILL", env: { ...process.env, MINNI_USE_LEDGER: "" } },
      (error, stdout) => resolve({ ms: Date.now() - started, killed: Boolean(error && error.killed), error, out: stdout }),
    );
  });
}

for (const shape of ["plain", "race"]) {
  test(`flagOn and featuresInEffect never block on a FIFO flags.json (${shape})`, { timeout: 30_000 }, async (t) => {
    const env = await vaultWithFifoFlags(t);
    const result = await runWorker(env.worker, {
      vault: env.vault,
      ...(shape === "race" ? { raceStat: env.fifo, regularFile: env.regularFile } : {}),
    });
    assert.equal(result.killed, false, `the flags read was still blocked on the FIFO after ${result.ms} ms`);
    assert.ifError(result.error);
    const parsed = JSON.parse(result.out);
    assert.equal(parsed.on, true, "a FIFO is no flags file: defaults (on)");
    assert.ok(
      parsed.notes.some((note) => /is not a regular file/.test(note)),
      `the refusal is noted: ${JSON.stringify(parsed.notes)}`,
    );
  });
}
