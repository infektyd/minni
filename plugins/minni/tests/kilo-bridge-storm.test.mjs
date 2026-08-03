// Behavioral pin for PR #260 round 9 — P6 storm path must not re-silence
// real bridge failures when DIAGNOSTIC_MAX_IN_FLIGHT is full.
//
// Model of the coalesce+flush control flow in kilo/minni-plugin.js, driven
// with real child processes so "after children exit, audit accounts for
// suppressed events" is an observable fact rather than a source-grep hope.
//
// Round 17: also pins undelivered backoff (no tight-loop spawn storm) and
// session-evict console visibility when the diagnostic budget is full.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const DIAGNOSTIC_MAX_IN_FLIGHT = 4;
const DIAGNOSTIC_TIMEOUT_MS = 2_000;
// Production uses 1s base / 60s max; tests use a short backoff so re-spawn
// still happens within waitForIdle without allowing a tight loop.
const TEST_UNDELIVERED_BACKOFF_MS = 80;
const TEST_UNDELIVERED_BACKOFF_MAX_MS = 400;
// Production uses 60s; tests use a short coalesce window so the deferred
// flush pin finishes without a minute-long wait.
const TEST_EVICTION_DIAGNOSTIC_INTERVAL_MS = 80;

/**
 * Faithful extract of the round-9+17 suppress coalesce + settle flush.
 * Uses the same return contract as reportBridgeFailure (spawned vs not).
 */
