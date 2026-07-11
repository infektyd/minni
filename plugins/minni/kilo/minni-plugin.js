/* global __MINNI_KILO_HOOK_SCRIPT__, __MINNI_KILO_HOOK_ENV__, process, setTimeout, clearTimeout, console */
import { spawn } from "node:child_process";

const HOOK_SCRIPT = __MINNI_KILO_HOOK_SCRIPT__;
const HOOK_ENV = __MINNI_KILO_HOOK_ENV__;
const booted = new Set();
const pending = new Map();

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
      await runHookFailOpen("Stop", { session_id: sessionID, workspace_id: directory });
    } else if (event?.type === "session.deleted") {
      booted.delete(sessionID);
      pending.delete(sessionID);
    }
  },
});

export default MinniPlugin;
