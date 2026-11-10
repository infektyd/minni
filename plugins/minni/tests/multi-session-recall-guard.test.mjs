// Slice S1 (H6c, R1): Session-scoped recall guard, consult disarm, and lifecycle state.
// Spawns two real MCP server child processes under distinct live host PIDs against a single vault.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  recordActiveSessionWorkspace,
  resolveMcpSessionIdentity,
} from "../dist/config.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import {
  readLifecycleState,
  readRecallState,
  sessionStateKey,
  writeRecallState,
} from "../dist/recall-state.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;

async function startFakeDaemon(socketPath) {
  const daemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        const result =
          request.method === "search"
            ? { results: [{ id: 1, title: "Hit", snippet: "ok", score: 0.9 }] }
            : { ok: true };
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      }
    });
  });
  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  return daemon;
}

async function spawnHostMcpSession(t, { home, socketPath, vaultPath }) {
  // Spawn a real host wrapper process so each MCP server child has its own distinct live process.ppid.
  const hostScript = [
    'const { spawn } = require("node:child_process");',
    "const child = spawn(process.execPath, [process.argv[1]], { stdio: 'inherit' });",
    "process.on('SIGTERM', () => child.kill('SIGKILL'));",
    "child.on('exit', (code) => process.exit(code ?? 0));",
  ].join(" ");
  const hostProc = spawn(process.execPath, ["-e", hostScript, SERVER_PATH], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: vaultPath,
      MINNI_CLAUDECODE_VAULT_PATH: vaultPath,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => hostProc.kill("SIGKILL"));

  const responses = new Map();
  const waiters = new Map();
  let buffered = "";
  hostProc.stdout.setEncoding("utf8");
  hostProc.stdout.on("data", (chunk) => {
    buffered += chunk;
    let nl;
    while ((nl = buffered.indexOf("\n")) >= 0) {
      const line = buffered.slice(0, nl).trim();
      buffered = buffered.slice(nl + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.id !== undefined) {
          responses.set(msg.id, msg);
          waiters.get(msg.id)?.(msg);
        }
      } catch {
        // ignore protocol noise
      }
    }
  });

  let nextId = 1;
  const send = (msg) => hostProc.stdin.write(`${JSON.stringify(msg)}\n`);
  const request = (method, params) => {
    const id = nextId++;
    send({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => {
      if (responses.has(id)) return resolve(responses.get(id));
      const timer = setTimeout(() => {
        waiters.delete(id);
        reject(new Error(`timeout waiting for MCP response id=${id} (${method})`));
      }, 15_000);
      waiters.set(id, (msg) => {
        clearTimeout(timer);
        waiters.delete(id);
        resolve(msg);
      });
    });
  };

  await request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "multi-session-recall-guard-test", version: "1.0.0" },
  });
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  return {
    hostPid: hostProc.pid,
    callTool: async (name, args = {}) => {
      const resp = await request("tools/call", { name, arguments: args });
      return resp.result;
    },
  };
}

