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
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { recordActiveSessionWorkspace } from "../dist/config.js";
import { discardDeliveryCommits, runDeliveryCommits } from "../dist/hook-delivery.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import { createPlan } from "../dist/plan.js";
import {
  MAX_RELAY_CURSORS,
  RELAY_LOCK_TIMEOUT_MS,
  advanceCursor,
  confirmDelivery,
  emptyRelayStore,
  forkSessionCursor,
  formatSessionSubscriberId,
  ingestJournalEvents,
  loadRelayStore,
  relayDir,
  saveRelayStore,
  sweepDeadRelayCursors,
  withRelayStoreLock,
} from "../dist/thread-notification-relay.js";
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

async function spawnHostMcpSession(t, { home, socketPath, vaultPath, agentId = "claude-code" }) {
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
      MINNI_CODEX_VAULT_PATH: vaultPath,
      MINNI_AGENT_ID: agentId,
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

    // 1. serverA creates planA (automatically activated for sessionA); serverB attaches via minni_thread_update({ activate: true })
    const created = await serverA.callToolJson("minni_thread_create", {
      goal: "Shared Multi-Session Plan",
      slices: [
        { id: "s1", title: "Worker Slice S1" },
        { id: "s2", title: "Peer Slice S2" },
      ],
    });
    const planA = created.plan_id;
    assert.ok(planA, `serverA must create planA, got: ${JSON.stringify(created)}`);

    const activatedB = await serverB.callToolJson("minni_thread_update", {
      plan_id: planA,
      activate: true,
    });
    assert.equal(activatedB.active, true);
    assert.equal(activatedB.plan_id, planA);

    // 2. Worker MCP session ("codex") completes slice s1 via minni_thread_update
    const serverWorker = await spawnHostMcpSession(t, {
      home,
      socketPath,
      vaultPath: vault,
      agentId: "codex",
    });
    const completedS1 = await serverWorker.callToolJson("minni_thread_update", {
      plan_id: planA,
      slice_id: "s1",
      status: "done",
      evidence: "s1 finished by codex worker",
    });
    assert.equal(completedS1.updated_slice?.status, "done");

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
          (item) => item.kind === "status_changed" && item.slice_id === "s1",
        ),
      `sessionA must receive status_changed for s1, got: ${JSON.stringify(bodyA1.pending_attention)}`,
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

    // 4. Assert: sessionB runs UserPromptSubmit and STILL receives status_changed for s1!
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
          (item) => item.kind === "status_changed" && item.slice_id === "s1",
        ),
      `sessionB must still receive status_changed for s1 after sessionA confirmed it! Got: ${JSON.stringify(bodyB1.pending_attention)}`,
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

    // 7. Regression check (Finding 2):
    // - serverA calls minni_thread_update on s2 (stamps actor_session_key and auto-acks for sessionA).
    // - Before sessionB runs UserPromptSubmit, serverB records its own scar via minni_thread_scar.
    //   serverB's auto-ack of its own scar must NOT wipe out serverA's unconfirmed status_changed notification in sessionB's queue!
    const updateS2Res = await serverA.callToolJson("minni_thread_update", {
      plan_id: planA,
      slice_id: "s2",
      status: "in_progress",
    });
    assert.ok(updateS2Res.rev >= 3, `expected update rev >= 3, got: ${JSON.stringify(updateS2Res)}`);

    const scarBRes = await serverB.callToolJson("minni_thread_scar", {
      plan_id: planA,
      kind: "dead_end",
      signal: "sessionB noted local dead_end",
      resolution: "preserve sibling status_changed event in queue",
    });
    assert.ok(scarBRes.rev >= 4);

    // sessionA must NOT self-notify on its own status_changed, but SHOULD receive sessionB's scar_added
    discardDeliveryCommits();
    const upsAfterReplanA = await handlers.handleUserPromptSubmit({
      session_id: sessionA,
      prompt: "sessionA turn after s2 update and sessionB scar",
    });
    await runDeliveryCommits();
    const bodyAfterReplanA = extractEnvelopeBody(upsAfterReplanA);
    assert.ok(
      Array.isArray(bodyAfterReplanA.pending_attention) &&
        bodyAfterReplanA.pending_attention.some((item) => item.kind === "scar_added") &&
        !bodyAfterReplanA.pending_attention.some((item) => item.kind === "status_changed"),
      `sessionA must auto-ack its own status_changed while receiving sessionB's scar_added! Got: ${JSON.stringify(bodyAfterReplanA.pending_attention)}`,
    );

    // sessionB must receive sessionA's status_changed (not wiped by sessionB's subsequent scar auto-ack) and NOT self-notify on its own scar
    discardDeliveryCommits();
    const upsAfterReplanB = await handlers.handleUserPromptSubmit({
      session_id: sessionB,
      prompt: "sessionB turn after sessionA s2 update and own scar",
    });
    await runDeliveryCommits();
    const bodyAfterReplanB = extractEnvelopeBody(upsAfterReplanB);
    assert.ok(
      Array.isArray(bodyAfterReplanB.pending_attention) &&
        bodyAfterReplanB.pending_attention.some(
          (item) => item.kind === "status_changed" && item.slice_id === "s2",
        ) &&
        !bodyAfterReplanB.pending_attention.some((item) => item.kind === "scar_added"),
      `sessionB must receive sessionA's status_changed and auto-ack its own scar! Got: ${JSON.stringify(bodyAfterReplanB.pending_attention)}`,
    );
  },
);

