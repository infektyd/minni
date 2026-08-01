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
import { createServer } from "node:net";
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
function runSessionStart(fixture, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [HOOK_JS, "SessionStart"], {
      env: { ...hookEnv(fixture), ...extraEnv },
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
 * Resolves with the exit code and stderr once the child has exited.
 */
function runSessionStartUndelivered(fixture) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [HOOK_JS, "SessionStart"], {
      env: hookEnv(fixture),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stderr }));
    child.stdin.end(JSON.stringify({ session_id: "delivery-fixture" }));
    // Close the delivery channel immediately — long before the child, which
    // must boot node and read the vault first, reaches its write.
    child.stdout.destroy();
  });
}

/** Whole audit log text (daily file plus the legacy log.md). */
async function auditText(fixture) {
  const day = new Date().toISOString().slice(0, 10);
  const parts = await Promise.all(
    [
      path.join(fixture.vault, "log.md"),
      path.join(fixture.vault, "logs", `${day}.md`),
    ].map((file) => readFile(file, "utf8").catch(() => "")),
  );
  return parts.join("\n");
}

test(
  "kill window: a SessionStart whose envelope never reaches the host leaves the correction re-deliverable",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture();
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    assert.equal((await pendingEntries(fixture)).length, 1, "fixture starts with one correction");

    // The undeliverable boot: the host never receives the envelope.
    const undelivered = await runSessionStartUndelivered(fixture);

    // The correction must survive by DESIGN — the rollback path running — not
    // because the process happened to crash before reaching the archive. An
    // unhandled EPIPE would leave the entry intact too, and would have made
    // this test pass against a hook that never rolled anything back.
    assert.equal(
      undelivered.code,
      0,
      `an undeliverable boot must exit cleanly, not crash: ${undelivered.stderr.slice(0, 400)}`,
    );
    assert.doesNotMatch(
      undelivered.stderr,
      /Unhandled 'error' event|EPIPE/,
      "a closed host pipe must not surface a raw node stack trace",
    );
    assert.match(
      await auditText(fixture),
      /hook_undelivered/,
      "a boot whose memory never reached the host must be visible as such in the audit",
    );

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

test(
  "corrections still re-inject when the budget is fully spent — reassert never rides a budgeted read",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture();
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    // A 1ms budget: every budgeted read degrades. The correction must NOT — a
    // post-compaction boot with a spent budget is precisely the boot this whole
    // path exists to serve, and reading corrections off the budgeted inbox read
    // would silently inject nothing here.
    const body = await runSessionStart(fixture, { MINNI_HOOK_BUDGET_MS: "1" });

    assert.deepEqual(
      body.corrections_reassert,
      [CORRECTION],
      "a spent budget may drop CONTEXT, never a stashed correction",
    );
    assert.ok(
      Array.isArray(body.degraded?.sections) && body.degraded.sections.length > 0,
      "a boot this degraded must say so rather than look clean",
    );
    // The handoff LIST failed (no daemon), so handoff_acks is an empty set the
    // hook never actually computed. Reporting [] without saying so is a false
    // all-clear — the same shape of lie as a zero-filled pending_learnings.
    assert.ok(
      body.degraded.sections.includes("handoff_leases"),
      `a failed handoff list must be named as degraded, not silently empty (got ${JSON.stringify(body.degraded.sections)})`,
    );
    // Still exactly-once: delivered, so archived.
    assert.equal((await archivedEntries(fixture)).length, 1);
  },
);

// ---------------------------------------------------------------------------
// A PLATFORM DROP is a non-delivery too. Writing a noop the host ignores is not
// delivering the correction that noop was supposed to carry.
// ---------------------------------------------------------------------------

const GROK_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "grok-hook.js");

/** Grok SessionStart: the wire cannot inject there, so the envelope is dropped. */
function runGrokSessionStart(fixture) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [GROK_HOOK_JS, "SessionStart"], {
      env: {
        ...process.env,
        MINNI_HOME: fixture.home,
        MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
        MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
        MINNI_BYPASS_AUDIT_LIMIT: "true",
        MINNI_GROK_VAULT_PATH: fixture.vault,
        MINNI_GROK_HOOKS: "on",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.resume();
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, output: JSON.parse(stdout.trim().split("\n").pop()) }));
    child.stdin.end(JSON.stringify({ session_id: "grok-fixture" }));
  });
}

