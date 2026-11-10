/**
 * Slice S3 (H6b, Q2 = Option A): Session-Scoped Thread Notification Relay Cursors & Atomic Store.
 *
 * Spawns real dist/server.js MCP server child processes under distinct live host PIDs:
 * 1. serverA and serverB (both "claude-code") share one vault and both attach to planA.
 * 2. A worker ("codex") claims and completes slice s1 on planA via minni_thread_worker_update.
 * 3. sessionA runs UserPromptSubmit, receiving and acknowledging s1 completion.
 * 4. Assert: sessionB subsequently runs UserPromptSubmit and STILL receives s1 completion!
 * 5. Next, serverA calls minni_thread_scar on planA.
 * 6. Assert (Q2 = Option A): the scar is auto-acked as self-authored for sessionA,
 *    but delivered as a pending notification to sessionB!
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { recordActiveSessionWorkspace } from "../dist/config.js";
import { discardDeliveryCommits, runDeliveryCommits } from "../dist/hook-delivery.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import { ensureVault } from "../dist/vault.js";

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
      CLAUDE_CODE_SESSION_ID: "",
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
    clientInfo: { name: "multi-session-relay-test", version: "1.0.0" },
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

test(
  "Slice S3 (H6b, Q2 = Option A): session-scoped relay cursors deliver worker events to both attached sessions independently and notify sibling session on same-agent scar/update",
  { timeout: 60_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "minni-msp-s3-"));
    t.after(async () => {
      discardDeliveryCommits();
      await rm(root, { recursive: true, force: true });
    });

    const vault = path.join(root, "claudecode-vault");
    const home = path.join(root, "home");
    await mkdir(vault, { recursive: true });
    await mkdir(home, { recursive: true });
    await ensureVault(vault);
    const socketPath = path.join(home, "minnid.sock");
    const daemon = await startFakeDaemon(socketPath);
    t.after(() => daemon.close());

    const serverA = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });
    const serverB = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });

    const sessionA = "session-alpha-s3-1111";
    const sessionB = "session-bravo-s3-2222";

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

    // 1. serverA creates planA (automatically activated for sessionA); serverB attaches via minni_thread_activate
    const created = await serverA.callToolJson("minni_thread_create", {
      goal: "Shared Multi-Session Plan",
      slices: [
        { id: "s1", title: "Worker Slice S1" },
        { id: "s2", title: "Peer Slice S2" },
      ],
    });
    const planA = created.plan_id;
    assert.ok(planA, `serverA must create planA, got: ${JSON.stringify(created)}`);

    const activatedB = await serverB.callToolJson("minni_thread_activate", {
      plan_id: planA,
    });
    assert.equal(activatedB.active, planA);

    // 2. Assign, claim, start, and complete slice s1 as worker "codex"
    await serverA.callToolJson("minni_thread_assign", {
      plan_id: planA,
      slice_id: "s1",
      worker_agent_id: "codex",
    });
    const claimed = await serverA.callToolJson("minni_thread_claim", {
      plan_id: planA,
      slice_id: "s1",
      worker_agent_id: "codex",
      idempotency_key: "claim-s1-key",
    });
    assert.ok(claimed.token, `expected claimed.token, got: ${JSON.stringify(claimed)}`);

    await serverA.callToolJson("minni_thread_worker_update", {
      plan_id: planA,
      slice_id: "s1",
      worker_agent_id: "codex",
      claim_token: claimed.token,
      idempotency_key: "start-s1-key",
      action: "start",
    });
    await serverA.callToolJson("minni_thread_worker_update", {
      plan_id: planA,
      slice_id: "s1",
      worker_agent_id: "codex",
      claim_token: claimed.token,
      idempotency_key: "complete-s1-key",
      action: "complete",
      evidence: "s1 finished by codex worker",
    });

    const handlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "workspace-s3",
      contextWindow: 200_000,
      hooksEnabled: true,
      auditPrefix: "hook_claude",
      wire: claudeCodeWire,
      sessionStartHookTimeoutMs: 5_000,
      promptHookTimeoutMs: 5_000,
    });

    // 3. sessionA runs UserPromptSubmit -> receives and confirms s1 completion
    discardDeliveryCommits();
    const upsA1 = await handlers.handleUserPromptSubmit({
      session_id: sessionA,
      prompt: "check progress on shared plan",
    });
    await runDeliveryCommits();
    const bodyA1 = extractEnvelopeBody(upsA1);
    assert.ok(
      Array.isArray(bodyA1.pending_attention) &&
        bodyA1.pending_attention.some(
          (item) => item.kind === "slice.completed" && item.slice_id === "s1",
        ),
      `sessionA must receive slice.completed for s1, got: ${JSON.stringify(bodyA1.pending_attention)}`,
    );

    // Verify sessionA's cursor is now caught up (a second UPS on sessionA has no pending_attention)
    discardDeliveryCommits();
    const upsA2 = await handlers.handleUserPromptSubmit({
      session_id: sessionA,
      prompt: "follow up turn on sessionA",
    });
    await runDeliveryCommits();
    const bodyA2 = extractEnvelopeBody(upsA2);
    assert.equal(
      bodyA2.pending_attention,
      undefined,
      "sessionA must not re-receive already-confirmed notifications",
    );

    // 4. Assert: sessionB runs UserPromptSubmit and STILL receives slice.completed for s1!
    discardDeliveryCommits();
    const upsB1 = await handlers.handleUserPromptSubmit({
      session_id: sessionB,
      prompt: "check progress from sessionB window",
    });
    await runDeliveryCommits();
    const bodyB1 = extractEnvelopeBody(upsB1);
    assert.ok(
      Array.isArray(bodyB1.pending_attention) &&
        bodyB1.pending_attention.some(
          (item) => item.kind === "slice.completed" && item.slice_id === "s1",
        ),
      `sessionB must still receive slice.completed for s1 after sessionA confirmed it! Got: ${JSON.stringify(bodyB1.pending_attention)}`,
    );

    // 5. Next (Q2 = Option A): serverA records a scar on planA via minni_thread_scar
    const scarRes = await serverA.callToolJson("minni_thread_scar", {
      plan_id: planA,
      kind: "dead_end",
      signal: "legacy approach deadlocked under lock contention",
      resolution: "use per-session cursor keys",
    });
    assert.ok(scarRes.rev >= 2);

    // 6. Assert (Q2 = Option A):
    // - For sessionA (the author of the scar), the scar is auto-acked as self-authored (no self-notification).
    discardDeliveryCommits();
    const upsAfterScarA = await handlers.handleUserPromptSubmit({
      session_id: sessionA,
      prompt: "continuing in sessionA after recording scar",
    });
    await runDeliveryCommits();
    const bodyAfterScarA = extractEnvelopeBody(upsAfterScarA);
    assert.equal(
      bodyAfterScarA.pending_attention,
      undefined,
      "sessionA must auto-ack its own scar_added event (never self-notify)",
    );

    // - For sessionB (the other open session of the same agent), scar_added IS delivered as pending_attention!
    discardDeliveryCommits();
    const upsAfterScarB = await handlers.handleUserPromptSubmit({
      session_id: sessionB,
      prompt: "continuing in sessionB after sessionA recorded a scar",
    });
    await runDeliveryCommits();
    const bodyAfterScarB = extractEnvelopeBody(upsAfterScarB);
    assert.ok(
      Array.isArray(bodyAfterScarB.pending_attention) &&
        bodyAfterScarB.pending_attention.some((item) => item.kind === "scar_added"),
      `Q2 = Option A: sessionB must receive scar_added authored by sibling sessionA! Got: ${JSON.stringify(bodyAfterScarB.pending_attention)}`,
    );
  },
);
