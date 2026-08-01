// R2 (SessionStart data loss): the two live-loss paths in the boot hook.
//
// 1. DELIVERY BEFORE ARCHIVE. SessionStart re-injects corrections stashed by
//    PreCompact (`corrections_reassert`) and then ARCHIVES the inbox entry that
//    carried them. Archiving inside the handler — before the envelope has left
//    the process — makes the window between "entry archived" and "host received
//    the envelope" a permanent-loss window: a hook killed there has consumed the
//    correction without ever delivering it, and nothing re-delivers it.
//
//    The kill-window test below reproduces that window WITHOUT racing a timer:
//    the parent destroys its read end of the child's stdout, so the child's
//    envelope write fails with EPIPE and the host provably never receives it.
//    That is the same observable as a mid-SessionStart kill (output never
//    lands) and it is deterministic. A correction must survive it.
//
// 2. TIME BUDGET. Gemini kills SessionStart at 10s (hooks-gemini.json). The
//    budget that bounds the prompt-time hook had no SessionStart counterpart,
//    so a cold daemon ran the boot RPCs past the kill and lost the whole
//    envelope. The manifest-parity test pins each platform's declared budget to
//    the timeout in its own manifest, so tightening one without the other fails
//    CI instead of shipping.
//
// Isolation follows tests/hook-behavior.test.mjs: every env knob points inside
// the tmp fixture, the daemon socket is absent (fast structured failure) and
// the AFM health URL is a closed loopback port. Live ~/.minni is never touched.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK_JS = path.join(PLUGIN_ROOT, "dist", "hook.js");
const HOOKS_DIR = path.join(PLUGIN_ROOT, "hooks");

/** The one correction under test: schema-valid, so it is fully consumable. */
const CORRECTION = {
  event_id: 4242,
  superseded_learning_id: 11,
  new_learning_id: 12,
  originating_agent: "codex",
  created_at: 1_754_000_000,
};

function inboxName(epochMs, slug) {
  const day = new Date(epochMs).toISOString().slice(0, 10);
  return `${day}-${epochMs.toString(36)}-${slug}.json`;
}

async function makeFixture() {
  const root = await mkdtemp(path.join(tmpdir(), "sm-sessionstart-delivery-"));
  const fixture = {
    root,
    vault: path.join(root, "claudecode-vault"),
    home: path.join(root, "home"),
  };
  fixture.inbox = path.join(fixture.vault, "inbox");
  await mkdir(fixture.inbox, { recursive: true });
  await mkdir(fixture.home, { recursive: true });
  await writeFile(
    path.join(fixture.inbox, inboxName(Date.now() - 86_400_000, "precompact-reassert")),
    JSON.stringify({
      slug: "precompact-reassert",
      kind: "precompact_reassert",
      agent_id: "claude-code",
      createdAt: new Date(Date.now() - 86_400_000).toISOString(),
      stale_belief_events: [CORRECTION],
    }),
    "utf8",
  );
  return fixture;
}

function hookEnv(fixture) {
  return {
    ...process.env,
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
    MINNI_CLAUDECODE_HOOKS: "on",
  };
}

/** Inbox entries still awaiting delivery (i.e. NOT yet archived). */
async function pendingEntries(fixture) {
  return (await readdir(fixture.inbox)).filter((name) => name.endsWith(".json"));
}

async function archivedEntries(fixture) {
  try {
    return (await readdir(path.join(fixture.inbox, ".archive"))).filter((name) =>
      name.endsWith(".json"),
    );
  } catch {
    return [];
  }
}

/** A normal SessionStart run: returns the parsed envelope body. */
function runSessionStart(fixture) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [HOOK_JS, "SessionStart"], {
      env: hookEnv(fixture),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.on("error", reject);
    child.on("close", () => {
      const line = stdout.trim().split("\n").pop();
      const output = JSON.parse(line);
      const context = output.hookSpecificOutput?.additionalContext ?? "";
      const body = context.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/)?.[1];
      assert.ok(body, "SessionStart must emit a minni:context envelope");
      resolve(JSON.parse(body));
    });
    child.stdin.end(JSON.stringify({ session_id: "delivery-fixture" }));
  });
}

