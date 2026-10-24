// Recall-to-use S1b (§4.3 "Retention"): `.runtime/use-ledger/<session_key>`
// directories past the 90-day horizon go once the use detector holds nothing
// open for the session. A bounded sweep, run after a SessionStart is settled:
// at most 20 per run, each renamed to `.trash-` first, a cooperative budget
// checked between items, a resumable cursor, once a day after a complete
// pass, never a symlink followed, never a step out of the vault.
import assert from "node:assert/strict";
import fsSync from "node:fs";
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
import { SESSION_MAINTENANCE_BUDGET_MS, runSessionMaintenance } from "../dist/use-capture.js";
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

  const handed = [];
  const result = await sweepLedgerDirs(v, { now: NOW, finalizer: (_vault, options) => handed.push(...options.sessionKeys) });
  assert.deepEqual(handed, [key(4), key(5), key(6)], "open work past the horizon goes to the idle close; locked and unknown never do");
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
  assert.equal((await sweepLedgerDirs(v, { now: NOW + 60 * 60 * 1000 })).skipped, true);
  assert.ok(await exists(path.join(ledger, key(14))));
  // A day later it does: the new one goes, and so does 7, whose lock has gone stale.
  const later = await sweepLedgerDirs(v, { now: NOW + LEDGER_SWEEP_INTERVAL_MS + 1 });
  assert.equal(later.removed, 2, JSON.stringify(later));
  assert.ok(!(await exists(path.join(ledger, key(14)))));
  assert.ok(!(await exists(path.join(ledger, key(7)))));
});

