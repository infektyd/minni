// Recall-to-use S1b round 4c: finalizeIdleSessions takes session keys and a
// time budget.
//
// The Stop-wiring slice's retention sweep calls the idle close from
// SessionStart, after delivery, for at most 20 candidate sessions with what is
// left of a 150 ms budget. So it must be able to examine ONLY the sessions it
// names (no walk of the other session directories, no read of their ledgers)
// and stop between sessions when the budget runs out, saying how many it
// left. With neither option the call behaves exactly as before.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { appendLedgerRows, buildSurfacedRow, LEDGER_RELDIR, readSessionLedger, sessionKeyFrom } from "../dist/use-ledger.js";
import { finalizeIdleSessions, runDetector } from "../dist/use-detector.js";
import { makeVault, T0 } from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const HOUR = 60 * 60 * 1000;
const IDLE_NOW = T0 + 25 * HOUR;
const keyOf = (id) => sessionKeyFrom({ session_id: id });
const A = keyOf("synthetic-idle-a");
const B = keyOf("synthetic-idle-b");
const C = keyOf("synthetic-idle-c");

/** One surfacing no run ever judged: an idle session with work to close. */
function seedIdle(vault, sessionKey, minute = 0) {
  const ctx = { session_key: sessionKey, agent_key: "main", wire: "claude-code", principal: "claude-code", features: ["MINNI_USE_LEDGER"] };
  const row = buildSurfacedRow(
    ctx,
    { channel: "prompt_pointer", mkey: `vault:wiki/sessions/${sessionKey}.md`, title: "Idle note", rank: 0, visible: true, delivered: true },
    new Date(T0 + minute * 60_000).toISOString(),
  );
  assert.equal(appendLedgerRows(vault, [row]).written, 1);
  return row;
}

const judgedCount = (vault, key) => readSessionLedger(vault, key).rows.filter((row) => row.kind === "judged").length;

/** Every fs path touched while `fn` runs. */
function withFsSpy(fn) {
  const touched = [];
  const names = ["readdirSync", "readFileSync", "openSync", "statSync", "lstatSync", "existsSync", "realpathSync"];
  const originals = Object.fromEntries(names.map((name) => [name, fs[name]]));
  for (const name of names) {
    fs[name] = function spy(target, ...rest) {
      touched.push(`${name} ${String(target)}`);
      return originals[name].call(fs, target, ...rest);
    };
  }
  try {
    return [fn(), touched];
  } finally {
    for (const name of names) fs[name] = originals[name];
  }
}

test("with sessionKeys: [A], only A is examined: B is never listed or read", async (t) => {
  const { vault } = await makeVault(t, "minni-idle-keys-");
  seedIdle(vault, A);
  seedIdle(vault, B);
  const [result, touched] = withFsSpy(() => finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: [A] }));
  assert.equal(result.status, "ran");
  assert.equal(result.sessions, 1);
  assert.equal(result.remaining, 0);
  assert.equal(judgedCount(vault, A), 1, "A was closed");
  assert.equal(judgedCount(vault, B), 0, "B was not");
  assert.deepEqual(touched.filter((entry) => entry.includes(B)), [], "nothing under B was touched");
  const ledgerRoot = path.join(vault, LEDGER_RELDIR);
  const sessionsRoot = path.join(vault, ".runtime", "use-detector", "sessions");
  assert.deepEqual(
    touched.filter((entry) => entry === `readdirSync ${ledgerRoot}` || entry === `readdirSync ${sessionsRoot}`),
    [],
    "no walk of the session directories",
  );
});

test("the budget stops between sessions, never inside one, and reports the remainder", async (t) => {
  const { vault } = await makeVault(t, "minni-idle-budget-");
  for (const key of [A, B, C]) seedIdle(vault, key);
  const realNow = Date.now;
  let fake = realNow();
  Date.now = () => (fake += 5); // every look at the clock costs 5 ms: a 1 ms budget is gone after one session
  let result;
  try {
    result = finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: [A, B, C], budgetMs: 1 });
  } finally {
    Date.now = realNow;
  }
  assert.equal(result.status, "ran");
  assert.equal(result.sessions, 1, "the first session always runs to completion");
  assert.equal(result.remaining, 2, "the caller knows two are left");
  assert.equal(judgedCount(vault, A), 1);
  assert.equal(judgedCount(vault, B), 0);
  assert.equal(judgedCount(vault, C), 0);

  // The retry picks up where it stopped.
  const retry = finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: [B, C], budgetMs: 10_000 });
  assert.deepEqual([retry.sessions, retry.remaining], [2, 0]);
  assert.equal(judgedCount(vault, B), 1);
  assert.equal(judgedCount(vault, C), 1);
});

