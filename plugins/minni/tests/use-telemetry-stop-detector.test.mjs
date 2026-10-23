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
  const { detectorCursorOffset, seedNextTurn } = await import("./use-stop-fixture.mjs");
  const at = path.join(tmpdir(), `minni-stop-detector-cap-${process.pid}`);
  t.after(() => rm(at, { recursive: true, force: true }));
  const T1 = Date.parse("2026-10-23T10:00:00.000Z");
  const T2 = Date.parse("2026-10-23T10:05:00.000Z");
  // 2.5 MB of tool chatter after turn 2's reply, in short lines: read from the
  // cursor turn 1 left, the slow path's 2 MB cap stops short of the end.
  const line = JSON.stringify({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "x".repeat(900) }] } });
  const chatter = `${line}\n`.repeat(Math.ceil((2.5 * 1024 * 1024) / (line.length + 1)));
  const outputs = {};
  for (const variant of ["normal", "capped"]) {
    const fixture = await makeFixture(null, "stop-detector", { at });
    await startFakeDaemon(fixture);
    try {
      const first = await seedStopTurn(fixture, { at: T1 });
      assert.equal((await runHook(fixture, "Stop", first.payload)).code, 0);
      const before = await detectorCursorOffset(fixture.vault);
      assert.ok(before > 0, "turn 1 left a cursor");
      const second = await seedNextTurn(fixture, first.transcriptPath, {
        turnIx: 2,
        at: T2,
        title: "Heron cursor drift",
        after: variant === "capped" ? chatter : "",
      });
      const run = await runHook(fixture, "Stop", second.payload);
      assert.equal(run.code, 0);
      outputs[variant] = run.raw;
      if (variant === "capped") {
        assert.equal(detectorFailures((await auditTail(fixture.vault, 100)).entries).length, 0, "a carry is not a failure");
        // Positive controls: the detector ran, and its read stopped at the cap.
        assert.ok(await exists(path.join(fixture.vault, ".runtime", "use-detector", "sessions", FIXTURE_SESSION, "windows.json")));
        const advanced = (await detectorCursorOffset(fixture.vault)) - before;
        assert.ok(
          advanced > 2 * 1024 * 1024 - 4096 && advanced <= 2 * 1024 * 1024,
          `the read advanced ${advanced} bytes: the 2 MB cap, not the whole 2.5 MB`,
        );
        assert.equal(
          ledgerRows(fixture.vault, "judged").filter((row) => row.sid === second.surfaced.sid && row.final).length,
          0,
          "turn 2's end was not reached: nothing is final yet",
        );
      }
    } finally {
      await closeFixture(fixture);
    }
  }
  assert.equal(outputs.capped, outputs.normal, "the caps change no byte of the output");
});

test("a transcript_path that is a FIFO never holds the host: Stop exits cleanly, promptly", { timeout: 60_000 }, async (t) => {
  const { execFileSync } = await import("node:child_process");
  const fixture = await makeFixture(t, "stop-detector-fifo");
  await startFakeDaemon(fixture);
  const { payload } = await seedStopTurn(fixture);
  const fifo = path.join(fixture.root, "transcript.fifo");
  execFileSync("mkfifo", [fifo]);
  const started = Date.now();
  const result = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(DEFAULT_DIST, "hook.js"), "Stop"], {
      env: hookEnv(fixture),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    const killer = setTimeout(() => child.kill("SIGKILL"), 20_000);
    child.on("error", reject);
    child.stderr.resume();
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.on("close", (code, signal) => {
      clearTimeout(killer);
      resolve({ code, signal, stdout });
    });
    child.stdin.end(JSON.stringify({ ...payload, transcript_path: fifo }));
  });
  assert.equal(result.signal, null, `the hook was still running after ${Date.now() - started} ms (blocked on the FIFO)`);
  assert.equal(result.code, 0);
  assert.equal(JSON.parse(result.stdout.trim().split("\n").at(-1)).continue, true);
  assert.equal(detectorFailures((await auditTail(fixture.vault, 100)).entries).length, 0, "no transcript is not a failure");
  // Positive control: the detector did run, with no transcript.
  assert.ok(
    await exists(path.join(fixture.vault, ".runtime", "use-detector", "sessions", FIXTURE_SESSION, "windows.json")),
    "the detector ran (its state was written) even though the transcript was refused",
  );
});

