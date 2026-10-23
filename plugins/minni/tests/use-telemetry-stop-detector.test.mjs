// Recall-to-use S1b (§4.4): Stop runs the use detector for the session's main
// agent, after its output has reached the host. Deliver first, record only: a
// detector that throws, will not load, is slow, or hits its caps changes no
// byte of the Stop output and never fails the hook; a failure is audited once,
// throttled, by code and never with a path. With the ledger off nothing runs.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { appendFile, cp, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { DETECTOR_RULE_VERSION } from "../dist/use-detector.js";
import { auditTail } from "../dist/vault.js";
import { FIXTURE_SESSION, ledgerRows } from "./fixtures/use-detector-fixtures.mjs";
import { seedStopTurn } from "./use-stop-fixture.mjs";
import {
  DEFAULT_DIST,
  PLUGIN_ROOT,
  closeFixture,
  exists,
  hookEnv,
  makeFixture,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

/** A copy of dist with use-detector.js rewritten by `edit`, resolvable like the real one. */
async function mutatedDist(t, edit) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-stop-detector-dist-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dist = path.join(root, "dist");
  await cp(DEFAULT_DIST, dist, { recursive: true });
  const file = path.join(dist, "use-detector.js");
  const source = await readFile(file, "utf8");
  const edited = edit(source);
  assert.notEqual(edited, source, "the mutation applied");
  await writeFile(file, edited, "utf8");
  await writeFile(path.join(root, "package.json"), '{"type":"module"}\n', "utf8");
  await symlink(path.join(PLUGIN_ROOT, "node_modules"), path.join(root, "node_modules"));
  return dist;
}

const RUN_DETECTOR = "export function runDetector(args) {";
const throwsInRun = (source) =>
  source.replace(RUN_DETECTOR, `${RUN_DETECTOR}\n    throw new Error("detector boom at /private/secret/install/path");`);
const throwsAtLoad = (source) => `throw new Error("detector load boom at /private/secret/install/path");\n${source}`;
const slowRun = (source) =>
  source.replace(RUN_DETECTOR, `${RUN_DETECTOR}\n    { const until = Date.now() + 2500; while (Date.now() < until) { /* wedged */ } }`);

/** One Stop in a fresh fixture at `at` (so two runs share byte-identical paths). */
async function stopAt(at, { dist, env = {}, seed = true, keep = false } = {}) {
  const fixture = await makeFixture(null, "stop-detector", { at });
  await startFakeDaemon(fixture);
  try {
    const seeded = seed ? await seedStopTurn(fixture, { at: Date.parse("2026-10-23T10:00:00.000Z") }) : null;
    const payload = seeded?.payload ?? { session_id: "stop-detector-unseeded", hook_event_name: "Stop" };
    const result = await runHook(fixture, "Stop", payload, { env, ...(dist ? { dist } : {}) });
    return { ...result, fixture, seeded };
  } finally {
    if (!keep) await closeFixture(fixture);
  }
}

function detectorFailures(entries) {
  return entries.filter((entry) => entry.includes("hook_use_detector_failed"));
}

test("Stop judges the turn's window after delivery; the ledger gains its judged row", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "stop-detector-judges");
  await startFakeDaemon(fixture);
  const { payload, surfaced } = await seedStopTurn(fixture);
  const { code, output } = await runHook(fixture, "Stop", payload);
  assert.equal(code, 0);
  assert.equal(output.continue, true);
  const judged = ledgerRows(fixture.vault, "judged").filter((row) => row.sid === surfaced.sid);
  assert.equal(judged.length, 1, JSON.stringify(ledgerRows(fixture.vault)));
  assert.equal(judged[0].final, true, "the reply is in the transcript: the judgment is final");
  assert.equal(judged[0].rule_version, DETECTOR_RULE_VERSION);
  assert.equal(judged[0].agent_key, "main", "the session's main agent");
  assert.equal(judged[0].session_key, FIXTURE_SESSION, "keyed as capture keys it");
  const sessions = path.join(fixture.vault, ".runtime", "use-detector", "sessions", FIXTURE_SESSION);
  assert.ok(await exists(path.join(sessions, "windows.json")));
  assert.ok(!(await exists(path.join(sessions, "windows.lock"))), "the lock is released");

  // A second Stop over the same transcript writes nothing twice.
  await runHook(fixture, "Stop", payload);
  assert.equal(ledgerRows(fixture.vault, "judged").filter((row) => row.sid === surfaced.sid && row.final).length, 1);
});

test("with the ledger off (env or flags.json), Stop runs no detector", { timeout: 60_000 }, async (t) => {
  for (const [label, env, flags] of [
    ["env", { MINNI_USE_LEDGER: "off" }, null],
    ["file", {}, '{"MINNI_USE_LEDGER":"off"}'],
  ]) {
    const fixture = await makeFixture(t, `stop-detector-off-${label}`);
    await startFakeDaemon(fixture);
    const { payload } = await seedStopTurn(fixture);
    if (flags) await writeFile(path.join(fixture.vault, ".runtime", "flags.json"), flags, "utf8");
    const { code } = await runHook(fixture, "Stop", payload, { env });
    assert.equal(code, 0);
    assert.equal(ledgerRows(fixture.vault, "judged").length, 0, label);
    assert.ok(!(await exists(path.join(fixture.vault, ".runtime", "use-detector"))), `${label}: no detector state`);
  }
});

test("an undelivered Stop runs no detector", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "stop-detector-undelivered");
  await startFakeDaemon(fixture);
  const { payload } = await seedStopTurn(fixture);
  const code = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(DEFAULT_DIST, "hook.js"), "Stop"], {
      env: hookEnv(fixture),
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.on("error", reject);
    child.stderr.resume();
    child.on("close", resolve);
    child.stdin.end(JSON.stringify(payload));
    // Close the host's read end before the child can write: the flush fails.
    child.stdout.destroy();
  });
  assert.equal(code, 0);
  assert.equal(ledgerRows(fixture.vault, "judged").length, 0, "nothing is judged for an output that never landed");
});