/**
 * A SessionStart run whose output never reaches the host: the parent destroys
 * its read end before the child writes, so the envelope write fails with EPIPE.
 * Resolves once the child has exited.
 */
function runSessionStartUndelivered(fixture) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [HOOK_JS, "SessionStart"], {
      env: hookEnv(fixture),
      stdio: ["pipe", "pipe", "pipe"],
    });
    // Drain stderr so a warning cannot block the child on a full pipe.
    child.stderr.resume();
    child.on("error", reject);
    child.on("close", () => resolve());
    child.stdin.end(JSON.stringify({ session_id: "delivery-fixture" }));
    // Close the delivery channel immediately — long before the child, which
    // must boot node and read the vault first, reaches its write.
    child.stdout.destroy();
  });
}

test(
  "kill window: a SessionStart whose envelope never reaches the host leaves the correction re-deliverable",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture();
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    assert.equal((await pendingEntries(fixture)).length, 1, "fixture starts with one correction");

    // The undeliverable boot: the host never receives the envelope.
    await runSessionStartUndelivered(fixture);

    // THE GATE. Archiving before delivery consumes the correction without
    // delivering it, and nothing ever re-delivers it.
    assert.deepEqual(
      await archivedEntries(fixture),
      [],
      "an undelivered SessionStart must archive nothing — the correction it consumed was never injected",
    );
    assert.equal(
      (await pendingEntries(fixture)).length,
      1,
      "the correction must still be pending, i.e. re-deliverable on the next boot",
    );

    // ...and it does in fact re-deliver on the next boot.
    const body = await runSessionStart(fixture);
    assert.deepEqual(
      body.corrections_reassert,
      [CORRECTION],
      "the surviving correction must re-inject on the next boot",
    );

    // Only NOW, after a delivered envelope, is it archived — exactly once.
    assert.equal(
      (await archivedEntries(fixture)).length,
      1,
      "a delivered correction is archived, so it re-injects exactly once",
    );
    assert.equal((await pendingEntries(fixture)).length, 0);
  },
);

test(
  "a delivered SessionStart archives the correction it injected (no unbounded inbox growth)",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture();
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const first = await runSessionStart(fixture);
    assert.deepEqual(first.corrections_reassert, [CORRECTION]);
    assert.equal((await archivedEntries(fixture)).length, 1);

    // Exactly-once: the second boot must not re-inject it.
    const second = await runSessionStart(fixture);
    assert.equal(
      second.corrections_reassert,
      undefined,
      "an archived correction must not re-inject",
    );
  },
);

// ---------------------------------------------------------------------------
// The deferral registry itself. `emitAndCommit` is deliberately NOT exercised
// in-process — it writes to this runner's stdout — so these cover the ordering
// primitive it is built on.
// ---------------------------------------------------------------------------

test("deferred commits do not run until they are explicitly committed", async (t) => {
  const { deferUntilDelivered, discardDeliveryCommits, pendingDeliveryCommitCount, runDeliveryCommits } =
    await import("../dist/hook-delivery.js");
  t.after(() => discardDeliveryCommits());
  discardDeliveryCommits();

  const ran = [];
  deferUntilDelivered(async () => void ran.push("first"));
  deferUntilDelivered(async () => void ran.push("second"));

  assert.deepEqual(ran, [], "registering must not run the work");
  assert.equal(pendingDeliveryCommitCount(), 2);

  await runDeliveryCommits();
  assert.deepEqual(ran, ["first", "second"], "commits run in registration order");
  assert.equal(pendingDeliveryCommitCount(), 0, "commits run exactly once");

  await runDeliveryCommits();
  assert.deepEqual(ran, ["first", "second"], "a second commit pass is a no-op");
});

test("discarded commits never run — the entry stays for the next boot", async (t) => {
  const { deferUntilDelivered, discardDeliveryCommits, pendingDeliveryCommitCount, runDeliveryCommits } =
    await import("../dist/hook-delivery.js");
  t.after(() => discardDeliveryCommits());
  discardDeliveryCommits();

  let ran = false;
  deferUntilDelivered(async () => {
    ran = true;
  });
  discardDeliveryCommits();
  await runDeliveryCommits();

  assert.equal(ran, false, "an undelivered output must not consume anything");
  assert.equal(pendingDeliveryCommitCount(), 0);
});

