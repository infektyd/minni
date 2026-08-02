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
// Review round 3 (PR #260): evictions must not exhaust the diagnostic budget
// real hook failures need. Under session churn one insert can evict a whole
// wave, and a spawn per eviction eats the DIAGNOSTIC_MAX_IN_FLIGHT slots in
// exactly the window a real Stop/SessionStart timeout would need them — the
// failure path going dark to pay for bound maintenance. Coalesce: at most one
// session-evict diagnostic per interval, carrying the count it stands for.
const EVICTION_DIAGNOSTIC_INTERVAL_MS = 60_000;
let evictionsSinceReport = 0;
let lastEvictionReportAt = 0;

function reportSessionEvictions(label, max, evicted) {
  evictionsSinceReport += evicted;
  const now = Date.now();
  if (now - lastEvictionReportAt < EVICTION_DIAGNOSTIC_INTERVAL_MS) {
    // Still visible per wave — just no spawn.
    console.warn(
      `[minni] evicted ${evicted} ${label} entr(y|ies) (bound ${max}); ` +
        `${evictionsSinceReport} since last diagnostic`,
    );
    return;
  }
  lastEvictionReportAt = now;
  const count = evictionsSinceReport;
  evictionsSinceReport = 0;
  reportBridgeFailure(
    "session-evict",
    new Error(`evicted ${count} ${label} entr(y|ies) for old sessions (bound ${max})`),
  );
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
function reportBridgeFailure(event, error) {
  const detail = error instanceof Error ? error.message : String(error);
  console.warn(`[minni] ${event} hook unavailable; continuing: ${detail}`);
  // Bounded on BOTH axes. This runs on the failure path, where failures arrive
  // in storms: without a concurrency cap a burst of hook timeouts spawns a
  // diagnostic per failure, and without a kill timer each one can hang exactly
  // the way the call it is reporting hung. Either way the degraded bridge
  // becomes a pile of stuck node processes.
  if (diagnosticsInFlight >= DIAGNOSTIC_MAX_IN_FLIGHT) {
    diagnosticsSuppressed += 1;
    console.warn(
      `[minni] bridge diagnostic suppressed (${diagnosticsInFlight} in flight, ` +
        `${diagnosticsSuppressed} suppressed since start)`,
    );
    return;
  }
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
    const settle = () => {
      if (settled) return;
      settled = true;
      clearTimeout(kill);
      diagnosticsInFlight -= 1;
    };
    child.once("close", settle);
    child.once("error", settle);
    child.unref();
    child.on("error", () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(
      JSON.stringify({
        hook_event_name: "BridgeFailure",
        bridge: "kilo",
        failed_event: event,
        error: detail.slice(0, 400),
      }),
    );
  } catch {
    // The console line above is the last resort; never throw from here.
  }
}

async function runHookFailOpen(event, payload) {
  try {
    return await runHook(event, payload);
  } catch (error) {
    reportBridgeFailure(event, error);
    return null;
  }
}

function queueContext(sessionID, result) {
  const context = hookContext(result);
  if (!context) return;
  const contexts = pending.get(sessionID) || [];
  contexts.push(context);
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
    }
    const prompt = output.parts
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n");
    if (prompt) {
      // Re-inserting moves it to the end, so the eviction below is LRU-ish.
      lastPrompt.delete(input.sessionID);
      lastPrompt.set(input.sessionID, prompt.slice(0, 400));
      while (lastPrompt.size > LAST_PROMPT_MAX) {
        lastPrompt.delete(lastPrompt.keys().next().value);
      }
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
