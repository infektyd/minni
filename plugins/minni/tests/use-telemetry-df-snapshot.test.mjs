// Recall-to-use S1b (§4.4 "Distinctive tokens"): the use detector judges
// titles against `.runtime/df-snapshot.json` and never scans the vault.
//
// UPS is read-only for the snapshot. A recount over the whole vault costs
// about 145 ms of tokenisation at the real vault size, which breaks the
// prompt budget, so UPS never counts, never writes the snapshot, and never
// loads the detector to do so, whatever the snapshot's age.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { DF_SNAPSHOT_RELPATH } from "../dist/use-detector.js";
import { writeDfSnapshot as writeFixtureSnapshot } from "./fixtures/use-detector-fixtures.mjs";
import { exists, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

async function writeNotes(vault) {
  const wiki = path.join(vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  await writeFile(path.join(wiki, "alpha.md"), "# Alpha retention rule\n\nThe alpha rule keeps pelican data.\n", "utf8");
  await writeFile(path.join(wiki, "beta.md"), "# Beta release notes\n\nThe beta train ships alpha fixes.\n", "utf8");
}

function upsPayload(prompt) {
  return { session_id: "df-ups", prompt_id: "p1", prompt };
}

/** The module file names a spawned hook resolves. */
async function loads(t, fixture, payload) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-df-ups-loads-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const log = path.join(dir, "loads.log");
  const preload = path.join(dir, "log-resolve.mjs");
  await writeFile(
    preload,
    [
      'import * as nodeModule from "node:module";',
      'import { isMainThread } from "node:worker_threads";',
      'import { appendFileSync } from "node:fs";',
      "export async function resolve(specifier, context, nextResolve) {",
      "  const result = await nextResolve(specifier, context);",
      "  appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${result.url}\\n`);",
      "  return result;",
      "}",
      "if (isMainThread) {",
      '  if (typeof nodeModule.registerHooks === "function") {',
      "    nodeModule.registerHooks({",
      "      resolve(specifier, context, nextResolve) {",
      "        const result = nextResolve(specifier, context);",
      "        appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${result.url}\\n`);",
      "        return result;",
      "      },",
      "    });",
      "  } else {",
      "    nodeModule.register(import.meta.url);",
      "  }",
      "}",
      "",
    ].join("\n"),
    "utf8",
  );
  const { code } = await runHook(fixture, "UserPromptSubmit", payload, {
    env: { MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  assert.equal(code, 0);
  return (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
}

test("UPS with the ledger on is read-only for the DF snapshot: absent stays absent, stale stays stale", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "df-ups-readonly");
  await startFakeDaemon(fixture);
  await writeNotes(fixture.vault);
  const file = path.join(fixture.vault, DF_SNAPSHOT_RELPATH);
  const partial = path.join(fixture.vault, ".runtime", "df-snapshot.partial.json");

  // No snapshot at all: a scanned prompt writes none, and loads no detector.
  const absent = await loads(t, fixture, upsPayload("where is the alpha retention rule"));
  assert.ok(absent.includes("use-capture.js"), "positive control: the ledger is on");
  assert.ok(!absent.includes("use-detector.js"), "UPS loads no detector to count");
  assert.ok(!(await exists(file)), "UPS wrote no snapshot");
  assert.ok(!(await exists(partial)), "UPS wrote no partial recount");

  // A stale snapshot (well past a day): UPS leaves it byte-identical, same mtime.
  await writeFixtureSnapshot(fixture.vault);
  const stale = new Date(Date.now() - 48 * HOUR);
  await utimes(file, stale, stale);
  const bytes = await readFile(file);
  const { mtimeMs } = await stat(file);
  const names = await loads(t, fixture, upsPayload("and the beta release notes"));
  assert.ok(!names.includes("use-detector.js"));
  assert.deepEqual(await readFile(file), bytes, "a stale snapshot is not rewritten by UPS");
  assert.equal((await stat(file)).mtimeMs, mtimeMs);
  assert.ok(!(await exists(partial)));
});

// ---- The Stop recount (use-df-recount.ts) -----------------------------------
//
// The snapshot is recounted at Stop, after delivery, one capped slice per Stop:
// a pass starts when the snapshot is missing or at least 7 days old, or when
// the early trigger fires (the note count moved by more than 5% since the last
// pass, checked at most daily from a directory listing), resumes from the
// partial state in `.runtime/df-snapshot.partial.json`, and writes the
// snapshot only when it completes.

const { DF_PARTIAL_RELPATH, DF_RECOUNT_SLICE_MS, DF_STATE_RELPATH, earlyTriggerFires, runDfRecountSlice } = await import("../dist/use-df-recount.js");
const { documentFrequencies, readDfSnapshot } = await import("../dist/use-detector.js");
const { countVaultNotes, listVaultNoteRelPaths, readVaultNoteTextForDf } = await import("../dist/vault.js");
const { lstat, symlink } = await import("node:fs/promises");

const WORDS = ["otter", "badger", "heron", "lattice", "quokka", "zebra", "pancake", "waffle", "kernel", "window"];

async function recountVault(t, notes = 40) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-df-recount-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  const wiki = path.join(vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  for (let i = 0; i < notes; i += 1) {
    const words = Array.from({ length: 30 }, (_, k) => WORDS[(i * 3 + k * 7) % WORDS.length]).join(" ");
    await writeFile(path.join(wiki, `note-${String(i).padStart(3, "0")}.md`), `# Note ${i}\n\n${words} unique${i}token\n`, "utf8");
  }
  return {
    root,
    vault,
    snapshot: path.join(vault, DF_SNAPSHOT_RELPATH),
    partial: path.join(vault, DF_PARTIAL_RELPATH),
    state: path.join(vault, DF_STATE_RELPATH),
  };
}

/** A fake clock: each reading moves it `step` ms. */
function stepClock(step) {
  let t = 1_000_000;
  return () => (t += step);
}

/** What a one-shot count over every countable note gives, with writeDfSnapshot's threshold applied. */
async function oneShot(vault) {
  const texts = (await listVaultNoteRelPaths(vault)).map((rel) => readVaultNoteTextForDf(vault, rel, 2 * 1024 * 1024)).filter((x) => x !== null);
  const { notes, df } = documentFrequencies(texts);
  const threshold = Math.max(1, Math.ceil(0.05 * notes));
  return { notes, df: Object.fromEntries(Object.entries(df).filter(([, n]) => n > threshold)) };
}

test("recount: the per-slice cap holds (counting stops at the budget, minus the write reserve)", async (t) => {
  const { vault, snapshot, partial } = await recountVault(t, 40);
  // 5 ms per reading, a 50 ms slice with 10 ms kept back: at most 8 notes.
  const result = await runDfRecountSlice(vault, { clock: stepClock(5), budgetMs: DF_RECOUNT_SLICE_MS });
  assert.equal(result.status, "partial");
  assert.ok(result.counted >= 1 && result.counted <= 8, JSON.stringify(result));
  assert.ok(await exists(partial), "the pass's state is kept between slices");
  assert.ok(!(await exists(snapshot)), "no snapshot before the pass completes");
});

test("recount: the pass resumes across Stops and completes with exactly the one-shot count", async (t) => {
  const { vault, snapshot, partial } = await recountVault(t, 40);
  const clock = stepClock(5);
  const statuses = [];
  let counted = 0;
  for (let i = 0; i < 20; i += 1) {
    const result = await runDfRecountSlice(vault, { clock });
    statuses.push(result.status);
    counted += result.counted;
    if (result.status === "written") break;
    assert.equal(result.status, "partial");
    assert.ok(!(await exists(snapshot)), `slice ${i + 1}: the snapshot is written only at completion`);
  }
  assert.equal(statuses.at(-1), "written", JSON.stringify(statuses));
  assert.ok(statuses.length > 1, "the pass spanned several slices");
  assert.equal(counted, 40, "every note counted once");
  assert.ok(!(await exists(partial)), "a completed pass removes its partial state");
  const written = JSON.parse(await readFile(snapshot, "utf8"));
  const expected = await oneShot(vault);
  assert.equal(written.notes, expected.notes);
  assert.deepEqual(written.df, expected.df, "slices sum to the one-shot count");
  assert.ok(readDfSnapshot(vault), "the detector reads it");

  // Within a day the next Stop does nothing at all.
  const again = await runDfRecountSlice(vault, { clock });
  assert.equal(again.status, "fresh");
  assert.ok(!(await exists(partial)));
});

test("recount: a stale snapshot stays byte-identical until the new pass completes", async (t) => {
  const { vault, snapshot } = await recountVault(t, 40);
  await writeFixtureSnapshot(vault);
  const stale = new Date(Date.now() - 8 * DAY);
  await utimes(snapshot, stale, stale);
  const bytes = await readFile(snapshot);
  const first = await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.equal(first.status, "partial");
  assert.deepEqual(await readFile(snapshot), bytes, "mid-pass, the old snapshot is untouched");
});

test("recount: a corrupt or foreign partial restarts the pass cleanly", async (t) => {
  for (const [label, content] of [
    ["not json", "{not json"],
    ["the old v1 format", JSON.stringify({ v: 1, kind: "df-recount", started_at: Date.now(), cursor: null, notes: 1, df: {} })],
    ["foreign kind", JSON.stringify({ v: 2, kind: "other", started_at: Date.now(), paths: [], next: 0, notes: 0, tokens: [], counts: [] })],
    ["not an object", "[]"],
    ["bad start", JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: "yesterday", paths: [], next: 0, notes: 0, tokens: [], counts: [] })],
    ["absolute path", JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now(), paths: ["/etc/passwd"], next: 0, notes: 0, tokens: [], counts: [] })],
    ["next past the end", JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now(), paths: ["wiki/a.md"], next: 9, notes: 0, tokens: [], counts: [] })],
    ["negative count", JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now(), paths: [], next: 0, notes: 0, tokens: ["otter"], counts: [-3] })],
    ["mismatched arrays", JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now(), paths: [], next: 0, notes: 0, tokens: ["otter"], counts: [] })],
  ]) {
    const { vault, partial } = await recountVault(t, 40);
    await mkdir(path.dirname(partial), { recursive: true });
    await writeFile(partial, content, "utf8");
    const result = await runDfRecountSlice(vault, { clock: stepClock(5) });
    assert.equal(result.restarted, true, label);
    assert.equal(result.status, "partial", label);
    assert.equal(result.pass_notes, result.counted, `${label}: nothing of the corrupt state survives`);
    const kept = JSON.parse(await readFile(partial, "utf8"));
    assert.equal(kept.v, 2, `${label}: replaced by a valid partial`);
    assert.equal(kept.next, result.counted, `${label}: the new pass starts at the first note`);
  }
});

