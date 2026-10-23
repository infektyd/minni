// Recall-to-use S1a (§8.2): kill switches.
//
// Hooks launched by the desktop app do not reliably inherit the user's shell
// environment, so a kill switch that lives only in env cannot be thrown. Flags
// are therefore read from <vault>/.runtime/flags.json, with the same-named env
// var as an override. Parsing is strict (only "on"/"off"), every flag defaults
// on, and the file read refuses symlink escapes exactly like readRecallState.
import assert from "node:assert/strict";
import fs from "node:fs";
import { mkdir, mkdtemp, rm, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  FLAG_NAMES,
  FLAGS_RELPATH,
  featuresInEffect,
  flagOn,
  loadFlags,
  resetFlagsCacheForTests,
} from "../dist/flags.js";

const EXPECTED_FLAGS = [
  "MINNI_USE_LEDGER",
  "MINNI_BOOT_FIT",
  "MINNI_GUARD_V2",
  "MINNI_SUBAGENT_BRIEF",
  "MINNI_PROMPT_CLASSES",
  "MINNI_RECENT_LEARNINGS_RANK",
  "MINNI_UPS_RANK_V2",
  "MINNI_RECALL_NOISE_LAST",
];

async function makeVault(t, flags) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-flags-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  t.after(() => resetFlagsCacheForTests());
  resetFlagsCacheForTests();
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  if (flags !== undefined) {
    await writeFile(path.join(vault, FLAGS_RELPATH), typeof flags === "string" ? flags : JSON.stringify(flags), "utf8");
  }
  return { root, vault };
}

test("the flag names are exactly the §8.2 table", () => {
  assert.deepEqual([...FLAG_NAMES], EXPECTED_FLAGS);
});

test("every flag defaults on with no file and no env", async (t) => {
  const { vault } = await makeVault(t);
  for (const name of EXPECTED_FLAGS) assert.equal(flagOn(vault, name, {}), true, name);
  assert.deepEqual(featuresInEffect(vault, {}), [...EXPECTED_FLAGS].sort());
  assert.deepEqual(loadFlags(vault, {}).notes, []);
});

test("the file turns a flag off", async (t) => {
  const { vault } = await makeVault(t, { MINNI_USE_LEDGER: "off", MINNI_GUARD_V2: "on" });
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), false);
  assert.equal(flagOn(vault, "MINNI_GUARD_V2", {}), true);
  assert.ok(!featuresInEffect(vault, {}).includes("MINNI_USE_LEDGER"));
  assert.equal(loadFlags(vault, {}).sources.MINNI_USE_LEDGER, "file");
});

test("the same-named env var overrides the file, both ways", async (t) => {
  const { vault } = await makeVault(t, { MINNI_USE_LEDGER: "off", MINNI_BOOT_FIT: "on" });
  const env = { MINNI_USE_LEDGER: "on", MINNI_BOOT_FIT: "off" };
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", env), true);
  assert.equal(flagOn(vault, "MINNI_BOOT_FIT", env), false);
  assert.equal(loadFlags(vault, env).sources.MINNI_BOOT_FIT, "env");
});

test("junk values are ignored, in the file and in env, and leave a note", async (t) => {
  const { vault } = await makeVault(t, {
    MINNI_USE_LEDGER: "OFF",
    MINNI_BOOT_FIT: false,
    MINNI_GUARD_V2: "disabled",
    MINNI_NOT_A_FLAG: "off",
  });
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true, '"OFF" is not "off"');
  assert.equal(flagOn(vault, "MINNI_BOOT_FIT", {}), true, "a boolean is not a value");
  assert.equal(flagOn(vault, "MINNI_GUARD_V2", {}), true);

  const env = { MINNI_USE_LEDGER: "0", MINNI_UPS_RANK_V2: " off" };
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", env), true, "junk env falls back to file/default");
  assert.equal(flagOn(vault, "MINNI_UPS_RANK_V2", env), true);
  const { notes } = loadFlags(vault, env);
  assert.ok(notes.length >= 5, `one note per ignored value: ${JSON.stringify(notes)}`);
  for (const note of notes) assert.ok(!note.includes("\n"), "each note is one line");

  // A junk env value does not hide a valid file value.
  const { vault: v2 } = await makeVault(t, { MINNI_USE_LEDGER: "off" });
  assert.equal(flagOn(v2, "MINNI_USE_LEDGER", { MINNI_USE_LEDGER: "maybe" }), false);
});

