/* global __MINNI_KILO_HOOK_SCRIPT__, __MINNI_KILO_HOOK_ENV__, process, setTimeout, clearTimeout, console */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Stamped installs replace the __MINNI_KILO_* identifiers with string/object
// literals (see the live ~/.config/kilo/plugin/minni.js). Unstamped loads —
// including anyone who drops this file in place before the deferred kilo
// install path lands — must still evaluate: typeof on an unbound global is
// safe, and we fall back to dist/ relative to this file plus process.env.
const _HERE = dirname(fileURLToPath(import.meta.url));
const _DEFAULT_HOOK_SCRIPT = join(_HERE, "..", "dist", "kilocode-hook.js");
const _DEFAULT_HOOK_ENV = {
  MINNI_KILOCODE_AGENT_ID: process.env.MINNI_KILOCODE_AGENT_ID || "kilocode",
  ...(process.env.MINNI_KILOCODE_VAULT_PATH
    ? { MINNI_KILOCODE_VAULT_PATH: process.env.MINNI_KILOCODE_VAULT_PATH }
    : {}),
  ...(process.env.MINNI_KILOCODE_WORKSPACE_ID
    ? { MINNI_KILOCODE_WORKSPACE_ID: process.env.MINNI_KILOCODE_WORKSPACE_ID }
    : {}),
  ...(process.env.MINNI_SOCKET_PATH
    ? { MINNI_SOCKET_PATH: process.env.MINNI_SOCKET_PATH }
    : {}),
};
const HOOK_SCRIPT =
  typeof __MINNI_KILO_HOOK_SCRIPT__ !== "undefined"
    ? __MINNI_KILO_HOOK_SCRIPT__
    : _DEFAULT_HOOK_SCRIPT;
const HOOK_ENV =
  typeof __MINNI_KILO_HOOK_ENV__ !== "undefined"
    ? __MINNI_KILO_HOOK_ENV__
    : _DEFAULT_HOOK_ENV;
const booted = new Set();
const pending = new Map();
// Kilo's session.idle event carries no message text, so Stop had nothing to key
// its learn candidate on and fell back to the bare session id. The prompt is
// already computed in chat.message -- stash it and carry it to Stop.
const lastPrompt = new Map();
// Kilo fires session.deleted while the session is still live, which used to
// clear this map before session.idle arrived, leaving Stop with no task text.
// The first fix added an UNKEYED fallback -- but that leaks: with two sessions
// interleaved, session A's Stop would pick up session B's prompt and write
// another conversation's text into A's vault candidate. A per-session map that
// simply does NOT honor the premature delete is correct and cannot cross
// sessions. Bounded so a long-lived process cannot grow it without limit.
const LAST_PROMPT_MAX = 64;
// P5: same bound, same reason — `session.deleted` no longer clears these, so
// they need their own ceiling. Insertion order is arrival order, so dropping
// from the front drops the oldest session.
const PENDING_MAX = 64;
const BOOTED_MAX = 256;
// #7 (review round 1): the diagnostic path needs its own bounds — see
// reportBridgeFailure.
const DIAGNOSTIC_TIMEOUT_MS = 5_000;
const DIAGNOSTIC_MAX_IN_FLIGHT = 4;
let diagnosticsInFlight = 0;
let diagnosticsSuppressed = 0;
// Review round 9 (PR #260): real hook failures suppressed by the in-flight
// cap used to die at console.warn — the original P6 defect, reintroduced for
// the high-load path the budget exists for. Coalesce per failed_event (same
// shape as session-evict), and flush when a diagnostic child settles and frees
// a slot. session-evict keeps its own carry-forward when suppressed; this map
// is for every other failed_event that reportBridgeFailure swallows.
const pendingSuppressedFailures = new Map();
// Round 17: undelivered → immediate settle flush is an unbounded spawn storm
// when the diagnostic child permanently fails (exit ≠ 0 forever). The in-flight
// cap only bounds concurrency (1 steady-state), not attempt rate. Back off
// before free-slot re-spawn; exponential per consecutive undelivered streak.
const DIAGNOSTIC_UNDELIVERED_BACKOFF_MS = 1_000;
const DIAGNOSTIC_UNDELIVERED_BACKOFF_MAX_MS = 60_000;
let diagnosticUndeliveredStreak = 0;
let diagnosticFlushNotBefore = 0;
let diagnosticFlushTimer = null;
// Review round 3 (PR #260): evictions must not exhaust the diagnostic budget
// real hook failures need. Under session churn one insert can evict a whole
// wave, and a spawn per eviction eats the DIAGNOSTIC_MAX_IN_FLIGHT slots in
// exactly the window a real Stop/SessionStart timeout would need them — the
// failure path going dark to pay for bound maintenance. Coalesce: at most one
// session-evict diagnostic per interval, carrying the count it stands for.
const EVICTION_DIAGNOSTIC_INTERVAL_MS = 60_000;
// Per-LABEL pending counts (review round 4): a single scalar let a mixed wave
// (pending then booted) report the whole count under the LAST wave's label and
// bound, sending an operator to remediate the wrong map.
const evictionsSinceReport = new Map();
let lastEvictionReportAt = 0;