test("recount: a pass older than 7 days restarts from the first note; a younger one continues", async (t) => {
  for (const [label, age, restarts] of [
    ["8 days", 8 * DAY, true],
    ["6 days", 6 * DAY, false],
  ]) {
    const { vault, partial } = await recountVault(t, 40);
    const paths = await listVaultNoteRelPaths(vault);
    await mkdir(path.dirname(partial), { recursive: true });
    await writeFile(
      partial,
      JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now() - age, listed: paths.length, paths, next: 30, notes: 30, tokens: ["otter"], counts: [30] }),
      "utf8",
    );
    const result = await runDfRecountSlice(vault, { clock: stepClock(5) });
    assert.equal(result.restarted, restarts, label);
    if (restarts) assert.equal(result.pass_notes, result.counted, `${label}: nothing of the old pass survives`);
    else assert.equal(result.pass_notes, 30 + result.counted, `${label}: the pass continues from its own list`);
  }
});

test("recount: links are never followed, and paths a safe write cannot replace do nothing", async (t) => {
  // A symlinked snapshot: refused, its target untouched.
  const linked = await recountVault(t, 5);
  const outside = path.join(linked.root, "outside.json");
  await writeFile(outside, "keep me", "utf8");
  await mkdir(path.dirname(linked.snapshot), { recursive: true });
  await symlink(outside, linked.snapshot);
  assert.equal((await runDfRecountSlice(linked.vault)).status, "refused");
  assert.equal(await readFile(outside, "utf8"), "keep me");
  assert.ok((await lstat(linked.snapshot)).isSymbolicLink());
  // A directory where the partial goes: refused.
  const blocked = await recountVault(t, 5);
  await mkdir(blocked.partial, { recursive: true });
  assert.equal((await runDfRecountSlice(blocked.vault)).status, "refused");
  // A symlinked note, a superseded note and a blocked note are not counted.
  const filtered = await recountVault(t, 3);
  const wiki = path.join(filtered.vault, "wiki", "concepts");
  await writeFile(path.join(filtered.root, "elsewhere.md"), "# Elsewhere\n\nnot in the vault\n", "utf8");
  await symlink(path.join(filtered.root, "elsewhere.md"), path.join(wiki, "linked.md"));
  await writeFile(path.join(wiki, "old.md"), "---\nstatus: superseded\n---\n# Old plan\n", "utf8");
  await writeFile(path.join(wiki, "secret.md"), "---\nprivacy: blocked\n---\n# Secret\n", "utf8");
  const done = await runDfRecountSlice(filtered.vault);
  assert.equal(done.status, "written");
  assert.equal(JSON.parse(await readFile(filtered.snapshot, "utf8")).notes, 3, "only the three countable notes");
});

