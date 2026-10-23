// Recall-to-use S1a-2 (§4.3 "Retention", §6.2.5): `.runtime/recall-session.d`
// holds one directory per session (counters, reset markers, hit-keys). A
// bounded sweep, run after a SessionStart is delivered, removes directories
// idle for 90 days: renamed to `.trash-` first, at most 20 per run, within a
// cooperative 150 ms budget checked between items, resuming from a cursor,
// and once a day after a complete pass. It never follows a symlink and never
// leaves the vault.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  SESSION_IDLE_MAX_MS,
  SWEEP_INTERVAL_MS,
  SWEEP_MAX_REMOVALS,
  sweepSessionDirs,
} from "../dist/use-capture.js";
import { exists, makeFixture, runHook, sessionKeyOf, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const NOW = Date.parse("2026-10-23T12:00:00.000Z");
const OLD = new Date(NOW - SESSION_IDLE_MAX_MS - 24 * 60 * 60 * 1000);

async function vault(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-session-sweep-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const v = path.join(root, "vault");
  const sessions = path.join(v, ".runtime", "recall-session.d");
  await mkdir(sessions, { recursive: true });
  return { root, vault: v, sessions };
}

/** A session directory with the usual files, every mtime set to `when`. */
async function sessionDir(sessions, name, when = OLD) {
  const dir = path.join(sessions, name);
  await mkdir(dir, { recursive: true });
  for (const file of ["counters.json", "main.reset", "hit-keys.json"]) {
    await writeFile(path.join(dir, file), "{}", "utf8");
    await utimes(path.join(dir, file), when, when);
  }
  await utimes(dir, when, when);
  return dir;
}

const key = (n) => `${String(n).padStart(4, "0")}aaaaaaaaaaaa`;

test("idle directories go, fresh ones stay, leftovers finish, links are never followed", async (t) => {
  const { root, vault: v, sessions } = await vault(t);
  await sessionDir(sessions, key(1));
  await sessionDir(sessions, key(2), new Date(NOW - 60_000));
  // Old directory, one recent file: still in use.
  const mixed = await sessionDir(sessions, key(3));
  await writeFile(path.join(mixed, "counters.json"), "{}", "utf8");
  await utimes(path.join(mixed, "counters.json"), new Date(NOW - 1000), new Date(NOW - 1000));
  await utimes(mixed, OLD, OLD);
  // A cut-off run's trash.
  await sessionDir(sessions, `.trash-1700000000000-${key(4)}`, new Date(NOW));
  // A session-key-shaped symlink to an old directory outside the vault.
  const outside = await sessionDir(root, "outside-target");
  await symlink(outside, path.join(sessions, key(5)));
  // A session-key-shaped plain file.
  await writeFile(path.join(sessions, key(6)), "not a directory", "utf8");

  const result = sweepSessionDirs(v, { now: NOW });
  assert.equal(result.complete, true);
  assert.equal(result.removed, 2, JSON.stringify(result));
  const left = (await readdir(sessions)).sort();
  assert.deepEqual(left, [".sweep.json", key(2), key(3), key(5), key(6)]);
  assert.ok(await exists(path.join(outside, "counters.json")), "the symlink's target is untouched");

  const state = JSON.parse(await readFile(path.join(sessions, ".sweep.json"), "utf8"));
  assert.deepEqual(state, { v: 1, next_after: NOW + SWEEP_INTERVAL_MS, cursor: null });

  // Within a day of a complete pass: nothing runs.
  await sessionDir(sessions, key(7));
  const again = sweepSessionDirs(v, { now: NOW + 60 * 60 * 1000 });
  assert.equal(again.skipped, true);
  assert.ok(await exists(path.join(sessions, key(7))));
  // A day later it does.
  const later = sweepSessionDirs(v, { now: NOW + SWEEP_INTERVAL_MS + 1 });
  assert.equal(later.removed, 1);
  assert.ok(!(await exists(path.join(sessions, key(7)))));
});

test("at most 20 removals per run; the next run resumes from the cursor", async (t) => {
  const { vault: v, sessions } = await vault(t);
  for (let n = 0; n < SWEEP_MAX_REMOVALS + 5; n += 1) await sessionDir(sessions, key(n));
  const first = sweepSessionDirs(v, { now: NOW });
  assert.equal(first.removed, SWEEP_MAX_REMOVALS);
  assert.equal(first.complete, false);
  const state = JSON.parse(await readFile(path.join(sessions, ".sweep.json"), "utf8"));
  assert.equal(state.cursor, key(SWEEP_MAX_REMOVALS - 1));
  const second = sweepSessionDirs(v, { now: NOW + 1000 });
  assert.equal(second.removed, 5);
  assert.equal(second.complete, true);
  assert.deepEqual(await readdir(sessions), [".sweep.json"]);
});

test("the budget is checked between items, and the cursor carries the pass on", async (t) => {
  const { vault: v, sessions } = await vault(t);
  for (let n = 0; n < 6; n += 1) await sessionDir(sessions, key(n), new Date(NOW));
  // A clock that jumps 100 ms per reading (the start takes one): a 250 ms
  // budget admits the items checked at +100 and +200, and stops at +300.
  let tick = 0;
  const clock = () => NOW + 100 * tick++;
  const first = sweepSessionDirs(v, { now: NOW, clock, budgetMs: 250 });
  assert.equal(first.complete, false);
  assert.equal(first.scanned, 2);
  const second = sweepSessionDirs(v, { now: NOW });
  assert.equal(second.scanned, 4, "resumes after the cursor; nothing is scanned twice");
  assert.equal(second.complete, true);
});

test("a recall-session.d that is a symlink out of the vault is refused", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-session-sweep-escape-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const v = path.join(root, "vault");
  await mkdir(path.join(v, ".runtime"), { recursive: true });
  const outside = path.join(root, "outside");
  await sessionDir(outside, key(1));
  await symlink(outside, path.join(v, ".runtime", "recall-session.d"));
  const result = sweepSessionDirs(v, { now: NOW });
  assert.equal(result.removed, 0);
  assert.ok(await exists(path.join(outside, key(1), "counters.json")), "nothing outside the vault is touched");
  assert.ok(!(await exists(path.join(outside, ".sweep.json"))));
});

