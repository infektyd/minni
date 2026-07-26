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

async function runHookFailOpen(event, payload) {
  try {
    return await runHook(event, payload);
  } catch (error) {
    console.warn(`[minni] ${event} hook unavailable; continuing: ${error instanceof Error ? error.message : error}`);
    return null;
  }
}

function queueContext(sessionID, result) {
  const context = hookContext(result);
  if (!context) return;
  const contexts = pending.get(sessionID) || [];
  contexts.push(context);
  pending.set(sessionID, contexts);
}

const MinniPlugin = async ({ directory }) => ({
  "chat.message": async (input, output) => {
    if (!booted.has(input.sessionID)) {
      const boot = await runHookFailOpen("SessionStart", {
        session_id: input.sessionID,
        workspace_id: directory,
      });
      if (boot) {
        queueContext(input.sessionID, boot);
        booted.add(input.sessionID);
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
    } else if (event?.type === "session.deleted") {
      booted.delete(sessionID);
      pending.delete(sessionID);
    }
  },
});

export default MinniPlugin;