test("recount at Stop: a measured Stop with the ledger on writes it; ledger off or an unmeasured wire does nothing", { timeout: 120_000 }, async (t) => {
  const { seedStopTurn } = await import("./use-stop-fixture.mjs");
  for (const [label, env, bin] of [
    ["claude-on", {}, undefined],
    ["claude-off", { MINNI_USE_LEDGER: "off" }, undefined],
    ["codex-on", {}, "codex-hook.js"],
  ]) {
    const fixture = await makeFixture(t, `df-stop-${label}`);
    await startFakeDaemon(fixture);
    await writeNotes(fixture.vault);
    const { payload } = await seedStopTurn(fixture);
    const vaultEnv = bin ? { MINNI_CODEX_VAULT_PATH: fixture.vault } : {};
    const { code } = await runHook(fixture, "Stop", payload, { env: { ...vaultEnv, ...env }, ...(bin ? { bin } : {}) });
    assert.equal(code, 0, label);
    const wrote = (await exists(path.join(fixture.vault, DF_SNAPSHOT_RELPATH))) || (await exists(path.join(fixture.vault, DF_PARTIAL_RELPATH)));
    assert.equal(wrote, label === "claude-on", `${label}: recount ${label === "claude-on" ? "ran" : "must not run"}`);
  }
});

