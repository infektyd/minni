import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { discardDeliveryCommits } from "../dist/hook-delivery.js";
import {
  activePointerPath,
  activeThreadSessionPath,
  createPlan,
  setActivePlan,
  sweepDeadActiveThreadPointers,
} from "../dist/plan.js";
import { sessionStateKey } from "../dist/recall-state.js";
import { stashPrecompactReassert } from "../dist/daemon_transport.js";
import {
  ensureSessionRelayCursor,
  formatSessionSubscriberId,
  ingestJournalIntoVault,
  loadRelayStore,
} from "../dist/thread-notification-relay.js";
import { ensureVault, readReassertPending } from "../dist/vault.js";

function startFakeContradictionDaemon(socketPath, events) {
  return new Promise((resolve, reject) => {
    const server = net.createServer((conn) => {
      let buf = "";
      conn.on("data", (chunk) => {
        buf += chunk.toString("utf8");
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          let req;
          try {
            req = JSON.parse(line);
          } catch {
            continue;
          }
          const id = req.id ?? 1;
          if (
            req.method === "minni_subscribe_contradictions" ||
            req.method === "subscribe_contradictions"
          ) {
            conn.write(
              JSON.stringify({
                jsonrpc: "2.0",
                id,
                result: { ok: true, events },
              }) + "\n",
            );
          } else if (req.method === "read") {
            conn.write(
              JSON.stringify({
                jsonrpc: "2.0",
                id,
                result: { context: "## Agent Identity: claude-code\nCore.\n" },
              }) + "\n",
            );
          } else if (req.method === "search") {
            conn.write(
              JSON.stringify({
                jsonrpc: "2.0",
                id,
                result: { results: [], count: 0, episodic: [], episodic_count: 0 },
              }) + "\n",
            );
          } else {
            conn.write(
              JSON.stringify({
                jsonrpc: "2.0",
                id,
                result: { status: "ok", ok: true },
              }) + "\n",
            );
          }
        }
      });
    });
    server.on("error", reject);
    server.listen(socketPath, () => resolve(server));
  });
}

function parseEnvelopeFromHookOutput(out) {
  const ctx = out?.hookSpecificOutput?.additionalContext ?? "";
  const match =
    ctx.match(/<minni:context[^>]*>\s*([\s\S]*?)\s*<\/minni:context>/) ??
    ctx.match(/```json\n([\s\S]*?)\n```/);
  assert.ok(match, `expected JSON envelope in additionalContext, got: ${ctx}`);
  return JSON.parse(match[1]);
}