test("a malformed or non-object file is ignored with a note", async (t) => {
  for (const body of ["{not json", "[\"off\"]", "\"off\"", "null"]) {
    const { vault } = await makeVault(t, body);
    assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true, body);
    assert.equal(loadFlags(vault, {}).notes.length, 1, body);
  }
});

test("a symlinked .runtime that escapes the vault is refused", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-flags-escape-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  t.after(() => resetFlagsCacheForTests());
  resetFlagsCacheForTests();
  const vault = path.join(root, "vault");
  const outside = path.join(root, "outside");
  await mkdir(vault, { recursive: true });
  await mkdir(outside, { recursive: true });
  await writeFile(path.join(outside, "flags.json"), JSON.stringify({ MINNI_USE_LEDGER: "off" }), "utf8");
  await symlink(outside, path.join(vault, ".runtime"));

  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true, "the escaped file is never read");
  assert.ok(loadFlags(vault, {}).notes.some((note) => /refused/.test(note)));
});

test("a flags.json that is itself a symlink is refused", async (t) => {
  const { root, vault } = await makeVault(t);
  const target = path.join(root, "elsewhere.json");
  await writeFile(target, JSON.stringify({ MINNI_USE_LEDGER: "off" }), "utf8");
  await symlink(target, path.join(vault, FLAGS_RELPATH));
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true);
});

test("a long-lived importer sees a flip once the file's mtime changes", async (t) => {
  const { vault } = await makeVault(t, { MINNI_USE_LEDGER: "on" });
  const file = path.join(vault, FLAGS_RELPATH);
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true);

  await writeFile(file, JSON.stringify({ MINNI_USE_LEDGER: "off" }), "utf8");
  const later = new Date(Date.now() + 5_000);
  await utimes(file, later, later);
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), false, "the server process sees the kill switch");

  // And back on, without any cache reset.
  await writeFile(file, JSON.stringify({ MINNI_USE_LEDGER: "on" }), "utf8");
  const later2 = new Date(Date.now() + 10_000);
  await utimes(file, later2, later2);
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true);

  // Deleting the file returns every flag to its default.
  await writeFile(file, JSON.stringify({ MINNI_USE_LEDGER: "off" }), "utf8");
  const later3 = new Date(Date.now() + 15_000);
  await utimes(file, later3, later3);
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), false);
  await rm(file);
  assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), true);
});

test("an unchanged file is parsed once; later calls only stat it", async (t) => {
  const { vault } = await makeVault(t, { MINNI_USE_LEDGER: "off" });
  const file = path.join(vault, FLAGS_RELPATH);
  const original = fs.readFileSync;
  let reads = 0;
  fs.readFileSync = function (target, ...rest) {
    if (target === file) reads += 1;
    return original.call(this, target, ...rest);
  };
  try {
    for (let i = 0; i < 5; i += 1) assert.equal(flagOn(vault, "MINNI_USE_LEDGER", {}), false);
  } finally {
    fs.readFileSync = original;
  }
  assert.equal(reads, 1, "one consistent snapshot while the file is unchanged");
});

test("a hostile vault path never throws", () => {
  for (const vaultPath of [undefined, null, 42, {}, "\0bad"]) {
    assert.doesNotThrow(() => flagOn(vaultPath, "MINNI_USE_LEDGER", {}));
    assert.equal(flagOn(vaultPath, "MINNI_USE_LEDGER", {}), true, String(vaultPath));
  }
});