// ---- Weekly cadence and the early trigger -------------------------------------

/** A completed pass's snapshot, `ageMs` old, and the recount state it leaves. */
async function settled(env, { ageMs, listed, checkedAgoMs }) {
  await writeFixtureSnapshot(env.vault);
  const when = new Date(Date.now() - ageMs);
  await utimes(env.snapshot, when, when);
  await writeFile(env.state, JSON.stringify({ v: 1, kind: "df-recount-state", listed, checked_at: Date.now() - checkedAgoMs }), "utf8");
}

test("cadence: no pass before 7 days without a trigger; a pass at 7 days", async (t) => {
  const young = await recountVault(t, 40);
  await settled(young, { ageMs: 6 * DAY, listed: 40, checkedAgoMs: 2 * DAY });
  const quiet = await runDfRecountSlice(young.vault);
  assert.equal(quiet.status, "fresh");
  assert.equal(quiet.checked, true, "the daily check ran");
  assert.ok(!(await exists(young.partial)), "no pass started");

  const due = await recountVault(t, 40);
  await settled(due, { ageMs: 7 * DAY + HOUR, listed: 40, checkedAgoMs: HOUR });
  const pass = await runDfRecountSlice(due.vault, { clock: stepClock(5) });
  assert.equal(pass.reason, "age");
  assert.equal(pass.status, "partial");
});

test("cadence: the early trigger fires at more than 5% and not at 5% or less", async (t) => {
  assert.equal(earlyTriggerFires(100, 106), true);
  assert.equal(earlyTriggerFires(100, 105), false, "exactly 5% does not fire");
  assert.equal(earlyTriggerFires(100, 95), false);
  assert.equal(earlyTriggerFires(100, 94), true);
  assert.equal(earlyTriggerFires(null, 500), false, "no baseline, no trigger");
  for (const [notes, fires] of [
    [42, false], // 40 -> 42 is exactly 5%
    [43, true], // 7.5%
  ]) {
    const env = await recountVault(t, notes);
    await settled(env, { ageMs: DAY, listed: 40, checkedAgoMs: 2 * DAY });
    const result = await runDfRecountSlice(env.vault, { clock: stepClock(5) });
    assert.equal(result.checked, true);
    assert.equal(result.reason === "trigger", fires, `${notes} notes against 40`);
    assert.equal(await exists(env.partial), fires);
  }
});

