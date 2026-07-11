import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { adaptCursorOutput, adaptCursorPayload, CURSOR_EVENTS } from "../dist/cursor-adapter.js";

const execFileAsync = promisify(execFile);
const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK = path.join(ROOT, "dist", "cursor-hook.js");

async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), "minni-cursor-hook-"));
  const vault = path.join(root, "cursor-vault");
  await mkdir(vault, { recursive: true });
  return { root, vault };
}

async function run(event, f, payload = {}) {
  const child = execFileAsync(process.execPath, [HOOK, event], {
    env: {
      ...process.env,
      MINNI_HOME: f.root,
      MINNI_CURSOR_AGENT_ID: "cursor",
      MINNI_CURSOR_VAULT_PATH: f.vault,
      MINNI_CURSOR_WORKSPACE_ID: "workspace-test",
      MINNI_SOCKET_PATH: path.join(f.root, "missing.sock"),
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
    },
    timeout: 30_000,
  });
  child.child.stdin.end(JSON.stringify(payload));
  return JSON.parse((await child).stdout.trim().split("\n").at(-1));
}

test("Cursor event and payload adapter maps native schema", () => {
  assert.equal(CURSOR_EVENTS.sessionStart, "SessionStart");
  assert.equal(CURSOR_EVENTS.beforeSubmitPrompt, "UserPromptSubmit");
  const adapted = adaptCursorPayload({
    conversation_id: "c-1",
    prompt: "remember this",
    workspace_roots: ["/work/repo"],
    tool_name: "Shell",
    tool_input: { command: "rg needle" },
  });
  assert.equal(adapted.session_id, "c-1");
  assert.equal(adapted.prompt, "remember this");
  assert.equal(adapted.workspace_id, "/work/repo");
  assert.equal(adapted.tool_name, "Bash");
});

test("native Cursor Shell read is denied once when strong recall is pending", async () => {
  const f = await fixture();
  try {
    const runtime = path.join(f.vault, ".runtime");
    await mkdir(runtime, { recursive: true });
    await writeFile(path.join(runtime, "recall-state.json"), JSON.stringify({
      task_signature: "cursor-task",
      intent: "status",
      top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
      top_score: 0.91,
      consumed: false,
      ts: new Date().toISOString(),
    }));
    const payload = {
      conversation_id: "cursor-session",
      tool_name: "Shell",
      tool_input: { command: "rg needle ." },
      workspace_roots: ["/work/repo"],
    };
    const denied = await run("preToolUse", f, payload);
    assert.equal(denied.permission, "deny");
    assert.match(denied.user_message, /prior-fix/);
    assert.deepEqual(await run("preToolUse", f, payload), { permission: "allow" });
  } finally {
    await rm(f.root, { recursive: true, force: true });
  }
});

test("Cursor output adapter uses native snake_case and permission schema", () => {
  assert.deepEqual(adaptCursorOutput("SessionStart", {
    continue: true,
    hookSpecificOutput: { additionalContext: "memory" },
  }), { additional_context: "memory" });
  assert.deepEqual(adaptCursorOutput("PreToolUse", { continue: true }), { permission: "allow" });
  assert.deepEqual(adaptCursorOutput("PreToolUse", {
    continue: true,
    hookSpecificOutput: { permissionDecision: "deny", permissionDecisionReason: "recall first" },
  }), { permission: "deny", user_message: "recall first" });
  assert.deepEqual(adaptCursorOutput("UserPromptSubmit", {
    hookSpecificOutput: { additionalContext: "unsupported by Cursor" },
  }), { continue: true });
});

test("Cursor hook writes only the Cursor vault and stamps Cursor identity", async () => {
  const f = await fixture();
  try {
    const output = await run("sessionStart", f, {
      conversation_id: "cursor-session",
      workspace_roots: ["/work/repo"],
    });
    assert.equal(typeof output.additional_context, "string");
    assert.match(output.additional_context, /agent=\"cursor\"/);
    assert.match(output.additional_context, new RegExp(f.vault.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.doesNotMatch(output.additional_context, /claude-code|claudecode-vault/);
    const log = await readFile(path.join(f.vault, "log.md"), "utf8");
    assert.match(log, /hook_cursor_session_start/);
  } finally {
    await rm(f.root, { recursive: true, force: true });
  }
});

test("Cursor manifest invokes only the native adapter with bounded timeouts", async () => {
  const manifest = JSON.parse(await readFile(path.join(ROOT, "hooks", "hooks-cursor.json"), "utf8"));
  assert.equal(manifest.version, 1);
  assert.deepEqual(Object.keys(manifest.hooks).sort(),
    ["beforeSubmitPrompt", "preCompact", "preToolUse", "sessionStart", "stop"].sort());
  for (const entries of Object.values(manifest.hooks)) {
    for (const hook of entries) {
      assert.match(hook.command, /\$\{CURSOR_PLUGIN_ROOT\}\/dist\/cursor-hook\.js/);
      assert.doesNotMatch(hook.command, /dist\/hook\.js|CLAUDE/);
      assert.ok(hook.timeout > 0 && hook.timeout <= 30);
    }
  }
});