test("withRelayStoreLock fails closed on live contention, forkSessionCursor stamps updated_at, and MAX_RELAY_CURSORS protects bare agents + live/pending cursors", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-relay-lock-cap-"));
  const vault = path.join(root, "claudecode-vault");
  await ensureVault(vault, { agentId: "claude-code" });
  t.after(async () => {
    await rm(root, { recursive: true, force: true });
  });

  // 1. RELAY_LOCK_TIMEOUT_MS is 5_000ms and withRelayStoreLock fails closed (never runs fn unlocked!)
  assert.equal(RELAY_LOCK_TIMEOUT_MS, 5_000);
  const initialStore = emptyRelayStore();
  initialStore.cursors.push(
    advanceCursor(
      { subscriber_id: "claude-code", plan_id: "plan-sentinel", last_delivered_seq: 0 },
      7,
      true,
    ),
  );
  await saveRelayStore(vault, initialStore);

  const lockDir = path.join(relayDir(vault), "cursors.lock");
  await mkdir(lockDir, { mode: 0o700 });
  await writeFile(
    path.join(lockDir, "owner.json"),
    JSON.stringify({ pid: process.pid, acquiredAt: new Date().toISOString() }),
    "utf8",
  );

  let unlockedCallbackRan = false;
  await assert.rejects(
    () =>
      withRelayStoreLock(
        vault,
        async () => {
          unlockedCallbackRan = true;
          await saveRelayStore(vault, emptyRelayStore());
        },
        { timeoutMs: 180 },
      ),
    /Timed out acquiring thread relay store lock/,
  );
  assert.equal(unlockedCallbackRan, false, "withRelayStoreLock must NEVER run fn() when lock times out");
  const preservedStore = await loadRelayStore(vault);
  assert.equal(preservedStore.cursors.length, 1);
  assert.equal(preservedStore.cursors[0].plan_id, "plan-sentinel");

  // Release simulated lock and verify normal lock acquisition succeeds
  await rm(lockDir, { recursive: true, force: true });

  // 2. forkSessionCursor, advanceCursor, ingestJournalEvents, and confirmDelivery stamp ISO updated_at
  const cursorStore = emptyRelayStore();
  const baseCur = advanceCursor(
    { subscriber_id: "claude-code", plan_id: "plan-ts", last_delivered_seq: 0 },
    2,
    true,
  );
  assert.ok(baseCur.updated_at && Number.isFinite(Date.parse(baseCur.updated_at)));
  cursorStore.cursors.push(baseCur);

  const sessionSub = formatSessionSubscriberId("claude-code", "a1b2c3d4e5f60718");
  const forkedCur = forkSessionCursor(cursorStore, sessionSub, "plan-ts");
  assert.ok(
    forkedCur.updated_at && Number.isFinite(Date.parse(forkedCur.updated_at)),
    `forkSessionCursor must stamp valid ISO updated_at, got: ${forkedCur.updated_at}`,
  );
  cursorStore.cursors.push(forkedCur);

  const ingestedStore = ingestJournalEvents(
    cursorStore,
    "plan-ts",
    [{ seq: 3, kind: "slice_completed", actor: "codex", at: new Date().toISOString(), slice_id: "s1" }],
    [sessionSub],
  );
  const ingestedCur = ingestedStore.cursors.find((c) => c.subscriber_id === sessionSub);
  assert.ok(ingestedCur?.updated_at && Number.isFinite(Date.parse(ingestedCur.updated_at)));

  const confirmedStore = confirmDelivery(ingestedStore, sessionSub, "plan-ts", 3, true);
  const confirmedCur = confirmedStore.cursors.find((c) => c.subscriber_id === sessionSub);
  assert.ok(confirmedCur?.updated_at && Number.isFinite(Date.parse(confirmedCur.updated_at)));

  // 3. MAX_RELAY_CURSORS eviction NEVER drops bare <agent> cursors, liveSessionKeys, or cursors with pending items
  const created = await createPlan(
    {
      goal: "Active Plan For Cap Test",
      slices: [{ id: "s1", title: "Cap Slice" }],
      vaultPath: vault,
    },
    { vaultPath: vault },
  );
  const activePlanId = created.plan.plan_id;
  const capStore = emptyRelayStore();
  const oldIso = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(); // 2h old (within 24h TTL, oldest for cap)
  const midIso = new Date(Date.now() - 60 * 60 * 1000).toISOString(); // 1h old

  // Oldest bare <agent> cursors — MUST survive even with oldest timestamps!
  capStore.cursors.push({
    subscriber_id: "claude-code",
    plan_id: activePlanId,
    last_delivered_seq: 1,
    updated_at: "2020-01-01T00:00:00.000Z",
  });
  capStore.cursors.push({
    subscriber_id: "codex",
    plan_id: activePlanId,
    last_delivered_seq: 1,
    updated_at: "2020-01-01T00:00:01.000Z",
  });

  // Live session cursor (backed by live active-thread.d pointer on disk) — MUST survive even with ancient cursor updated_at!
  const liveKey = "1111222233334444";
  const liveSub = formatSessionSubscriberId("claude-code", liveKey);
  const activeThreadDir = path.join(vault, ".runtime", "active-thread.d");
  await mkdir(activeThreadDir, { recursive: true });
  await writeFile(
    path.join(activeThreadDir, `${liveKey}.json`),
    JSON.stringify({
      plan_id: activePlanId,
      path: `wiki/artifacts/${activePlanId}.md`,
      session_key: liveKey,
      host_pid: process.pid,
      set_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }),
    "utf8",
  );
  capStore.cursors.push({
    subscriber_id: liveSub,
    plan_id: activePlanId,
    last_delivered_seq: 1,
    updated_at: "2020-01-01T00:00:02.000Z",
  });

  // Inactive session cursor WITH pending notification — MUST survive!
  const pendingKey = "5555666677778888";
  const pendingSub = formatSessionSubscriberId("claude-code", pendingKey);
  capStore.cursors.push({
    subscriber_id: pendingSub,
    plan_id: activePlanId,
    last_delivered_seq: 0,
    updated_at: oldIso,
  });
  capStore.pending.push({
    id: `${activePlanId}:1:${pendingSub}`,
    plan_id: activePlanId,
    seq: 1,
    subscriber_id: pendingSub,
    kind: "slice_completed",
    actor: "codex",
    created_at: oldIso,
    summary: "s1 done",
  });

  // Fill 260 additional inactive composite session cursors (> MAX_RELAY_CURSORS = 256)
  for (let i = 0; i < 260; i++) {
    const hexKey = i.toString(16).padStart(16, "a");
    capStore.cursors.push({
      subscriber_id: formatSessionSubscriberId("claude-code", hexKey),
      plan_id: activePlanId,
      last_delivered_seq: 1,
      updated_at: midIso,
    });
  }
  await saveRelayStore(vault, capStore);

  await sweepDeadRelayCursors(vault, Date.now());

  const afterCap = await loadRelayStore(vault);
  assert.equal(afterCap.cursors.length, MAX_RELAY_CURSORS);
  assert.ok(
    afterCap.cursors.some((c) => c.subscriber_id === "claude-code"),
    "bare 'claude-code' cursor must NEVER be evicted by MAX_RELAY_CURSORS",
  );
  assert.ok(
    afterCap.cursors.some((c) => c.subscriber_id === "codex"),
    "bare 'codex' cursor must NEVER be evicted by MAX_RELAY_CURSORS",
  );
  assert.ok(
    afterCap.cursors.some((c) => c.subscriber_id === liveSub),
    "liveSessionKeys cursor must NEVER be evicted by MAX_RELAY_CURSORS",
  );
  assert.ok(
    afterCap.cursors.some((c) => c.subscriber_id === pendingSub),
    "cursor with non-empty pending queue must NEVER be evicted by MAX_RELAY_CURSORS",
  );
});