test("cadence: the note-count check runs at most once a day per vault", async (t) => {
  const env = await recountVault(t, 40);
  await settled(env, { ageMs: DAY, listed: 40, checkedAgoMs: 2 * DAY });
  const first = await runDfRecountSlice(env.vault);
  assert.equal(first.checked, true);
  const stamp = JSON.parse(await readFile(env.state, "utf8")).checked_at;
  // The vault grows by 25% within the day: no check, so no trigger yet.
  for (let i = 40; i < 50; i += 1) await writeFile(path.join(env.vault, "wiki", "concepts", `late-${i}.md`), `# Late ${i}\n`, "utf8");
  const same = await runDfRecountSlice(env.vault);
  assert.equal(same.checked, false, "within a day: no listing at all");
  assert.equal(same.status, "fresh");
  assert.equal(JSON.parse(await readFile(env.state, "utf8")).checked_at, stamp);
  // A day later the check runs and the trigger fires.
  const later = await runDfRecountSlice(env.vault, { now: Date.now() + DAY + HOUR, clock: stepClock(5) });
  assert.equal(later.checked, true);
  assert.equal(later.reason, "trigger");
});

test("cadence: a corrupt state file means no baseline: the check runs, takes today's count, and does not fire", async (t) => {
  const env = await recountVault(t, 40);
  await settled(env, { ageMs: DAY, listed: 40, checkedAgoMs: 0 });
  await writeFile(env.state, "{not json", "utf8");
  const result = await runDfRecountSlice(env.vault);
  assert.equal(result.checked, true);
  assert.equal(result.status, "fresh");
  const state = JSON.parse(await readFile(env.state, "utf8"));
  assert.equal(state.listed, 40, "the baseline is today's count");
});

test("countVaultNotes counts exactly what listVaultNoteRelPaths lists, from entries alone", async (t) => {
  const env = await recountVault(t, 12);
  const wiki = path.join(env.vault, "wiki");
  await mkdir(path.join(wiki, "deep", "deeper"), { recursive: true });
  await writeFile(path.join(wiki, "deep", "deeper", "x.md"), "# X\n", "utf8");
  await writeFile(path.join(wiki, "deep", "notes.txt"), "not markdown", "utf8");
  await writeFile(path.join(env.root, "outside.md"), "# Outside\n", "utf8");
  await symlink(path.join(env.root, "outside.md"), path.join(wiki, "linked.md"));
  await mkdir(path.join(env.root, "outside-dir"), { recursive: true });
  await writeFile(path.join(env.root, "outside-dir", "y.md"), "# Y\n", "utf8");
  await symlink(path.join(env.root, "outside-dir"), path.join(wiki, "linked-dir"));
  const listed = await listVaultNoteRelPaths(env.vault);
  assert.equal(countVaultNotes(env.vault), listed.length);
  assert.equal(listed.length, 13, "12 notes and the nested one; no symlink, no .txt");
});

// ---- Review follow-ups: progress, the partial's guards, due edges, empty vaults ----

test("recount: every slice counts at least one note, so a spent clock can never livelock the pass", async (t) => {
  const { vault, snapshot } = await recountVault(t, 12);
  // Each reading jumps a second: the counting window is gone before any note.
  const clock = stepClock(1000);
  const counts = [];
  for (let i = 0; i < 30; i += 1) {
    const result = await runDfRecountSlice(vault, { clock });
    counts.push(result.counted);
    if (result.status === "written") break;
  }
  assert.ok(counts.every((n) => n >= 1), `a note per slice at least: ${JSON.stringify(counts)}`);
  assert.equal(counts.reduce((a, b) => a + b, 0), 12);
  assert.ok(await exists(snapshot), "the pass completed");
});

test("recount: the partial is plain counts at mode 0600, and is gone once the snapshot is written", async (t) => {
  const { vault, snapshot, partial } = await recountVault(t, 40);
  const first = await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.equal(first.status, "partial");
  assert.equal((await stat(partial)).mode & 0o777, 0o600, "created 0600");
  const kept = JSON.parse(await readFile(partial, "utf8"));
  assert.equal(kept.listed, kept.paths.length, "the pass records the note count it started from");
  assert.ok(Array.isArray(kept.tokens) && Array.isArray(kept.counts), "token counts, in plain text");
  for (let i = 0; i < 20 && !(await exists(snapshot)); i += 1) await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.ok(await exists(snapshot), "the snapshot is present");
  assert.ok(!(await exists(partial)), "and the partial is gone");
});

