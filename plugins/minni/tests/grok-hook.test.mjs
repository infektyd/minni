// Grok Build PreToolUse adapter + hook: camelCase toolName/toolInput, native
// tool aliases, and {decision,reason} output — without these the file-backed
// s6 guard never sees cold tools in scope on Grok.
//
// Isolation mirrors gemini-hook / cursor-hook: every env knob points inside a
// tmp fixture; the live ~/.minni is never read or written.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  adaptGrokPayload,
  adaptGrokPreToolUseOutput,
  grokAllow,
} from "../dist/grok-adapter.js";

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const GROK_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "grok-hook.js");
const HOOKS_GROK_JSON = path.join(PLUGIN_ROOT, "hooks", "hooks-grok.json");

/** Shape from Grok Build hooks docs (paths shortened). */
function grokPreToolUsePayload(overrides = {}) {
  return {
    hookEventName: "pre_tool_use",
    sessionId: "grok-session-abc",
    cwd: "/tmp/work",
    workspaceRoot: "/tmp/work",
    permissionMode: "default",
    toolName: "read_file",
    toolInput: { target_file: "/tmp/work/README.md" },
    timestamp: "2026-04-14T12:00:00Z",
    ...overrides,
  };
}

async function makeFixture() {
  const root = await mkdtemp(path.join(tmpdir(), "minni-grok-hook-"));
  const vault = path.join(root, "grok-vault");
  await mkdir(vault, { recursive: true });
  return { root, vault };
}

async function runGrokHook(event, fixture, payload, extraEnv = {}) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.root,
    MINNI_GROK_VAULT_PATH: fixture.vault,
    MINNI_GROK_AGENT_ID: "grok-build",
    MINNI_SOCKET_PATH: path.join(fixture.root, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    ...extraEnv,
  };
  const child = execFileAsync(process.execPath, [GROK_HOOK_JS, event], {
    env,
    timeout: 30_000,
  });
  child.child.stdin.end(payload === undefined ? "" : JSON.stringify(payload));
  const { stdout } = await child;
  return JSON.parse(stdout.trim().split("\n").at(-1));
}

test("adaptGrokPayload maps camelCase + native tools to guard canonical names", () => {
  const adapted = adaptGrokPayload(grokPreToolUsePayload());
  assert.equal(adapted.session_id, "grok-session-abc");
  assert.equal(adapted.cwd, "/tmp/work");
  assert.equal(adapted.tool_name, "Read");
  assert.deepEqual(adapted.tool_input, { target_file: "/tmp/work/README.md" });
  // Original Grok fields preserved for forward-compat.
  assert.equal(adapted.sessionId, "grok-session-abc");
  assert.equal(adapted.toolName, "read_file");
});

test("adaptGrokPayload aliases grep/list_dir/run_terminal_command and never clobbers", () => {
  assert.equal(
    adaptGrokPayload({ toolName: "grep", toolInput: { pattern: "x" } }).tool_name,
    "Grep",
  );
  assert.equal(
    adaptGrokPayload({ toolName: "list_dir", toolInput: { path: "." } }).tool_name,
    "Glob",
  );
  const shell = adaptGrokPayload({
    toolName: "run_terminal_command",
    toolInput: { command: "rg needle ." },
  });
  assert.equal(shell.tool_name, "Bash");
  assert.deepEqual(shell.tool_input, { command: "rg needle ." });

  const native = adaptGrokPayload({
    session_id: "already-snake",
    sessionId: "camel-ignored",
    tool_name: "Read",
    toolName: "read_file",
  });
  assert.equal(native.session_id, "already-snake");
  assert.equal(native.tool_name, "Read");
});

test("adaptGrokPreToolUseOutput: allow collapses to explicit allow, deny carries reason", () => {
  assert.deepEqual(adaptGrokPreToolUseOutput({ continue: true }), { decision: "allow" });
  assert.deepEqual(grokAllow(), { decision: "allow" });
  const deny = adaptGrokPreToolUseOutput({
    continue: true,
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "consult recall first",
    },
  });
  assert.deepEqual(deny, { decision: "deny", reason: "consult recall first" });
});

test("hooks-grok.json template stamps GROK_PLUGIN_ROOT and PreToolUse", async () => {
  const template = JSON.parse(await readFile(HOOKS_GROK_JSON, "utf8"));
  assert.ok(template.hooks?.PreToolUse, "PreToolUse must be registered");
  for (const [event, groups] of Object.entries(template.hooks)) {
    for (const group of groups) {
      for (const hook of group.hooks) {
        assert.equal(hook.type, "command");
        assert.ok(
          hook.command.includes("${GROK_PLUGIN_ROOT}/dist/grok-hook.js"),
          `${event} must run grok-hook.js via GROK_PLUGIN_ROOT`,
        );
        assert.ok(hook.command.endsWith(` ${event}`), "command must pass its event name");
        assert.ok(hook.timeout > 0, `${event} must bound a timeout`);
      }
    }
  }
});

test("PreToolUse with no recall state prints exactly the explicit allow", async () => {
  const fixture = await makeFixture();
  try {
    const output = await runGrokHook("PreToolUse", fixture, grokPreToolUsePayload());
    assert.deepEqual(output, { decision: "allow" });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("PreToolUse never emits Claude permissionDecision shape (hooks disabled / allow path)", async () => {
  const fixture = await makeFixture();
  try {
    const disabled = await runGrokHook("PreToolUse", fixture, grokPreToolUsePayload(), {
      MINNI_GROK_HOOKS: "off",
    });
    assert.deepEqual(disabled, { decision: "allow" });
    assert.equal(disabled.hookSpecificOutput, undefined);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("native Grok read_file allows leftover consumed=false — UPS inject is dropped", async () => {
  const fixture = await makeFixture();
  try {
    const runtime = path.join(fixture.vault, ".runtime");
    await mkdir(runtime, { recursive: true });
    const statePath = path.join(runtime, "recall-state.json");
    await writeFile(
      statePath,
      JSON.stringify({
        task_signature: "grok-task",
        intent: "status",
        top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
        top_score: 0.91,
        consumed: false,
        ts: new Date().toISOString(),
      }),
    );
    const payload = grokPreToolUsePayload({
      toolName: "read_file",
      toolInput: { target_file: "/tmp/work/src/main.ts" },
    });
    assert.deepEqual(await runGrokHook("PreToolUse", fixture, payload), {
      decision: "allow",
    });
    const leftover = JSON.parse(await readFile(statePath, "utf8"));
    assert.equal(leftover.consumed, false, "defense in depth must not consume; leftover file cannot deny");
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("Grok hook writes only the Grok vault and stamps grok-build identity", async () => {
  const fixture = await makeFixture();
  try {
    const output = await runGrokHook("SessionStart", fixture, {
      sessionId: "grok-session",
      workspaceRoot: "/work/repo",
      cwd: "/work/repo",
    });
    // Passive SessionStart stdout is ignored by Grok host, but Minni still
    // emits continue-shaped output for side-effect work (vault stamp).
    assert.equal(typeof output, "object");
    const log = await readFile(path.join(fixture.vault, "log.md"), "utf8");
    assert.match(log, /hook_grok_session_start/);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});