test("without the new options the result and the ledger are what they were", async (t) => {
  const seed = async (prefix) => {
    const { vault } = await makeVault(t, prefix);
    seedIdle(vault, A);
    seedIdle(vault, B);
    return vault;
  };
  const walked = await seed("minni-idle-same-a-");
  const named = await seed("minni-idle-same-b-");
  const plain = finalizeIdleSessions(walked, { now: IDLE_NOW });
  assert.deepEqual(Object.keys(plain).sort(), ["exposures_written", "judged_written", "sessions", "status"], "no new field without the new options");
  assert.deepEqual(plain, { status: "ran", sessions: 2, judged_written: 2, exposures_written: 2 });
  const viaKeys = finalizeIdleSessions(named, { now: IDLE_NOW, sessionKeys: [A, B] });
  assert.deepEqual({ ...viaKeys, remaining: undefined }, { ...plain, remaining: undefined });
  for (const key of [A, B]) {
    const strip = (rows) => rows.map((row) => JSON.stringify(row));
    assert.deepEqual(strip(readSessionLedger(named, key).rows), strip(readSessionLedger(walked, key).rows), "the same rows");
  }
});

test("invalid session keys are skipped, by the walk's own rule", async (t) => {
  const { vault } = await makeVault(t, "minni-idle-invalid-");
  seedIdle(vault, A);
  seedIdle(vault, B);
  seedIdle(vault, "unlabeled");
  const result = finalizeIdleSessions(vault, {
    now: IDLE_NOW,
    sessionKeys: ["../escape", "", "unlabeled", "a/b", "x".repeat(65), 7, null, A, A],
  });
  assert.equal(result.status, "ran");
  assert.equal(result.sessions, 1, "only A, once");
  assert.equal(result.remaining, 0);
  assert.equal(judgedCount(vault, A), 1);
  assert.equal(judgedCount(vault, B), 0, "not named: untouched");
  assert.equal(judgedCount(vault, "unlabeled"), 0, "the unlabeled session stays out, as in the walk");
  assert.equal(fs.existsSync(path.join(vault, ".runtime", "use-detector", "escape")), false, "no directory outside the sessions root");
});

test("duplicate keys are examined once and never inflate the remainder", async (t) => {
  const { vault } = await makeVault(t, "minni-idle-dupes-");
  seedIdle(vault, A);
  seedIdle(vault, B);
  const realNow = Date.now;
  let fake = realNow();
  Date.now = () => (fake += 5);
  let result;
  try {
    result = finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: [A, A, A, B], budgetMs: 1 });
  } finally {
    Date.now = realNow;
  }
  assert.deepEqual([result.sessions, result.remaining], [1, 1], "A once; only B is left");
});

// r4d: every key the ledger writes is lowercase, so only that spelling is
// accepted from a caller. Two shapes, so the test holds on both kinds of
// filesystem:
//   - EXACT: the cased spelling is a real directory of its own (no lowercase
//     twin), with an unjudged surfacing and a state file holding an open
//     window. That is what a case-sensitive filesystem (Linux CI) has, and on
//     a case-insensitive one it is still its own directory: base finalises
//     it on both.
//   - ALIAS: only the lowercase session exists and the caller names it in
//     another case. Base finalises it only where the filesystem folds case
//     (APFS by default); elsewhere the alias names nothing.

/** A detector state file with one open window (the shape agentFrom reads). */
function openWindowState(sessionKey, sid) {
  const acc = { actions: 0, seen: 0, first_text: false, dismissed: false, applied: false, cite_handle: false, cite_title: false, opened: false, title_assessable: false };
  return {
    v: 1,
    session_key: sessionKey,
    agents: {
      main: {
        wire: "claude-code",
        principal: "claude-code",
        reset_id: "initial",
        windows: [
          { sid, mkey: "vault:wiki/sessions/open.md", channel: "prompt_pointer", visible: true, title: "Open note", handles: [], shingles: [], echo: [], injected_is_memory: false, scope: "turn", turn_key: -1, boot_turns: 0, acc },
        ],
        exposures: [],
        done: [],
        last_activity: new Date(T0).toISOString(),
      },
    },
  };
}