function createStormReporter({
  hookScript,
  logPath,
  undeliveredBackoffMs = TEST_UNDELIVERED_BACKOFF_MS,
  undeliveredBackoffMaxMs = TEST_UNDELIVERED_BACKOFF_MAX_MS,
  evictionIntervalMs = TEST_EVICTION_DIAGNOSTIC_INTERVAL_MS,
  nowFn = () => Date.now(),
}) {
  let diagnosticsInFlight = 0;
  let diagnosticsSuppressed = 0;
  const pendingSuppressedFailures = new Map();
  const evictionsSinceReport = new Map();
  const delivered = [];
  const consoleWarns = [];
  let diagnosticUndeliveredStreak = 0;
  let diagnosticFlushNotBefore = 0;
  let diagnosticFlushTimer = null;
  let sessionEvictFlushTimer = null;
  let lastEvictionReportAt = 0;
  let spawnCount = 0;

  function queueSuppressedFailure(event, detail) {
    if (event === "session-evict") return;
    const entry = pendingSuppressedFailures.get(event) || { count: 0, lastError: "" };
    entry.count += 1;
    entry.lastError = detail;
    pendingSuppressedFailures.set(event, entry);
  }

  function restoreSuppressedFailures(flushed) {
    for (const [name, info] of flushed) {
      const cur = pendingSuppressedFailures.get(name) || { count: 0, lastError: "" };
      cur.count += info.count;
      cur.lastError = info.lastError || cur.lastError;
      pendingSuppressedFailures.set(name, cur);
    }
  }

  function noteDiagnosticUndelivered() {
    diagnosticUndeliveredStreak += 1;
    const exp = Math.min(diagnosticUndeliveredStreak - 1, 6);
    const backoff = Math.min(
      undeliveredBackoffMaxMs,
      undeliveredBackoffMs * (2 ** exp),
    );
    diagnosticFlushNotBefore = Math.max(
      diagnosticFlushNotBefore,
      nowFn() + backoff,
    );
  }

  function noteDiagnosticDelivered() {
    diagnosticUndeliveredStreak = 0;
    diagnosticFlushNotBefore = 0;
  }

  function scheduleDiagnosticFlush() {
    const delay = Math.max(0, diagnosticFlushNotBefore - nowFn());
    if (delay === 0) {
      flushDiagnosticQueues();
      return;
    }
    if (diagnosticFlushTimer != null) return;
    diagnosticFlushTimer = setTimeout(() => {
      diagnosticFlushTimer = null;
      flushDiagnosticQueues();
    }, delay);
    if (typeof diagnosticFlushTimer.unref === "function") {
      diagnosticFlushTimer.unref();
    }
  }

  function flushDiagnosticQueues() {
    if (nowFn() < diagnosticFlushNotBefore) {
      scheduleDiagnosticFlush();
      return false;
    }
    let progressed = false;
    if (evictionsSinceReport.size > 0) {
      progressed = flushPendingSessionEvictions() || progressed;
    }
    if (
      pendingSuppressedFailures.size > 0
      && diagnosticsInFlight < DIAGNOSTIC_MAX_IN_FLIGHT
    ) {
      progressed = flushPendingSuppressedFailures() || progressed;
    }
    return progressed;
  }

  function clearSessionEvictFlushTimer() {
    if (sessionEvictFlushTimer == null) return;
    clearTimeout(sessionEvictFlushTimer);
    sessionEvictFlushTimer = null;
  }

  function scheduleSessionEvictFlush() {
    if (evictionsSinceReport.size === 0) return;
    if (sessionEvictFlushTimer != null) return;
    // Round 22: max(coalesce, undelivered backoff) — avoid delay=0 recursion
    // when flushPendingSessionEvictions is gated on diagnosticFlushNotBefore.
    const delay = Math.max(
      0,
      lastEvictionReportAt + evictionIntervalMs - nowFn(),
      diagnosticFlushNotBefore - nowFn(),
    );
    if (delay === 0) {
      flushPendingSessionEvictions();
      return;
    }
    sessionEvictFlushTimer = setTimeout(() => {
      sessionEvictFlushTimer = null;
      if (evictionsSinceReport.size === 0) return;
      flushPendingSessionEvictions();
    }, delay);
    if (typeof sessionEvictFlushTimer.unref === "function") {
      sessionEvictFlushTimer.unref();
    }
  }

  function flushPendingSessionEvictions() {
    if (evictionsSinceReport.size === 0) return false;
    // Round 22: honor undelivered backoff (mirror production plugin).
    if (nowFn() < diagnosticFlushNotBefore) {
      scheduleSessionEvictFlush();
      scheduleDiagnosticFlush();
      return false;
    }
    if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) {
      const detail = [...evictionsSinceReport.entries()]
        .map(([name, info]) => `${info.count} ${name} entr(y|ies) (bound ${info.max})`)
        .join("; ");
      const msg =
        `[minni] session-evict diagnostic suppressed (budget full); ` +
        `carrying forward: ${detail}`;
      consoleWarns.push(msg);
      return false;
    }
    const flushed = [...evictionsSinceReport.entries()];
    const detail = flushed
      .map(([name, info]) => `${info.count} ${name} entr(y|ies) (bound ${info.max})`)
      .join("; ");
    const accepted = reportBridgeFailure(
      "session-evict",
      new Error(`evicted for old sessions: ${detail}`),
      () => {
        for (const [name, info] of flushed) {
          const cur = evictionsSinceReport.get(name) || { count: 0, max: info.max };
          cur.count += info.count;
          cur.max = info.max;
          evictionsSinceReport.set(name, cur);
        }
        // Round 22: keep lastEvictionReportAt; schedule deferred retry.
        scheduleSessionEvictFlush();
      },
    );
    if (accepted) {
      lastEvictionReportAt = nowFn();
      evictionsSinceReport.clear();
      clearSessionEvictFlushTimer();
      return true;
    }
    consoleWarns.push(
      `[minni] session-evict diagnostic suppressed (budget full); ` +
        `carrying forward: ${detail}`,
    );
    return false;
  }

  function flushPendingSuppressedFailures() {
    if (pendingSuppressedFailures.size === 0) return false;
    if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) return false;
    const flushed = [...pendingSuppressedFailures.entries()];
    const totalCount = flushed.reduce((sum, [, info]) => sum + info.count, 0);
    const detail = flushed
      .map(([name, info]) => `${info.count}x ${name}: ${info.lastError}`)
      .join("; ");
    const suppressedAtFlush = diagnosticsSuppressed;
    const failedEvent = flushed.length === 1 ? flushed[0][0] : "bridge-storm";
    pendingSuppressedFailures.clear();
    const accepted = spawnBridgeDiagnostic(
      failedEvent,
      `coalesced bridge failures: ${detail}`,
      () => {
        restoreSuppressedFailures(flushed);
        diagnosticsSuppressed = Math.max(diagnosticsSuppressed, suppressedAtFlush);
      },
      {
        coalesced_count: totalCount,
        suppressed_since_last_report: suppressedAtFlush,
        onDelivered: () => {
          diagnosticsSuppressed = Math.max(
            0,
            diagnosticsSuppressed - suppressedAtFlush,
          );
        },
      },
    );
    if (accepted) {
      return true;
    }
    restoreSuppressedFailures(flushed);
    return false;
  }

  function spawnBridgeDiagnostic(event, detail, onUndelivered, extras = {}) {
    if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) return false;
    // Round 23: claim + catch release (mirrors production minni-plugin.js).
    let claimed = false;
    let settled = false;
    let undeliveredReported = false;
    let kill = null;
    let child = null;
    const settle = () => {
      if (settled) return;
      settled = true;
      if (kill != null) clearTimeout(kill);
      if (claimed) {
        claimed = false;
        diagnosticsInFlight -= 1;
      }
      if (undeliveredReported) {
        noteDiagnosticUndelivered();
        scheduleDiagnosticFlush();
      } else {
        noteDiagnosticDelivered();
        flushDiagnosticQueues();
      }
    };
    try {
      child = spawn("node", [hookScript, "BridgeFailure"], {
        env: { ...process.env, MINNI_STORM_LOG: logPath },
        stdio: ["pipe", "ignore", "ignore"],
        detached: false,
      });
      spawnCount += 1;
      diagnosticsInFlight += 1;
      claimed = true;
      kill = setTimeout(() => child.kill("SIGKILL"), DIAGNOSTIC_TIMEOUT_MS);
      const undelivered = () => {
        if (undeliveredReported) return;
        undeliveredReported = true;
        if (onUndelivered) onUndelivered();
      };
      const markDelivered = () => {
        if (undeliveredReported) return;
        if (extras.onDelivered) extras.onDelivered();
      };
      // Round 14: undelivered before settle (same as production plugin).
      child.once("close", (code) => {
        if (code !== 0) undelivered();
        else markDelivered();
        settle();
      });
      child.once("error", () => {
        undelivered();
        settle();
      });
      child.unref();
      child.on("error", () => {});
      child.stdin.on("error", () => {});
      const payload = {
        hook_event_name: "BridgeFailure",
        bridge: "kilo",
        failed_event: event,
        error: detail.slice(0, 400),
      };
      if (extras.coalesced_count != null) payload.coalesced_count = extras.coalesced_count;
      if (extras.suppressed_since_last_report != null) {
        payload.suppressed_since_last_report = extras.suppressed_since_last_report;
      }
      delivered.push(payload);
      child.stdin.end(JSON.stringify(payload));
      return true;
    } catch {
      // Round 24: post-claim = spawned-but-undelivered; return true so the
      // caller does not double-queue (mirrors production minni-plugin.js).
      if (claimed) {
        if (!undeliveredReported) {
          undeliveredReported = true;
          if (onUndelivered) {
            try {
              onUndelivered();
            } catch {
              /* never throw from diagnostic path */
            }
          }
        }
        if (child) {
          try {
            child.kill("SIGKILL");
          } catch {
            /* already dead */
          }
        }
        settle();
        return true;
      }
      return false;
    }
  }

  function reportBridgeFailure(event, error, onUndelivered) {
    const detail = error instanceof Error ? error.message : String(error);
    if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) {
      // session-evict owns carry-forward; do not inflate diagnosticsSuppressed.
      if (event === "session-evict") return false;
      diagnosticsSuppressed += 1;
      queueSuppressedFailure(event, detail);
      return false;
    }
    // Round 16: undelivered without caller onUndelivered still queues (P6).
    const accepted = spawnBridgeDiagnostic(event, detail, () => {
      if (onUndelivered) onUndelivered();
      else {
        diagnosticsSuppressed += 1;
        queueSuppressedFailure(event, detail);
      }
    });
    // Round 13: sync spawn failure must queue, not console-only.
    if (!accepted) {
      if (event === "session-evict") return false;
      diagnosticsSuppressed += 1;
      queueSuppressedFailure(event, detail);
    }
    return accepted;
  }

  function reportSessionEvictions(label, max, evicted) {
    const entry = evictionsSinceReport.get(label) || { count: 0, max };
    entry.count += evicted;
    entry.max = max;
    evictionsSinceReport.set(label, entry);
    const now = nowFn();
    if (now - lastEvictionReportAt < evictionIntervalMs) {
      consoleWarns.push(
        `[minni] evicted ${evicted} ${label} entr(y|ies) (bound ${max}); ` +
          `${entry.count} pending for the next diagnostic`,
      );
      scheduleSessionEvictFlush();
      return;
    }
    flushPendingSessionEvictions();
  }

  return {
    reportBridgeFailure,
    reportSessionEvictions,
    getState: () => ({
      diagnosticsInFlight,
      diagnosticsSuppressed,
      pending: new Map(pendingSuppressedFailures),
      evictions: new Map(evictionsSinceReport),
      delivered: [...delivered],
      consoleWarns: [...consoleWarns],
      spawnCount,
      undeliveredStreak: diagnosticUndeliveredStreak,
      flushNotBefore: diagnosticFlushNotBefore,
      lastEvictionReportAt,
      sessionEvictTimer: sessionEvictFlushTimer != null,
    }),
    waitForIdle: async (timeoutMs = 5_000) => {
      const start = Date.now();
      while (Date.now() - start < timeoutMs) {
        const s = {
          diagnosticsInFlight,
          pending: pendingSuppressedFailures.size,
          timer: diagnosticFlushTimer != null,
          evictTimer: sessionEvictFlushTimer != null,
          evictions: evictionsSinceReport.size,
        };
        if (
          s.diagnosticsInFlight === 0
          && s.pending === 0
          && !s.timer
          && !s.evictTimer
          && s.evictions === 0
        ) {
          return;
        }
        await new Promise((r) => setTimeout(r, 25));
      }
      throw new Error(
        `storm reporter did not drain: inFlight=${diagnosticsInFlight} ` +
          `pending=${pendingSuppressedFailures.size} suppressed=${diagnosticsSuppressed} ` +
          `timer=${diagnosticFlushTimer != null} ` +
          `evictTimer=${sessionEvictFlushTimer != null} ` +
          `evictions=${evictionsSinceReport.size}`,
      );
    },
    dispose: () => {
      if (diagnosticFlushTimer != null) {
        clearTimeout(diagnosticFlushTimer);
        diagnosticFlushTimer = null;
      }
      clearSessionEvictFlushTimer();
    },
  };
}

