// Recall-to-use S1a-2: with MINNI_USE_LEDGER off a hook costs about what it
// did before the ledger existed. dispatch asks one cheap check
// (ledger-gate.ts) and, when the ledger is off, never loads use-capture.js or
// what stands behind it (use-ledger, flags, harness-envelope). Observed with a
// module resolve hook in the spawned hook process.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { daemonResult, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const CAPTURE_MODULES = ["use-capture.js", "use-ledger.js", "flags.js", "harness-envelope.js", "harness-envelope-rules.js"];

/** Spawn a hook with a resolve hook that logs every module URL it loads. */
async function loadedModules(t, fixture, event, payload, env) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-ledger-gate-loads-"));
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
      '    appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${result.url}\\n`);',
      "    return result;",
      "  },",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
  const { code, output } = await runHook(fixture, event, payload, {
    env: { ...env, MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  assert.equal(code, 0);
  const text = await readFile(log, "utf8");
  return { output, names: text.split("\n").filter(Boolean).map((url) => url.split("/").pop()) };
}

test("ledger off: SessionStart, UPS and PreToolUse load none of the capture modules", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "ledger-gate-loads");
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results: [daemonResult({ source: "/e/a.md", docId: 1, wikilink: "[[a]]", title: "A" })] }),
  });
  const session = { session_id: "gate-loads" };
  for (const [event, payload] of [
    ["SessionStart", { ...session, source: "startup" }],
    ["UserPromptSubmit", { ...session, prompt: "where is the alpha note" }],
    ["PreToolUse", { ...session, tool_name: "Read", tool_input: { file_path: "/tmp/x" } }],
  ]) {
    const { names } = await loadedModules(t, fixture, event, payload, { MINNI_USE_LEDGER: "off" });
    assert.ok(names.includes("hook-handlers.js"), `${event}: the hook ran`);
    for (const module of CAPTURE_MODULES) {
      assert.ok(!names.includes(module), `${event} with the ledger off loaded ${module}`);
    }
  }
});

test("ledger off in flags.json: no capture module loads; ledger on (control): they do", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "ledger-gate-file");
  await startFakeDaemon(fixture);
  await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
  const flags = path.join(fixture.vault, ".runtime", "flags.json");
  const payload = { session_id: "gate-file", prompt: "a question" };

  await writeFile(flags, '{"MINNI_USE_LEDGER":"off"}', "utf8");
  const off = await loadedModules(t, fixture, "UserPromptSubmit", payload, {});
  for (const module of CAPTURE_MODULES) assert.ok(!off.names.includes(module), `flags.json off loaded ${module}`);

  await writeFile(flags, '{"MINNI_USE_LEDGER":"on"}', "utf8");
  const on = await loadedModules(t, fixture, "UserPromptSubmit", payload, {});
  for (const module of ["use-capture.js", "use-ledger.js", "flags.js", "harness-envelope.js"]) {
    assert.ok(on.names.includes(module), `with the ledger on, ${module} must load`);
  }
  assert.deepEqual(on.output, off.output, "loading the capture changes no output");
});

// S1b (§8.2): Stop's module-load contract, which replaces S1a-2's "a non-ledger
// event never loads the capture modules, whatever the flag". With the ledger
// off Stop loads no ledger module. With it on it loads the detector and the
// receipt's reader, never the capture writers, and writes only the closed list
// below (each path from the code that writes it).
const STOP_LEDGER_MODULES = [...CAPTURE_MODULES, "use-detector.js", "use-report.js"];

test("Stop with the ledger off loads no ledger module", { timeout: 60_000 }, async (t) => {
  delete process.env.MINNI_USE_LEDGER;
  const { seedStopTurn } = await import("./use-stop-fixture.mjs");
  const fixture = await makeFixture(t, "ledger-gate-stop-off");
  await startFakeDaemon(fixture);
  // A judgeable turn: with the ledger on, Stop would have work to do.
  const { payload } = await seedStopTurn(fixture);
  const envOff = await loadedModules(t, fixture, "Stop", payload, { MINNI_USE_LEDGER: "off" });
  assert.ok(envOff.names.includes("hook-handlers.js"), "the hook ran");
  for (const module of STOP_LEDGER_MODULES) {
    assert.ok(!envOff.names.includes(module), `Stop with MINNI_USE_LEDGER=off loaded ${module}`);
  }
  await writeFile(path.join(fixture.vault, ".runtime", "flags.json"), '{"MINNI_USE_LEDGER":"off"}', "utf8");
  const fileOff = await loadedModules(t, fixture, "Stop", payload, {});
  assert.ok(fileOff.names.includes("hook-handlers.js"), "the hook ran");
  for (const module of STOP_LEDGER_MODULES) {
    assert.ok(!fileOff.names.includes(module), `Stop with flags.json off loaded ${module}`);
  }
});

