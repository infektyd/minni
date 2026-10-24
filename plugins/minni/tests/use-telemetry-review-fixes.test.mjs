// Recall-to-use S1a-2, same-lab review fixes:
//   F4  the session sweep runs on every outcome, also where SessionStart is dropped (Grok);
//   F2  PreToolUse loads the capture only for a deny it returns, never on the allow path;
//   F3  rows a hook failed to write are audited once, by count;
//   F1  turn_ix is taken under an O_EXCL lock: no two prompts share one;
//   F8  a present-but-corrupt counters.json is left alone and the turn says 0;
//   F9  a planted hit-keys.json cannot put an absolute or `..` vault key in a row;
//   F6  orphaned counts-file temp files older than an hour are swept.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { sweepSessionDirs } from "../dist/use-capture.js";
import { auditTail } from "../dist/vault.js";
import {
  daemonResult,
  exists,
  makeFixture,
  readLedger,
  runHook,
  sessionKeyOf,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const DAY = 24 * 60 * 60 * 1000;

async function idleSessionDir(sessions, name, ageMs) {
  const dir = path.join(sessions, name);
  await mkdir(dir, { recursive: true });
  const when = new Date(Date.now() - ageMs);
  await writeFile(path.join(dir, "counters.json"), '{"v":1,"turn_ix":3}', "utf8");
  await utimes(path.join(dir, "counters.json"), when, when);
  await utimes(dir, when, when);
  return dir;
}

function sessionsOf(vault) {
  return path.join(vault, ".runtime", "recall-session.d");
}

function turnsOf(rows) {
  return rows.filter((row) => row.kind === "turn");
}

// ---- F4 ---------------------------------------------------------------------

test("F4: a Grok boot (SessionStart dropped by the wire) still sweeps a 91-day-idle session", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f4");
  fixture.vault = path.join(fixture.root, "grok-vault");
  await mkdir(fixture.vault, { recursive: true });
  await startFakeDaemon(fixture);
  const sessions = sessionsOf(fixture.vault);
  await mkdir(sessions, { recursive: true });
  const stale = await idleSessionDir(sessions, "0123456789abcdef", 91 * DAY);
  const { code } = await runHook(
    fixture,
    "SessionStart",
    { session_id: "grok-boot", source: "startup" },
    { bin: "grok-hook.js", env: { MINNI_GROK_VAULT_PATH: fixture.vault, MINNI_GROK_AGENT_ID: "grok-build" } },
  );
  assert.equal(code, 0);
  assert.ok(!(await exists(stale)), "the idle directory must be swept even though the boot was not delivered");
  assert.ok(await exists(path.join(sessions, sessionKeyOf("grok-boot"))), "the live session stays");
  const audit = (await auditTail(fixture.vault, 50)).text;
  assert.ok(!/hook_grok_undelivered/.test(audit), "the sweep is not a rollback");
});

// ---- F2 ---------------------------------------------------------------------

async function loadedModules(t, fixture, event, payload, env = {}) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-review-loads-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const log = path.join(dir, "loads.log");
  const preload = path.join(dir, "log-resolve.mjs");
  await writeFile(
    preload,
    'import { registerHooks } from "node:module";\nimport { appendFileSync } from "node:fs";\n' +
      "registerHooks({ resolve(specifier, context, nextResolve) { const r = nextResolve(specifier, context);" +
      " appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${r.url}\\n`); return r; } });\n",
    "utf8",
  );
  const result = await runHook(fixture, event, payload, {
    env: { ...env, MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  const names = (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
  return { ...result, names };
}

test("F2: an allowed PreToolUse never loads the capture; a returned deny does, and records", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f2");
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results: [daemonResult({ source: "/e/a.md", docId: 1, wikilink: "[[a]]", title: "Alpha" })] }),
  });
  const session = { session_id: "review-f2" };
  const read = { ...session, tool_name: "Read", tool_input: { file_path: "/tmp/x" } };

  const cold = await loadedModules(t, fixture, "PreToolUse", read);
  assert.equal(cold.output.hookSpecificOutput, undefined, "nothing armed: allowed");
  assert.ok(cold.names.includes("hook-handlers.js"));
  assert.ok(!cold.names.includes("use-capture.js"), "the allow path must not load the capture");

  await runHook(fixture, "UserPromptSubmit", { ...session, prompt: "tell me about alpha" });
  const deny = await loadedModules(t, fixture, "PreToolUse", read);
  assert.equal(deny.output.hookSpecificOutput?.permissionDecision, "deny");
  assert.ok(deny.names.includes("use-capture.js"), "a deny being returned loads the capture");
  const guard = (await readLedger(fixture.vault)).filter((row) => row.channel === "guard");
  assert.equal(guard.length, 1);
  assert.equal(guard[0].mkey, "doc:personal:1");

  const reissued = await loadedModules(t, fixture, "PreToolUse", read);
  assert.equal(reissued.output.hookSpecificOutput, undefined, "consumed: allowed");
  assert.ok(!reissued.names.includes("use-capture.js"));
});