test("P6 behavioral: suppressed hook failures flush to audit after storm drains", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-storm-"));
  const logPath = path.join(root, "bridge-audit.jsonl");
  const hookScript = path.join(root, "fake-hook.mjs");
  // Fake diagnostic: hang briefly so the in-flight cap binds, then write the
  // payload and exit 0 (delivered).
  await writeFile(
    hookScript,
    [
      "import { appendFileSync } from 'node:fs';",
      "const chunks = [];",
      "for await (const c of process.stdin) chunks.push(c);",
      "const body = Buffer.concat(chunks).toString('utf8');",
      "await new Promise((r) => setTimeout(r, 120));",
      "appendFileSync(process.env.MINNI_STORM_LOG, body + '\\n');",
      "process.exit(0);",
    ].join("\n"),
    "utf8",
  );

  const reporter = createStormReporter({ hookScript, logPath });
  try {
    // Fill the budget with 4 immediate failures (each spawns a hanging child).
    for (let i = 0; i < DIAGNOSTIC_MAX_IN_FLIGHT; i += 1) {
      const accepted = reporter.reportBridgeFailure("Stop", new Error(`timeout #${i}`));
      assert.equal(accepted, true, `slot ${i} should spawn`);
    }
    // N more under a full budget — must NOT be silent forever.
    const EXTRA = 5;
    for (let i = 0; i < EXTRA; i += 1) {
      const accepted = reporter.reportBridgeFailure("Stop", new Error(`storm #${i}`));
      assert.equal(accepted, false, "budget full must suppress spawn");
    }
    const mid = reporter.getState();
    assert.equal(mid.diagnosticsInFlight, DIAGNOSTIC_MAX_IN_FLIGHT);
    assert.ok(mid.diagnosticsSuppressed >= EXTRA);
    assert.equal(mid.pending.get("Stop")?.count, EXTRA, "suppressed Stop failures must be carried");

    await reporter.waitForIdle();

    const final = reporter.getState();
    assert.equal(final.pending.size, 0, "pending suppress queue must drain");
    assert.equal(final.diagnosticsInFlight, 0);
    // At least one delivered payload must account for the coalesced storm.
    const coalesced = final.delivered.filter(
      (p) => p.coalesced_count != null || String(p.error).includes("coalesced"),
    );
    assert.ok(
      coalesced.length >= 1,
      "after children settle, a coalesced diagnostic must have been spawned",
    );
    const totalCoalesced = coalesced.reduce(
      (sum, p) => sum + (Number(p.coalesced_count) || 0),
      0,
    );
    assert.ok(
      totalCoalesced >= EXTRA,
      `coalesced_count must cover the ${EXTRA} suppressed failures, got ${totalCoalesced}`,
    );
    assert.ok(
      coalesced.some((p) => Number(p.suppressed_since_last_report) >= EXTRA),
      "payload must name suppressed_since_last_report so the audit row is not silent",
    );

    // Durable log from the fake hook must also show the flush (not only the
    // first four individual Stop failures).
    const log = await readFile(logPath, "utf8");
    assert.match(log, /coalesced bridge failures|suppressed_since_last_report/);
    assert.match(log, /Stop/);
  } finally {
    reporter.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("P6 source: minni-plugin.js wires settle → flush with undelivered backoff", async () => {
  const pluginPath = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
    "kilo",
    "minni-plugin.js",
  );
  const source = await readFile(pluginPath, "utf8");
  assert.match(source, /pendingSuppressedFailures/);
  assert.match(source, /function flushPendingSuppressedFailures/);
  assert.match(source, /function scheduleDiagnosticFlush/);
  assert.match(source, /function flushDiagnosticQueues/);
  assert.match(source, /function noteDiagnosticUndelivered/);
  assert.match(source, /DIAGNOSTIC_UNDELIVERED_BACKOFF_MS/);
  // The settle body must branch undelivered → schedule, delivered → drain.
  const settle = source.indexOf("const settle = () =>");
  assert.ok(settle !== -1);
  const settleWindow = source.slice(settle, settle + 900);
  assert.match(settleWindow, /noteDiagnosticUndelivered/);
  assert.match(settleWindow, /scheduleDiagnosticFlush\(\)/);
  assert.match(settleWindow, /flushDiagnosticQueues\(\)/);
  // Round 14: undelivered must run before settle on close/error paths.
  const closeHandler = source.indexOf('child.once("close"');
  assert.ok(closeHandler !== -1);
  const closeSlice = source.slice(closeHandler, closeHandler + 220);
  const undeliveredAt = closeSlice.indexOf("undelivered()");
  const settleAt = closeSlice.indexOf("settle()");
  assert.ok(undeliveredAt !== -1 && settleAt !== -1 && undeliveredAt < settleAt,
    "close handler must call undelivered() before settle()");
  // diagnosticsSuppressed adjusted only via onDelivered (subtract snapshot).
  assert.match(source, /onDelivered:\s*\(\)\s*=>\s*\{/s);
  assert.match(source, /diagnosticsSuppressed\s*-\s*suppressedAtFlush/);
  assert.doesNotMatch(
    source,
    /if \(accepted\) \{\s*diagnosticsSuppressed\s*=\s*0/,
  );
  // Round 17: budget-full early return on session-evict must console.warn.
  // Round 22: window widened — undelivered-backoff gate precedes budget-full.
  const flushEvict = source.indexOf("function flushPendingSessionEvictions");
  assert.ok(flushEvict !== -1);
  const flushEvictWindow = source.slice(flushEvict, flushEvict + 2200);
  assert.match(
    flushEvictWindow,
    /diagnosticFlushNotBefore/,
    "session-evict flush must honor undelivered backoff",
  );
  assert.match(
    flushEvictWindow,
    /diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT[\s\S]*console\.warn/,
    "budget-full session-evict early return must console.warn (not silent)",
  );
  // Fair drain: session-evict before suppress in flushDiagnosticQueues.
  const drain = source.indexOf("function flushDiagnosticQueues");
  const drainWindow = source.slice(drain, drain + 700);
  const evictCall = drainWindow.indexOf("flushPendingSessionEvictions");
  const suppressCall = drainWindow.indexOf("flushPendingSuppressedFailures");
  assert.ok(
    evictCall !== -1 && suppressCall !== -1 && evictCall < suppressCall,
    "flushDiagnosticQueues must try session-evict before suppress storm",
  );
});

test("P6 behavioral: undelivered restore re-spawns on free slot (ordering pin)", async () => {
  // Grok R14 High: if settle() runs before undelivered(), free-slot flush
  // sees empty maps and never re-spawns — restored counts sit console-only.
  // Scenario: storm fills budget → suppress coalesces → free-slot flush spawns
  // a coalesced audit that dies once → restore+re-flush must deliver without
  // further reportBridgeFailure calls.
  const root = await mkdtemp(path.join(tmpdir(), "minni-storm-undel-"));
  const logPath = path.join(root, "bridge-audit.jsonl");
  const hookScript = path.join(root, "fake-hook.mjs");
  const failCounter = path.join(root, "coalesced-fail-left");
  // Individual diagnostics deliver; the first coalesced audit exits 1 once.
  await writeFile(failCounter, "1", "utf8");
  await writeFile(
    hookScript,
    [
      "import { appendFileSync, readFileSync, writeFileSync, existsSync } from 'node:fs';",
      "const chunks = [];",
      "for await (const c of process.stdin) chunks.push(c);",
      "const body = Buffer.concat(chunks).toString('utf8');",
      "const isCoalesced = body.includes('coalesced') || body.includes('bridge-storm');",
      "const counter = process.env.MINNI_STORM_FAIL_COUNTER;",
      "if (isCoalesced && counter && existsSync(counter)) {",
      "  const left = Number(readFileSync(counter, 'utf8') || '0');",
      "  if (left > 0) {",
      "    writeFileSync(counter, String(left - 1));",
      "    process.exit(1);",
      "  }",
      "}",
      "await new Promise((r) => setTimeout(r, 40));",
      "appendFileSync(process.env.MINNI_STORM_LOG, body + '\\n');",
      "process.exit(0);",
    ].join("\n"),
    "utf8",
  );

  process.env.MINNI_STORM_FAIL_COUNTER = failCounter;
  const reporter = createStormReporter({ hookScript, logPath });
  try {
    for (let i = 0; i < DIAGNOSTIC_MAX_IN_FLIGHT; i += 1) {
      assert.equal(
        reporter.reportBridgeFailure("Stop", new Error(`hold #${i}`)),
        true,
      );
    }
    const EXTRA = 3;
    for (let i = 0; i < EXTRA; i += 1) {
      assert.equal(
        reporter.reportBridgeFailure("Stop", new Error(`storm #${i}`)),
        false,
      );
    }
    const mid = reporter.getState();
    assert.equal(mid.pending.get("Stop")?.count, EXTRA);

    await reporter.waitForIdle(10_000);

    const final = reporter.getState();
    assert.equal(final.pending.size, 0, "pending must drain after re-spawn delivery");
    assert.equal(final.diagnosticsInFlight, 0);
    const coalesced = final.delivered.filter(
      (p) => p.coalesced_count != null || String(p.error).includes("coalesced"),
    );
    // First coalesced attempt undelivered + second attempt spawned = ≥2 coalesced payloads.
    assert.ok(
      coalesced.length >= 2,
      `expected re-spawn after undelivered coalesced audit, got ${coalesced.length}: ` +
        JSON.stringify(coalesced),
    );
    const log = await readFile(logPath, "utf8");
    assert.match(log, /coalesced bridge failures|suppressed_since_last_report/);
    // Med: zero diagnosticsSuppressed only after confirmed delivery (exit 0).
    assert.equal(final.diagnosticsSuppressed, 0);
  } finally {
    delete process.env.MINNI_STORM_FAIL_COUNTER;
    reporter.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("P6 behavioral: ordinary hook undelivered queues suppress map and re-spawns", async () => {
  // Round 16 High: runHookFailOpen calls reportBridgeFailure without
  // onUndelivered. Spawn accepted + child exit ≠ 0 must still land in the
  // suppress map so a free slot re-spawns the audit (not console-only).
  const root = await mkdtemp(path.join(tmpdir(), "minni-storm-hook-undel-"));
  const logPath = path.join(root, "bridge-audit.jsonl");
  const hookScript = path.join(root, "fake-hook.mjs");
  const failCounter = path.join(root, "fail-left");
  // First diagnostic child dies; subsequent children deliver.
  await writeFile(failCounter, "1", "utf8");
  await writeFile(
    hookScript,
    [
      "import { appendFileSync, readFileSync, writeFileSync, existsSync } from 'node:fs';",
      "const chunks = [];",
      "for await (const c of process.stdin) chunks.push(c);",
      "const body = Buffer.concat(chunks).toString('utf8');",
      "const counter = process.env.MINNI_STORM_FAIL_COUNTER;",
      "if (counter && existsSync(counter)) {",
      "  const left = Number(readFileSync(counter, 'utf8') || '0');",
      "  if (left > 0) {",
      "    writeFileSync(counter, String(left - 1));",
      "    process.exit(1);",
      "  }",
      "}",
      "await new Promise((r) => setTimeout(r, 30));",
      "appendFileSync(process.env.MINNI_STORM_LOG, body + '\\n');",
      "process.exit(0);",
    ].join("\n"),
    "utf8",
  );

  process.env.MINNI_STORM_FAIL_COUNTER = failCounter;
  const reporter = createStormReporter({ hookScript, logPath });
  try {
    // Single ordinary failure (no onUndelivered) — spawn accepted, then dies.
    assert.equal(
      reporter.reportBridgeFailure("Stop", new Error("hook timed out")),
      true,
      "first failure must take a budget slot",
    );

    // Wait until the undelivered path has queued (or drain finishes).
    const start = Date.now();
    let mid;
    while (Date.now() - start < 5_000) {
      mid = reporter.getState();
      if (mid.pending.has("Stop") || mid.pending.size > 0 || mid.delivered.length > 0) {
        break;
      }
      await new Promise((r) => setTimeout(r, 20));
    }
    // After exit ≠ 0 without onUndelivered, suppress map must carry the event
    // (or it already re-spawned and delivered — either proves the queue path).
    mid = reporter.getState();
    const queuedOrDelivered =
      (mid.pending.get("Stop")?.count ?? 0) >= 1 ||
      mid.delivered.some((p) => p.failed_event === "Stop" || p.failed_event === "bridge-storm");
    assert.ok(
      queuedOrDelivered || mid.diagnosticsSuppressed >= 1,
      `undelivered ordinary hook must queue suppress map, got state=${JSON.stringify({
        pending: Object.fromEntries(mid.pending),
        suppressed: mid.diagnosticsSuppressed,
        delivered: mid.delivered.length,
        inFlight: mid.diagnosticsInFlight,
      })}`,
    );

    await reporter.waitForIdle(10_000);

    const final = reporter.getState();
    assert.equal(final.pending.size, 0, "suppress queue must drain after re-spawn");
    assert.equal(final.diagnosticsInFlight, 0);
    assert.ok(
      final.delivered.length >= 1,
      `free-slot re-spawn must deliver at least one audit, got ${final.delivered.length}`,
    );
    const log = await readFile(logPath, "utf8");
    assert.match(log, /Stop|hook timed out|coalesced bridge failures/);
  } finally {
    delete process.env.MINNI_STORM_FAIL_COUNTER;
    reporter.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("P6 behavioral: permanent undelivered does not tight-loop spawn storm", async () => {
  // Round 17 High: child always exit 1 → undelivered → re-queue → settle flush
  // used to re-spawn immediately forever. DIAGNOSTIC_MAX_IN_FLIGHT only bounds
  // concurrency, not attempt rate. Backoff must cap spawns over a window.
  const root = await mkdtemp(path.join(tmpdir(), "minni-storm-perm-"));
  const logPath = path.join(root, "bridge-audit.jsonl");
  const hookScript = path.join(root, "fake-hook.mjs");
  await writeFile(
    hookScript,
    [
      "const chunks = [];",
      "for await (const c of process.stdin) chunks.push(c);",
      "process.exit(1);",
    ].join("\n"),
    "utf8",
  );

  // 200ms base / 800ms max — still exponential, never immediate.
  const reporter = createStormReporter({
    hookScript,
    logPath,
    undeliveredBackoffMs: 200,
    undeliveredBackoffMaxMs: 800,
  });
  try {
    assert.equal(
      reporter.reportBridgeFailure("Stop", new Error("permanently broken hook")),
      true,
    );

    // Observe for ~1.2s of wall clock. Without backoff this is dozens of
    // spawns (tight loop); with backoff it is a small handful.
    await new Promise((r) => setTimeout(r, 1_200));
    const state = reporter.getState();
    // First spawn + maybe 2–3 backoff retries in 1.2s with 200/400/800ms.
    assert.ok(
      state.spawnCount <= 6,
      `permanent undelivered must not tight-loop: spawnCount=${state.spawnCount}`,
    );
    assert.ok(
      state.spawnCount >= 1,
      "at least the original spawn must have happened",
    );
    // Counts must still be carried — not discarded to silence.
    assert.ok(
      (state.pending.get("Stop")?.count ?? 0) >= 1
        || state.diagnosticsSuppressed >= 1
        || state.diagnosticsInFlight >= 1,
      "permanent undelivered must keep loss on a P6 surface (pending or in-flight)",
    );
    assert.ok(
      state.undeliveredStreak >= 1 || state.flushNotBefore > 0 || state.spawnCount === 1,
      "backoff gate must engage after undelivered",
    );
  } finally {
    reporter.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("P6 behavioral: session-evict warns when diagnostic budget is full", async () => {
  // Round 17 High: under a sustained hook-failure storm, flushPendingSessionEvictions
  // early-returned with no console.warn while lastEvictionReportAt stayed 0 —
  // P5 eviction loss was fully silent in process memory.
  const root = await mkdtemp(path.join(tmpdir(), "minni-storm-evict-"));
  const logPath = path.join(root, "bridge-audit.jsonl");
  const hookScript = path.join(root, "fake-hook.mjs");
  // Hang so the budget stays full for the eviction check.
  await writeFile(
    hookScript,
    [
      "const chunks = [];",
      "for await (const c of process.stdin) chunks.push(c);",
      "await new Promise((r) => setTimeout(r, 800));",
      "process.exit(0);",
    ].join("\n"),
    "utf8",
  );

  const reporter = createStormReporter({ hookScript, logPath });
  try {
    for (let i = 0; i < DIAGNOSTIC_MAX_IN_FLIGHT; i += 1) {
      assert.equal(
        reporter.reportBridgeFailure("Stop", new Error(`hold #${i}`)),
        true,
      );
    }
    assert.equal(reporter.getState().diagnosticsInFlight, DIAGNOSTIC_MAX_IN_FLIGHT);

    reporter.reportSessionEvictions("pending", 64, 3);
    const mid = reporter.getState();
    assert.equal(mid.evictions.get("pending")?.count, 3, "counts must be retained");
    assert.ok(
      mid.consoleWarns.some((w) => w.includes("session-evict") && w.includes("budget full")),
      `budget-full eviction must console.warn, got: ${JSON.stringify(mid.consoleWarns)}`,
    );
    // No session-evict spawn while budget is full.
    assert.ok(
      !mid.delivered.some((p) => p.failed_event === "session-evict"),
      "must not steal a budget slot while full",
    );
    // Round 21 Low: session-evict budget-full must not inflate diagnosticsSuppressed.
    assert.equal(
      mid.diagnosticsSuppressed,
      0,
      "session-evict must not inflate diagnosticsSuppressed (owns its own carry-forward)",
    );
  } finally {
    reporter.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("P5 behavioral: within-interval session-evict flushes after window opens (no further churn)", async () => {
  // Round 21 Medium: after a successful diagnostic, a second eviction inside
  // EVICTION_DIAGNOSTIC_INTERVAL_MS used to console.warn only — and if no
  // later churn arrived, those counts never reached the audit channel.
  // The deferred unref timer must fire flushPendingSessionEvictions when the
  // window opens, even with zero further traffic.
  const root = await mkdtemp(path.join(tmpdir(), "minni-storm-evict-timer-"));
  const logPath = path.join(root, "bridge-audit.jsonl");
  const hookScript = path.join(root, "fake-hook.mjs");
  await writeFile(
    hookScript,
    [
      "import { appendFileSync } from 'node:fs';",
      "const chunks = [];",
      "for await (const c of process.stdin) chunks.push(c);",
      "const body = Buffer.concat(chunks).toString('utf8');",
      "appendFileSync(process.env.MINNI_STORM_LOG, body + '\\n');",
      "process.exit(0);",
    ].join("\n"),
    "utf8",
  );

  // Long enough that settle (~tens of ms) stays inside the window; short
  // enough the deferred flush pin finishes quickly.
  const INTERVAL = 400;
  const reporter = createStormReporter({
    hookScript,
    logPath,
    evictionIntervalMs: INTERVAL,
  });
  try {
    // Wave 1: interval open (lastEvictionReportAt=0) → immediate spawn.
    reporter.reportSessionEvictions("pending", 64, 2);
    let mid = reporter.getState();
    assert.ok(
      mid.delivered.some((p) => p.failed_event === "session-evict"),
      "first wave must spawn a session-evict diagnostic",
    );
    assert.equal(mid.evictions.size, 0, "accepted flush clears the map");
    assert.ok(mid.lastEvictionReportAt > 0, "coalesce clock advances on spawn");

    // Let the first diagnostic settle so free-slot drain does not race the
    // second wave (and so we prove the TIMER path, not free-slot drain).
    // Must finish well inside INTERVAL.
    const settleDeadline = Date.now() + INTERVAL - 80;
    while (Date.now() < settleDeadline) {
      mid = reporter.getState();
      if (mid.diagnosticsInFlight === 0) break;
      await new Promise((r) => setTimeout(r, 15));
    }
    mid = reporter.getState();
    assert.equal(mid.diagnosticsInFlight, 0, "first diagnostic must settle inside window");
    const deliveredAfterFirst = mid.delivered.length;
    const elapsed = Date.now() - mid.lastEvictionReportAt;
    assert.ok(
      elapsed < INTERVAL,
      `wave 2 setup must stay inside coalesce window (elapsed=${elapsed}ms, interval=${INTERVAL}ms)`,
    );

    // Wave 2: still inside the coalesce window → console only + arm timer.
    reporter.reportSessionEvictions("pending", 64, 5);
    mid = reporter.getState();
    assert.equal(mid.evictions.get("pending")?.count, 5, "within-interval counts retained");
    assert.equal(
      mid.delivered.length,
      deliveredAfterFirst,
      "within-interval must not spawn immediately",
    );
    assert.ok(mid.sessionEvictTimer, "must schedule deferred flush");
    assert.ok(
      mid.consoleWarns.some((w) => w.includes("pending for the next diagnostic")),
      "within-interval must still console.warn per wave",
    );

    // No further churn. Wait past the interval for the one-shot to fire.
    await reporter.waitForIdle(3_000);

    const final = reporter.getState();
    assert.equal(final.evictions.size, 0, "deferred flush must clear carried counts");
    const sessionEvicts = final.delivered.filter((p) => p.failed_event === "session-evict");
    assert.ok(
      sessionEvicts.length >= 2,
      `expected deferred session-evict audit after interval, got ${sessionEvicts.length}: ` +
        JSON.stringify(sessionEvicts),
    );
    assert.ok(
      sessionEvicts.some((p) => String(p.error).includes("5 pending")),
      "deferred audit must carry the within-interval count",
    );
    const log = await readFile(logPath, "utf8");
    assert.match(log, /session-evict/);
    assert.match(log, /5 pending/);
  } finally {
    reporter.dispose();
    await rm(root, { recursive: true, force: true });
  }
});