test("deliver first: a wedged detector leaves the output byte-identical, already delivered", { timeout: 120_000 }, async (t) => {
  const dist = await mutatedDist(t, slowRun);
  const at = path.join(tmpdir(), `minni-stop-detector-slow-${process.pid}`);
  t.after(() => rm(at, { recursive: true, force: true }));
  const good = await stopAt(at);

  const fixture = await makeFixture(null, "stop-detector", { at });
  t.after(() => closeFixture(fixture));
  await startFakeDaemon(fixture);
  const { payload } = await seedStopTurn(fixture, { at: Date.parse("2026-10-23T10:00:00.000Z") });
  const timing = await new Promise((resolve, reject) => {
    const started = Date.now();
    let firstOut = null;
    let stdout = "";
    const child = spawn(process.execPath, [path.join(dist, "hook.js"), "Stop"], {
      env: hookEnv(fixture),
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.on("error", reject);
    child.stderr.resume();
    child.stdout.on("data", (chunk) => {
      firstOut ??= Date.now();
      stdout += chunk;
    });
    child.on("close", (code) => resolve({ code, stdout, firstOut: firstOut - started, exit: Date.now() - started }));
    child.stdin.end(JSON.stringify(payload));
  });
  assert.equal(timing.code, 0);
  assert.equal(timing.stdout.trim().split("\n").at(-1), good.raw, "the Stop output is byte-identical");
  assert.ok(
    timing.exit - timing.firstOut >= 2000,
    `the output reached the host before the detector ran (out at ${timing.firstOut} ms, exit at ${timing.exit} ms)`,
  );
});

test("a throwing detector: output byte-identical, exit clean, one audit line by code with no path", { timeout: 180_000 }, async (t) => {
  // runDetector throws (the module loads, so the receipt still reads the ledger).
  const inRun = await mutatedDist(t, throwsInRun);
  const at = path.join(tmpdir(), `minni-stop-detector-throw-${process.pid}`);
  t.after(() => rm(at, { recursive: true, force: true }));
  const good = await stopAt(at);
  assert.match(good.raw, /Use: 1 surfaced/, "positive control: the receipt reads the ledger");
  const bad = await stopAt(at, { dist: inRun, keep: true, env: { MINNI_BYPASS_AUDIT_LIMIT: "false" } });
  t.after(() => closeFixture(bad.fixture));
  assert.equal(bad.code, 0);
  assert.equal(bad.raw, good.raw, "a throw after delivery changes no byte of the output");
  assert.doesNotMatch(bad.raw, /degraded/);
  // A second Stop inside the throttle window: still one line.
  const again = await runHook(bad.fixture, "Stop", bad.seeded.payload, { dist: inRun, env: { MINNI_BYPASS_AUDIT_LIMIT: "false" } });
  assert.equal(again.code, 0);
  const lines = detectorFailures((await auditTail(bad.fixture.vault, 100)).entries);
  assert.equal(lines.length, 1, JSON.stringify(lines));
  assert.match(lines[0], /use detector threw/);
  assert.ok(!lines[0].includes("/private/secret") && !lines[0].includes("boom"), `no message, no path: ${lines[0]}`);

  // use-detector.js throws at load (a half-updated install): the same, by its own code.
  const atLoad = await mutatedDist(t, throwsAtLoad);
  const goodBare = await stopAt(at, { seed: false });
  const badLoad = await stopAt(at, { dist: atLoad, seed: false, keep: true });
  t.after(() => closeFixture(badLoad.fixture));
  assert.equal(badLoad.code, 0);
  assert.equal(badLoad.raw, goodBare.raw);
  const loadLines = detectorFailures((await auditTail(badLoad.fixture.vault, 100)).entries);
  assert.equal(loadLines.length, 1, JSON.stringify(loadLines));
  assert.match(loadLines[0], /use detector unavailable/);
  assert.ok(!loadLines[0].includes("/private/secret") && !loadLines[0].includes("use-detector.js"));
});

test("a capped detector (a transcript over 2 MB) carries forward: same output, no failure audit", { timeout: 120_000 }, async (t) => {
  const at = path.join(tmpdir(), `minni-stop-detector-cap-${process.pid}`);
  t.after(() => rm(at, { recursive: true, force: true }));
  const normal = await stopAt(at);

  const fixture = await makeFixture(null, "stop-detector", { at });
  t.after(() => closeFixture(fixture));
  await startFakeDaemon(fixture);
  const { payload, transcriptPath, surfaced } = await seedStopTurn(fixture, { at: Date.parse("2026-10-23T10:00:00.000Z") });
  // 2.5 MB of tool chatter after the reply, in short lines: the slow path's
  // 2 MB read cap stops short of the end, and the rest carries forward.
  const line = JSON.stringify({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "x".repeat(900) }] } });
  await appendFile(transcriptPath, `${line}\n`.repeat(Math.ceil((2.5 * 1024 * 1024) / (line.length + 1))), "utf8");
  const capped = await runHook(fixture, "Stop", payload);
  assert.equal(capped.code, 0);
  assert.equal(capped.raw, normal.raw, "the caps change no byte of the output");
  assert.equal(detectorFailures((await auditTail(fixture.vault, 100)).entries).length, 0, "a carry is not a failure");
  assert.equal(
    ledgerRows(fixture.vault, "judged").filter((row) => row.sid === surfaced.sid && row.final).length,
    0,
    "the turn's end was not reached: nothing is final yet",
  );
});