test("Slice S1 (H6c): two concurrent MCP sessions isolate recall guard arming, consult disarm, and lifecycle state", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-msp-s1-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "claudecode-vault");
  const home = path.join(root, "home");
  await mkdir(vault, { recursive: true });
  await mkdir(home, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const daemon = await startFakeDaemon(socketPath);
  t.after(() => daemon.close());

  const serverA = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });
  const serverB = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });
  assert.notEqual(serverA.hostPid, serverB.hostPid);

  const sessionA = "session-alpha-111";
  const sessionB = "session-bravo-222";
  const keyA = sessionStateKey(sessionA);
  const keyB = sessionStateKey(sessionB);
  assert.ok(keyA && keyB && keyA !== keyB);

  // Record active-session hints for both live host PIDs
  recordActiveSessionWorkspace(vault, "/tmp/project-alpha", {
    agentId: "claude-code",
    hostPid: serverA.hostPid,
    sessionId: sessionA,
  });
  recordActiveSessionWorkspace(vault, "/tmp/project-bravo", {
    agentId: "claude-code",
    hostPid: serverB.hostPid,
    sessionId: sessionB,
  });

  // Verify R1 ancestor PID walk resolves even when an intermediate wrapper PID sits between MCP and host
  const ancestorResolved = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: 999991,
    checkHostPidAlive: false,
    parentPidResolver: (pid) => (pid === 999991 ? serverA.hostPid : undefined),
  });
  assert.equal(ancestorResolved.sessionId, sessionA);
  assert.equal(ancestorResolved.sessionKey, keyA);
  assert.equal(ancestorResolved.matchedHintPid, serverA.hostPid);

  // Arm both sessions with unconsumed recall states
  await writeRecallState(
    vault,
    {
      task_signature: "sig-alpha",
      intent: "recall",
      top_hits: [{ title: "Alpha Spec", wikilink: "[[wiki/alpha-spec]]", score: 0.92 }],
      top_score: 0.92,
      session_id: sessionA,
    },
    { sessionId: sessionA, guardV2: true },
  );
  await writeRecallState(
    vault,
    {
      task_signature: "sig-bravo",
      intent: "recall",
      top_hits: [{ title: "Bravo Spec", wikilink: "[[wiki/bravo-spec]]", score: 0.95 }],
      top_score: 0.95,
      session_id: sessionB,
    },
    { sessionId: sessionB, guardV2: true },
  );

  // Server A calls minni_recall -> disarms Session A ONLY, leaving Session B armed
  await serverA.callTool("minni_recall", { query: "alpha architecture" });

  const stateAAfterConsult = await readRecallState(vault, { sessionId: sessionA, guardV2: true });
  const stateBAfterConsult = await readRecallState(vault, { sessionId: sessionB, guardV2: true });
  assert.equal(stateAAfterConsult?.consumed, true, "Session A must be disarmed after serverA minni_recall");
  assert.equal(stateBAfterConsult?.consumed, false, "Session B must remain armed after serverA minni_recall");

  // PreToolUse in strict mode: Session A allows cold Grep; Session B denies with its own pointer
  const handlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "workspace-msp",
    contextWindow: 200_000,
    auditPrefix: "hook_msp_s1",
    hooksEnabled: true,
    recallGuardMode: "strict",
    lifecycleEnabled: true,
    wire: claudeCodeWire,
  });

  const preToolA = await handlers.handlePreToolUse({
    session_id: sessionA,
    tool_name: "Grep",
    tool_input: { pattern: "alpha" },
  });
  assert.equal(
    preToolA?.hookSpecificOutput,
    undefined,
    "Session A cold Grep must pass cleanly without guard surfacing because serverA already consulted minni_recall",
  );

  const preToolB = await handlers.handlePreToolUse({
    session_id: sessionB,
    tool_name: "Grep",
    tool_input: { pattern: "bravo" },
  });
  assert.equal(
    typeof preToolB?.hookSpecificOutput?.additionalContext,
    "string",
    "Session B cold Grep must still trigger recall guard because Session B never consulted minni_recall",
  );
  assert.match(
    String(preToolB?.hookSpecificOutput?.additionalContext ?? ""),
    /Bravo Spec|bravo-spec/,
  );

  // Lifecycle state isolation: SessionStart in Session B resets Session B's lifecycle state without wiping Session A's
  await handlers.handleSessionStart({ session_id: sessionA, cwd: "/tmp/project-alpha" });
  await handlers.handleUserPromptSubmit({
    session_id: sessionA,
    prompt: "implement the new storage layout parser",
  });
  const lifeABeforeBBoot = await readLifecycleState(vault, { sessionId: sessionA });
  assert.ok(
    Array.isArray(lifeABeforeBBoot?.emphasized) && lifeABeforeBBoot.emphasized.length > 0,
    "Session A should record emphasized lifecycle surface",
  );

  await handlers.handleSessionStart({ session_id: sessionB, cwd: "/tmp/project-bravo" });
  const lifeAAfterBBoot = await readLifecycleState(vault, { sessionId: sessionA });
  const lifeBAfterBBoot = await readLifecycleState(vault, { sessionId: sessionB });
  assert.deepEqual(
    lifeAAfterBBoot?.emphasized,
    lifeABeforeBBoot?.emphasized,
    "Session B boot must not wipe Session A lifecycle emphasis",
  );
  assert.deepEqual(lifeBAfterBBoot?.emphasized, []);

  // R1 (§9): Session C started outside any git repository (workspace-unknown / disallowed hint path)
  // still writes its session identity record and never resolves to Session A's session_key.
  const sessionC = "session-charlie-non-repo";
  const keyC = sessionStateKey(sessionC);
  await handlers.handleSessionStart({
    session_id: sessionC,
    cwd: "workspace-unknown",
    host_pid: 999993,
  });
  const resolvedC = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: 999993,
    checkHostPidAlive: false,
  });
  assert.equal(resolvedC.sessionId, sessionC);
  assert.equal(resolvedC.sessionKey, keyC);
  assert.notEqual(resolvedC.sessionKey, keyA, "Session C outside a repo must never borrow Session A's key");

  // R2 (§9): Foreign host environment variable (CLAUDE_CODE_SESSION_ID) is ignored when agent is codex
  const prevClaudeSession = process.env.CLAUDE_CODE_SESSION_ID;
  process.env.CLAUDE_CODE_SESSION_ID = "foreign-claude-session-999";
  try {
    const codexResolved = resolveMcpSessionIdentity(vault, {
      agentId: "codex",
      hostPid: 999994,
      checkHostPidAlive: false,
    });
    assert.notEqual(
      codexResolved.sessionId,
      "foreign-claude-session-999",
      "R2: codex agent must ignore inherited CLAUDE_CODE_SESSION_ID",
    );
  } finally {
    if (prevClaudeSession === undefined) delete process.env.CLAUDE_CODE_SESSION_ID;
    else process.env.CLAUDE_CODE_SESSION_ID = prevClaudeSession;
  }
});