test("a throwing commit does not block the commits after it", async (t) => {
  const { deferUntilDelivered, discardDeliveryCommits, runDeliveryCommits } = await import(
    "../dist/hook-delivery.js"
  );
  t.after(() => discardDeliveryCommits());
  discardDeliveryCommits();

  const ran = [];
  deferUntilDelivered(async () => {
    throw new Error("archive failed");
  });
  deferUntilDelivered(async () => void ran.push("after"));

  await runDeliveryCommits();
  assert.deepEqual(ran, ["after"], "a failed cleanup must not lose the rest");
});

// ---------------------------------------------------------------------------
// Time budget: declared in code AND in the manifest, and the two must agree.
// ---------------------------------------------------------------------------

/** SessionStart `timeout` (seconds) declared in a platform manifest. */
async function manifestSessionStartTimeout(file, read) {
  const manifest = JSON.parse(await readFile(path.join(HOOKS_DIR, file), "utf8"));
  return read(manifest);
}

test("every platform manifest declares a SessionStart timeout", async () => {
  const declared = {
    "hooks.json": (m) => m.hooks.SessionStart[0].hooks[0].timeout,
    "hooks-codex.json": (m) => m.hooks.SessionStart[0].hooks[0].timeout,
    "hooks-grok.json": (m) => m.hooks.SessionStart[0].hooks[0].timeout,
    "hooks-gemini.json": (m) => m.minni.SessionStart[0].timeout,
    "hooks-cursor.json": (m) => m.hooks.sessionStart[0].timeout,
  };
  for (const [file, read] of Object.entries(declared)) {
    const timeout = await manifestSessionStartTimeout(file, read);
    assert.equal(
      typeof timeout,
      "number",
      `${file}: SessionStart must declare a timeout, or the budget has nothing to stay inside`,
    );
    assert.ok(timeout > 0, `${file}: SessionStart timeout must be positive`);
  }
});

test("each entrypoint's sessionStartHookTimeoutMs mirrors its manifest", async () => {
  const cases = [
    {
      entry: "codex-hook.js",
      file: "hooks-codex.json",
      read: (m) => m.hooks.SessionStart[0].hooks[0].timeout,
    },
    {
      entry: "grok-hook.js",
      file: "hooks-grok.json",
      read: (m) => m.hooks.SessionStart[0].hooks[0].timeout,
    },
    {
      entry: "gemini-hook.js",
      file: "hooks-gemini.json",
      read: (m) => m.minni.SessionStart[0].timeout,
    },
    {
      entry: "cursor-hook.js",
      file: "hooks-cursor.json",
      read: (m) => m.hooks.sessionStart[0].timeout,
    },
  ];
  for (const { entry, file, read } of cases) {
    const source = await readFile(path.join(PLUGIN_ROOT, "src", entry.replace(/\.js$/, ".ts")), "utf8");
    const match = source.match(/sessionStartHookTimeoutMs:\s*([\d_]+)/);
    assert.ok(match, `${entry}: must declare sessionStartHookTimeoutMs`);
    const declaredMs = Number(match[1].replace(/_/g, ""));
    const manifestMs = (await manifestSessionStartTimeout(file, read)) * 1000;
    assert.equal(
      declaredMs,
      manifestMs,
      `${entry}: sessionStartHookTimeoutMs (${declaredMs}) must mirror ${file} (${manifestMs})`,
    );
  }
});

test("the SessionStart budget stays inside gemini's 10s kill with room to write", async () => {
  const { effectiveHookBudgetMs, HOOK_BUDGET_HARNESS_FRACTION } = await import(
    "../dist/hook-utils.js"
  );
  const geminiBudget = effectiveHookBudgetMs(10_000, {});
  assert.ok(
    geminiBudget < 10_000,
    "the internal budget must finish before the harness kill, not at it",
  );
  assert.equal(geminiBudget, Math.floor(10_000 * HOOK_BUDGET_HARNESS_FRACTION));
});
