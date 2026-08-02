// Behavioral pin for PR #260 round 9 — P6 storm path must not re-silence
// real bridge failures when DIAGNOSTIC_MAX_IN_FLIGHT is full.
//
// Model of the coalesce+flush control flow in kilo/minni-plugin.js, driven
// with real child processes so "after children exit, audit accounts for
// suppressed events" is an observable fact rather than a source-grep hope.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const DIAGNOSTIC_MAX_IN_FLIGHT = 4;
const DIAGNOSTIC_TIMEOUT_MS = 2_000;

/**
 * Faithful extract of the round-9 suppress coalesce + settle flush.
 * Uses the same return contract as reportBridgeFailure (spawned vs not).
 */
function createStormReporter({ hookScript, logPath }) {
  let diagnosticsInFlight = 0;
  let diagnosticsSuppressed = 0;
  const pendingSuppressedFailures = new Map();
  const delivered = [];

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
      () => restoreSuppressedFailures(flushed),
      {
        coalesced_count: totalCount,
        suppressed_since_last_report: suppressedAtFlush,
      },
    );
    if (accepted) {
      diagnosticsSuppressed = 0;
      return true;
    }
    restoreSuppressedFailures(flushed);
    return false;
  }

  function spawnBridgeDiagnostic(event, detail, onUndelivered, extras = {}) {
    if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) return false;
    try {
      const child = spawn("node", [hookScript, "BridgeFailure"], {
        env: { ...process.env, MINNI_STORM_LOG: logPath },
        stdio: ["pipe", "ignore", "ignore"],
        detached: false,
      });
      diagnosticsInFlight += 1;
      const kill = setTimeout(() => child.kill("SIGKILL"), DIAGNOSTIC_TIMEOUT_MS);
      let settled = false;
      const settle = () => {
        if (settled) return;
        settled = true;
        clearTimeout(kill);
        diagnosticsInFlight -= 1;
        flushPendingSuppressedFailures();
      };
      let undeliveredReported = false;
      const undelivered = () => {
        if (undeliveredReported) return;
        undeliveredReported = true;
        if (onUndelivered) onUndelivered();
      };
      child.once("close", (code) => {
        settle();
        if (code !== 0) undelivered();
      });
      child.once("error", () => {
        settle();
        undelivered();
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
      return false;
    }
  }

  function reportBridgeFailure(event, error) {
    const detail = error instanceof Error ? error.message : String(error);
    if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) {
      diagnosticsSuppressed += 1;
      queueSuppressedFailure(event, detail);
      return false;
    }
    return spawnBridgeDiagnostic(event, detail);
  }

  return {
    reportBridgeFailure,
    getState: () => ({
      diagnosticsInFlight,
      diagnosticsSuppressed,
      pending: new Map(pendingSuppressedFailures),
      delivered: [...delivered],
    }),
    waitForIdle: async (timeoutMs = 5_000) => {
      const start = Date.now();
      while (Date.now() - start < timeoutMs) {
        const s = {
          diagnosticsInFlight,
          pending: pendingSuppressedFailures.size,
        };
        if (s.diagnosticsInFlight === 0 && s.pending === 0) return;
        await new Promise((r) => setTimeout(r, 25));
      }
      throw new Error(
        `storm reporter did not drain: inFlight=${diagnosticsInFlight} ` +
          `pending=${pendingSuppressedFailures.size} suppressed=${diagnosticsSuppressed}`,
      );
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

  try {
    const reporter = createStormReporter({ hookScript, logPath });

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
    await rm(root, { recursive: true, force: true });
  }
});

test("P6 source: minni-plugin.js wires settle → flushPendingSuppressedFailures", async () => {
  const pluginPath = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
    "kilo",
    "minni-plugin.js",
  );
  const source = await readFile(pluginPath, "utf8");
  assert.match(source, /pendingSuppressedFailures/);
  assert.match(source, /function flushPendingSuppressedFailures/);
  // The settle body must call the flusher — not only define it.
  const settle = source.indexOf("const settle = () =>");
  assert.ok(settle !== -1);
  assert.match(
    source.slice(settle, settle + 350),
    /flushPendingSuppressedFailures\(\)/,
  );
});