test("at most 20 removals per run; the next run resumes from the cursor", async (t) => {
  const { vault: v, ledger } = await vault(t);
  for (let n = 0; n < LEDGER_SWEEP_MAX_REMOVALS + 5; n += 1) await ledgerDir(ledger, key(n));
  const first = await sweepLedgerDirs(v, { now: NOW });
  assert.equal(first.removed, LEDGER_SWEEP_MAX_REMOVALS);
  assert.equal(first.complete, false);
  const state = JSON.parse(await readFile(path.join(ledger, ".sweep.json"), "utf8"));
  assert.equal(state.cursor, key(LEDGER_SWEEP_MAX_REMOVALS - 1));
  const second = await sweepLedgerDirs(v, { now: NOW + 1000 });
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
  const first = await sweepLedgerDirs(v, { now: NOW, clock, budgetMs: 250 });
  assert.equal(first.complete, false);
  assert.equal(first.scanned, 2);
  const second = await sweepLedgerDirs(v, { now: NOW });
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
  const result = await sweepLedgerDirs(v, { now: NOW });
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
  assert.equal((await sweepLedgerDirs(second.vault, { now: NOW })).removed, 0);
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
  assert.equal((await sweepLedgerDirs(v, { now: NOW })).removed, 0);
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
  // The finalize is a no-op here: this case pins the state reader, not the idle close.
  assert.equal((await sweepLedgerDirs(v, { finalizer: () => {} })).removed, 0);
  assert.ok(await exists(ledgerOf));

  // The session ends: every window and the epoch close.
  const last = runDetector(detectorArgs(v, transcriptPath, clock, { sessionEnd: true }));
  assert.equal(last.status, "ran");
  assert.equal(detectorSessionState(v, FIXTURE_SESSION), "closed");
  await ageTree(ledgerOf);
  // The first pass completed a day ago (by the cursor); this one runs.
  const result = await sweepLedgerDirs(v, { now: Date.now() + LEDGER_SWEEP_INTERVAL_MS + 1 });
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

// ---- Retention (a): the idle close, then removal (§4.3) ---------------------

/** A session whose detector state holds an open epoch, written by the real detector at `at`. */
async function openSessionAt(t, at) {
  const { vault: v, transcriptPath } = await makeVault(t, "minni-ledger-retention-finalize-");
  const clock = new Clock(at);
  const tx = new Transcript(transcriptPath, clock);
  append(v, [surfacedRow({ mkey: "vault:wiki/a.md", title: "Alpha retention note", ts: clock.next(), channel: "boot_recall" })]);
  tx.prompt("first prompt of the session", "p1");
  tx.say("working on it");
  await tx.flush();
  assert.equal(runDetector(detectorArgs(v, transcriptPath, clock)).status, "ran");
  return { vault: v, ledgerOf: path.join(v, LEDGER_RELDIR, FIXTURE_SESSION) };
}

/** Age every entry of a ledger session directory to `when`. */
async function ageTo(dir, when) {
  for (const name of await readdir(dir)) await utimes(path.join(dir, name), when, when);
  await utimes(dir, when, when);
}

test("an idle session past the horizon with an open epoch is finalized, then removed, in one run", async (t) => {
  const longAgo = Date.now() - LEDGER_IDLE_MAX_MS - 10 * DAY;
  const { vault: v, ledgerOf } = await openSessionAt(t, longAgo);
  await ageTo(ledgerOf, new Date(longAgo));
  assert.equal(detectorSessionState(v, FIXTURE_SESSION), "open", "precondition: open work");
  const result = await sweepLedgerDirs(v);
  assert.equal(result.finalized, 1, JSON.stringify(result));
  assert.equal(result.removed, 1);
  assert.ok(!(await exists(ledgerOf)), "the ledger directory is gone");
  assert.equal(detectorSessionState(v, FIXTURE_SESSION), "closed", "the detector closed its epoch first");
});

test("a recent session is never finalized, open or not", async (t) => {
  const { vault: v, ledgerOf } = await openSessionAt(t, Date.now() - 2 * DAY);
  const state = path.join(v, DETECTOR_RELDIR, "sessions", FIXTURE_SESSION, "windows.json");
  const before = await readFile(state);
  const handed = [];
  const result = await sweepLedgerDirs(v, { finalizer: (_vault, options) => handed.push(...options.sessionKeys) });
  assert.deepEqual(handed, [], "nothing young reaches the idle close");
  assert.equal(result.removed, 0);
  assert.ok(await exists(ledgerOf));
  assert.deepEqual(await readFile(state), before, "its state is untouched");
});

/** A finalizer that closes what it is handed, the way the detector's idle close leaves state. */
function closingFinalizer(v, handed) {
  return (_vault, options) => {
    handed.push(options.sessionKeys.length);
    for (const name of options.sessionKeys) {
      const file = path.join(v, DETECTOR_RELDIR, "sessions", name, "windows.json");
      fsSync.writeFileSync(file, JSON.stringify({ v: 1, session_key: name, agents: { main: idleAgent } }));
    }
  };
}

test("more than 20 finalize candidates are spread across runs, through the cursor", async (t) => {
  const { vault: v, ledger } = await vault(t);
  for (let n = 0; n < LEDGER_SWEEP_MAX_REMOVALS + 5; n += 1) {
    await ledgerDir(ledger, key(n));
    await detectorState(v, key(n), { main: { ...idleAgent, exposures: [{ exposure_id: "e" }] } });
  }
  const handed = [];
  const finalizer = closingFinalizer(v, handed);
  const first = await sweepLedgerDirs(v, { now: NOW, finalizer });
  assert.equal(first.finalized, LEDGER_SWEEP_MAX_REMOVALS);
  assert.equal(first.complete, false);
  const second = await sweepLedgerDirs(v, { now: NOW + 1000, finalizer });
  assert.equal(second.finalized, 5);
  assert.equal(second.complete, true);
  assert.deepEqual(handed, [LEDGER_SWEEP_MAX_REMOVALS, 5]);
  assert.deepEqual(await readdir(ledger), [".sweep.json"]);
});

test("candidates the close did not reach are carried: the cursor resumes before them", async (t) => {
  const { vault: v, ledger } = await vault(t);
  for (let n = 0; n < 4; n += 1) {
    await ledgerDir(ledger, key(n));
    await detectorState(v, key(n), { main: { ...idleAgent, windows: [{ sid: "w" }] } });
  }
  // A close whose own budget ends after the first candidate, as
  // finalizeIdleSessions reports it: `remaining` names the rest unexamined.
  const handed = [];
  const firstOnly = (vaultPath, options) => {
    handed.push(options.sessionKeys.length);
    closingFinalizer(v, [])(vaultPath, { sessionKeys: options.sessionKeys.slice(0, 1) });
    return { remaining: options.sessionKeys.length - 1 };
  };
  const first = await sweepLedgerDirs(v, { now: NOW, finalizer: firstOnly });
  assert.equal(first.removed, 1, `the examined candidate is removed: ${JSON.stringify(first)}`);
  assert.equal(first.carried, 3, `the unexamined ones are carried: ${JSON.stringify(first)}`);
  assert.equal(first.complete, false);
  // The next run resumes before the first carried candidate, never past it.
  const second = await sweepLedgerDirs(v, { now: NOW, finalizer: closingFinalizer(v, handed) });
  assert.equal(first.removed + second.removed, 4, JSON.stringify(second));
  assert.deepEqual(await readdir(ledger), [".sweep.json"]);
});

test("a spent budget does nothing at all", async (t) => {
  const { vault: v, ledger } = await vault(t);
  await ledgerDir(ledger, key(1));
  for (const budgetMs of [0, -5]) {
    const result = await sweepLedgerDirs(v, { now: NOW, budgetMs });
    assert.equal(result.skipped, true);
  }
  assert.deepEqual(await readdir(ledger), [key(1)], "no removal, and no cursor written");
});

test("SessionStart's maintenance runs under ONE 150 ms budget, and every sweep progresses across runs", async (t) => {
  const { vault: v, ledger } = await vault(t);
  const sessions = path.join(v, ".runtime", "recall-session.d");
  await mkdir(sessions, { recursive: true });
  for (let n = 0; n < 30; n += 1) {
    const dir = path.join(sessions, key(n));
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, "counters.json"), "{}", "utf8");
    await utimes(path.join(dir, "counters.json"), OLD, OLD);
    await utimes(dir, OLD, OLD);
    await ledgerDir(ledger, key(n));
    if (n % 3 === 0) await detectorState(v, key(n), { main: { ...idleAgent, exposures: [{ exposure_id: "e" }] } });
  }
  // A fake clock that moves 10 ms at every reading: the budget is spent by
  // readings, deterministically, whatever the machine is doing.
  const STEP = 10;
  let reading = 0;
  let last = NOW;
  const clock = () => (last = NOW + STEP * reading++);
  const handed = [];
  const finalizer = closingFinalizer(v, handed);
  let runs = 0;
  let sessionsLeft = true;
  let ledgerLeft = true;
  while ((sessionsLeft || ledgerLeft) && runs < 20) {
    const start = clock();
    const result = await runSessionMaintenance(v, { budgetMs: SESSION_MAINTENANCE_BUDGET_MS, clock, now: NOW, finalizer });
    runs += 1;
    // The whole closure stays inside the one budget, plus one item's slack:
    // the reading that found it spent, and the reading that started the item.
    assert.ok(last - start <= SESSION_MAINTENANCE_BUDGET_MS + 2 * STEP, `run ${runs} took ${last - start} clock ms`);
    const progressed = (result.sessions?.removed ?? 0) + (result.ledger?.removed ?? 0);
    sessionsLeft = (await readdir(sessions)).some((name) => !name.startsWith("."));
    ledgerLeft = (await readdir(ledger)).some((name) => !name.startsWith("."));
    if (sessionsLeft || ledgerLeft) assert.ok(progressed > 0, `run ${runs} made no progress: ${JSON.stringify(result)}`);
  }
  assert.equal(sessionsLeft, false, "recall-session.d emptied across runs");
  assert.equal(ledgerLeft, false, "the ledger emptied across runs, its finalize candidates included");
  assert.ok(handed.reduce((a, b) => a + b, 0) === 10, `the 10 open sessions were finalized: ${JSON.stringify(handed)}`);
});