test("Slice S4 (V3): Session B SessionStart does not steal Session A's fresh PreCompact reassert stash; orphaned (>10m) stashes are recovered", async (t) => {
  const tmpRoot = await mkdtemp(path.join(tmpdir(), "minni-s4-reassert-"));
  const vaultPath = path.join(tmpRoot, "claudecode-vault");
  const socketPath = path.join(tmpRoot, "minnid.sock");
  await ensureVault(vaultPath, { agentId: "claude-code" });

  const sampleCorrection = {
    event_id: 42,
    superseded_learning_id: 10,
    new_learning_id: 11,
    originating_agent: "claude-code",
    created_at: 1700000000,
  };
  const daemon = await startFakeContradictionDaemon(socketPath, [sampleCorrection]);
  const prevSocket = process.env.MINNI_SOCKET_PATH;
  process.env.MINNI_SOCKET_PATH = socketPath;

  t.after(async () => {
    if (prevSocket === undefined) delete process.env.MINNI_SOCKET_PATH;
    else process.env.MINNI_SOCKET_PATH = prevSocket;
    await new Promise((r) => daemon.close(r));
    await rm(tmpRoot, { recursive: true, force: true });
  });

  const handlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "ws-s4",
    contextWindow: 200_000,
    hooksEnabled: true,
    runtime: "claude-code",
    hookScript: "hook.js",
    auditPrefix: "hook_cc",
    bootIdentity: "identity-recall",
  });

  const sessionA = "sess-a-compacting";
  const sessionB = "sess-b-concurrent";

  // 1. Session A runs PreCompact and stashes sampleCorrection in inbox.
  discardDeliveryCommits();
  await handlers.handlePreCompact({
    session_id: sessionA,
    trigger: "auto",
  });

  const inboxDir = path.join(vaultPath, "inbox");
  const filesAfterPreCompact = (await readdir(inboxDir)).filter((f) => f.endsWith(".json"));
  assert.equal(filesAfterPreCompact.length, 1, "Session A PreCompact must write 1 inbox stash");
  const stashPayload = JSON.parse(
    await readFile(path.join(inboxDir, filesAfterPreCompact[0]), "utf8"),
  );
  assert.equal(stashPayload.session_id, sessionA);
  assert.equal(stashPayload.session_key, sessionStateKey(sessionA));

  // 2. Session B boots 100ms later: must NOT steal Session A's fresh stash!
  discardDeliveryCommits();
  const bootB = await handlers.handleSessionStart({
    session_id: sessionB,
  });
  const envB = parseEnvelopeFromHookOutput(bootB);
  assert.equal(
    envB.corrections_reassert,
    undefined,
    "Session B must not steal Session A's fresh PreCompact reassert stash",
  );

  // 2b. Unlabeled SessionStart (no session_id) within 10m TTL must ALSO NOT steal Session A's fresh stash!
  const unlabeledPending = await readReassertPending(vaultPath, 3);
  assert.equal(
    unlabeledPending.length,
    0,
    "Unlabeled SessionStart must not steal a fresh (<10m) session-tagged PreCompact stash",
  );
  const filesAfterBootB = (await readdir(inboxDir)).filter((f) => f.endsWith(".json"));
  assert.equal(
    filesAfterBootB.length,
    1,
    "Session A's PreCompact stash must remain in inbox after Session B boots",
  );

  // 3. Session A re-boots after compaction: must receive its own corrections_reassert!
  discardDeliveryCommits();
  const bootA = await handlers.handleSessionStart({
    session_id: sessionA,
  });
  const envA = parseEnvelopeFromHookOutput(bootA);
  assert.ok(
    Array.isArray(envA.corrections_reassert),
    "Session A must receive its stashed corrections_reassert on post-compaction boot",
  );
  assert.equal(envA.corrections_reassert.length, 1);
  assert.equal(envA.corrections_reassert[0].event_id, 42);

  // Settle Session A's consumed stash by reading readReassertPending for an orphaned stash test.
  for (const f of filesAfterBootB) {
    await rm(path.join(inboxDir, f), { force: true });
  }

  // 4. Orphaned stash (>10m old) from a dead session IS recovered by another session.
  const orphanSession = "sess-dead-orphan";
  const orphanPath = await stashPrecompactReassert({
    vaultPath,
    sessionId: orphanSession,
    agentId: "claude-code",
    staleBeliefEvents: [
      {
        event_id: 99,
        superseded_learning_id: 20,
        new_learning_id: 21,
        originating_agent: "claude-code",
      },
    ],
  });
  assert.ok(orphanPath);
  const fifteenMinAgoMs = Date.now() - 15 * 60 * 1000;
  const orphanData = JSON.parse(await readFile(orphanPath, "utf8"));
  orphanData.createdAt = new Date(fifteenMinAgoMs).toISOString();
  await writeFile(orphanPath, JSON.stringify(orphanData, null, 2), "utf8");

  const recoveredByB = await readReassertPending(vaultPath, 3, {
    sessionId: sessionB,
    sessionKey: sessionStateKey(sessionB),
  });
  assert.equal(
    recoveredByB.length,
    1,
    "Orphaned PreCompact stash (>10m old) must be recovered by the next booting session",
  );
  assert.equal(recoveredByB[0].payload.stale_belief_events[0].event_id, 99);
});

