// Recall-to-use S1a-2: the cheap check in front of the use-ledger capture
// (ledger-gate.ts). It may say "off" only where flags.ts says "off" too; every
// doubt says "maybe on" and leaves the decision to flags.ts. A FIFO in the
// flags file's place cannot hang it. (That "off" loads nothing is pinned by
// use-telemetry-flag-off-cost.test.mjs.)
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { loadFlags, resetFlagsCacheForTests } from "../dist/flags.js";
import { ledgerMayBeOn } from "../dist/ledger-gate.js";

async function vaultWith(t, setup) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-gate-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(vault, { recursive: true });
  await setup?.({ root, vault, runtime: path.join(vault, ".runtime") });
  return vault;
}

async function flagsFile(runtime, content) {
  await mkdir(runtime, { recursive: true });
  await writeFile(path.join(runtime, "flags.json"), content, "utf8");
}

// [name, env, setup, expected gate answer]
const CASES = [
  ["env off", { MINNI_USE_LEDGER: "off" }, undefined, false],
  ["env on beats file off", { MINNI_USE_LEDGER: "on" }, (v) => flagsFile(v.runtime, '{"MINNI_USE_LEDGER":"off"}'), true],
  ["env neither on nor off is a doubt", { MINNI_USE_LEDGER: "OFF" }, undefined, true],
  ["env empty is a doubt", { MINNI_USE_LEDGER: "" }, undefined, true],
  ["no env, no file: the default", {}, undefined, true],
  ["file off", {}, (v) => flagsFile(v.runtime, '{"MINNI_USE_LEDGER":"off"}'), false],
  ["file on", {}, (v) => flagsFile(v.runtime, '{"MINNI_USE_LEDGER":"on"}'), true],
  ["file value not strict", {}, (v) => flagsFile(v.runtime, '{"MINNI_USE_LEDGER":"Off"}'), true],
  ["file not JSON", {}, (v) => flagsFile(v.runtime, "MINNI_USE_LEDGER=off"), true],
  ["file is an array", {}, (v) => flagsFile(v.runtime, '["MINNI_USE_LEDGER","off"]'), true],
  [
    "file over the size cap",
    {},
    (v) => flagsFile(v.runtime, JSON.stringify({ MINNI_USE_LEDGER: "off", pad: "x".repeat(70 * 1024) })),
    true,
  ],
  [
    "file is a directory",
    {},
    async (v) => mkdir(path.join(v.runtime, "flags.json"), { recursive: true }),
    true,
  ],
  [
    "file is a symlink",
    {},
    async (v) => {
      await mkdir(v.runtime, { recursive: true });
      const real = path.join(v.root, "elsewhere.json");
      await writeFile(real, '{"MINNI_USE_LEDGER":"off"}', "utf8");
      await symlink(real, path.join(v.runtime, "flags.json"));
    },
    true,
  ],
  [
    ".runtime is a symlink",
    {},
    async (v) => {
      const real = path.join(v.root, "runtime-elsewhere");
      await flagsFile(real, '{"MINNI_USE_LEDGER":"off"}');
      await symlink(real, v.runtime);
    },
    true,
  ],
  [
    "a FIFO in the file's place cannot hang the check",
    {},
    async (v) => {
      await mkdir(v.runtime, { recursive: true });
      execFileSync("mkfifo", [path.join(v.runtime, "flags.json")]);
    },
    true,
  ],
];

test("the gate answers off only when certain, and flags.ts agrees every time it does", async (t) => {
  for (const [name, env, setup, expected] of CASES) {
    if (process.platform === "win32" && name.includes("FIFO")) continue;
    const vault = await vaultWith(t, setup);
    resetFlagsCacheForTests();
    const started = Date.now();
    const gate = ledgerMayBeOn(vault, env);
    assert.ok(Date.now() - started < 1_000, `${name}: the check must not block`);
    assert.equal(gate, expected, name);
    // The gate is a pre-check: "off" from it must be "off" from flags.ts.
    // (flags.ts is not run on the FIFO case: it is only ever reached when the
    // gate says "maybe on", and its own reader is not what this test pins.)
    if (!name.includes("FIFO")) {
      const full = loadFlags(vault, env).values.MINNI_USE_LEDGER;
      if (gate === false) assert.equal(full, "off", `${name}: the gate said off, flags.ts said ${full}`);
    }
  }
});

test("an invalid vault path is a doubt, never an exception", () => {
  assert.equal(ledgerMayBeOn("", {}), true);
  assert.equal(ledgerMayBeOn("bad\0path", {}), true);
  assert.equal(ledgerMayBeOn(undefined, {}), true);
});