test("SessionStart closes and removes an old open session; it loads use-detector only for a candidate", { timeout: 120_000 }, async (t) => {
  const { pathToFileURL } = await import("node:url");
  const dir = await mkdtemp(path.join(tmpdir(), "minni-retention-loads-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
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
  async function boot(fixture, label) {
    const log = path.join(dir, `${label}.log`);
    const { code } = await runHook(fixture, "SessionStart", { session_id: `retention-${label}`, source: "startup" }, {
      env: { MINNI_TEST_LOAD_LOG: log },
      nodeArgs: ["--import", pathToFileURL(preload).href],
    });
    assert.equal(code, 0);
    return (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
  }

  // No candidate: an old CLOSED session goes, and the detector never loads.
  const quiet = await makeFixture(t, "retention-loads-quiet");
  await startFakeDaemon(quiet);
  await ledgerDir(path.join(quiet.vault, LEDGER_RELDIR), key(1), new Date(Date.now() - LEDGER_IDLE_MAX_MS - DAY));
  await detectorState(quiet.vault, key(1), { main: idleAgent });
  const quietNames = await boot(quiet, "quiet");
  assert.ok(quietNames.includes("use-retention.js"), "positive control: retention ran");
  assert.ok(!quietNames.includes("use-detector.js"), "no candidate, no detector");
  assert.ok(!(await exists(path.join(quiet.vault, LEDGER_RELDIR, key(1)))));

  // A candidate: an old session with an open epoch, written by the real detector.
  const busy = await makeFixture(t, "retention-loads-busy");
  await startFakeDaemon(busy);
  const longAgo = Date.now() - LEDGER_IDLE_MAX_MS - 10 * DAY;
  const transcriptPath = path.join(busy.root, "transcript.jsonl");
  await writeFile(transcriptPath, "", "utf8");
  const clock = new Clock(longAgo);
  const tx = new Transcript(transcriptPath, clock);
  append(busy.vault, [surfacedRow({ mkey: "vault:wiki/a.md", title: "Alpha retention note", ts: clock.next(), channel: "boot_recall" })]);
  tx.prompt("first prompt of the session", "p1");
  tx.say("working on it");
  await tx.flush();
  assert.equal(runDetector(detectorArgs(busy.vault, transcriptPath, clock)).status, "ran");
  const ledgerOf = path.join(busy.vault, LEDGER_RELDIR, FIXTURE_SESSION);
  await ageTo(ledgerOf, new Date(longAgo));
  assert.equal(detectorSessionState(busy.vault, FIXTURE_SESSION), "open");
  const busyNames = await boot(busy, "busy");
  assert.ok(busyNames.includes("use-detector.js"), "a candidate loads the detector for its idle close");
  assert.ok(!(await exists(ledgerOf)), "finalized, then removed, after the boot was delivered");
  assert.equal(detectorSessionState(busy.vault, FIXTURE_SESSION), "closed");
});

test("every candidate the REAL close examined is removed in the same run, even when the budget ends after the close", async (t) => {
  const { finalizeIdleSessions } = await import("../dist/use-detector.js");
  const { appendLedgerRows, buildSurfacedRow } = await import("../dist/use-ledger.js");
  const { vault: v, transcriptPath } = await makeVault(t, "minni-ledger-retention-cut-");
  const longAgo = Date.now() - LEDGER_IDLE_MAX_MS - 10 * DAY;
  /** An open epoch for `sessionKey`, written by the real detector `longAgo`, its ledger aged to match. */
  async function openSession(sessionKey, txPath) {
    await writeFile(txPath, "", "utf8");
    const clock = new Clock(longAgo);
    const tx = new Transcript(txPath, clock);
    const context = { session_key: sessionKey, agent_key: "main", wire: "claude-code", principal: "claude-code", features: ["MINNI_USE_LEDGER"] };
    const row = buildSurfacedRow(
      context,
      { channel: "boot_recall", mkey: `vault:wiki/${sessionKey}.md`, title: `Note ${sessionKey} retention`, rank: 0, visible: true, delivered: true },
      clock.next(),
    );
    assert.equal(appendLedgerRows(v, [row]).written, 1);
    tx.prompt("first prompt of the session", "p1");
    tx.say("working on it");
    await tx.flush();
    assert.equal(runDetector({ ...detectorArgs(v, txPath, clock), session_key: sessionKey }).status, "ran");
    const dir = path.join(v, LEDGER_RELDIR, sessionKey);
    await ageTo(dir, new Date(longAgo));
    assert.equal(detectorSessionState(v, sessionKey), "open");
    return dir;
  }
  const dirA = await openSession(FIXTURE_SESSION, transcriptPath);
  const dirB = await openSession("bbbbbbbbbbbbbbbb", path.join(path.dirname(transcriptPath), "second.jsonl"));
  // The real close, after which the sweep's clock says the budget is gone (a slow close).
  let after = false;
  const clock = () => (after ? Date.now() + 10 * 60 * 1000 : Date.now());
  const finalizer = (vaultPath, options) => {
    const closed = finalizeIdleSessions(vaultPath, options);
    after = true;
    return closed;
  };
  const run = await sweepLedgerDirs(v, { budgetMs: 150, clock, finalizer });
  assert.equal(run.finalized, 2, JSON.stringify(run));
  assert.ok(!(await exists(dirA)) && !(await exists(dirB)), "both closed candidates went in the run that closed them");
  assert.equal(detectorSessionState(v, "bbbbbbbbbbbbbbbb"), "closed");
});