function sessionEvictDetail() {
  return [...evictionsSinceReport.entries()]
    .map(([name, info]) => `${info.count} ${name} entr(y|ies) (bound ${info.max})`)
    .join("; ");
}

function noteDiagnosticUndelivered() {
  diagnosticUndeliveredStreak += 1;
  const exp = Math.min(diagnosticUndeliveredStreak - 1, 6);
  const backoff = Math.min(
    DIAGNOSTIC_UNDELIVERED_BACKOFF_MAX_MS,
    DIAGNOSTIC_UNDELIVERED_BACKOFF_MS * (2 ** exp),
  );
  diagnosticFlushNotBefore = Math.max(
    diagnosticFlushNotBefore,
    Date.now() + backoff,
  );
}

function noteDiagnosticDelivered() {
  diagnosticUndeliveredStreak = 0;
  // Success clears the gate so free-slot drain is immediate again.
  diagnosticFlushNotBefore = 0;
}

function scheduleDiagnosticFlush() {
  const delay = Math.max(0, diagnosticFlushNotBefore - Date.now());
  if (delay === 0) {
    flushDiagnosticQueues();
    return;
  }
  if (diagnosticFlushTimer != null) return;
  diagnosticFlushTimer = setTimeout(() => {
    diagnosticFlushTimer = null;
    flushDiagnosticQueues();
  }, delay);
  // Don't pin the Kilo process open solely for a retry of a broken audit path.
  if (typeof diagnosticFlushTimer.unref === "function") {
    diagnosticFlushTimer.unref();
  }
}

function flushDiagnosticQueues() {
  // Honor undelivered backoff — never tight-loop re-spawn.
  if (Date.now() < diagnosticFlushNotBefore) {
    scheduleDiagnosticFlush();
    return false;
  }
  // Round 17 fair drain: session-evict first so a sustained suppress storm
  // cannot starve P5 eviction loss under the shared budget.
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

function flushPendingSessionEvictions() {
  // Round 10: when a diagnostic slot frees, deliver carried session-evict
  // counts even if no new eviction arrives. Without this, a storm that
  // suppressed session-evict left the loss console-only until further churn
  // (or process death).
  if (evictionsSinceReport.size === 0) return false;
  // Round 17: budget-full must always console.warn — lastEvictionReportAt only
  // advances on successful flush, so the within-interval path never fired while
  // suppress storms kept the budget full and this early return was silent.
  if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) {
    console.warn(
      `[minni] session-evict diagnostic suppressed (budget full); ` +
        `carrying forward: ${sessionEvictDetail()}`,
    );
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
      // Round 5: "accepted" only means a child spawned. If it dies before
      // writing the audit, restore the flushed counts so the next slot
      // carries them — otherwise the loss vanishes from the P6 surface with
      // only a console line to show for it.
      for (const [name, info] of flushed) {
        const cur = evictionsSinceReport.get(name) || { count: 0, max: info.max };
        cur.count += info.count;
        cur.max = info.max;
        evictionsSinceReport.set(name, cur);
      }
      // Round 7: rewind the coalesce clock too. It advanced on spawn, so a
      // one-shot wave whose audit failed sat restored-but-console-only until
      // some FUTURE eviction reopened the window — which may never come.
      lastEvictionReportAt = 0;
      console.warn(
        `[minni] session-evict audit child died before writing; ` +
          `restored counts: ${detail}`,
      );
    },
  );
  if (accepted) {
    lastEvictionReportAt = Date.now();
    evictionsSinceReport.clear();
    return true;
  }
  console.warn(
    `[minni] session-evict diagnostic suppressed (budget full); ` +
      `carrying forward: ${detail}`,
  );
  return false;
}