test(
  "a platform that cannot inject at SessionStart must not have its correction archived",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-grok-drop-"));
    const fixture = {
      root,
      vault: path.join(root, "grok-build-vault"),
      home: path.join(root, "home"),
    };
    fixture.inbox = path.join(fixture.vault, "inbox");
    await mkdir(fixture.inbox, { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    const stamp = Date.now() - 86_400_000;
    await writeFile(
      path.join(fixture.inbox, inboxName(stamp, "grok-precompact-handoff")),
      JSON.stringify({
        slug: "grok-precompact-handoff",
        kind: "grok_precompact_handoff",
        agent_id: "grok-build",
        createdAt: new Date(stamp).toISOString(),
        stale_belief_events: [CORRECTION],
      }),
      "utf8",
    );
    t.after(() => rm(root, { recursive: true, force: true }));

    const { output } = await runGrokSessionStart(fixture);

    // Grok's wire declares SessionStart un-injectable (only Stop parses hook
    // output), so this boot emits a bare noop carrying nothing.
    assert.equal(
      output.hookSpecificOutput,
      undefined,
      "fixture precondition: grok SessionStart must be a dropped injection",
    );

    // THE GATE. The noop was written to stdout successfully — but the CORRECTION
    // never went anywhere. Treating that as delivery archives the only durable
    // copy of the correction in exchange for output the host ignores.
    // THE GATE. The noop was written to stdout successfully — but the CORRECTION
    // never went anywhere. Treating that as delivery would archive the only
    // durable copy in exchange for output the host ignores.
    assert.deepEqual(
      await archivedEntries(fixture),
      [],
      "a dropped envelope delivered nothing — the correction it carried must not be CONSUMED",
    );
    // It survives; on this wire the drop is structural, so it is parked out of
    // the retry window rather than retried forever (see the parking test).
    const parked = await readdir(path.join(fixture.inbox, ".undeliverable")).catch(() => []);
    assert.equal(
      (await pendingEntries(fixture)).length + parked.filter((f) => f.endsWith(".json")).length,
      1,
      "the correction must still exist somewhere on disk — undelivered is not consumed",
    );
  },
);

test(
  "an EMPTY stash is still cleared on a drop-only platform (no unbounded growth)",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-grok-empty-"));
    const fixture = {
      root,
      vault: path.join(root, "grok-build-vault"),
      home: path.join(root, "home"),
    };
    fixture.inbox = path.join(fixture.vault, "inbox");
    await mkdir(fixture.inbox, { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    const stamp = Date.now() - 86_400_000;
    // codex and grok stash at PreCompact unconditionally, so most of these are
    // empty. They carry no correction, so clearing them cannot depend on
    // delivery — if it did, a platform whose wire never injects at SessionStart
    // would accumulate one file per compaction cycle forever.
    await writeFile(
      path.join(fixture.inbox, inboxName(stamp, "grok-precompact-handoff")),
      JSON.stringify({
        slug: "grok-precompact-handoff",
        kind: "grok_precompact_handoff",
        agent_id: "grok-build",
        createdAt: new Date(stamp).toISOString(),
        stale_belief_events: [],
      }),
      "utf8",
    );
    t.after(() => rm(root, { recursive: true, force: true }));

    await runGrokSessionStart(fixture);

    assert.equal(
      (await archivedEntries(fixture)).length,
      1,
      "an empty stash carries nothing, so a dropped envelope must not keep it alive",
    );
    assert.equal((await pendingEntries(fixture)).length, 0);
  },
);

test(
  "a MIXED window settles the empty stash even though the real correction is held back",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-grok-mixed-"));
    const fixture = {
      root,
      vault: path.join(root, "grok-build-vault"),
      home: path.join(root, "home"),
    };
    fixture.inbox = path.join(fixture.vault, "inbox");
    await mkdir(fixture.inbox, { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    const base = Date.now() - 86_400_000;
    // One empty stash and one real correction in the SAME reassert window. A
    // single deferred settlement covering both would hold the empty one hostage
    // to the correction's delivery — and on a platform that can never inject,
    // that is the unbounded growth the empty-only case was supposed to rule out.
    await writeFile(
      path.join(fixture.inbox, inboxName(base, "grok-empty-handoff")),
      JSON.stringify({
        slug: "grok-empty-handoff",
        kind: "grok_precompact_handoff",
        agent_id: "grok-build",
        createdAt: new Date(base).toISOString(),
        stale_belief_events: [],
      }),
      "utf8",
    );
    await writeFile(
      path.join(fixture.inbox, inboxName(base + 1000, "grok-correction-handoff")),
      JSON.stringify({
        slug: "grok-correction-handoff",
        kind: "grok_precompact_handoff",
        agent_id: "grok-build",
        createdAt: new Date(base + 1000).toISOString(),
        stale_belief_events: [CORRECTION],
      }),
      "utf8",
    );
    t.after(() => rm(root, { recursive: true, force: true }));

    await runGrokSessionStart(fixture);

    const archived = await archivedEntries(fixture);
    const parked = (await readdir(path.join(fixture.inbox, ".undeliverable")).catch(() => [])).filter(
      (f) => f.endsWith(".json"),
    );
    assert.equal(
      archived.length,
      1,
      "the empty stash must still clear — it carried nothing to deliver",
    );
    assert.ok(
      archived[0].includes("empty"),
      `the archived entry must be the EMPTY one, not the correction (got ${archived[0]})`,
    );
    // The real correction is NOT consumed: it survives, parked out of the retry
    // window because this wire's drop is structural.
    assert.equal(parked.length, 1, "the real correction must survive the undeliverable boot");
    assert.ok(
      parked[0].includes("correction"),
      `the preserved entry must be the CORRECTION, not the empty stash (got ${parked[0]})`,
    );
  },
);

test(
  "a structurally undeliverable correction is PARKED, not retained forever",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-grok-park-"));
    const fixture = {
      root,
      vault: path.join(root, "grok-build-vault"),
      home: path.join(root, "home"),
    };
    fixture.inbox = path.join(fixture.vault, "inbox");
    await mkdir(fixture.inbox, { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    const stamp = Date.now() - 86_400_000;
    await writeFile(
      path.join(fixture.inbox, inboxName(stamp, "grok-precompact-handoff")),
      JSON.stringify({
        slug: "grok-precompact-handoff",
        kind: "grok_precompact_handoff",
        agent_id: "grok-build",
        createdAt: new Date(stamp).toISOString(),
        stale_belief_events: [CORRECTION],
      }),
      "utf8",
    );
    t.after(() => rm(root, { recursive: true, force: true }));

    await runGrokSessionStart(fixture);

    // Grok's wire can NEVER inject at SessionStart, so "keep it and retry next
    // boot" never terminates: one durable file per correction-bearing
    // compaction, each permanently occupying a reassert-window slot.
    assert.deepEqual(
      await pendingEntries(fixture),
      [],
      "a structural drop must not leave the entry in the live window to retry forever",
    );
    assert.deepEqual(
      await archivedEntries(fixture),
      [],
      "parking is not archiving — .archive means consumed, and this was never delivered",
    );
    const parked = await readdir(path.join(fixture.inbox, ".undeliverable")).catch(() => []);
    assert.equal(
      parked.filter((f) => f.endsWith(".json")).length,
      1,
      "the correction must be preserved on disk for the issue #253 delivery path",
    );
    assert.match(
      await auditText(fixture),
      /undeliverable_on_wire/,
      "parking must be audited, so the undelivered backlog is countable",
    );

    // And the window is genuinely clear: a second boot re-reads nothing.
    await runGrokSessionStart(fixture);
    assert.deepEqual(await pendingEntries(fixture), []);
  },
);

test(
  "a reaped handoff is still reported when the budget cut the inbox read",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-expired-degraded-"));
    const fixture = {
      root,
      vault: path.join(root, "claudecode-vault"),
      home: path.join(root, "home"),
    };
    fixture.inbox = path.join(fixture.vault, "inbox");
    await mkdir(fixture.inbox, { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    // An aged handoff the TTL reaper will archive on this boot. The reaper is
    // unbudgeted precisely because it archives as it walks — so this boot is the
    // entry's ONLY chance to be reported. Riding inside pending_learnings meant
    // a budget-cut inbox read dropped it and it surfaced zero times.
    const aged = Date.now() - 40 * 86_400_000;
    // The reaper pre-filters on the COMPACT handoff filename (YYYYMMDDTHHMMSSZ-),
    // not the dashed inbox form — an aged file under the wrong name is simply
    // never a handoff to it.
    const compactName = `${new Date(aged).toISOString().slice(0, 19).replace(/[-:]/g, "")}Z-stale-handoff.json`;
    await writeFile(
      path.join(fixture.inbox, compactName),
      JSON.stringify({
        slug: "stale-handoff",
        kind: "handoff",
        agent_id: "claude-code",
        createdAt: new Date(aged).toISOString(),
        ref: "wiki/notes/whatever",
      }),
      "utf8",
    );
    t.after(() => rm(root, { recursive: true, force: true }));

    const body = await runSessionStart(fixture, { MINNI_HOOK_BUDGET_MS: "1" });

    assert.ok(
      body.degraded?.sections?.includes("pending_learnings"),
      "fixture precondition: the budget must have cut the inbox read",
    );
    assert.ok(
      Array.isArray(body.expired_handoffs) && body.expired_handoffs.length === 1,
      `a reaped handoff must still be reported when its usual carrier is missing (got ${JSON.stringify(body.expired_handoffs)})`,
    );
    assert.equal(body.expired_handoffs[0].slug, "stale-handoff");
  },
);

test(
  "the settle audit distinguishes eager, pending and parked instead of one number",
  { timeout: 120_000 },
  async (t) => {
    // Mixed window on a DELIVERABLE wire: one empty stash settles immediately,
    // one real correction waits on delivery. A single count covering both
    // claimed two archives were pending when only one ever was — an operator
    // grepping "how many will settle on delivery" got a wrong answer.
    const fixture = await makeFixture();
    const base = Date.now() - 43_200_000;
    await writeFile(
      path.join(fixture.inbox, inboxName(base, "empty-reassert")),
      JSON.stringify({
        slug: "empty-reassert",
        kind: "precompact_reassert",
        agent_id: "claude-code",
        createdAt: new Date(base).toISOString(),
        stale_belief_events: [],
      }),
      "utf8",
    );
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const body = await runSessionStart(fixture);
    assert.deepEqual(body.corrections_reassert, [CORRECTION]);

    // The audit writes details as pretty-printed JSON, so match the whole log
    // rather than a single line.
    const audit = await auditText(fixture);
    assert.match(
      audit,
      /"reassert_entries_cleared_eager":\s*1/,
      "the empty stash settled before the audit and must be counted as such",
    );
    assert.match(
      audit,
      /"reassert_entries_pending_clear":\s*1/,
      "only the correction-bearing entry is still waiting on delivery",
    );
    // The old single count would have reported 2 here — both consumed paths,
    // one of which had already settled.
    assert.doesNotMatch(
      audit,
      /"reassert_entries_pending_clear":\s*2/,
      "an already-settled empty must not be counted as an archive still pending",
    );
  },
);

test("an exhausted budget does not orphan the rejection of the work it abandons", async () => {
  const { withBudget } = await import("../dist/hook-utils.js");
  // withBudget's argument is an ALREADY-RUNNING promise, so the zero-budget
  // branch must still attach a handler — an unhandled rejection terminates the
  // process, and a spent budget is the ordinary case for every read after the
  // first, not an exceptional one.
  const rejected = Promise.reject(new Error("wedged filesystem"));
  assert.equal(await withBudget(rejected, 0, "fallback"), "fallback");

  let unhandled;
  const onUnhandled = (error) => {
    unhandled = error;
  };
  process.on("unhandledRejection", onUnhandled);
  await new Promise((resolve) => setTimeout(resolve, 50));
  process.off("unhandledRejection", onUnhandled);
  assert.equal(unhandled, undefined, "the abandoned work's rejection must be absorbed");
});

test("the TTL reaper is never raced by a budget: it archives as it walks", async () => {
  // expireStaleInboxHandoffs ARCHIVES while it walks, and withBudget races
  // rather than cancels. Budgeting it lets it keep archiving in the background
  // while the boot reports an empty `expired` list — and archived entries are
  // invisible to readInboxStatus, so those handoffs would be reported ZERO
  // times, breaking the surfaces-exactly-once contract.
  for (const file of ["hook.ts", "hook-handlers.ts"]) {
    const source = await readFile(path.join(PLUGIN_ROOT, "src", file), "utf8");
    assert.doesNotMatch(
      source,
      /withBudget\(\s*expireStaleInboxHandoffs/,
      `${file}: the reaper is a destructive walk — a budget may skip a READ, never half-report a WRITE`,
    );
  }
});

test(
  "a wedged daemon cannot pin the boot past its budget",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture();
    // A socket that ACCEPTS and never answers is the case a raced promise
    // cannot handle: withBudget abandons the RPC but the live handle keeps the
    // event loop alive for the full 30s JSON-RPC default, so the hook is still
    // running when the harness deadline expires — and a killed hook's output is
    // discarded even though the bytes were already flushed and, worse, the
    // correction already archived. The deadline has to reach the socket.
    const socketPath = path.join(fixture.home, "wedged.sock");
    const server = createServer(() => {
      /* accept, then never respond */
    });
    await new Promise((resolve) => server.listen(socketPath, resolve));
    t.after(async () => {
      server.close();
      await rm(fixture.root, { recursive: true, force: true });
    });

    const started = Date.now();
    const { code } = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [HOOK_JS, "SessionStart"], {
        env: { ...hookEnv(fixture), MINNI_SOCKET_PATH: socketPath, MINNI_HOOK_BUDGET_MS: "1500" },
        stdio: ["pipe", "pipe", "pipe"],
      });
      child.stdout.resume();
      child.stderr.resume();
      child.on("error", reject);
      child.on("close", (c) => resolve({ code: c }));
      child.stdin.end(JSON.stringify({ session_id: "wedged" }));
    });
    const elapsed = Date.now() - started;

    assert.equal(code, 0, "the boot must exit cleanly rather than be killed");
    assert.ok(
      elapsed < 20_000,
      `boot must not outlive its budget waiting on a wedged daemon (took ${elapsed}ms; the ` +
        "un-threaded JSON-RPC default alone is 30000ms)",
    );
  },
);

test(
  "an undelivered boot is audited even when it had no correction to roll back",
  { timeout: 120_000 },
  async (t) => {
    // No inbox entry at all: nothing is deferred, so there is no rollback to
    // report — but an envelope carrying identity, recall and the active plan
    // still reached nobody, and a boot that degraded that far must not look
    // clean in the log.
    const root = await mkdtemp(path.join(tmpdir(), "sm-silent-undelivered-"));
    const fixture = {
      root,
      vault: path.join(root, "claudecode-vault"),
      home: path.join(root, "home"),
    };
    fixture.inbox = path.join(fixture.vault, "inbox");
    await mkdir(fixture.inbox, { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    t.after(() => rm(root, { recursive: true, force: true }));

    const { code } = await runSessionStartUndelivered(fixture);

    assert.equal(code, 0, "a closed host pipe must not crash the hook");
    assert.match(
      await auditText(fixture),
      /hook_undelivered/,
      "an undelivered envelope must be audited even with zero deferred commits",
    );
  },
);

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

test("claude-code's SessionStart constant mirrors hooks.json too", async () => {
  // hook.ts names its constant differently from the factory's config field, so
  // the loop above cannot cover it — and it is the entrypoint whose own comment
  // demands the two be edited together.
  const source = await readFile(path.join(PLUGIN_ROOT, "src", "hook.ts"), "utf8");
  const match = source.match(/CLAUDECODE_SESSION_START_TIMEOUT_MS\s*=\s*([\d_]+)/);
  assert.ok(match, "hook.ts must declare CLAUDECODE_SESSION_START_TIMEOUT_MS");
  const declaredMs = Number(match[1].replace(/_/g, ""));
  const manifestMs =
    (await manifestSessionStartTimeout("hooks.json", (m) => m.hooks.SessionStart[0].hooks[0].timeout)) *
    1000;
  assert.equal(
    declaredMs,
    manifestMs,
    `CLAUDECODE_SESSION_START_TIMEOUT_MS (${declaredMs}) must mirror hooks.json (${manifestMs})`,
  );
  // 600s is Claude Code's documented `command`-hook default, which SessionStart
  // does not override. Declaring anything smaller would TIGHTEN the real boot
  // deadline rather than document it — a regression disguised as a mirror.
  assert.equal(
    declaredMs,
    600_000,
    "claude-code's SessionStart deadline must stay at the platform default, not be tightened",
  );
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