// ---- F3 ---------------------------------------------------------------------

test("F3: rows the hook could not write are audited once, by count only", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f3");
  await startFakeDaemon(fixture);
  await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
  await writeFile(path.join(fixture.vault, ".runtime", "use-ledger"), "not a directory\n", "utf8");
  const { code } = await runHook(fixture, "UserPromptSubmit", { session_id: "review-f3", prompt: "a question" });
  assert.equal(code, 0);
  const notes = (await auditTail(fixture.vault, 50)).entries.filter((entry) => entry.includes("hook_use_ledger_failed"));
  assert.equal(notes.length, 1, "one line for the hook");
  assert.match(notes[0], /1 row\(s\) not written/);
  assert.ok(!notes[0].includes(fixture.vault) && !notes[0].includes("use-ledger/"), `count only: ${notes[0]}`);
});

test("F3: a hook whose rows all landed writes no failure line", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f3-clean");
  await startFakeDaemon(fixture);
  await runHook(fixture, "UserPromptSubmit", { session_id: "review-f3-clean", prompt: "a question" });
  const notes = (await auditTail(fixture.vault, 50)).entries.filter((entry) => entry.includes("_use_ledger_failed"));
  assert.equal(notes.length, 0);
});

// ---- F1 ---------------------------------------------------------------------

test("F1: concurrent prompts in one session never share a turn_ix", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f1");
  await startFakeDaemon(fixture);
  const session = "review-f1";
  const N = 8;
  for (let round = 0; round < 2; round += 1) {
    await Promise.all(
      Array.from({ length: N }, (_, i) =>
        runHook(fixture, "UserPromptSubmit", { session_id: session, prompt_id: `r${round}-p${i}`, prompt: `question ${round} ${i}` }),
      ),
    );
  }
  const indexes = turnsOf(await readLedger(fixture.vault)).map((row) => row.turn_ix).sort((a, b) => a - b);
  assert.deepEqual(indexes, Array.from({ length: 2 * N }, (_, i) => i + 1), `duplicate or lost indices: ${indexes}`);
});

test("F1: a held lock fails open to turn_ix 0 within its bound; a stale one is broken", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f1-lock");
  await startFakeDaemon(fixture);
  const session = "review-f1-lock";
  const dir = path.join(sessionsOf(fixture.vault), sessionKeyOf(session));
  await mkdir(dir, { recursive: true });
  const lock = path.join(dir, "counters.json.lock");

  await writeFile(lock, "4242 0\n", "utf8");
  const started = Date.now();
  const held = await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt_id: "held", prompt: "first question" });
  assert.equal(held.code, 0);
  assert.ok(Date.now() - started < 10_000);
  assert.equal(turnsOf(await readLedger(fixture.vault)).at(-1).turn_ix, 0, "a live lock is waited out, then fails open");
  assert.ok(await exists(lock), "another process's live lock is not removed");

  const old = new Date(Date.now() - 5_000);
  await utimes(lock, old, old);
  await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt_id: "stale", prompt: "second question" });
  assert.equal(turnsOf(await readLedger(fixture.vault)).at(-1).turn_ix, 1, "a stale lock is broken");
  assert.ok(!(await exists(lock)), "and released after");
});

// ---- F8 ---------------------------------------------------------------------

