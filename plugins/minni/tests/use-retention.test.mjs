// Recall-to-use S1b (§4.3 "Retention"): `.runtime/use-ledger/<session_key>`
// directories past the 90-day horizon go once the use detector holds nothing
// open for the session. A bounded sweep, run after a SessionStart is settled:
// at most 20 per run, each renamed to `.trash-` first, a cooperative budget
// checked between items, a resumable cursor, once a day after a complete
// pass, never a symlink followed, never a step out of the vault.
import assert from "node:assert/strict";
import { lutimes, mkdir, mkdtemp, readdir, readFile, rm, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { DETECTOR_RELDIR, runDetector } from "../dist/use-detector.js";
import { LEDGER_RELDIR } from "../dist/use-ledger.js";
import {
  LEDGER_IDLE_MAX_MS,
  LEDGER_SWEEP_INTERVAL_MS,
  LEDGER_SWEEP_MAX_REMOVALS,
  detectorSessionState,
  sweepLedgerDirs,
} from "../dist/use-retention.js";
import {
  Clock,
  FIXTURE_SESSION,
  Transcript,
  append,
  detectorArgs,
  makeVault,
  surfacedRow,
} from "./fixtures/use-detector-fixtures.mjs";
import { exists, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const NOW = Date.parse("2026-10-23T12:00:00.000Z");
const DAY = 24 * 60 * 60 * 1000;
const OLD = new Date(NOW - LEDGER_IDLE_MAX_MS - DAY);

async function vault(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-retention-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const v = path.join(root, "vault");
  const ledger = path.join(v, LEDGER_RELDIR);
  await mkdir(ledger, { recursive: true });
  return { root, vault: v, ledger };
}

/** A ledger session directory with one day file, every mtime set to `when`. */
async function ledgerDir(ledger, name, when = OLD) {
  const dir = path.join(ledger, name);
  await mkdir(dir, { recursive: true });
  const day = path.join(dir, "2026-07-01.jsonl");
  await writeFile(day, '{"v":5,"kind":"turn"}\n', "utf8");
  await utimes(day, when, when);
  await utimes(dir, when, when);
  return dir;
}

/** The detector's per-session state, as use-detector.ts lays it out. */
async function detectorState(v, name, agents, { lock } = {}) {
  const dir = path.join(v, DETECTOR_RELDIR, "sessions", name);
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, "windows.json"), JSON.stringify({ v: 1, session_key: name, agents }), "utf8");
  if (lock) {
    const file = path.join(dir, "windows.lock");
    await writeFile(file, "1 now\n", "utf8");
    await utimes(file, lock, lock);
  }
  return dir;
}

const idleAgent = { windows: [], exposures: [], turn: null };
const key = (n) => `${String(n).padStart(4, "0")}bbbbbbbbbbbb`;

