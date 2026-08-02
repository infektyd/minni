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
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

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

  const generic = new Set([...VALID_EVENTS, "PreToolUse"]);
  const claudeRouted = await routedEventsFor(path.join(PLUGIN_ROOT, "src", "hook.ts"));

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

test("P5: an eviction is reported, not silent", async () => {
  // A bound that discards silently is the same defect in a smaller box.
  const source = await readFile(
    path.join(PLUGIN_ROOT, "kilo", "minni-plugin.js"),
    "utf8",
  );
  const evict = source.indexOf("function evictOldest(");
  const window = source.slice(evict, evict + 600);
  assert.match(window, /reportBridgeFailure/);
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
    source.slice(queue, queue + 700),
    /evictOldest\(pending/,
    "pending must be bounded where it GROWS",
  );

  const boot = source.indexOf("booted.add(input.sessionID)");
  assert.ok(boot !== -1);
  assert.match(source.slice(boot, boot + 200), /evictOldest\(booted/);
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

  const report = source.indexOf("function reportBridgeFailure");
  const window = source.slice(report, report + 2400);
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
