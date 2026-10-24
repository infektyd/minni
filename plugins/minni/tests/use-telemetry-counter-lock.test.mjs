// Recall-to-use S1a-2 re-review R1: two processes that both judge the same
// `counters.json.lock` stale must not both end up holding the section (S1b
// joins credits on turn_ix). The interleaving is forced through the
// `beforeBreak` seam: breaker B judges the stale lock, then breaker A breaks it
// and takes a fresh one, then B tries its break.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { COUNTER_LOCK_STALE_MS, acquireCounterLock } from "../dist/use-capture.js";

async function sessionDir(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-counter-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  const dir = path.join(vault, ".runtime", "recall-session.d", "0123456789abcdef");
  await mkdir(dir, { recursive: true });
  return { vault, dir, lock: path.join(dir, "counters.json.lock") };
}

async function plantStaleLock(lock) {
  await writeFile(lock, "4242 dead-holder\n", "utf8");
  const old = new Date(Date.now() - COUNTER_LOCK_STALE_MS - 3_000);
  await utimes(lock, old, old);
}

test("two breakers on one stale lock: exactly one holds the section", async (t) => {
  const { vault, dir, lock } = await sessionDir(t);
  await plantStaleLock(lock);

  let releaseA = null;
  // B judges the planted lock stale; before B breaks it, A breaks it and takes a fresh one.
  const releaseB = acquireCounterLock(dir, vault, {
    beforeBreak: () => {
      if (releaseA === null) releaseA = acquireCounterLock(dir, vault);
    },
  });
  assert.equal(typeof releaseA, "function", "A broke the stale lock and holds the section");
  assert.equal(releaseB, null, "B must not hold it too: it waits its bound, then fails open");
  assert.ok((await readFile(lock, "utf8")).length > 0, "A's lock is still in place");
  assert.ok(!(await readFile(lock, "utf8")).includes("dead-holder"), "and it is A's, not the stale one");

  releaseA();
  const releaseC = acquireCounterLock(dir, vault);
  assert.equal(typeof releaseC, "function", "after A releases, the next taker gets it");
  releaseC();
  const left = (await readdir(dir)).sort();
  assert.deepEqual(left, [], `no lock or grave is left behind: ${left}`);
});

test("one breaker on a stale lock takes it; a live lock is never broken", async (t) => {
  const { vault, dir, lock } = await sessionDir(t);
  await plantStaleLock(lock);
  const release = acquireCounterLock(dir, vault);
  assert.equal(typeof release, "function");
  // While held (fresh), another taker fails open and leaves it alone.
  const second = acquireCounterLock(dir, vault);
  assert.equal(second, null);
  const content = await readFile(lock, "utf8");
  assert.ok(!content.includes("dead-holder"));
  release();
  assert.deepEqual(await readdir(dir), []);
});

test("a release removes only its own lock", async (t) => {
  const { vault, dir, lock } = await sessionDir(t);
  const release = acquireCounterLock(dir, vault);
  // Someone else's lock now sits at the path (as after a break and re-take).
  await writeFile(lock, "9999 someone-else\n", "utf8");
  release();
  assert.equal(await readFile(lock, "utf8"), "9999 someone-else\n", "another holder's lock is not removed");
});