test("recount: an aged-out pass is removed; its replacement, if a pass starts, is the new pass's", async (t) => {
  // Aged out, and nothing due: the stale partial goes and none replaces it.
  const quiet = await recountVault(t, 40);
  await settled(quiet, { ageMs: DAY, listed: 40, checkedAgoMs: HOUR });
  const paths = await listVaultNoteRelPaths(quiet.vault);
  const stale = { v: 2, kind: "df-recount-pass", started_at: Date.now() - 8 * DAY, listed: paths.length, paths, next: 10, notes: 10, tokens: ["otter"], counts: [10] };
  await writeFile(quiet.partial, JSON.stringify(stale), "utf8");
  const none = await runDfRecountSlice(quiet.vault);
  assert.equal(none.status, "fresh");
  assert.ok(!(await exists(quiet.partial)), "the aged-out partial is gone");
  // Aged out while a pass is due: the replacement belongs to the new pass.
  const due = await recountVault(t, 40);
  await mkdir(path.dirname(due.partial), { recursive: true });
  await writeFile(due.partial, JSON.stringify(stale), "utf8");
  const fresh = await runDfRecountSlice(due.vault, { clock: stepClock(5) });
  assert.equal(fresh.restarted, true);
  const kept = JSON.parse(await readFile(due.partial, "utf8"));
  assert.ok(Date.now() - kept.started_at < HOUR, "the new pass's partial");
  assert.equal(kept.next, fresh.counted);
});

test("recount: a pass the vault outgrew (note count past the trigger) is discarded and restarts at once", async (t) => {
  const env = await recountVault(t, 40);
  await settled(env, { ageMs: DAY, listed: 40, checkedAgoMs: HOUR });
  const paths = await listVaultNoteRelPaths(env.vault);
  await writeFile(
    env.partial,
    JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now() - HOUR, listed: paths.length, paths, next: 10, notes: 10, tokens: ["otter"], counts: [10] }),
    "utf8",
  );
  for (let i = 0; i < 10; i += 1) await writeFile(path.join(env.vault, "wiki", "concepts", `grown-${i}.md`), `# Grown ${i}\n`, "utf8");
  const result = await runDfRecountSlice(env.vault, { clock: stepClock(5) });
  assert.equal(result.restarted, true);
  assert.equal(result.reason, "trigger");
  const kept = JSON.parse(await readFile(env.partial, "utf8"));
  assert.equal(kept.listed, 50, "the new pass lists the grown vault");
});

test("recount: a symlinked partial is never followed; the link is replaced by a fresh 0600 file", async (t) => {
  const { root, vault, partial } = await recountVault(t, 40);
  const outside = path.join(root, "outside-partial.json");
  await writeFile(outside, "keep me", "utf8");
  await mkdir(path.dirname(partial), { recursive: true });
  await symlink(outside, partial);
  const result = await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.equal(result.status, "partial");
  assert.equal(await readFile(outside, "utf8"), "keep me", "the link's target is untouched");
  const st = await lstat(partial);
  assert.ok(st.isFile() && !st.isSymbolicLink(), "a regular file now");
  assert.equal(st.mode & 0o777, 0o600);
});