/** Like loadedModules, with each resolve's importer: [{ parent, child }] by file name. */
async function loadEdges(t, fixture, event, payload, env) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-ledger-gate-edges-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const log = path.join(dir, "edges.log");
  const preload = path.join(dir, "log-edges.mjs");
  await writeFile(
    preload,
    [
      'import { registerHooks } from "node:module";',
      'import { appendFileSync } from "node:fs";',
      "registerHooks({",
      "  resolve(specifier, context, nextResolve) {",
      "    const result = nextResolve(specifier, context);",
      '    appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${context.parentURL ?? ""} ${result.url}\\n`);',
      "    return result;",
      "  },",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
  const { code, output } = await runHook(fixture, event, payload, {
    env: { ...env, MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  assert.equal(code, 0);
  const edges = (await readFile(log, "utf8"))
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      const [parent, child] = line.split(" ");
      return { parent: parent.split("/").pop(), child: child.split("/").pop() };
    });
  return { output, edges, names: edges.map((edge) => edge.child) };
}

const DAY_FILE = /^use-ledger\/[0-9a-f]{16}\/\d{4}-\d{2}-\d{2}\.jsonl$/;
// Stop's closed write list under .runtime, ledger on, each from its writer:
const STOP_WRITES = [
  // the session's ledger day file: appendLedgerRows (use-ledger.ts, O_APPEND),
  // append-only, the detector's own rows (checked below);
  DAY_FILE,
  // the detector's per-session state: writeJsonAtomic (use-detector.ts);
  /^use-detector\/sessions\/[0-9a-f]{16}\/windows\.json$/,
  // its per-transcript cursor: writeCursor (use-detector.ts);
  /^use-detector\/cursors\/[0-9a-f]{16}\.json$/,
  // pre-existing Stop behaviour, independent of the ledger: the Jev harvest
  // cursor (jev-harvest.ts JEV_HARVEST_STATE_RELPATH), compared on vs off below.
  /^jev-harvest-state\.json$/,
];
// windows.lock (acquireLock), temp names (`.tmp`) and stale-lock asides are
// transient: absent once the hook has exited.

test(
  "Stop with the ledger on loads the detector and reader, never use-capture; the classifier loads only through the detector and Stop writes no template counts",
  { timeout: 180_000 },
  async (t) => {
    delete process.env.MINNI_USE_LEDGER;
    const { createHash } = await import("node:crypto");
    const { stat } = await import("node:fs/promises");
    const { changedPaths, seedStopTurn, snapshotTree } = await import("./use-stop-fixture.mjs");
    const { FIXTURE_SESSION, writeDfSnapshot } = await import("./fixtures/use-detector-fixtures.mjs");
    const { closeFixture } = await import("./use-telemetry-hook-fixture.mjs");
    const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");

    /** One Stop that judges a window, at a fixed root; the before/after .runtime snapshots. */
    async function judgedStop(at, { counts, env = {} }) {
      const fixture = await makeFixture(null, "ledger-gate-stop-on", { at });
      t.after(() => closeFixture(fixture));
      await startFakeDaemon(fixture);
      const { payload } = await seedStopTurn(fixture, { explicitRecall: true, at: Date.parse("2026-10-23T10:00:00.000Z") });
      const runtime = path.join(fixture.vault, ".runtime");
      // Capture's side files and the DF snapshot exist; Stop must not touch them.
      const side = path.join(runtime, "recall-session.d", FIXTURE_SESSION);
      await mkdir(side, { recursive: true });
      await writeFile(path.join(side, "counters.json"), '{"v":1,"turn_ix":1}', "utf8");
      await writeFile(path.join(side, "hit-keys.json"), '{"v":1,"task_signature":"t","hits":[]}', "utf8");
      await writeFile(path.join(side, "main.reset"), '{"reset_id":"r1","at":"2026-10-23T09:59:00.000Z","reason":"SessionStart:startup"}', "utf8");
      await writeDfSnapshot(fixture.vault);
      const countsFile = path.join(runtime, "prompt-templates.json");
      if (counts) await writeFile(countsFile, '{"v":1,"templates":{}}', "utf8");
      const before = await snapshotTree(runtime);
      const loaded = await loadEdges(t, fixture, "Stop", payload, env);
      const after = await snapshotTree(runtime);
      return { fixture, runtime, countsFile, before, after, ...loaded };
    }

    const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-gate-stop-on-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    for (const counts of [false, true]) {
      const run = await judgedStop(path.join(root, counts ? "with-counts" : "no-counts"), { counts });
      const label = counts ? "counts file present" : "no counts file";

      // 1-2. The detector and the reader load; the capture writers never do.
      for (const module of ["use-detector.js", "use-report.js"]) {
        assert.ok(run.names.includes(module), `${label}: Stop with the ledger on must load ${module}`);
      }
      assert.ok(!run.names.includes("use-capture.js"), `${label}: Stop loaded use-capture.js`);

      // 3. Any harness-envelope module is reached only through the detector.
      for (const { parent, child } of run.edges) {
        if (!child.startsWith("harness-envelope")) continue;
        assert.ok(
          parent === "use-detector.js" || parent.startsWith("harness-envelope"),
          `${label}: ${child} imported by ${parent}, not through use-detector.js`,
        );
      }

      // 4. The template counts file: absent stays absent; present stays byte-identical, same mtime.
      const countsRel = "prompt-templates.json";
      if (counts) {
        const was = run.before.get(countsRel);
        const now = run.after.get(countsRel);
        assert.ok(now, `${label}: the counts file is still there`);
        assert.equal(now.sha256, was.sha256, `${label}: counts file bytes changed`);
        assert.equal(now.mtimeMs, was.mtimeMs, `${label}: counts file mtime changed`);
      } else {
        await assert.rejects(stat(run.countsFile), { code: "ENOENT" }, `${label}: Stop created the counts file`);
      }

      // 5. Stop's writes are the closed list, and the day file only grows by the detector's own rows.
      const changed = changedPaths(run.before, run.after);
      for (const rel of changed) {
        assert.ok(STOP_WRITES.some((pattern) => pattern.test(rel)), `${label}: Stop wrote ${rel}, which is not on its list`);
        assert.ok(run.after.has(rel), `${label}: Stop removed ${rel}`);
      }
      for (const rel of run.after.keys()) {
        assert.ok(!/windows\.lock|\.tmp$|\.stale-/.test(rel), `${label}: transient ${rel} left behind`);
      }
      assert.ok(!changed.some((rel) => rel.startsWith("recall-session.d/")), `${label}: recall-session.d changed`);
      assert.ok(!changed.includes("df-snapshot.json"), `${label}: only UPS writes the DF snapshot`);
      const dayFiles = changed.filter((rel) => DAY_FILE.test(rel));
      assert.ok(dayFiles.length > 0, `${label}: the Stop judged nothing (positive control)`);
      const appended = [];
      for (const rel of dayFiles) {
        const old = run.before.get(rel)?.bytes ?? Buffer.alloc(0);
        const now = run.after.get(rel).bytes;
        assert.ok(now.length > old.length && now.subarray(0, old.length).equals(old), `${label}: ${rel} was rewritten, not appended`);
        for (const line of now.subarray(old.length).toString("utf8").split("\n")) {
          if (line.trim()) appended.push(JSON.parse(line));
        }
      }
      for (const row of appended) {
        const allowed =
          row.kind === "judged" || row.kind === "exposure" || (row.kind === "surfaced" && row.channel === "explicit_recall");
        assert.ok(allowed, `${label}: Stop appended a ${row.kind}${row.channel ? `/${row.channel}` : ""} row`);
      }
      assert.ok(appended.some((row) => row.kind === "judged"), `${label}: a judged row`);
      assert.ok(
        appended.some((row) => row.kind === "surfaced" && row.channel === "explicit_recall"),
        `${label}: the detector's explicit_recall rebuild (the one surfaced channel it writes)`,
      );

      // jev-harvest-state.json is base Stop behaviour: the same with the ledger off.
      if (!counts) {
        await closeFixture(run.fixture);
        const off = await judgedStop(path.join(root, "no-counts"), { counts, env: { MINNI_USE_LEDGER: "off" } });
        const onJev = run.after.get("jev-harvest-state.json");
        const offJev = off.after.get("jev-harvest-state.json");
        assert.equal(Boolean(onJev), Boolean(offJev), "jev-harvest-state.json: same presence on and off");
        if (onJev) assert.equal(sha(onJev.bytes), sha(offJev.bytes), "jev-harvest-state.json: byte-identical on and off");
      }
    }
  },
);