test("Slice S4 (§5.1.2 & §5.2): Dead-session GC sweep prunes expired active-thread pointers and relay cursors while preserving live sessions and all plan artifacts", async (t) => {
  const tmpRoot = await mkdtemp(path.join(tmpdir(), "minni-s4-gc-"));
  const vaultPath = path.join(tmpRoot, "claudecode-vault");
  await ensureVault(vaultPath, { agentId: "claude-code" });

  t.after(async () => {
    await rm(tmpRoot, { recursive: true, force: true });
  });

  const liveSessionId = "sess-live-gc";
  const deadSessionId = "sess-dead-gc";
  const liveKey = sessionStateKey(liveSessionId);
  const deadKey = sessionStateKey(deadSessionId);

  // Create a real plan with markdown note, journal (.log.md), and history (.history.jsonl).
  const created = await createPlan(
    {
      goal: "Verify S4 GC never deletes plan artifacts",
      slices: [{ id: "S1", title: "Slice 1", status: "pending" }],
      sessionKey: liveKey,
      sessionId: liveSessionId,
      hostPid: process.pid,
      vaultPath,
    },
    { vaultPath },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;
  const logPath = path.join(vaultPath, "wiki", "artifacts", `${planId}.log.md`);
  const historyPath = path.join(vaultPath, "wiki", "artifacts", `${planId}.history.jsonl`);

  // Ensure history file exists on disk too so we can assert §5.2 NEVER DELETE on all 3 files.
  await writeFile(historyPath, JSON.stringify({ rev: 0, plan_id: planId }) + "\n", "utf8");

  // Bind deadKey to the plan with a dead PID (999999) and an updated_at 25 hours in the past.
  await setActivePlan(vaultPath, planId, notePath, {
    sessionKey: deadKey,
    sessionId: deadSessionId,
    hostPid: 999_999,
  });
  const deadPointerFile = activeThreadSessionPath(vaultPath, deadKey);
  const livePointerFile = activeThreadSessionPath(vaultPath, liveKey);
  assert.ok(deadPointerFile && existsSync(deadPointerFile));
  assert.ok(livePointerFile && existsSync(livePointerFile));

  const nowMs = Date.now();
  const twentyFiveHoursAgoIso = new Date(nowMs - 25 * 60 * 60 * 1000).toISOString();
  const deadPointerRaw = JSON.parse(await readFile(deadPointerFile, "utf8"));
  deadPointerRaw.set_at = twentyFiveHoursAgoIso;
  deadPointerRaw.updated_at = twentyFiveHoursAgoIso;
  deadPointerRaw.host_pid = 999_999;
  await writeFile(deadPointerFile, JSON.stringify(deadPointerRaw, null, 2), "utf8");

  // Seed relay cursors and pending notifications for both liveKey and deadKey.
  const subLive = formatSessionSubscriberId("claude-code", liveKey);
  const subDead = formatSessionSubscriberId("claude-code", deadKey);
  await ensureSessionRelayCursor(vaultPath, planId, subLive);
  await ensureSessionRelayCursor(vaultPath, planId, subDead);
  await ingestJournalIntoVault(
    vaultPath,
    planId,
    [
      {
        seq: 1,
        kind: "slice_completed",
        actor: "codex",
        at: new Date(nowMs).toISOString(),
        slice_id: "S1",
      },
    ],
    ["claude-code", subLive, subDead],
  );

  const storeBefore = await loadRelayStore(vaultPath);
  assert.ok(storeBefore.cursors.some((c) => c.subscriber_id === subDead));
  assert.ok(storeBefore.cursors.some((c) => c.subscriber_id === subLive));
  assert.ok(storeBefore.pending.some((p) => p.subscriber_id === subDead));

  // Remove _active_plan.json before sweep to verify §5.1.2 preservation of last non-terminal plan_id.
  await rm(activePointerPath(vaultPath), { force: true });

  // Run the GC sweep.
  const sweepResult = await sweepDeadActiveThreadPointers(vaultPath, nowMs);
  assert.equal(sweepResult.prunedPointers, 1, "must prune the 1 dead expired session pointer");
  assert.equal(sweepResult.prunedCursors, 1, "must prune the 1 dead session relay cursor");

  // Verify dead pointer is gone and live pointer remains.
  assert.equal(existsSync(deadPointerFile), false, "dead session pointer must be removed");
  assert.equal(existsSync(livePointerFile), true, "live session pointer must remain");

  // Verify _active_plan.json was preserved before deleting the non-terminal session pointer.
  assert.equal(
    existsSync(activePointerPath(vaultPath)),
    true,
    "_active_plan.json must be preserved with the last non-terminal plan_id",
  );
  const preservedActive = JSON.parse(await readFile(activePointerPath(vaultPath), "utf8"));
  assert.equal(preservedActive.plan_id, planId);

  // Verify relay store pruned subDead cursor & pending while keeping subLive.
  const storeAfter = await loadRelayStore(vaultPath);
  assert.equal(
    storeAfter.cursors.some((c) => c.subscriber_id === subDead),
    false,
    "dead session cursor must be pruned from cursors.json",
  );
  assert.equal(
    storeAfter.pending.some((p) => p.subscriber_id === subDead),
    false,
    "dead session pending items must be pruned from cursors.json",
  );
  assert.equal(
    storeAfter.cursors.some((c) => c.subscriber_id === subLive),
    true,
    "live session cursor must remain in cursors.json",
  );
  assert.equal(
    storeAfter.pending.some((p) => p.subscriber_id === subLive),
    true,
    "live session pending items must remain in cursors.json",
  );

  // Verify §5.2 NEVER DELETE invariants: plan markdown, log, and history are untouched!
  assert.equal(existsSync(notePath), true, "plan-*.md must NEVER be deleted by GC");
  assert.equal(existsSync(logPath), true, "plan-*.log.md must NEVER be deleted by GC");
  assert.equal(existsSync(historyPath), true, "plan-*.history.jsonl must NEVER be deleted by GC");
});
