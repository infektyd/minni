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

test("a non-ledger event never loads the capture modules, whatever the flag", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "ledger-gate-stop");
  await startFakeDaemon(fixture);
  const { names } = await loadedModules(t, fixture, "Stop", { session_id: "gate-stop" }, { MINNI_USE_LEDGER: "on" });
  for (const module of CAPTURE_MODULES) assert.ok(!names.includes(module), `Stop loaded ${module}`);
});