test("F8: a corrupt counters.json is left as it is, and the turn says 0", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f8");
  await startFakeDaemon(fixture);
  const session = "review-f8";
  const dir = path.join(sessionsOf(fixture.vault), sessionKeyOf(session));
  await mkdir(dir, { recursive: true });
  const counters = path.join(dir, "counters.json");
  for (const corrupt of ["{not json", '{"v":2,"turn_ix":7}', '{"v":1,"turn_ix":-3}']) {
    await writeFile(counters, corrupt, "utf8");
    await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt: "a question" });
    assert.equal(turnsOf(await readLedger(fixture.vault)).at(-1).turn_ix, 0, corrupt);
    assert.equal(await readFile(counters, "utf8"), corrupt, "the file is evidence: never overwritten");
  }
  // Absent is not corrupt: it starts at 1.
  await rm(counters);
  await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt: "a question" });
  assert.equal(turnsOf(await readLedger(fixture.vault)).at(-1).turn_ix, 1);
});

// ---- F9 ---------------------------------------------------------------------

test("F9: planted absolute or `..` vault keys in hit-keys.json never reach a guard row", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "review-f9");
  const results = [
    daemonResult({ source: "/e/one.md", docId: 1, wikilink: "[[one]]", title: "One", confidence: 0.99 }),
    daemonResult({ source: "/e/two.md", docId: 2, wikilink: "[[two]]", title: "Two", confidence: 0.98 }),
    daemonResult({ source: "/e/three.md", docId: 3, wikilink: "[[three]]", title: "Three", confidence: 0.97 }),
    daemonResult({ source: "/e/four.md", docId: 4, wikilink: "[[four]]", title: "Four", confidence: 0.96 }),
  ];
  await startFakeDaemon(fixture, { search: () => ({ agent_id: "claude-code", results }) });
  const session = "review-f9";
  await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt: "tell me about the four notes" });
  const file = path.join(sessionsOf(fixture.vault), sessionKeyOf(session), "hit-keys.json");
  const recorded = JSON.parse(await readFile(file, "utf8"));
  const planted = ["vault:/etc/passwd", "vault:../outside.md", "vault:wiki/../../escape.md", "vault:wiki/fine.md"];
  recorded.hits = recorded.hits.map((hit, index) => ({ ...hit, mkey: planted[index] }));
  await writeFile(file, JSON.stringify(recorded), "utf8");

  const { output } = await runHook(fixture, "PreToolUse", {
    session_id: session,
    tool_name: "Read",
    tool_input: { file_path: "/tmp/x" },
  });
  assert.equal(output.hookSpecificOutput?.permissionDecision, "deny");
  const guard = (await readLedger(fixture.vault)).filter((row) => row.channel === "guard");
  assert.deepEqual(
    guard.map((row) => row.mkey),
    ["vault:wiki/fine.md"],
    "only the well-formed vault-relative key is recorded",
  );
});

// ---- F6 ---------------------------------------------------------------------

test("F6: orphaned counts-file temp files older than an hour are swept; links and fresh ones stay", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-review-f6-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  const runtime = path.join(vault, ".runtime");
  await mkdir(path.join(runtime, "recall-session.d"), { recursive: true });
  const uuid = "00000000-0000-4000-8000-000000000001";
  const name = (pid) => `prompt-templates.json.${pid}.${uuid}.tmp`;
  const now = Date.now();
  const twoHours = new Date(now - 2 * 60 * 60 * 1000);
  const orphan = path.join(runtime, name(101));
  const fresh = path.join(runtime, name(102));
  const other = path.join(runtime, "notes.json.101.tmp");
  for (const file of [orphan, fresh, other]) await writeFile(file, "{}", "utf8");
  await utimes(orphan, twoHours, twoHours);
  await utimes(other, twoHours, twoHours);
  const target = path.join(root, "outside.tmp");
  await writeFile(target, "keep", "utf8");
  await symlink(target, path.join(runtime, name(103)));

  sweepSessionDirs(vault, { now });
  assert.ok(!(await exists(orphan)), "the orphan is removed");
  assert.ok(await exists(fresh), "a temp file under an hour old may still be in use");
  assert.ok(await exists(other), "only the counts file's temp names are swept");
  assert.equal(await readFile(target, "utf8"), "keep", "a symlink is never followed");
  assert.ok(await exists(path.join(runtime, name(103))), "nor removed");
});
