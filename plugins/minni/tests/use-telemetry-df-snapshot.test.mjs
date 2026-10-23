// Recall-to-use S1b (§4.4 "Distinctive tokens"): the use detector judges
// titles against `.runtime/df-snapshot.json`, and never scans the vault. UPS
// writes the snapshot from the vault scan it already runs: at most once a day,
// after delivery, record-only, and only with the ledger on. The scan's texts
// come from `scannedNoteTexts`, which reads the search cache (no I/O, a fresh
// array) and is null when this process ran no scan.
import assert from "node:assert/strict";
import { lstat, mkdir, mkdtemp, readFile, rm, stat, symlink, unlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { DF_SNAPSHOT_MAX_AGE_MS, dfSnapshotDue, refreshDfSnapshot } from "../dist/use-capture.js";
import { DF_SNAPSHOT_RELPATH, readDfSnapshot } from "../dist/use-detector.js";
import { scannedNoteTexts, searchVaultNotes } from "../dist/vault.js";
import { exists, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const HOUR = 60 * 60 * 1000;

async function vaultWithNotes(t, label) {
  const root = await mkdtemp(path.join(tmpdir(), `minni-df-${label}-`));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await writeNotes(vault);
  return { root, vault };
}

async function writeNotes(vault) {
  const wiki = path.join(vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  await writeFile(path.join(wiki, "alpha.md"), "# Alpha retention rule\n\nThe alpha rule keeps pelican data.\n", "utf8");
  await writeFile(path.join(wiki, "beta.md"), "# Beta release notes\n\nThe beta train ships alpha fixes.\n", "utf8");
  await writeFile(
    path.join(wiki, "gamma.md"),
    "---\nstatus: superseded\n---\n# Gamma old plan\n\nZanzibarquux is only here.\n",
    "utf8",
  );
}

test("scannedNoteTexts: null before a scan; the scan's texts after; a fresh array; no I/O", async (t) => {
  const { vault } = await vaultWithNotes(t, "texts");
  assert.equal(scannedNoteTexts(vault), null, "no scan ran in this process for this vault");
  await searchVaultNotes(vault, "alpha retention rule", 5);
  const texts = scannedNoteTexts(vault);
  assert.ok(Array.isArray(texts));
  assert.ok(texts.some((text) => text.startsWith("Alpha retention rule\n") && text.includes("pelican data")));
  assert.ok(texts.some((text) => text.includes("beta train")));
  assert.ok(!texts.some((text) => text.includes("Zanzibarquux")), "a note search never scores is left out");

  // A caller mutating the result cannot touch the cache.
  const before = [...texts];
  texts.length = 0;
  texts.push("injected");
  assert.deepEqual(scannedNoteTexts(vault), before);
  assert.notEqual(scannedNoteTexts(vault), scannedNoteTexts(vault), "a fresh array every call");

  // No I/O: with the notes gone from disk it still answers from the cache.
  await rm(path.join(vault, "wiki", "concepts"), { recursive: true, force: true });
  assert.deepEqual(scannedNoteTexts(vault), before);
});

test("refreshDfSnapshot: no scan writes nothing; due writes; fresh skips; stale and future rewrite", async (t) => {
  const { vault } = await vaultWithNotes(t, "refresh");
  const file = path.join(vault, DF_SNAPSHOT_RELPATH);
  assert.equal(await refreshDfSnapshot(vault), "no_scan");
  assert.ok(!(await exists(file)));

  await searchVaultNotes(vault, "alpha retention rule", 5);
  assert.equal(await refreshDfSnapshot(vault), "written");
  const snapshot = JSON.parse(await readFile(file, "utf8"));
  assert.equal(snapshot.v, 1);
  assert.equal(snapshot.notes, scannedNoteTexts(vault).length);
  assert.ok(readDfSnapshot(vault), "the detector reads what UPS wrote");

  const written = await readFile(file);
  assert.equal(await refreshDfSnapshot(vault), "fresh");
  assert.deepEqual(await readFile(file), written);

  for (const when of [new Date(Date.now() - DF_SNAPSHOT_MAX_AGE_MS - HOUR), new Date(Date.now() + 48 * HOUR)]) {
    await utimes(file, when, when);
    assert.equal(dfSnapshotDue(vault), true);
    assert.equal(await refreshDfSnapshot(vault), "written");
    const age = Date.now() - (await stat(file)).mtimeMs;
    assert.ok(Math.abs(age) < HOUR, "rewritten now");
  }
});

test("a symlinked or directory snapshot path is never followed, and never recounted", async (t) => {
  const { root, vault } = await vaultWithNotes(t, "link");
  await searchVaultNotes(vault, "alpha retention rule", 5);
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  const file = path.join(vault, DF_SNAPSHOT_RELPATH);
  const outside = path.join(root, "outside.json");
  await writeFile(outside, "keep me", "utf8");
  const old = new Date(Date.now() - 3 * DF_SNAPSHOT_MAX_AGE_MS);
  await utimes(outside, old, old);
  await symlink(outside, file);
  assert.equal(dfSnapshotDue(vault), false, "the safe write would refuse a link: no count is paid for it");
  assert.equal(await refreshDfSnapshot(vault), "fresh");
  assert.equal(await readFile(outside, "utf8"), "keep me");
  assert.ok((await lstat(file)).isSymbolicLink());

  await unlink(file);
  await mkdir(file);
  assert.equal(dfSnapshotDue(vault), false);
  assert.equal(await refreshDfSnapshot(vault), "fresh");
  assert.ok((await stat(file)).isDirectory());
});

function upsPayload(prompt) {
  return { session_id: "df-hook", prompt_id: "p1", prompt };
}

async function hookFixture(t, label) {
  const fixture = await makeFixture(t, label);
  await startFakeDaemon(fixture);
  await writeNotes(fixture.vault);
  return fixture;
}

test("UPS writes the snapshot after delivery with the ledger on, at most once a day", { timeout: 120_000 }, async (t) => {
  const fixture = await hookFixture(t, "df-hook-on");
  const file = path.join(fixture.vault, DF_SNAPSHOT_RELPATH);
  const first = await runHook(fixture, "UserPromptSubmit", upsPayload("where is the alpha retention rule"));
  assert.equal(first.code, 0);
  assert.ok(await exists(file), "the first scanned prompt writes it");
  const snapshot = JSON.parse(await readFile(file, "utf8"));
  // The same count the scan saw: compare against this process's own scan.
  await searchVaultNotes(fixture.vault, "alpha retention rule", 5);
  assert.equal(snapshot.notes, scannedNoteTexts(fixture.vault).length);

  const bytes = await readFile(file);
  const { mtimeMs } = await stat(file);
  const second = await runHook(fixture, "UserPromptSubmit", upsPayload("and the beta release notes"));
  assert.equal(second.code, 0);
  assert.deepEqual(await readFile(file), bytes, "within a day: not rewritten");
  assert.equal((await stat(file)).mtimeMs, mtimeMs);

  const stale = new Date(Date.now() - DF_SNAPSHOT_MAX_AGE_MS - HOUR);
  await utimes(file, stale, stale);
  await runHook(fixture, "UserPromptSubmit", upsPayload("the alpha rule again"));
  assert.ok(Math.abs(Date.now() - (await stat(file)).mtimeMs) < HOUR, "a day later it is rewritten");
});

test("no snapshot with the ledger off, on an empty query, or on a spent budget", { timeout: 120_000 }, async (t) => {
  for (const [label, prompt, env] of [
    ["env-off", "where is the alpha retention rule", { MINNI_USE_LEDGER: "off" }],
    ["no-terms", "?", {}],
    ["spent-budget", "where is the alpha retention rule", { MINNI_HOOK_BUDGET_MS: "1" }],
  ]) {
    const fixture = await hookFixture(t, `df-hook-${label}`);
    const { code } = await runHook(fixture, "UserPromptSubmit", upsPayload(prompt), { env });
    assert.equal(code, 0, label);
    assert.ok(!(await exists(path.join(fixture.vault, DF_SNAPSHOT_RELPATH))), `${label}: no snapshot`);
  }
  // flags.json off too.
  const fileOff = await hookFixture(t, "df-hook-file-off");
  await mkdir(path.join(fileOff.vault, ".runtime"), { recursive: true });
  await writeFile(path.join(fileOff.vault, ".runtime", "flags.json"), '{"MINNI_USE_LEDGER":"off"}', "utf8");
  await runHook(fileOff, "UserPromptSubmit", upsPayload("where is the alpha retention rule"));
  assert.ok(!(await exists(path.join(fileOff.vault, DF_SNAPSHOT_RELPATH))));
});

/** The module file names a spawned hook resolves. */
async function loads(t, fixture, prompt) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-df-loads-"));
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
  const { code } = await runHook(fixture, "UserPromptSubmit", upsPayload(prompt), {
    env: { MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  assert.equal(code, 0);
  return (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
}

test("the daily check is the fast path: a fresh snapshot loads no use-detector; a due one does", { timeout: 120_000 }, async (t) => {
  const fixture = await hookFixture(t, "df-hook-loads");
  const due = await loads(t, fixture, "where is the alpha retention rule");
  assert.ok(due.includes("use-capture.js"), "the ledger is on");
  assert.ok(due.includes("use-detector.js"), "a due snapshot loads the detector to count");
  const fresh = await loads(t, fixture, "where is the alpha retention rule");
  assert.ok(fresh.includes("use-capture.js"));
  assert.ok(!fresh.includes("use-detector.js"), "a fresh snapshot costs one lstat");
});
