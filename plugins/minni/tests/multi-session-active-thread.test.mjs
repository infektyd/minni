// Slice S2 (H4, Q1 = Option A): Per-session active thread pointers & minni_thread_create({ activate }).
// Spawns three real MCP server child processes under distinct live host PIDs against a single vault.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { recordActiveSessionWorkspace } from "../dist/config.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";

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
        socket.write(
          `${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { ok: true } })}\n`,
        );
      }
    });
  });
  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  return daemon;
}

async function spawnHostMcpSession(t, { home, socketPath, vaultPath }) {
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
      MINNI_AGENT_ID: "claude-code",
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
    clientInfo: { name: "multi-session-active-thread-test", version: "1.0.0" },
  });
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  return {
    hostPid: hostProc.pid,
    callToolJson: async (name, args = {}) => {
      const resp = await request("tools/call", { name, arguments: args });
      const text = resp.result?.content?.[0]?.text ?? "{}";
      return JSON.parse(text);
    },
  };
}

function extractEnvelopeBody(hookOutput) {
  const ctx =
    hookOutput?.hookSpecificOutput?.additionalContext ??
    hookOutput?.systemMessage ??
    "";
  const match = /<minni:context[^>]*>\s*([\s\S]*?)\s*<\/minni:context>/.exec(ctx);
  assert.ok(match, `expected <minni:context> in hook output, got: ${ctx}`);
  return JSON.parse(match[1]);
}

test("Slice S2 (H4, Q1 = Option A): concurrent MCP sessions maintain independent active threads, hooks inject per-session active_thread_ref, and unattached third session fails closed", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-msp-s2-"));
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
  const serverC = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });

  const sessionA = "session-s2-alpha";
  const sessionB = "session-s2-bravo";
  const sessionC = "session-s2-charlie";

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
  recordActiveSessionWorkspace(vault, "/tmp/project-charlie", {
    agentId: "claude-code",
    hostPid: serverC.hostPid,
    sessionId: sessionC,
  });

  // 1. serverA creates Plan A
  const createdA = await serverA.callToolJson("minni_thread_create", {
    goal: "Coordinator Plan A",
    slices: [{ id: "s1", title: "Slice A1" }],
  });
  const planA = createdA.plan_id;
  assert.ok(planA, "serverA must create planA");
  assert.equal(createdA.displaced_active, undefined);

  const handlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "workspace-s2",
    contextWindow: 200_000,
    auditPrefix: "hook_msp_s2",
    hooksEnabled: true,
    wire: claudeCodeWire,
  });

  // Q1 = Option A: When Session B boots while Session A is actively running Plan A,
  // Session B gets a clean slate (no active_thread) and a 1-line awareness entry in concurrent_active_threads!
  const bootB = await handlers.handleSessionStart({
    session_id: sessionB,
    cwd: "/tmp/project-bravo",
    host_pid: serverB.hostPid,
  });
  const bootBBody = extractEnvelopeBody(bootB);
  assert.equal(
    bootBBody.active_thread,
    undefined,
    "Q1 Option A: Session B must not auto-attach to Session A's live plan at SessionStart",
  );
  assert.ok(
    Array.isArray(bootBBody.concurrent_active_threads) &&
      bootBBody.concurrent_active_threads.some((item) => item.plan_id === planA),
    "Q1 Option A: Session B must receive concurrent_active_threads listing Plan A",
  );

  // 2. serverB creates Plan B (with default activate: true) -> must NOT displace Session A's Plan A!
  const createdB = await serverB.callToolJson("minni_thread_create", {
    goal: "Worker Plan B",
    slices: [{ id: "b1", title: "Slice B1" }],
  });
  const planB = createdB.plan_id;
  assert.ok(planB && planB !== planA);
  assert.equal(
    createdB.displaced_active,
    undefined,
    "serverB had no prior plan of its own, so creating planB must not report displaced_active",
  );

  // 2b. serverA creates a backlog Plan D with activate: false -> must not change serverA's active planA
  const createdBacklog = await serverA.callToolJson("minni_thread_create", {
    goal: "Backlog Plan D",
    slices: [{ id: "d1", title: "Slice D1" }],
    activate: false,
  });
  assert.ok(createdBacklog.plan_id && createdBacklog.plan_id !== planA);

  // Assert 1: serverA id-less minni_thread_status and minni_thread_update target planA (NOT planB or planD)
  const statusA = await serverA.callToolJson("minni_thread_status", {});
  assert.equal(statusA.view?.goal, "Coordinator Plan A");
  assert.equal(statusA.active, true);

  const updatedA = await serverA.callToolJson("minni_thread_update", {
    slice_id: "s1",
    status: "in_progress",
  });
  assert.equal(updatedA.plan?.plan_id, planA);

  // Assert 2: serverB id-less minni_thread_status resolves to planB
  const statusB = await serverB.callToolJson("minni_thread_status", {});
  assert.equal(statusB.view?.goal, "Worker Plan B");
  assert.equal(statusB.active, true);

  // Assert 3: UserPromptSubmit hook for sessionA injects active_thread_ref for planA; sessionB injects planB
  const upsA = await handlers.handleUserPromptSubmit({
    session_id: sessionA,
    prompt: "continue working on the coordinator slice",
  });
  const upsABody = extractEnvelopeBody(upsA);
  assert.equal(upsABody.active_thread_ref?.plan_id, planA);

  const upsB = await handlers.handleUserPromptSubmit({
    session_id: sessionB,
    prompt: "continue working on the worker slice",
  });
  const upsBBody = extractEnvelopeBody(upsB);
  assert.equal(upsBBody.active_thread_ref?.plan_id, planB);

  // Assert 4: serverB calling minni_thread_deactivate deactivates planB for sessionB while planA stays active for sessionA
  const deactB = await serverB.callToolJson("minni_thread_deactivate", {});
  assert.equal(deactB.active, null);

  const statusBAfterDeact = await serverB.callToolJson("minni_thread_status", {});
  assert.ok(statusBAfterDeact.error, "serverB id-less status after deactivate must fail closed");

  const statusAAfterBDeact = await serverA.callToolJson("minni_thread_status", {});
  assert.equal(statusAAfterBDeact.view?.goal, "Coordinator Plan A");
  assert.equal(statusAAfterBDeact.active, true);

  // Assert 5: Third fresh session serverC (with no active plan of its own while serverA is live)
  // calling id-less minni_thread_update fails closed with disambiguation error rather than mutating planA
  const updateC = await serverC.callToolJson("minni_thread_update", {
    slice_id: "s1",
    status: "blocked",
  });
  assert.ok(updateC.error, "serverC id-less update must return an error");
  assert.match(updateC.error, new RegExp(planA), "disambiguation error must name live planA");
});