test("recount: a snapshot write that fails keeps the partial, so the pass resumes", async (t) => {
  const { cp, mkdtemp: mkd, writeFile: wf, symlink: ln, readFile: rf } = await import("node:fs/promises");
  const { PLUGIN_ROOT, DEFAULT_DIST } = await import("./use-telemetry-hook-fixture.mjs");
  const root = await mkd(path.join(tmpdir(), "minni-df-recount-nowrite-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dist = path.join(root, "dist");
  await cp(DEFAULT_DIST, dist, { recursive: true });
  const detector = path.join(dist, "use-detector.js");
  const source = await rf(detector, "utf8");
  const marker = "export function writeDfSnapshot(vaultPath, input) {";
  assert.ok(source.includes(marker), "the mutation's anchor exists");
  await wf(detector, source.replace(marker, `${marker}\n    return false;`), "utf8");
  await wf(path.join(root, "package.json"), '{"type":"module"}\n', "utf8");
  await ln(path.join(PLUGIN_ROOT, "node_modules"), path.join(root, "node_modules"));
  const failing = await import(pathToFileURL(path.join(dist, "use-df-recount.js")).href);
  const env = await recountVault(t, 5);
  const result = await failing.runDfRecountSlice(env.vault);
  assert.equal(result.status, "failed");
  assert.ok(!(await exists(env.snapshot)));
  const kept = JSON.parse(await readFile(env.partial, "utf8"));
  assert.equal(kept.next, kept.paths.length, "the counted pass is kept whole");
  assert.equal(kept.notes, 5);
});

test("recount: a future-dated snapshot is due; a directory at the snapshot path refuses", async (t) => {
  const future = await recountVault(t, 5);
  await writeFixtureSnapshot(future.vault);
  const ahead = new Date(Date.now() + DAY);
  await utimes(future.snapshot, ahead, ahead);
  const due = await runDfRecountSlice(future.vault);
  assert.equal(due.reason, "age", "an mtime well ahead of the clock is a skewed clock, so the snapshot is due");
  const dir = await recountVault(t, 5);
  await mkdir(dir.snapshot, { recursive: true });
  assert.equal((await runDfRecountSlice(dir.vault)).status, "refused");
  assert.ok((await stat(dir.snapshot)).isDirectory(), "left as it was");
});

test("recount: a vault with no countable note is parked for the week, not recounted every Stop", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-df-recount-empty-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  const wiki = path.join(vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  for (let i = 0; i < 20; i += 1) await writeFile(path.join(wiki, `old-${i}.md`), `---\nstatus: superseded\n---\n# Old ${i}\n`, "utf8");
  const first = await runDfRecountSlice(vault);
  assert.equal(first.status, "empty");
  const again = await runDfRecountSlice(vault);
  assert.equal(again.status, "fresh", "parked: the next Stop does not recount");
  assert.equal(again.restarted, false);
  // A week later it runs again; and within the week, a count past the trigger still fires.
  const later = await runDfRecountSlice(vault, { now: Date.now() + 8 * DAY });
  assert.equal(later.status, "empty");
  for (let i = 0; i < 5; i += 1) await writeFile(path.join(wiki, `new-${i}.md`), `# New ${i}\n\nfresh words here\n`, "utf8");
  const grown = await runDfRecountSlice(vault, { now: Date.now() + 9 * DAY + HOUR });
  assert.equal(grown.reason, "trigger");
  assert.equal(grown.status, "written");
});

// ---- Review 3 follow-ups ------------------------------------------------------

test("recount: an empty vault with an OLD snapshot is parked for the week too (R1)", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-df-recount-empty-old-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  const wiki = path.join(vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  for (let i = 0; i < 20; i += 1) await writeFile(path.join(wiki, `old-${i}.md`), `---\nstatus: superseded\n---\n# Old ${i}\n`, "utf8");
  // A snapshot from when the vault still had countable notes, over a week old.
  await writeFixtureSnapshot(vault);
  const old = new Date(Date.now() - 8 * DAY);
  await utimes(path.join(vault, DF_SNAPSHOT_RELPATH), old, old);
  const statuses = [];
  for (let i = 0; i < 6; i += 1) statuses.push((await runDfRecountSlice(vault)).status);
  assert.equal(statuses[0], "empty", "the first due Stop finds nothing to count");
  assert.deepEqual(statuses.slice(1), ["fresh", "fresh", "fresh", "fresh", "fresh"], `parked, not recounted every Stop: ${JSON.stringify(statuses)}`);
  // A week later it runs again.
  assert.equal((await runDfRecountSlice(vault, { now: Date.now() + 8 * DAY })).status, "empty");
});

test("recount: a partial whose token count exceeds the notes counted restarts the pass (N4)", async (t) => {
  const { vault, partial } = await recountVault(t, 40);
  const paths = await listVaultNoteRelPaths(vault);
  await mkdir(path.dirname(partial), { recursive: true });
  await writeFile(
    partial,
    JSON.stringify({ v: 2, kind: "df-recount-pass", started_at: Date.now() - HOUR, listed: paths.length, paths, next: 10, notes: 10, tokens: ["otter"], counts: [11] }),
    "utf8",
  );
  const result = await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.equal(result.restarted, true, "a token in 11 of 10 notes is not a pass this module wrote");
  assert.equal(result.pass_notes, result.counted);
});
