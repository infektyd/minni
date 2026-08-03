// R8 observability slice — issue #228 (hook event parity, Kilo bridge honesty).
//
// P4: declared-vs-consumed event parity, and the clean-continue swallow — an
//     event that reaches the hook and is dropped exits with a SUCCESS status
//     and no marker, so a dropped event is indistinguishable from one that was
//     never sent.
// P5: the Kilo bridge drops `pending`/`booted` on a premature `session.deleted`
//     (a hazard already documented in the bridge and fixed only for lastPrompt).
// P6: Kilo bridge failures are console.warn only and never audited, so a
//     persistently failing bridge is indistinguishable from an idle one.
//
// Each test below fails against the pre-R8 behavior.

import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { CURSOR_EVENTS } from "../dist/cursor-adapter.js";
import { AGY_EVENTS } from "../dist/gemini-adapter.js";
import * as hookUtils from "../dist/hook-utils.js";
import { ensureVault, recordAudit } from "../dist/vault.js";

// Tolerant on purpose: pre-R8 dist has no BRIDGE_FAILURE_EVENT export, and a
// bare named import would fail the whole FILE to load, collapsing eleven
// independent verdicts into one. Each test must fail on its own merits.
const { VALID_EVENTS } = hookUtils;
const BRIDGE_FAILURE_EVENT = hookUtils.BRIDGE_FAILURE_EVENT ?? "__missing__";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.join(HERE, "..");
const REPO_ROOT = path.join(PLUGIN_ROOT, "..", "..");