test("old closed ledgers go; young, open, locked and unknown ones stay; links are never followed", async (t) => {
  const { root, vault: v, ledger } = await vault(t);
  // 1: old, detector never ran. 2: old, detector state all closed.
  await ledgerDir(ledger, key(1));
  await ledgerDir(ledger, key(2));
  await detectorState(v, key(2), { main: idleAgent });
  // 3: young.
  await ledgerDir(ledger, key(3), new Date(NOW - DAY));
  // 4: old, an open window. 5: old, an open epoch. 6: old, a reply not yet flushed.
  await ledgerDir(ledger, key(4));
  await detectorState(v, key(4), { main: { ...idleAgent, windows: [{ sid: "w" }] } });
  await ledgerDir(ledger, key(5));
  await detectorState(v, key(5), { main: idleAgent, "a-0123456789ab": { ...idleAgent, exposures: [{ exposure_id: "e" }] } });
  await ledgerDir(ledger, key(6));
  await detectorState(v, key(6), { main: { ...idleAgent, turn: { ended: { ts: "x" } } } });
  // 7: old, closed state, but a detector run holds the lock right now.
  await ledgerDir(ledger, key(7));
  await detectorState(v, key(7), { main: idleAgent }, { lock: new Date(NOW - 1000) });
  // 8: old, closed state, a stale lock (a crashed holder): it does not defer.
  await ledgerDir(ledger, key(8));
  await detectorState(v, key(8), { main: idleAgent }, { lock: new Date(NOW - 60_000) });
  // 9: old, a state this reader cannot read with certainty.
  await ledgerDir(ledger, key(9));
  const corrupt = await detectorState(v, key(9), {});
  await writeFile(path.join(corrupt, "windows.json"), "{not json", "utf8");
  // 10: a session-key-shaped symlink to an old directory outside the vault.
  const outside = await ledgerDir(root, "outside-target");
  await symlink(outside, path.join(ledger, key(10)));
  // 11: a session-key-shaped plain file. 12: a cut-off run's trash.
  await writeFile(path.join(ledger, key(11)), "not a directory", "utf8");
  await ledgerDir(ledger, `.trash-1700000000000-${key(12)}`, new Date(NOW));
  // 13: old, but a day file inside it is a symlink to a file outside the vault.
  const linked = await ledgerDir(ledger, key(13));
  const outsideFile = path.join(root, "outside.jsonl");
  await writeFile(outsideFile, "keep me\n", "utf8");
  await symlink(outsideFile, path.join(linked, "2026-07-02.jsonl"));
  await lutimes(path.join(linked, "2026-07-02.jsonl"), OLD, OLD);
  await utimes(linked, OLD, OLD);

  assert.equal(detectorSessionState(v, key(1), NOW), "none");
  assert.equal(detectorSessionState(v, key(2), NOW), "closed");
  assert.equal(detectorSessionState(v, key(4), NOW), "open");
  assert.equal(detectorSessionState(v, key(5), NOW), "open");
  assert.equal(detectorSessionState(v, key(6), NOW), "open");
  assert.equal(detectorSessionState(v, key(7), NOW), "locked");
  assert.equal(detectorSessionState(v, key(8), NOW), "closed");
  assert.equal(detectorSessionState(v, key(9), NOW), "unknown");

  const result = sweepLedgerDirs(v, { now: NOW });
  assert.equal(result.complete, true);
  assert.equal(result.removed, 5, JSON.stringify(result));
  assert.equal(result.kept_open, 5, JSON.stringify(result));
  const left = (await readdir(ledger)).sort();
  assert.deepEqual(left, [".sweep.json", key(3), key(4), key(5), key(6), key(7), key(9), key(10), key(11)]);
  assert.ok(await exists(path.join(outside, "2026-07-01.jsonl")), "the symlink's target is untouched");
  assert.equal(await readFile(outsideFile, "utf8"), "keep me\n", "a link inside a removed directory is not followed");
  // The detector's own state is not retention's to remove.
  assert.ok(await exists(path.join(v, DETECTOR_RELDIR, "sessions", key(2), "windows.json")));

  const state = JSON.parse(await readFile(path.join(ledger, ".sweep.json"), "utf8"));
  assert.deepEqual(state, { v: 1, next_after: NOW + LEDGER_SWEEP_INTERVAL_MS, cursor: null });

  // Within a day of a complete pass nothing runs.
  await ledgerDir(ledger, key(14));
  assert.equal(sweepLedgerDirs(v, { now: NOW + 60 * 60 * 1000 }).skipped, true);
  assert.ok(await exists(path.join(ledger, key(14))));
  // A day later it does: the new one goes, and so does 7, whose lock has gone stale.
  const later = sweepLedgerDirs(v, { now: NOW + LEDGER_SWEEP_INTERVAL_MS + 1 });
  assert.equal(later.removed, 2, JSON.stringify(later));
  assert.ok(!(await exists(path.join(ledger, key(14)))));
  assert.ok(!(await exists(path.join(ledger, key(7)))));
});

test("at most 20 removals per run; the next run resumes from the cursor", async (t) => {
  const { vault: v, ledger } = await vault(t);
  for (let n = 0; n < LEDGER_SWEEP_MAX_REMOVALS + 5; n += 1) await ledgerDir(ledger, key(n));
  const first = sweepLedgerDirs(v, { now: NOW });
  assert.equal(first.removed, LEDGER_SWEEP_MAX_REMOVALS);
  assert.equal(first.complete, false);
  const state = JSON.parse(await readFile(path.join(ledger, ".sweep.json"), "utf8"));
  assert.equal(state.cursor, key(LEDGER_SWEEP_MAX_REMOVALS - 1));
  const second = sweepLedgerDirs(v, { now: NOW + 1000 });
  assert.equal(second.removed, 5);
  assert.equal(second.complete, true);
  assert.deepEqual(await readdir(ledger), [".sweep.json"]);
});

test("the budget is checked between items, and the cursor carries the pass on", async (t) => {
  const { vault: v, ledger } = await vault(t);
  for (let n = 0; n < 6; n += 1) await ledgerDir(ledger, key(n), new Date(NOW));
  // A clock that jumps 100 ms per reading (the start takes one): a 250 ms
  // budget admits the items checked at +100 and +200, and stops at +300.
  let tick = 0;
  const clock = () => NOW + 100 * tick++;
  const first = sweepLedgerDirs(v, { now: NOW, clock, budgetMs: 250 });
  assert.equal(first.complete, false);
  assert.equal(first.scanned, 2);
  const second = sweepLedgerDirs(v, { now: NOW });
  assert.equal(second.scanned, 4, "resumes after the cursor; nothing is scanned twice");
  assert.equal(second.complete, true);
});