test("a session directory with more entries than the cap is never judged idle", async (t) => {
  const { vault: v, sessions } = await vault(t);
  const dir = path.join(sessions, key(1));
  await mkdir(dir, { recursive: true });
  for (let n = 0; n < 300; n += 1) {
    const file = path.join(dir, `a-${String(n).padStart(12, "0")}.reset`);
    await writeFile(file, "{}", "utf8");
    await utimes(file, OLD, OLD);
  }
  await utimes(dir, OLD, OLD);
  assert.equal(sweepSessionDirs(v, { now: NOW }).removed, 0);
  assert.ok(await exists(dir));
});

test("SessionStart runs the sweep after delivery; with the ledger off it does not", { timeout: 60_000 }, async (t) => {
  for (const [ledger, gone] of [
    ["on", true],
    ["off", false],
  ]) {
    const fixture = await makeFixture(t, `session-sweep-hook-${ledger}`);
    await startFakeDaemon(fixture);
    const sessions = path.join(fixture.vault, ".runtime", "recall-session.d");
    await mkdir(sessions, { recursive: true });
    // Old by the real clock: the hook sweeps with Date.now().
    const stale = new Date(Date.now() - SESSION_IDLE_MAX_MS - 24 * 60 * 60 * 1000);
    await sessionDir(sessions, key(9), stale);
    const { code } = await runHook(
      fixture,
      "SessionStart",
      { session_id: "sweep-hook", source: "startup" },
      { env: { MINNI_USE_LEDGER: ledger } },
    );
    assert.equal(code, 0);
    assert.equal(!(await exists(path.join(sessions, key(9)))), gone, `ledger ${ledger}`);
    assert.equal(await exists(path.join(sessions, sessionKeyOf("sweep-hook"), "main.reset")), ledger === "on");
  }
});