/** The module file names a spawned hook resolves. */
async function loadedNames(t, fixture, event, payload, { env = {}, bin } = {}) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-stop-detector-loads-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const log = path.join(dir, "loads.log");
  const preload = path.join(dir, "log-resolve.mjs");
  await writeFile(
    preload,
    [
      'import { registerHooks } from "node:module";',
      'import { appendFileSync } from "node:fs";',
      "registerHooks({",
      "  resolve(specifier, context, nextResolve) {",
      "    const result = nextResolve(specifier, context);",
      "    appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${result.url}\\n`);",
      "    return result;",
      "  },",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
  const { pathToFileURL } = await import("node:url");
  const result = await runHook(fixture, event, payload, {
    env: { ...env, MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
    ...(bin ? { bin } : {}),
  });
  const names = (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
  return { ...result, names };
}

const LEDGER_MODULES = [
  "use-capture.js",
  "use-ledger.js",
  "flags.js",
  "harness-envelope.js",
  "harness-envelope-rules.js",
  "use-detector.js",
  "use-report.js",
];

test("an unmeasured wire (codex) with the ledger on: no detector, no receipt counts, no ledger module, today's line", { timeout: 120_000 }, async (t) => {
  const at = path.join(tmpdir(), `minni-stop-detector-codex-${process.pid}`);
  t.after(() => rm(at, { recursive: true, force: true }));
  const raws = {};
  for (const ledger of ["on", "off"]) {
    const fixture = await makeFixture(null, "stop-detector-codex", { at });
    await startFakeDaemon(fixture);
    try {
      // A Claude-shaped transcript the detector could parse: only the wire gate stops it.
      const { payload } = await seedStopTurn(fixture, { at: Date.parse("2026-10-23T10:00:00.000Z") });
      const run = await loadedNames(t, fixture, "Stop", payload, {
        bin: "codex-hook.js",
        env: { MINNI_CODEX_VAULT_PATH: fixture.vault, MINNI_USE_LEDGER: ledger },
      });
      assert.equal(run.code, 0);
      assert.ok(run.names.includes("hook-handlers.js"), "the hook ran");
      raws[ledger] = run.raw;
      if (ledger === "on") {
        for (const module of LEDGER_MODULES) assert.ok(!run.names.includes(module), `codex Stop loaded ${module}`);
        assert.equal(ledgerRows(fixture.vault, "judged").length, 0, "an unmeasured wire writes no judged rows");
        assert.ok(!(await exists(path.join(fixture.vault, ".runtime", "use-detector"))));
        assert.doesNotMatch(run.raw, / Use: /);
      }
    } finally {
      await closeFixture(fixture);
    }
  }
  assert.equal(raws.on, raws.off, "the receipt line is today's, byte-identical to the ledger off");

  // The measured wire is unchanged: the same seed on claude-code judges and counts.
  const fixture = await makeFixture(t, "stop-detector-claude-measured");
  await startFakeDaemon(fixture);
  const { payload, surfaced } = await seedStopTurn(fixture);
  const claude = await runHook(fixture, "Stop", payload);
  assert.match(claude.raw, / Use: 1 surfaced, 0 cited, 0 applied\./);
  assert.equal(ledgerRows(fixture.vault, "judged").filter((row) => row.sid === surfaced.sid).length, 1);
});

test("the detector's commit runs last: a wedged detector never delays the parked-correction archive", { timeout: 120_000 }, async (t) => {
  const dist = await mutatedDist(t, slowRun);
  const fixture = await makeFixture(t, "stop-detector-order");
  await startFakeDaemon(fixture);
  const { payload } = await seedStopTurn(fixture);
  // A correction SessionStart parked as undeliverable: claude-code's Stop can
  // inject, so it re-asserts it and archives the entry once delivered.
  const parked = path.join(fixture.vault, "inbox", ".undeliverable");
  const { mkdir, readdir } = await import("node:fs/promises");
  await mkdir(parked, { recursive: true });
  const stamp = Date.now() - 86_400_000;
  await writeFile(
    path.join(parked, `${new Date(stamp).toISOString().slice(0, 10)}-${stamp.toString(36)}-precompact-reassert.json`),
    JSON.stringify({
      slug: "precompact-reassert",
      kind: "precompact_reassert",
      createdAt: new Date(stamp).toISOString(),
      stale_belief_events: [
        { event_id: 4242, superseded_learning_id: 11, new_learning_id: 12, originating_agent: "codex", created_at: 1_754_000_000 },
      ],
    }),
    "utf8",
  );
  const listJson = async (dir) => (await readdir(dir).catch(() => [])).filter((name) => name.endsWith(".json"));
  const observed = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(dist, "hook.js"), "Stop"], { env: hookEnv(fixture), stdio: ["pipe", "pipe", "pipe"] });
    let exited = false;
    let stdout = "";
    let check = null;
    child.on("error", reject);
    child.stderr.resume();
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      // 700 ms after the output is delivered: inside the detector's 2.5 s wedge.
      check ??= new Promise((done) =>
        setTimeout(async () => {
          done({ exited, parked: await listJson(parked), archived: await listJson(path.join(fixture.vault, "inbox", ".archive")) });
        }, 700),
      );
    });
    child.on("close", async (code) => {
      exited = true;
      resolve({ code, stdout, during: await check });
    });
    child.stdin.end(JSON.stringify(payload));
  });
  assert.equal(observed.code, 0);
  assert.match(observed.stdout, /corrections_reassert/, "positive control: Stop re-asserted the parked correction");
  assert.equal(observed.during.exited, false, "the check ran while the wedged detector was still running");
  assert.deepEqual(observed.during.parked, [], "the parked entry was archived before the detector ran");
  assert.equal(observed.during.archived.length, 1);
});

test("a Stop with no session id is unmeasured: no detector run, no detector state, no ledger module", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "stop-detector-no-session");
  await startFakeDaemon(fixture);
  const { payload } = await seedStopTurn(fixture);
  const { session_id: _dropped, ...noSession } = payload;
  const run = await loadedNames(t, fixture, "Stop", noSession);
  assert.equal(run.code, 0);
  assert.ok(run.names.includes("hook-handlers.js"), "the hook ran");
  for (const module of LEDGER_MODULES) assert.ok(!run.names.includes(module), `a session-less Stop loaded ${module}`);
  assert.ok(!(await exists(path.join(fixture.vault, ".runtime", "use-detector"))), "no detector state under a tx- key");
  assert.equal(ledgerRows(fixture.vault, "judged").length, 0);
});