test("a use-ledger root (or detector tree) that is a symlink out of the vault is refused", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-retention-escape-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const v = path.join(root, "vault");
  await mkdir(path.join(v, ".runtime"), { recursive: true });
  const outside = path.join(root, "outside");
  await ledgerDir(outside, key(1));
  await symlink(outside, path.join(v, LEDGER_RELDIR));
  const result = sweepLedgerDirs(v, { now: NOW });
  assert.equal(result.removed, 0);
  assert.ok(await exists(path.join(outside, key(1), "2026-07-01.jsonl")), "nothing outside the vault is touched");
  assert.ok(!(await exists(path.join(outside, ".sweep.json"))));

  // A detector tree that escapes is "unknown", never "none": the ledger stays.
  const second = await vault(t);
  await ledgerDir(second.ledger, key(2));
  const elsewhere = path.join(second.root, "elsewhere");
  await mkdir(elsewhere, { recursive: true });
  await symlink(elsewhere, path.join(second.vault, DETECTOR_RELDIR));
  assert.equal(detectorSessionState(second.vault, key(2), NOW), "unknown");
  assert.equal(sweepLedgerDirs(second.vault, { now: NOW }).removed, 0);
  assert.ok(await exists(path.join(second.ledger, key(2))));
});

test("a ledger directory with more entries than the cap is never judged idle", async (t) => {
  const { vault: v, ledger } = await vault(t);
  const dir = path.join(ledger, key(1));
  await mkdir(dir, { recursive: true });
  for (let n = 0; n < 300; n += 1) {
    const file = path.join(dir, `2026-01-${String(n).padStart(4, "0")}.jsonl`);
    await writeFile(file, "", "utf8");
    await utimes(file, OLD, OLD);
  }
  await utimes(dir, OLD, OLD);
  assert.equal(sweepLedgerDirs(v, { now: NOW }).removed, 0);
  assert.ok(await exists(dir));
});

test("open windows are read from the real detector's state: open keeps the ledger, closed lets it go", async (t) => {
  const { vault: v, transcriptPath } = await makeVault(t, "minni-ledger-retention-real-");
  const clock = new Clock();
  const tx = new Transcript(transcriptPath, clock);
  // A boot surfacing stays open until the second non-skip prompt turn ends.
  append(v, [surfacedRow({ mkey: "vault:wiki/a.md", title: "Alpha retention note", ts: clock.next(), channel: "boot_recall" })]);
  tx.prompt("first prompt of the session", "p1");
  tx.say("working on it");
  await tx.flush();
  const first = runDetector(detectorArgs(v, transcriptPath, clock));
  assert.equal(first.status, "ran");
  const ledgerOf = path.join(v, LEDGER_RELDIR, FIXTURE_SESSION);
  await ageTree(ledgerOf);
  assert.equal(detectorSessionState(v, FIXTURE_SESSION), "open", "the detector's own state layout is the one read");
  assert.equal(sweepLedgerDirs(v).removed, 0);
  assert.ok(await exists(ledgerOf));

  // The session ends: every window and the epoch close.
  const last = runDetector(detectorArgs(v, transcriptPath, clock, { sessionEnd: true }));
  assert.equal(last.status, "ran");
  assert.equal(detectorSessionState(v, FIXTURE_SESSION), "closed");
  await ageTree(ledgerOf);
  // The first pass completed a day ago (by the cursor); this one runs.
  const result = sweepLedgerDirs(v, { now: Date.now() + LEDGER_SWEEP_INTERVAL_MS + 1 });
  assert.equal(result.removed, 1, JSON.stringify(result));
  assert.ok(!(await exists(ledgerOf)));
});

/** Set every mtime under a ledger session directory past the horizon (by the real clock). */
async function ageTree(dir) {
  const when = new Date(Date.now() - LEDGER_IDLE_MAX_MS - DAY);
  for (const name of await readdir(dir)) await utimes(path.join(dir, name), when, when);
  await utimes(dir, when, when);
}

test("SessionStart sweeps old idle ledgers after delivery; with the ledger off it does not", { timeout: 60_000 }, async (t) => {
  for (const [ledgerFlag, gone] of [
    ["on", true],
    ["off", false],
  ]) {
    const fixture = await makeFixture(t, `ledger-retention-hook-${ledgerFlag}`);
    await startFakeDaemon(fixture);
    const ledger = path.join(fixture.vault, LEDGER_RELDIR);
    await mkdir(ledger, { recursive: true });
    // Old by the real clock: the hook sweeps with Date.now().
    const stale = new Date(Date.now() - LEDGER_IDLE_MAX_MS - DAY);
    await ledgerDir(ledger, key(9), stale);
    const { code } = await runHook(
      fixture,
      "SessionStart",
      { session_id: "ledger-retention-hook", source: "startup" },
      { env: { MINNI_USE_LEDGER: ledgerFlag } },
    );
    assert.equal(code, 0);
    assert.equal(!(await exists(path.join(ledger, key(9)))), gone, `ledger ${ledgerFlag}`);
  }
});