async function withVault(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-r8-"));
  try {
    await ensureVault(root);
    return await fn(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

// ── P4: declared-vs-consumed parity is a CI gate, not a hope ────────────────

test("P4: every event a platform manifest declares is routed by the hook it invokes", async () => {
  // "What done looks like": a manifest/handler divergence must fail CI rather
  // than ship. Pre-R8 nothing cross-checked these two sets at all.
  const manifests = [
    ["hooks/hooks.json", "hook.js"],
    ["hooks/hooks-codex.json", null],
    ["hooks/hooks-grok.json", null],
    [".kilocode-plugin/hooks/hooks.json", null],
  ];

  // Round 5 (PR #260): the generic set used to be VALID_EVENTS ∪ {PreToolUse},
  // which only proves an event gets past the envelope GATE. VALID_EVENTS
  // includes PostCompact/CompactSummary, but the factory dispatch has no
  // PostCompact case — a manifest declaring it passed this gate while the
  // hook only audited a drop. "Routed" must mean an actual dispatch arm, so
  // the consumed set is read from createHookHandlers' switch, same as hook.ts.
  const generic = await routedEventsFor(
    path.join(PLUGIN_ROOT, "src", "hook-handlers.ts"),
  );
  const claudeRouted = await routedEventsFor(path.join(PLUGIN_ROOT, "src", "hook.ts"));
  assert.ok(
    VALID_EVENTS.includes("PostCompact") && !generic.has("PostCompact"),
    "precondition: PostCompact passes the envelope gate but has no factory " +
      "dispatch arm — the exact gap that made the old VALID_EVENTS-based " +
      "check overstate routing",
  );

  for (const [relative, entry] of manifests) {
    const raw = await readFile(path.join(PLUGIN_ROOT, relative), "utf8");
    const parsed = JSON.parse(raw);
    // Two manifest shapes in the tree: a `hooks` wrapper (Claude/Codex/Grok)
    // and a bare event map (Kilocode).
    const declared = Object.keys(parsed.hooks ?? parsed);
    assert.ok(declared.length > 0, `${relative} declares no events`);

    for (const event of declared) {
      const consumed = entry === "hook.js" ? claudeRouted : generic;
      assert.ok(
        consumed.has(event),
        `${relative} declares "${event}" but the hook it invokes does not route it — ` +
          "a declared-but-unhandled event exits clean and carries nothing",
      );
    }
  }

  // Round 3 (PR #260): cursor and gemini are real platform entry points with
  // their OWN declared→routed maps, not the generic envelope set — omitting
  // them meant a declared-but-unhandled event on either platform passed this
  // gate. Their adapters export the routed maps precisely so this check can
  // read them without importing an entry point (which executes on import).
  const platformManifests = [
    ["hooks/hooks-cursor.json", new Set(Object.keys(CURSOR_EVENTS))],
    ["hooks/hooks-gemini.json", new Set([...Object.keys(AGY_EVENTS), "PreToolUse"])],
  ];
  for (const [relative, routed] of platformManifests) {
    const raw = await readFile(path.join(PLUGIN_ROOT, relative), "utf8");
    const parsed = JSON.parse(raw);
    // Gemini nests its event map under a `minni` wrapper; cursor uses `hooks`.
    const declared = Object.keys(parsed.minni ?? parsed.hooks ?? parsed);
    assert.ok(declared.length > 0, `${relative} declares no events`);
    for (const event of declared) {
      assert.ok(
        routed.has(event),
        `${relative} declares "${event}" but the platform hook does not route it — ` +
          "a declared-but-unhandled event exits clean and carries nothing",
      );
    }
  }
});

test("P4: the bridge-failure diagnostic is deliberately NOT an envelope event", () => {
  // It carries no memory and reaches no handler. Pinning this keeps someone
  // from "fixing" the parity test by adding it to VALID_EVENTS, which would
  // route a diagnostic into the memory path.
  assert.notEqual(
    hookUtils.BRIDGE_FAILURE_EVENT,
    undefined,
    "the bridge-failure diagnostic channel does not exist",
  );
  assert.equal(VALID_EVENTS.includes(BRIDGE_FAILURE_EVENT), false);
});

test("P4: an unrouted event records a drop marker instead of exiting clean", async () => {
  // Pre-R8: `emit({continue:true}); return;` with no marker of any kind.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "src", "hook-handlers.ts"),
    "utf8",
  );
  const gate = source.indexOf("!VALID_EVENTS.includes(event as EnvelopeEvent)");
  assert.ok(gate !== -1, "the VALID_EVENTS gate moved; re-point this test");
  const window = source.slice(gate, gate + 800);
  assert.match(
    window,
    /recordUnroutedEvent/,
    "an event that reaches the hook and is not routed must be recorded, not swallowed",
  );
});

test("P4: a valid event with no dispatch case records a drop marker", async () => {
  // Pre-R8: `default: return render(noIntent)` — a clean, successful no-op for
  // an event that passed the VALID_EVENTS gate and then found no handler.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "src", "hook-handlers.ts"),
    "utf8",
  );
  const marker = source.indexOf("      default:\n");
  assert.ok(marker !== -1, "the dispatch default moved; re-point this test");
  const window = source.slice(marker, marker + 900);
  assert.match(window, /recordUnroutedEvent/);
});

// ── P5: a premature session.deleted must not drop queued context ────────────

test("P5: session.deleted no longer clears pending or booted", async () => {
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const branch = source.indexOf('event?.type === "session.deleted"');
  assert.ok(branch !== -1, "the session.deleted branch moved; re-point this test");
  const window = source.slice(branch, branch + 1200);

  // Pre-R8 these two lines were the whole branch. Kilo fires session.deleted
  // while the session is STILL LIVE, so clearing `pending` drops boot context
  // that experimental.chat.system.transform has not yet delivered.
  assert.doesNotMatch(
    window,
    /pending\.delete\(sessionID\)/,
    "a premature session.deleted must not drop this session's queued context",
  );
  assert.doesNotMatch(
    window,
    /booted\.delete\(sessionID\)/,
    "a premature session.deleted must not un-boot a live session",
  );
});

test("P5: pending and booted are bounded, so not honoring the delete cannot leak", async () => {
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  assert.match(source, /const PENDING_MAX = \d+/);
  assert.match(source, /const BOOTED_MAX = \d+/);
  assert.match(
    source,
    /function evictOldest\(/,
    "the maps need a ceiling now that session.deleted no longer clears them",
  );
});

test("P5: an eviction is reported, not silent — and coalesced, not budget-burning", async () => {
  // A bound that discards silently is the same defect in a smaller box.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const evict = source.indexOf("function evictOldest(");
  const window = source.slice(evict, evict + 600);
  assert.match(window, /reportSessionEvictions/);
  // Round 3 (PR #260): a diagnostic SPAWN per evicted key shared — and under
  // session churn exhausted — the DIAGNOSTIC_MAX_IN_FLIGHT budget real hook
  // failures need, silencing exactly the diagnostics P6 exists to keep alive.
  assert.doesNotMatch(
    window,
    /reportBridgeFailure\(/,
    "evictions must not spawn a diagnostic per evicted key",
  );
  // Round 10: flush lives in flushPendingSessionEvictions; reportSessionEvictions
  // accumulates + rate-limits, then delegates.
  const flush = source.indexOf("function flushPendingSessionEvictions(");
  assert.ok(flush !== -1, "the session-evict flusher is missing");
  const fWindow = source.slice(flush, flush + 2800);
  assert.match(fWindow, /reportBridgeFailure/, "coalesced evictions still reach the audit channel");
  assert.match(
    fWindow,
    /const accepted = reportBridgeFailure/,
    "the reporter must observe whether the diagnostic was accepted",
  );
  assert.match(
    fWindow,
    /if \(accepted\) \{/,
    "counts may only be cleared when the diagnostic actually spawned",
  );
  assert.match(
    fWindow,
    /cur\.count \+= info\.count/,
    "an undelivered audit must restore the flushed eviction counts",
  );
  // Round 22: undelivered must NOT rewind lastEvictionReportAt to 0 (that
  // bypassed undelivered backoff on the next eviction wave). Retry via
  // scheduleSessionEvictFlush + diagnosticFlushNotBefore instead.
  assert.doesNotMatch(
    fWindow,
    /lastEvictionReportAt = 0/,
    "undelivered must not rewind coalesce clock (bypasses undelivered backoff)",
  );
  assert.match(
    fWindow,
    /diagnosticFlushNotBefore/,
    "session-evict flush must honor undelivered backoff",
  );
  assert.match(
    fWindow,
    /scheduleSessionEvictFlush/,
    "undelivered session-evict must schedule a deferred retry",
  );
  const coalesce = source.indexOf("function reportSessionEvictions(");
  assert.ok(coalesce !== -1, "the coalescing reporter is missing");
  const cWindow = source.slice(coalesce, coalesce + 1200);
  assert.match(cWindow, /EVICTION_DIAGNOSTIC_INTERVAL_MS/);
  assert.match(cWindow, /flushPendingSessionEvictions/);
  // Round 21: within-interval branch must schedule a deferred flush so counts
  // are not console-only forever when no later churn reopens the window.
  assert.match(
    cWindow,
    /scheduleSessionEvictFlush/,
    "within-interval session-evict must arm a deferred flush timer",
  );
  assert.match(
    source,
    /function scheduleSessionEvictFlush/,
    "scheduleSessionEvictFlush must exist",
  );
  assert.match(
    source,
    /sessionEvictFlushTimer\.unref/,
    "deferred session-evict timer must unref so it does not pin the process",
  );
  // Round 4: per-label counts — a mixed pending+booted wave must not report
  // the whole count under the last wave's label and bound.
  assert.match(
    cWindow,
    /evictionsSinceReport\.get\(label\)/,
    "eviction counts must be tracked per label, not as one scalar",
  );
});

test("P6: a suppressed diagnostic reports itself as NOT delivered", async () => {
  // reportBridgeFailure's boolean is what keeps the eviction coalescer honest;
  // pin both verdict paths so a refactor cannot quietly make it void again.
  // Round 9: the spawn body lives in spawnBridgeDiagnostic; suppress stays
  // on reportBridgeFailure and returns false after queueing.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const report = source.indexOf("function reportBridgeFailure");
  // Round 16: reportBridgeFailure grew (undelivered queue for ordinary hooks).
  const reportWindow = source.slice(report, report + 1800);
  const suppress = reportWindow.indexOf("diagnosticsSuppressed += 1");
  assert.ok(suppress !== -1);
  assert.match(
    reportWindow.slice(suppress, suppress + 400),
    /return false;/,
    "the suppressed path must say the diagnostic was NOT delivered",
  );
  // Round 16: accepted-but-undelivered without onUndelivered must still queue
  // (runHookFailOpen path) — not console-only P6 for ordinary hook failures.
  assert.match(
    reportWindow,
    /if \(onUndelivered\) onUndelivered\(\);\s*else \{[\s\S]*queueSuppressedFailure/,
    "ordinary undelivered diagnostics must queue for free-slot re-spawn",
  );
  const spawn = source.indexOf("function spawnBridgeDiagnostic");
  assert.ok(spawn !== -1, "spawn path must live in spawnBridgeDiagnostic");
  // Round 14: function grew (onDelivered + undelivered-before-settle comments);
  // keep a window that still covers return true and the close/error handlers.
  const spawnWindow = source.slice(spawn, spawn + 4500);
  assert.match(spawnWindow, /return true;/, "the spawned path must say it was spawned");
  // Round 5: spawned is still not DELIVERED — a child that errors or exits
  // non-zero before writing the audit must tell the caller, so coalesced
  // counts can be restored instead of vanishing with only a console line.
  assert.match(spawnWindow, /onUndelivered/, "delivery failure must be observable");
  assert.match(
    spawnWindow,
    /if \(code !== 0\) undelivered\(\)/,
    "a non-zero exit is a failed audit write",
  );
  // Round 14: free-slot flush must see restored counts — undelivered before settle.
  const closeAt = spawnWindow.indexOf('child.once("close"');
  assert.ok(closeAt !== -1);
  const closeSlice = spawnWindow.slice(closeAt, closeAt + 220);
  assert.ok(
    closeSlice.indexOf("undelivered()") < closeSlice.indexOf("settle()"),
    "close handler must undelivered() before settle()",
  );
});

test("P5: queued context per session is bounded by volume, not only by session count", async () => {
  // Round 4: PENDING_MAX bounds sessions; one session with a delayed delivery
  // transform grew its context array without limit — and P5 correctly removed
  // the accidental reset on premature session.deleted, so nothing else
  // truncated it either.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  assert.match(source, /const PENDING_CONTEXTS_PER_SESSION_MAX = \d+/);
  const queue = source.indexOf("function queueContext(");
  const window = source.slice(queue, queue + 900);
  assert.match(window, /PENDING_CONTEXTS_PER_SESSION_MAX/);
  assert.match(
    window,
    /reportSessionEvictions/,
    "a dropped context chunk is lost memory injection and must be reported "
      + "through the same eviction path as the maps",
  );
});

test("P5: lastPrompt eviction is reported, not silent", async () => {
  // Round 15: lastPrompt was a silent while-delete bound; Stop then learned
  // with empty last_user_message and no P6 surface.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  assert.match(source, /const LAST_PROMPT_MAX = \d+/);
  const setAt = source.indexOf("lastPrompt.set(");
  assert.ok(setAt !== -1);
  const afterSet = source.slice(setAt, setAt + 350);
  assert.match(
    afterSet,
    /evictOldest\(\s*lastPrompt\s*,\s*LAST_PROMPT_MAX\s*,\s*["']last prompt["']\s*\)/,
    "lastPrompt overflow must use the same reported eviction path as pending/booted",
  );
  assert.doesNotMatch(
    afterSet,
    /while\s*\(\s*lastPrompt\.size\s*>\s*LAST_PROMPT_MAX/,
    "silent while-delete on lastPrompt reopens the P5 hole",
  );
});

// ── P6: bridge failures must reach the audit log ───────────────────────────

test("P6: the Kilo bridge reports failures to the audit channel, not just console", async () => {
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const failOpen = source.indexOf("async function runHookFailOpen");
  const window = source.slice(failOpen, failOpen + 400);
  assert.match(
    window,
    /reportBridgeFailure/,
    "a bridge failure that only reaches the console leaves no surface on which " +
      "a failing bridge differs from an idle one",
  );

  const report = source.indexOf("function reportBridgeFailure");
  assert.ok(report !== -1);
  assert.match(
    source.slice(report, report + 900),
    /BridgeFailure/,
    "the failure must be carried to the vault through the hook's audit channel",
  );
});

test("P6: a BridgeFailure event writes an audit entry naming the failed event", async () => {
  await withVault(async (root) => {
    // What the hook's BridgeFailure branch does, exercised directly against the
    // same audit primitive it calls. Pre-R8 nothing wrote this record at all.
    await recordAudit(root, {
      tool: "hook_kilocode_bridge_failure",
      summary: "kilo bridge: Stop failed: hook timed out",
      throttleKey: "hook_kilocode_bridge_failure__Stop",
    });
    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /bridge_failure/);
    assert.match(log, /Stop failed/);
  });
});

test("P6: bridge failures bucket per failed event so the second is not throttled away", async () => {
  await withVault(async (root) => {
    // The same reasoning as the _intent_dropped throttleKey: one bucket across
    // every failed event would hide all but the first and report the bridge as
    // healthier than it is.
    await recordAudit(root, {
      tool: "hook_kilocode_bridge_failure",
      summary: "kilo bridge: Stop failed: timeout",
      throttleKey: "hook_kilocode_bridge_failure__Stop",
    });
    await recordAudit(root, {
      tool: "hook_kilocode_bridge_failure",
      summary: "kilo bridge: SessionStart failed: timeout",
      throttleKey: "hook_kilocode_bridge_failure__SessionStart",
    });
    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /Stop failed/);
    assert.match(
      log,
      /SessionStart failed/,
      "a second failed event within the throttle window must still be recorded",
    );
  });
});

const execFileAsync = promisify(execFile);
const KILOCODE_HOOK = path.join(PLUGIN_ROOT, "dist", "kilocode-hook.js");

async function runBridgeFailureChild(vaultPath, extraEnv = {}) {
  const child = execFileAsync(process.execPath, [KILOCODE_HOOK, "BridgeFailure"], {
    env: {
      ...process.env,
      MINNI_KILOCODE_VAULT_PATH: vaultPath,
      // Keep the audit throttle timestamps inside the test sandbox.
      MINNI_HOME: path.join(vaultPath, ".minni-home"),
      ...extraEnv,
    },
  });
  child.child.stdin.end(
    JSON.stringify({
      hook_event_name: "BridgeFailure",
      bridge: "kilo",
      failed_event: "Stop",
      error: "hook timed out",
    }),
  );
  try {
    await child;
    return 0;
  } catch (error) {
    return error.code ?? 1;
  }
}

test("P6 round 7: an audit that cannot write exits non-zero", async () => {
  // The parent's whole delivery contract is the exit code: it restores its
  // coalesced eviction counts only on error/non-zero exit. A clean exit
  // after a swallowed recordAudit throw cleared counts that never landed
  // anywhere durable. Vault path UNDER a plain file = guaranteed write fail.
  const root = await mkdtemp(path.join(tmpdir(), "minni-r8-badvault-"));
  try {
    const file = path.join(root, "not-a-dir");
    await writeFile(file, "occupied");
    const code = await runBridgeFailureChild(path.join(file, "vault"));
    assert.notEqual(code, 0, "a failed audit write must not report delivered");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("P6 round 7: hooks disabled does not disable the bridge-failure surface", async () => {
  // The hooksEnabled early-return used to exit 0 BEFORE the BridgeFailure
  // branch — turning memory hooks off also silently turned off the one
  // surface that says the bridge is broken, while the parent read the clean
  // exit as delivered.
  await withVault(async (root) => {
    const code = await runBridgeFailureChild(root, { MINNI_KILOCODE_HOOKS: "off" });
    assert.equal(code, 0, "a delivered diagnostic exits clean");
    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(
      log,
      /bridge_failure/,
      "the diagnostic must land even with memory hooks disabled",
    );
  });
});

test("P6 round 8: a throttled non-write cannot report delivered", async () => {
  // Round 7 made the exit code the delivery signal; recordAudit's 5s throttle
  // returned success WITHOUT appending, so an immediate retry — exactly what
  // the eviction coalescer does after an undelivered wave — exited 0 while
  // nothing reached log.md, and the parent cleared its counts as delivered.
  // bridge_failure audits are now throttle-exempt like *_stop: every
  // diagnostic that spawned writes a row.
  await withVault(async (root) => {
    assert.equal(await runBridgeFailureChild(root), 0);
    assert.equal(await runBridgeFailureChild(root), 0);
    const log = await readFile(path.join(root, "log.md"), "utf8");
    const rows = log.match(/bridge_failure/g) || [];
    assert.ok(
      rows.length >= 2,
      `both diagnostics exited 0, so both must have written (got ${rows.length} row(s))`,
    );
  });
});

test("P6: the hook routes BridgeFailure before the VALID_EVENTS gate", async () => {
  const source = await readFile(
    path.join(PLUGIN_ROOT, "src", "hook-handlers.ts"),
    "utf8",
  );
  const bridge = source.indexOf("event === BRIDGE_FAILURE_EVENT");
  const gate = source.indexOf("!VALID_EVENTS.includes(event as EnvelopeEvent)");
  assert.ok(bridge !== -1, "BridgeFailure is not routed");
  assert.ok(
    bridge < gate,
    "BridgeFailure must be handled before the envelope-event gate, or the " +
      "diagnostic itself gets recorded as a dropped intent",
  );
});

// ── Review round 1 (PR #260): P4 must cover every entry point ─────────────

test("P4: every hook entry point records an unrouted event, not just the factory", async () => {
  // The first version of this fix only covered hook-handlers.ts. Claude Code
  // is the PRIMARY manifest and runs hook.ts, which had its own clean-continue
  // swallow in both the VALID_EVENTS gate and the dispatch default; Gemini and
  // Cursor had theirs too. A fix that misses the primary path is not a fix.
  for (const entry of [
    "hook.ts",
    "gemini-hook.ts",
    "cursor-hook.ts",
    "hook-handlers.ts",
  ]) {
    const source = await readFile(path.join(PLUGIN_ROOT, "src", entry), "utf8");
    assert.match(
      source,
      /recordUnroutedEvent\(/,
      `${entry} still exits clean and silent on an event it does not route`,
    );
  }
});

test("P4: hook.ts records the drop on BOTH of its swallow paths", async () => {
  const source = await readFile(path.join(PLUGIN_ROOT, "src", "hook.ts"), "utf8");
  const calls = [...source.matchAll(/recordUnroutedEvent\(/g)];
  assert.ok(
    calls.length >= 2,
    "the VALID_EVENTS gate and the dispatch default are two separate swallows",
  );
});

test("P4: Cursor stays silent when hooks are DISABLED, loud on an unknown event", async () => {
  // A deliberate opt-out is not a drop; conflating them would make the signal
  // noise and train people to ignore it.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "src", "cursor-hook.ts"),
    "utf8",
  );
  const branch = source.indexOf("if (!CONFIG.hooksEnabled || !event)");
  assert.ok(branch !== -1);
  const window = source.slice(branch, branch + 700);
  assert.match(window, /if \(CONFIG\.hooksEnabled\)/);
});

// ── Review round 1: P5 must bound at INSERT, not only on session.deleted ──

test("P5: pending and booted are bounded at insert, not only on session.deleted", async () => {
  // Bounding only inside the session.deleted branch left the maps unbounded
  // whenever that event is missing or delayed (version skew, bus drop) — the
  // exact leak class the lastPrompt fix already solved correctly.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );

  const queue = source.indexOf("function queueContext(");
  assert.ok(queue !== -1);
  assert.match(
    source.slice(queue, queue + 1400),
    /evictOldest\(pending/,
    "pending must be bounded where it GROWS",
  );

  const boot = source.indexOf("booted.add(input.sessionID)");
  assert.ok(boot !== -1);
  assert.match(source.slice(boot, boot + 200), /evictOldest\(booted/);
});

test("P5: booted is re-touched on chat.message so the bound is LRU not FIFO", async () => {
  // Round 19: insert-only made BOOTED_MAX FIFO. An active long-lived session
  // stayed first-in and was the first evicted when one-shot sessions filled
  // the bound — forcing a redundant SessionStart on the next message.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const chat = source.indexOf('"chat.message"');
  assert.ok(chat !== -1);
  // Window covers the boot branch + the already-booted re-touch.
  const window = source.slice(chat, chat + 1200);
  assert.match(
    window,
    /booted\.delete\(\s*input\.sessionID\s*\)/,
    "already-booted sessions must delete+add (re-touch) like lastPrompt",
  );
  assert.match(
    window,
    /booted\.add\(\s*input\.sessionID\s*\)/,
    "re-touch must re-insert so insertion order reflects activity",
  );

  // Behavioral pin of the Set LRU pattern the bridge uses (delete+add +
  // front-evict). fill BOOTED_MAX, activity-touch the oldest, insert one more
  // → touched survives, untouched first-in is gone.
  const BOOTED_MAX = 3;
  const booted = new Set();
  function touchBooted(id) {
    booted.delete(id);
    booted.add(id);
    while (booted.size > BOOTED_MAX) {
      const oldest = booted.keys().next().value;
      booted.delete(oldest);
    }
  }
  touchBooted("s0");
  touchBooted("s1");
  touchBooted("s2");
  assert.deepEqual([...booted], ["s0", "s1", "s2"]);
  touchBooted("s0"); // activity on the oldest
  assert.deepEqual([...booted], ["s1", "s2", "s0"]);
  touchBooted("s3"); // insert one more → s1 (untouched first-in) evicted
  assert.deepEqual([...booted], ["s2", "s0", "s3"]);
  assert.ok(booted.has("s0"), "activity-touched session must not be FIFO-evicted");
  assert.equal(booted.has("s1"), false, "untouched oldest must be the eviction victim");
});

test("P5: the session.deleted branch no longer carries the only bound", async () => {
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const branch = source.indexOf('event?.type === "session.deleted"');
  const window = source.slice(branch, branch + 800);
  assert.doesNotMatch(window, /pending\.delete\(sessionID\)/);
  assert.doesNotMatch(window, /booted\.delete\(sessionID\)/);
});

// ── Review round 1: the diagnostic spawn must be bounded ─────────────────

test("P6: the bridge diagnostic is killed on a timer and capped in flight", async () => {
  // It runs on the failure path, where failures arrive in storms. Unbounded,
  // a burst of hook timeouts leaves a pile of hung node processes, each
  // hanging exactly the way the call it is reporting hung.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  assert.match(source, /const DIAGNOSTIC_TIMEOUT_MS = [\d_]+/);
  assert.match(source, /const DIAGNOSTIC_MAX_IN_FLIGHT = \d+/);

  // Round 9: timer / settle live on spawnBridgeDiagnostic (reportBridgeFailure
  // is the suppress/coalesce gate that calls it).
  const spawn = source.indexOf("function spawnBridgeDiagnostic");
  assert.ok(spawn !== -1);
  const window = source.slice(spawn, spawn + 4200);
  assert.match(window, /setTimeout\(/, "the diagnostic child needs a kill timer");
  assert.match(window, /child\.unref\(\)/);
  assert.match(window, /diagnosticsInFlight/);
  // Review round 2: a failed spawn can fire BOTH `error` and `close`; an
  // unguarded settle decrements twice, drives the counter negative, and the
  // in-flight cap stops binding.
  assert.match(
    window,
    /if \(settled\) return;/,
    "settle must be idempotent — error+close both firing must not double-decrement",
  );
  // Round 23: post-spawn sync throw must not permanently leak the budget.
  assert.match(
    window,
    /let claimed = false/,
    "claim flag required so catch can release the in-flight slot",
  );
  assert.match(
    window,
    /if \(claimed\)/,
    "settle/catch must only decrement when the slot was claimed",
  );
});

test("P6: storm suppress coalesces real failures and flushes on settle", async () => {
  // Round 9: under a full DIAGNOSTIC_MAX_IN_FLIGHT budget, further hook
  // failures used to console.warn only — the original P6 defect reintroduced
  // for the high-load path. Suppress must carry counts forward; settle must
  // flush; the audit payload names the dark interval.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  assert.match(source, /pendingSuppressedFailures/);
  assert.match(source, /function queueSuppressedFailure/);
  assert.match(source, /function flushPendingSuppressedFailures/);
  assert.match(
    source,
    /flushPendingSuppressedFailures\(\)/,
    "settle must attempt a flush when a diagnostic slot frees",
  );
  assert.match(source, /suppressed_since_last_report/);
  assert.match(source, /coalesced_count/);
  // session-evict already has its own carry-forward; do not double-queue it.
  const queue = source.indexOf("function queueSuppressedFailure");
  assert.match(
    source.slice(queue, queue + 400),
    /session-evict/,
    "session-evict must not be double-queued into the suppress map",
  );
});

test("P6: session-evict carried counts flush when a diagnostic slot frees", async () => {
  // Round 10: settle used to only flush pendingSuppressedFailures; session-evict
  // counts sat until more churn. free slot must also call flushPendingSessionEvictions.
  // Round 17: settle drains via flushDiagnosticQueues (fair order + backoff).
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  assert.match(source, /function flushPendingSessionEvictions/);
  assert.match(source, /function flushDiagnosticQueues/);
  const settle = source.indexOf("const settle = () =>");
  assert.ok(settle !== -1);
  const settleWindow = source.slice(settle, settle + 900);
  assert.match(
    settleWindow,
    /flushDiagnosticQueues\(\)/,
    "settle must drain diagnostic queues on free slot",
  );
  // Fair drain: session-evict before suppress so storms cannot starve P5.
  const drain = source.indexOf("function flushDiagnosticQueues");
  const drainWindow = source.slice(drain, drain + 700);
  assert.match(drainWindow, /flushPendingSessionEvictions/);
  assert.match(drainWindow, /flushPendingSuppressedFailures/);
  const evictAt = drainWindow.indexOf("flushPendingSessionEvictions");
  const suppressAt = drainWindow.indexOf("flushPendingSuppressedFailures");
  assert.ok(
    evictAt !== -1 && suppressAt !== -1 && evictAt < suppressAt,
    "session-evict must drain before suppress storm under shared budget",
  );
});

// ── helper ─────────────────────────────────────────────────────────────────

/** The set of events a hook entry point actually routes (its switch cases). */
async function routedEventsFor(sourcePath) {
  const source = await readFile(sourcePath, "utf8");
  const routed = new Set();
  for (const match of source.matchAll(/case\s+"([A-Za-z]+)":/g)) {
    routed.add(match[1]);
  }
  // PreToolUse is routed through a constant rather than a string literal.
  if (source.includes("PRE_TOOL_USE_EVENT")) routed.add("PreToolUse");
  return routed;
}