test("EXACT: a cased session that really exists on disk is never finalised through sessionKeys", async (t) => {
  for (const cased of ["Unlabeled", "UNLABELED", A.toUpperCase()]) {
    const { vault } = await makeVault(t, "minni-idle-exact-");
    const row = seedIdle(vault, cased);
    const stateDir = path.join(vault, ".runtime", "use-detector", "sessions", cased);
    fs.mkdirSync(stateDir, { recursive: true });
    const stateFile = path.join(stateDir, "windows.json");
    fs.writeFileSync(stateFile, `${JSON.stringify(openWindowState(cased, row.sid))}\n`);
    const before = fs.readFileSync(stateFile, "utf8");

    const result = finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: [cased] });
    assert.deepEqual([result.status, result.sessions, result.remaining], ["ran", 0, 0], cased);
    assert.equal(judgedCount(vault, cased), 0, `${cased}: no judged rows`);
    assert.equal(fs.readFileSync(stateFile, "utf8"), before, `${cased}: its state is unchanged`);
  }
});

test("EXACT, walked: the walk leaves any spelling of unlabeled out", async (t) => {
  for (const cased of ["Unlabeled", "UNLABELED"]) {
    const { vault } = await makeVault(t, "minni-idle-walk-case-");
    seedIdle(vault, cased);
    seedIdle(vault, A);
    const result = finalizeIdleSessions(vault, { now: IDLE_NOW });
    assert.equal(result.sessions, 1, `${cased}: only A`);
    assert.equal(judgedCount(vault, cased), 0, `${cased}: never finalised by the walk`);
    assert.equal(judgedCount(vault, A), 1);
  }
});

test("ALIAS: a cased spelling of an existing lowercase session finalises nothing and creates no directory", async (t) => {
  for (const spelling of ["Unlabeled", "UNLABELED", "upper-of-A"]) {
    const { vault } = await makeVault(t, "minni-idle-case-");
    seedIdle(vault, "unlabeled");
    seedIdle(vault, A);
    const key = spelling === "upper-of-A" ? A.toUpperCase() : spelling;
    const result = finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: [key] });
    assert.deepEqual([result.status, result.sessions, result.remaining], ["ran", 0, 0], spelling);
    assert.equal(judgedCount(vault, "unlabeled"), 0, `${spelling}: the unlabeled session stays out`);
    assert.equal(judgedCount(vault, A), 0, `${spelling}: no session is finalised under an alias`);
    assert.equal(fs.existsSync(path.join(vault, ".runtime", "use-detector", "sessions")), false, `${spelling}: no state directory`);
  }
});

test("isCanonicalSessionKey: a valid session name, lowercase, never unlabeled", async () => {
  const { isCanonicalSessionKey } = await import("../dist/use-detector.js");
  assert.equal(typeof isCanonicalSessionKey, "function", "exported");
  for (const key of [A, "tx-0123456789abcdef", "a", "0", "ab_c-d"]) assert.equal(isCanonicalSessionKey(key), true, key);
  for (const key of ["unlabeled", "Unlabeled", "UNLABELED", A.toUpperCase(), "Tx-0123456789abcdef", "../x", "a/b", "", "-a", "x".repeat(65), 7, null, undefined, {}]) {
    assert.equal(isCanonicalSessionKey(key), false, JSON.stringify(key) ?? String(key));
  }
});

test("runDetector treats any spelling of unlabeled as unlabeled, and refuses a cased key", async (t) => {
  for (const [key, status] of [["Unlabeled", "unlabeled"], ["UNLABELED", "unlabeled"], [A.toUpperCase(), "invalid"]]) {
    const { vault, transcriptPath } = await makeVault(t, "minni-detector-case-");
    const result = runDetector({ vaultPath: vault, wire: "claude-code", session_key: key, agent_key: "main", transcriptPath, now: IDLE_NOW });
    assert.equal(result.status, status, key);
    assert.equal(fs.existsSync(path.join(vault, ".runtime", "use-detector")), false, `${key}: nothing created`);
  }
});

test("a non-array sessionKeys yields no candidates, never the full walk", async (t) => {
  for (const value of [A, { 0: A, length: 1 }, null, 7]) {
    const { vault } = await makeVault(t, "minni-idle-nonarray-");
    seedIdle(vault, A);
    const result = finalizeIdleSessions(vault, { now: IDLE_NOW, sessionKeys: value });
    assert.deepEqual([result.status, result.sessions, result.remaining], ["ran", 0, 0], JSON.stringify(value));
    assert.equal(judgedCount(vault, A), 0, `${JSON.stringify(value)}: nothing examined`);
  }
});
