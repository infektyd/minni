// Recall-to-use S1b (§8.2 "Ledger-off stretches stay visible"): every
// `<prefix>_session_start` audit row says whether that boot records to the use
// ledger, `details.use_ledger: "on" | "off"`, and with the ledger on, the
// switches in effect, `details.features` (sorted, featuresInEffect's list).
// use-report lists the ledger-off stretches from exactly these two fields.
// With the ledger off the field costs nothing: no capture module loads.
import assert from "node:assert/strict";
import { cp, mkdir, mkdtemp, readFile, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { FLAG_NAMES } from "../dist/flags.js";
import {
  DEFAULT_DIST,
  PLUGIN_ROOT,
  makeFixture,
  readLedger,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const ALL_ON = [...FLAG_NAMES].sort();
const LEDGER_MODULES = ["use-capture.js", "use-ledger.js", "flags.js", "harness-envelope.js", "harness-envelope-rules.js"];

/** The details of the last `<tool>` row in the vault's rolling log. */
async function lastDetails(vault, tool) {
  const text = await readFile(path.join(vault, "log.md"), "utf8");
  const entries = text.split(/^## /m).filter((entry) => entry.includes(`] ${tool} |`));
  assert.ok(entries.length > 0, `no ${tool} row in log.md`);
  const json = /```json\s*([\s\S]*?)```/.exec(entries.at(-1));
  assert.ok(json, `the ${tool} row has details`);
  return JSON.parse(json[1]);
}

async function boot(t, label, { env = {}, flags, bin, dist, nodeArgs } = {}) {
  const fixture = await makeFixture(t, label);
  await startFakeDaemon(fixture);
  if (flags) {
    await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
    await writeFile(path.join(fixture.vault, ".runtime", "flags.json"), JSON.stringify(flags), "utf8");
  }
  const vaultEnv = bin === "codex-hook.js" ? { MINNI_CODEX_VAULT_PATH: fixture.vault } : {};
  const { code } = await runHook(
    fixture,
    "SessionStart",
    { session_id: `audit-${label}`, source: "startup" },
    { env: { ...vaultEnv, ...env }, ...(bin ? { bin } : {}), ...(dist ? { dist } : {}), ...(nodeArgs ? { nodeArgs } : {}) },
  );
  assert.equal(code, 0);
  return fixture;
}

test("ledger on: use_ledger is on and features lists the switches in effect, sorted", { timeout: 60_000 }, async (t) => {
  const fixture = await boot(t, "ss-audit-on");
  const details = await lastDetails(fixture.vault, "hook_session_start");
  assert.equal(details.use_ledger, "on");
  assert.deepEqual(details.features, ALL_ON);

  // One switch off in flags.json: it leaves the list, and the list matches the
  // `features` the same boot stamped on its ledger rows.
  const partial = await boot(t, "ss-audit-partial", { flags: { MINNI_GUARD_V2: "off", MINNI_BOOT_FIT: "off" } });
  const partialDetails = await lastDetails(partial.vault, "hook_session_start");
  assert.equal(partialDetails.use_ledger, "on");
  const expected = ALL_ON.filter((name) => name !== "MINNI_GUARD_V2" && name !== "MINNI_BOOT_FIT");
  assert.deepEqual(partialDetails.features, expected);
  const reset = (await readLedger(partial.vault)).find((row) => row.kind === "reset");
  assert.ok(reset, "the boot wrote its reset row");
  assert.deepEqual(reset.features, partialDetails.features);
});

test("ledger off (env, flags.json, or the capture module gone): use_ledger is off, no features", { timeout: 120_000 }, async (t) => {
  const envOff = await boot(t, "ss-audit-env-off", { env: { MINNI_USE_LEDGER: "off" } });
  const fileOff = await boot(t, "ss-audit-file-off", { flags: { MINNI_USE_LEDGER: "off" } });
  for (const fixture of [envOff, fileOff]) {
    const details = await lastDetails(fixture.vault, "hook_session_start");
    assert.equal(details.use_ledger, "off");
    assert.ok(!("features" in details), "features only with the ledger on");
  }

  // A stripped install: the capture module will not load, so nothing records.
  const root = await mkdtemp(path.join(tmpdir(), "minni-ss-audit-stripped-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dist = path.join(root, "dist");
  await cp(DEFAULT_DIST, dist, { recursive: true });
  await unlink(path.join(dist, "use-capture.js"));
  await writeFile(path.join(root, "package.json"), '{"type":"module"}\n', "utf8");
  await symlink(path.join(PLUGIN_ROOT, "node_modules"), path.join(root, "node_modules"));
  const stripped = await boot(t, "ss-audit-stripped", { dist });
  const strippedDetails = await lastDetails(stripped.vault, "hook_session_start");
  assert.equal(strippedDetails.use_ledger, "off");
  assert.ok(!("features" in strippedDetails));
});

test("ledger off: the field is computed without loading any ledger module", { timeout: 60_000 }, async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-ss-audit-loads-"));
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
  const fixture = await boot(t, "ss-audit-off-loads", {
    env: { MINNI_USE_LEDGER: "off", MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  assert.equal((await lastDetails(fixture.vault, "hook_session_start")).use_ledger, "off");
  const names = (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
  assert.ok(names.includes("hook-handlers.js"), "the hook ran");
  for (const module of [...LEDGER_MODULES, "use-retention.js", "use-detector.js", "use-report.js"]) {
    assert.ok(!names.includes(module), `SessionStart with the ledger off loaded ${module}`);
  }
});

test("every prefix carries the fields: the codex hook's hook_codex_session_start", { timeout: 60_000 }, async (t) => {
  const on = await boot(t, "ss-audit-codex-on", { bin: "codex-hook.js" });
  const onDetails = await lastDetails(on.vault, "hook_codex_session_start");
  assert.equal(onDetails.use_ledger, "on");
  assert.deepEqual(onDetails.features, ALL_ON);
  const off = await boot(t, "ss-audit-codex-off", { bin: "codex-hook.js", env: { MINNI_USE_LEDGER: "off" } });
  const offDetails = await lastDetails(off.vault, "hook_codex_session_start");
  assert.equal(offDetails.use_ledger, "off");
  assert.ok(!("features" in offDetails));
});