function reportSessionEvictions(label, max, evicted) {
  const entry = evictionsSinceReport.get(label) || { count: 0, max };
  entry.count += evicted;
  entry.max = max;
  evictionsSinceReport.set(label, entry);
  const now = Date.now();
  if (now - lastEvictionReportAt < EVICTION_DIAGNOSTIC_INTERVAL_MS) {
    // Still visible per wave — just no spawn.
    console.warn(
      `[minni] evicted ${evicted} ${label} entr(y|ies) (bound ${max}); ` +
        `${entry.count} pending for the next diagnostic`,
    );
    return;
  }
  flushPendingSessionEvictions();
}

function evictOldest(collection, max, label) {
  // Evicting queued context IS losing memory injection for that session.
  // A bound that discards silently is the same defect in a smaller box.
  let evicted = 0;
  while (collection.size > max) {
    const oldest = collection.keys().next().value;
    collection.delete(oldest);
    evicted += 1;
  }
  if (evicted) reportSessionEvictions(label, max, evicted);
}

function hookContext(result) {
  return result?.hookSpecificOutput?.additionalContext || result?.systemMessage || "";
}

function runHook(event, payload) {
  return new Promise((resolve, reject) => {
    // Kilo runs plugins under Bun, where process.execPath is the Kilo/Bun
    // executable rather than Node. The compiled hook is a Node entry point.
    const child = spawn("node", [HOOK_SCRIPT, event], {
      env: { ...process.env, ...HOOK_ENV },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`Minni ${event} hook timed out`));
    }, event === "PreToolUse" ? 10_000 : 30_000);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        const detail = stderr.trim() || stdout.trim();
        return reject(new Error(detail || `Minni ${event} hook exited ${code}`));
      }
      try {
        resolve(JSON.parse(stdout.trim().split("\n").at(-1) || "{}"));
      } catch (error) {
        reject(new Error(`Minni ${event} returned invalid JSON: ${error}`));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

// P6 (#228): bridge failures were `console.warn` only and never reached the
// audit log, so there was no surface on which a persistently failing Kilo
// bridge was distinguishable from one that simply had no traffic — the health
// signal overstated the bridge's coverage for as long as it stayed broken.
//
// The bridge has no direct vault handle (it spawns the hook to reach it), so
// the failure is reported through the hook's OWN audit channel via a
// fire-and-forget BridgeFailure event. Deliberately not awaited and fully
// fail-silent: this runs on the failure path, and a second failure here must
// not turn a degraded bridge into a broken session.
// Returns true when a diagnostic child was actually SPAWNED (a budget slot
// was taken), false when the in-flight cap or a spawn error suppressed it.
// Spawned is NOT delivered (round 5): the child is fire-and-forget, so a
// caller that needs to know its audit never landed passes `onUndelivered`,
// invoked once if the child errors or exits non-zero before writing.
//
// Review round 9: when the in-flight cap suppresses a spawn, the loss is
// coalesced (per failed_event) and flushed when a child settles — not
// console-only forever. session-evict already carries its own counts on
// suppress; every other caller rides this path.
function queueSuppressedFailure(event, detail) {
  // session-evict owns its carry-forward in evictionsSinceReport; re-queueing
  // it here would double-report once a slot frees.
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
  // Snapshot then clear; restore if the spawn is suppressed or undelivered.
  pendingSuppressedFailures.clear();
  const accepted = spawnBridgeDiagnostic(
    failedEvent,
    `coalesced bridge failures: ${detail}`,
    () => {
      restoreSuppressedFailures(flushed);
      // Round 14: undelivered means the dark interval is still unreported —
      // restore the suppressed count that would have been in the payload.
      diagnosticsSuppressed = Math.max(diagnosticsSuppressed, suppressedAtFlush);
    },
    {
      coalesced_count: totalCount,
      suppressed_since_last_report: suppressedAtFlush,
      // Round 15: subtract the snapshot, don't hard-zero — mid-flight
      // suppressions after this spawn must remain attributed.
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
  try {
    const child = spawn("node", [HOOK_SCRIPT, "BridgeFailure"], {
      env: { ...process.env, ...HOOK_ENV },
      stdio: ["pipe", "ignore", "ignore"],
      detached: false,
    });
    diagnosticsInFlight += 1;
    const kill = setTimeout(() => child.kill("SIGKILL"), DIAGNOSTIC_TIMEOUT_MS);
    // `once` guards each event individually, but a failed spawn can fire BOTH
    // `error` and `close`. Double-decrementing drives the counter negative and
    // the in-flight cap stops binding — under exactly the failure storm it
    // exists to bound (review round 2 on PR #260).
    let settled = false;
    // Round 5: the same idempotence guard for delivery-failure reporting —
    // a failed spawn can fire both `error` and `close`. Declared before settle
    // so the free-slot path can branch on undelivered vs delivered.
    let undeliveredReported = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      clearTimeout(kill);
      diagnosticsInFlight -= 1;
      // Round 17: undelivered + immediate flush = permanent-fail spawn storm.
      // Back off and schedule; a successful delivery drains free slots now.
      if (undeliveredReported) {
        noteDiagnosticUndelivered();
        scheduleDiagnosticFlush();
      } else {
        noteDiagnosticDelivered();
        // Round 9/10 via flushDiagnosticQueues: free slot drains suppress
        // queue and session-evict carry-forward (session-evict first).
        flushDiagnosticQueues();
      }
    };
    const undelivered = () => {
      if (undeliveredReported) return;
      undeliveredReported = true;
      if (onUndelivered) onUndelivered();
    };
    const delivered = () => {
      // Never mark delivered after undelivered: a failed spawn can fire both
      // `error` and a subsequent `close` (sometimes with code 0).
      if (undeliveredReported) return;
      if (extras.onDelivered) extras.onDelivered();
    };
    // Round 14: undelivered BEFORE settle. settle() free-slot flushes; if
    // restore ran after flush, restored counts sat console-only with no
    // re-spawn (P6 hole under one-shot undelivered waves).
    child.once("close", (code) => {
      if (code !== 0) undelivered();
      else delivered();
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
    if (extras.coalesced_count != null) {
      payload.coalesced_count = extras.coalesced_count;
    }
    if (extras.suppressed_since_last_report != null) {
      payload.suppressed_since_last_report = extras.suppressed_since_last_report;
    }
    child.stdin.end(JSON.stringify(payload));
    return true;
  } catch {
    // The console line above is the last resort; never throw from here.
    return false;
  }
}

function reportBridgeFailure(event, error, onUndelivered) {
  const detail = error instanceof Error ? error.message : String(error);
  console.warn(`[minni] ${event} hook unavailable; continuing: ${detail}`);
  // Bounded on BOTH axes. This runs on the failure path, where failures arrive
  // in storms: without a concurrency cap a burst of hook timeouts spawns a
  // diagnostic per failure, and without a kill timer each one can hang exactly
  // the way the call it is reporting hung. Either way the degraded bridge
  // becomes a pile of stuck node processes.
  if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) {
    diagnosticsSuppressed += 1;
    queueSuppressedFailure(event, detail);
    console.warn(
      `[minni] bridge diagnostic suppressed (${diagnosticsInFlight} in flight, ` +
        `${diagnosticsSuppressed} suppressed since start); carrying forward for flush`,
    );
    return false;
  }
  // Round 16: undelivered after an *accepted* spawn used to be a no-op when
  // the caller omitted onUndelivered (runHookFailOpen). Budget-full and sync
  // spawn-fail already queue; session-evict passes its own restore. Ordinary
  // hook failures that spawn then die (exit ≠ 0 / error) re-opened P6 —
  // console.warn only, no suppress map, no free-slot re-spawn.
  const accepted = spawnBridgeDiagnostic(event, detail, () => {
    if (onUndelivered) onUndelivered();
    else {
      diagnosticsSuppressed += 1;
      queueSuppressedFailure(event, detail);
    }
  });
  // Round 13: sync spawn failure (EMFILE, bad hook path) used to die at
  // console.warn only — same P6 hole as budget-full, without the coalesce.
  if (!accepted) {
    diagnosticsSuppressed += 1;
    queueSuppressedFailure(event, detail);
    console.warn(
      `[minni] bridge diagnostic spawn failed; carrying forward for flush ` +
        `(${diagnosticsSuppressed} suppressed since start)`,
    );
  }
  return accepted;
}

async function runHookFailOpen(event, payload) {
  try {
    return await runHook(event, payload);
  } catch (error) {
    // Round 9: ignore the boolean — suppress coalesces + flushes on settle.
    // A silent drop of the return value is only safe because the suppress
    // path no longer discards the loss.
    reportBridgeFailure(event, error);
    return null;
  }
}

// Review round 4 (PR #260): PENDING_MAX bounds the number of SESSIONS, not
// the volume queued per session — one live session whose delivery transform
// is delayed or missing grew its context array without limit (and P5
// correctly removed the accidental reset on premature session.deleted). A
// bounded map that still grows without bound through its values is the same
// silent-bound defect one box smaller.
const PENDING_CONTEXTS_PER_SESSION_MAX = 8;

function queueContext(sessionID, result) {
  const context = hookContext(result);
  if (!context) return;
  const contexts = pending.get(sessionID) || [];
  contexts.push(context);
  let overflow = 0;
  while (contexts.length > PENDING_CONTEXTS_PER_SESSION_MAX) {
    // Oldest first, reported through the same eviction path as the maps.
    contexts.shift();
    overflow += 1;
  }
  if (overflow) {
    reportSessionEvictions(
      "queued-context (per-session)",
      PENDING_CONTEXTS_PER_SESSION_MAX,
      overflow,
    );
  }
  // Re-inserting moves it to the end, so the eviction below is LRU-ish —
  // same shape as lastPrompt.
  pending.delete(sessionID);
  pending.set(sessionID, contexts);
  // P5 (review round 1): bound at INSERT, exactly as lastPrompt does. Bounding
  // only inside the session.deleted branch left the maps unbounded whenever
  // that event is missing or delayed (version skew, bus drop) — the very leak
  // class the lastPrompt fix already solved correctly.
  evictOldest(pending, PENDING_MAX, "pending context");
}

// Post-compaction summary read-back (plan-3e5a410b9ab6f715). Kilo's
// experimental.session.compacting hook fires BEFORE the summary exists and its
// input is {sessionID} only (verified against the installed 7.1.0 bundle — the
// docs describe a richer shape that is not present locally). The summary is
// instead fetched AFTER the session.compacted bus event via the SDK client the
// plugin factory receives: the summary message is the newest one flagged
// info.summary === true with info.mode === "compaction"; an aborted compaction
// carries the same flags plus an error and zero text parts, and is skipped.
// Fail-open throughout — a failed read-back must never break the session.
async function harvestCompactedSummary(client, sessionID, directory) {
  try {
    const res = await client.session.messages({ path: { id: sessionID } });
    const messages = Array.isArray(res) ? res : Array.isArray(res?.data) ? res.data : [];
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const info = messages[i]?.info;
      if (!info || info.summary !== true || info.mode !== "compaction" || info.error) continue;
      const text = (messages[i].parts || [])
        .filter((part) => part?.type === "text" && typeof part.text === "string")
        .map((part) => part.text)
        .join("\n");
      if (!text) continue;
      await runHookFailOpen("CompactSummary", {
        session_id: sessionID,
        workspace_id: directory,
        summary_text: text,
        summary_id: info.id,
        ...(info.time?.created
          ? { summary_timestamp: new Date(info.time.created).toISOString() }
          : {}),
      });
      return;
    }
  } catch {
    // runHookFailOpen already swallows hook errors; this guards the SDK call.
  }
}

const MinniPlugin = async ({ directory, client }) => ({
  "chat.message": async (input, output) => {
    if (!booted.has(input.sessionID)) {
      const boot = await runHookFailOpen("SessionStart", {
        session_id: input.sessionID,
        workspace_id: directory,
      });
      if (boot) {
        queueContext(input.sessionID, boot);
        booted.add(input.sessionID);
        evictOldest(booted, BOOTED_MAX, "booted session");
      }
    } else {
      // Round 19: re-touch so the bound is activity-ordered LRU, not FIFO.
      // Without delete+add, a long-lived active session stays first-in and is
      // the first evicted when BOOTED_MAX fills with one-shot sessions —
      // forcing a redundant SessionStart (and re-inject) on the next message.
      booted.delete(input.sessionID);
      booted.add(input.sessionID);
    }
    const prompt = output.parts
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n");
    if (prompt) {
      // Re-inserting moves it to the end, so the eviction below is LRU-ish.
      lastPrompt.delete(input.sessionID);
      lastPrompt.set(input.sessionID, prompt.slice(0, 400));
      // Round 15: same reported eviction path as pending/booted — a silent
      // lastPrompt bound dropped Stop learn-candidate text with no audit.
      evictOldest(lastPrompt, LAST_PROMPT_MAX, "last prompt");
    }
    const result = await runHookFailOpen("UserPromptSubmit", {
      session_id: input.sessionID,
      prompt,
      workspace_id: directory,
    });
    queueContext(input.sessionID, result);
  },
  "experimental.chat.system.transform": async (input, output) => {
    const sessionID = input.sessionID;
    if (sessionID && pending.has(sessionID)) {
      output.system.push(...pending.get(sessionID));
      pending.delete(sessionID);
    }
  },
  "tool.execute.before": async (input, output) => {
    const result = await runHookFailOpen("PreToolUse", {
      session_id: input.sessionID,
      tool_name: input.tool,
      tool_input: output.args,
      workspace_id: directory,
    });
    if (result?.hookSpecificOutput?.permissionDecision === "deny") {
      throw new Error(result.hookSpecificOutput.permissionDecisionReason || "Minni recall guard denied tool call");
    }
  },
  "experimental.session.compacting": async (input, output) => {
    const result = await runHookFailOpen("PreCompact", { session_id: input.sessionID, workspace_id: directory });
    const context = hookContext(result);
    if (context) output.context.push(context);
  },
  event: async ({ event }) => {
    const sessionID = event.properties?.sessionID || event.properties?.info?.id || "kilo-session-unknown";
    if (event?.type === "session.idle") {
      await runHookFailOpen("Stop", {
        session_id: sessionID,
        workspace_id: directory,
        // Synthesized by this bridge, not by Kilo -- see kilocodeWire.lastTaskText.
        last_user_message: lastPrompt.get(sessionID) ?? "",
      });
    } else if (event?.type === "session.compacted") {
      await harvestCompactedSummary(client, sessionID, directory);
    } else if (event?.type === "session.deleted") {
      // P5 (#228): this used to clear `booted` and `pending` outright. Kilo
      // fires session.deleted while the session is STILL LIVE — the hazard
      // already documented above for `lastPrompt`, where the fix was applied
      // and then never extended here. Clearing `pending` drops queued boot
      // context that experimental.chat.system.transform has not yet handed to
      // the model, so that session's memory injection is lost; clearing
      // `booted` re-runs SessionStart on a session that already booted.
      //
      // Same remedy as lastPrompt: do NOT honor the premature delete, and
      // bound the maps instead so a long-lived process cannot grow them. The
      // eviction is what is counted, because that is the only real loss.
      // Deliberately a no-op for THIS session: the maps are bounded at insert
      // (above), so there is nothing left for this branch to do beyond not
      // destroying live state.
    }
  },
});

export default MinniPlugin;
