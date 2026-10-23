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
// a pass starts when the snapshot is missing or over a day old, resumes from a
// cursor kept in `.runtime/df-snapshot.partial.json`, and writes the snapshot
// only when it completes.

const { DF_PARTIAL_RELPATH, DF_RECOUNT_SLICE_MS, runDfRecountSlice } = await import("../dist/use-df-recount.js");
const { documentFrequencies, readDfSnapshot } = await import("../dist/use-detector.js");
const { listVaultNoteRelPaths, readVaultNoteTextForDf } = await import("../dist/vault.js");
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
  return { root, vault, snapshot: path.join(vault, DF_SNAPSHOT_RELPATH), partial: path.join(vault, DF_PARTIAL_RELPATH) };
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
  const stale = new Date(Date.now() - 48 * HOUR);
  await utimes(snapshot, stale, stale);
  const bytes = await readFile(snapshot);
  const first = await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.equal(first.status, "partial");
  assert.deepEqual(await readFile(snapshot), bytes, "mid-pass, the old snapshot is untouched");
});

test("recount: a corrupt or foreign partial restarts the pass cleanly", async (t) => {
  for (const [label, content] of [
    ["not json", "{not json"],
    ["foreign version", JSON.stringify({ v: 2, kind: "df-recount", started_at: Date.now(), cursor: null, notes: 1, df: {} })],
    ["foreign kind", JSON.stringify({ v: 1, kind: "other", started_at: Date.now(), cursor: null, notes: 1, df: {} })],
    ["not an object", "[]"],
    ["bad start", JSON.stringify({ v: 1, kind: "df-recount", started_at: "yesterday", cursor: null, notes: 1, df: {} })],
    ["absolute cursor", JSON.stringify({ v: 1, kind: "df-recount", started_at: Date.now(), cursor: "/etc/passwd", notes: 1, df: {} })],
    ["negative count", JSON.stringify({ v: 1, kind: "df-recount", started_at: Date.now(), cursor: null, notes: 1, df: { otter: -3 } })],
  ]) {
    const { vault, partial } = await recountVault(t, 40);
    await mkdir(path.dirname(partial), { recursive: true });
    await writeFile(partial, content, "utf8");
    const result = await runDfRecountSlice(vault, { clock: stepClock(5) });
    assert.equal(result.restarted, true, label);
    assert.equal(result.status, "partial", label);
    assert.equal(result.pass_notes, result.counted, `${label}: nothing of the corrupt state survives`);
    const kept = JSON.parse(await readFile(partial, "utf8"));
    assert.equal(kept.v, 1, `${label}: replaced by a valid partial`);
  }
});

test("recount: a pass older than a day restarts from the first note", async (t) => {
  const { vault, partial } = await recountVault(t, 40);
  await mkdir(path.dirname(partial), { recursive: true });
  await writeFile(
    partial,
    JSON.stringify({ v: 1, kind: "df-recount", started_at: Date.now() - 25 * HOUR, cursor: "wiki/concepts/note-030.md", notes: 999, df: { otter: 999 } }),
    "utf8",
  );
  const result = await runDfRecountSlice(vault, { clock: stepClock(5) });
  assert.equal(result.restarted, true);
  assert.equal(result.pass_notes, result.counted);
  assert.equal(JSON.parse(await readFile(partial, "utf8")).cursor, `wiki/concepts/note-${String(result.counted - 1).padStart(3, "0")}.md`);
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
