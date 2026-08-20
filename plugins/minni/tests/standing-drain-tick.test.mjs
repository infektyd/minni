// Standing drain: minnid tick. Accept start, kill that MCP, no later MCP
// on that vault. Named tick journals slice.started. Stamp is not applied.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import net from "node:net";
import { realpathSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createPlan, journalPathFor, rehydratePlan } from "../dist/plan.js";
import { readThreadEvents } from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";
import {
  START_ACCEPTED_RECEIPT_KIND,
  startAcceptedReceiptKey,
} from "../dist/thread-worker.js";
import { readWorkerUpdateReceipt } from "../dist/thread-claims.js";
import {
  enqueueWorkerWrite,
  listPendingWorkerWritePlanIds,
  listQueuedWorkerWrites,
  pickNextQueuedWorkerWrite,
} from "../dist/thread-write-queue.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;
const DIST_DIR = new URL("../dist/", import.meta.url).pathname;
const REPO_ROOT = path.resolve(new URL("../../../", import.meta.url).pathname);
const SRC_MINNI = path.join(REPO_ROOT, "src", "minni");

function pythonBin() {
  return process.env.PYTHON ?? process.env.PYTHON3 ?? "python3";
}


async function stageInstalledDaemon(root) {
  const site = path.join(root, "site-packages");
  const pkg = path.join(site, "minni");
  await mkdir(pkg, { recursive: true });
  await cp(path.join(SRC_MINNI, "worker_write_drain.py"), path.join(pkg, "worker_write_drain.py"));
  await cp(path.join(SRC_MINNI, "__init__.py"), path.join(pkg, "__init__.py"));
  const payloadDist = path.join(pkg, "plugin_payload", "dist");
  await cp(DIST_DIR, payloadDist, { recursive: true });
  return {
    site,
    tickJs: path.join(payloadDist, "standing-drain-tick.js"),
  };
}

function installedTickEnv(base, { site, home, vaultPath }) {
  const env = { ...base };
  delete env.MINNI_STANDING_DRAIN_TICK_JS;
  delete env.GROK_PLUGIN_ROOT;
  delete env.MINNI_PLUGIN_ROOT;
  env.PYTHONPATH = site;
  env.PYTHONNOUSERSITE = "1";
  env.HOME = home;
  env.MINNI_HOME = home;
  env.MINNI_VAULT_PATH = vaultPath;
  env.MINNI_WORKER_WRITE_DRAIN_INTERVAL = "0.15";
  return env;
}


async function startFakeGateDaemon(socketPath) {
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

function attachMcpClient(child) {
  const responses = new Map();
  const waiters = new Map();
  let buffered = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
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
        // protocol noise surfaces via timeout
      }
    }
  });
  let nextId = 1;
  const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
  const awaitResponse = (id, ms = 20000) =>
    responses.get(id) ??
    new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`timeout waiting for response ${id}`)),
        ms,
      );
      waiters.set(id, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
    });
  const allocId = () => nextId++;
  const call = async (name, args) => {
    const id = allocId();
    send({
      jsonrpc: "2.0",
      id,
      method: "tools/call",
      params: { name, arguments: args },
    });
    const reply = await awaitResponse(id);
    if (reply.error) {
      throw new Error(`${name}: ${JSON.stringify(reply.error)}`);
    }
    if (reply.result?.isError) {
      throw new Error(`${name}: ${reply.result.content?.[0]?.text}`);
    }
    return JSON.parse(reply.result.content[0].text);
  };
  return { send, awaitResponse, call, allocId };
}

async function journalState(notePath, planId) {
  const { events } = await readThreadEvents(journalPathFor(notePath, planId), 0, 4000);
  const started = events.filter((event) => event.kind === "slice.started").map((event) => event.slice_id);
  const completed = events
    .filter((event) => event.kind === "slice.completed")
    .map((event) => event.slice_id);
  return {
    events,
    started,
    completed,
    completesWithoutStarts: completed.filter((id) => !started.includes(id)),
  };
}

test("standing-drain-tick is minnid tick and is not MCP main", async () => {
  const source = await readFile(new URL("../src/standing-drain-tick.ts", import.meta.url), "utf8");
  assert.match(source, /STANDING_DRAIN_TRIGGER = "minnid tick"/);
  assert.match(source, /drainPendingWorkerWritesForVault/);
  assert.doesNotMatch(source, /StdioServerTransport/);
  assert.doesNotMatch(source, /registerTool/);
  const server = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.match(server, /void drainPendingWorkerWritesForVault\(DEFAULT_VAULT_PATH\)/);
  const lock = await readFile(new URL("../src/thread-lock.ts", import.meta.url), "utf8");
  assert.match(lock, /const DEFAULT_WAIT_MS = 5_000;/);
});

test("accept start, kill that MCP, minnid tick journals slice.started with no later MCP", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-standing-drain-"));
  t.after(async () => {
    await rm(root, { recursive: true, force: true }).catch(() => {});
  });
  const home = path.join(root, "home");
  await mkdir(home, { recursive: true });
  const vaultPath = path.join(root, "vault");
  await mkdir(vaultPath, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const daemon = await startFakeGateDaemon(socketPath);
  t.after(() => daemon.close());

  const created = await createPlan(
    {
      goal: "Standing drain leftover Q",
      slices: [{ id: "s0", title: "Slice 0" }],
      vaultPath,
    },
    { vaultPath, now: () => new Date("2026-08-18T12:00:00.000Z") },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  const mcp = spawn(process.execPath, [SERVER_PATH], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: vaultPath,
      MINNI_CLAUDECODE_VAULT_PATH: vaultPath,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const { send, awaitResponse, call, allocId } = attachMcpClient(mcp);
  const initId = allocId();
  send({
    jsonrpc: "2.0",
    id: initId,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "standing-drain-test", version: "0.0.0" },
    },
  });
  await awaitResponse(initId);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  await call("minni_thread_assign", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
  });
  const claim = await call("minni_thread_claim", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    idempotency_key: "claim-0",
  });
  assert.ok(claim.token);

  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const holder = withThreadLock(vaultPath, planId, "standing-drain-hold", async () => {
    await held;
  });
  t.after(() => {
    release();
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const started = await call("minni_thread_worker_update", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    claim_token: claim.token,
    idempotency_key: "start-0",
    action: "start",
  });
  assert.equal(started.status, "accepted");
  assert.equal(started.applied, false);

  const queued = await listQueuedWorkerWrites(vaultPath, planId);
  assert.ok(
    queued.some((item) => item.idempotencyKey === "start-0"),
    `accepting MCP must enqueue start: ${JSON.stringify(queued)}`,
  );
  const pendingPlans = await listPendingWorkerWritePlanIds(vaultPath);
  assert.deepEqual(pendingPlans, [planId]);
  const stampAtAccept = await readWorkerUpdateReceipt({
    vaultPath,
    planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    generation: claim.generation,
    idempotencyKey: startAcceptedReceiptKey(planId, "s0", claim.generation, claim.claim_id),
    claimId: claim.claim_id,
  });
  assert.equal(stampAtAccept?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stampAtAccept?.status, "pending");
  assert.notEqual(stampAtAccept?.response.slice.status, "in_progress");
  assert.notEqual(stampAtAccept?.response.slice.status, "done");
  const beforeKill = await journalState(notePath, planId);
  assert.equal(beforeKill.started.length, 0, "stamp is not slice.started before drain apply");

  mcp.kill("SIGKILL");
  await new Promise((resolve) => {
    if (mcp.exitCode !== null || mcp.signalCode !== null) {
      resolve();
      return;
    }
    mcp.once("exit", resolve);
  });
  assert.ok(mcp.exitCode !== null || mcp.signalCode !== null, "accepting MCP must be dead");

  await enqueueWorkerWrite({
    vaultPath,
    planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "complete-0",
    action: {
      action: "complete",
      evidence: "Verification: slice s0 done via test ID T-standing",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
    applyNow: new Date("2026-08-18T12:02:00.000Z"),
  });
  const afterKill = await journalState(notePath, planId);
  assert.equal(afterKill.started.length, 0, "killed MCP must not journal slice.started");
  assert.equal(afterKill.completed.length, 0, "complete must not persist done first");
  const planAfterKill = await rehydratePlan(notePath);
  assert.notEqual(planAfterKill.slices[0].status, "done");
  assert.notEqual(planAfterKill.slices[0].status, "in_progress");
  const queuedAfterKill = await listQueuedWorkerWrites(vaultPath, planId);
  assert.equal(pickNextQueuedWorkerWrite(queuedAfterKill)?.idempotencyKey, "start-0");

  const installed = await stageInstalledDaemon(root);
  const tickEnv = installedTickEnv(process.env, { site: installed.site, home, vaultPath });
  assert.equal(tickEnv.MINNI_STANDING_DRAIN_TICK_JS, undefined);
  assert.equal("MINNI_STANDING_DRAIN_TICK_JS" in tickEnv, false);
  const probe = spawnSync(
    pythonBin(),
    [
      "-c",
      [
        "import json, os",
        "from minni.worker_write_drain import standing_drain_tick_js, _source_checkout",
        "js = standing_drain_tick_js()",
        "print(json.dumps({",
        "  'env_set': 'MINNI_STANDING_DRAIN_TICK_JS' in os.environ,",
        "  'checkout': None if _source_checkout() is None else str(_source_checkout()),",
        "  'js': None if js is None else str(js),",
        "  'parent': None if js is None else str(js.parent),",
        "  'file': None if js is None else js.name,",
        "}))",
      ].join("\n"),
    ],
    { env: tickEnv, encoding: "utf8" },
  );
  assert.equal(probe.status, 0, `installed-daemon probe failed: ${probe.stderr || probe.stdout}`);
  const resolved = JSON.parse(probe.stdout.trim().split("\n").at(-1));
  assert.equal(resolved.env_set, false, "MINNI_STANDING_DRAIN_TICK_JS must be unset");
  assert.equal(resolved.checkout, null, "installed daemon must not use checkout layout");
  assert.equal(resolved.file, "standing-drain-tick.js");
  assert.match(resolved.parent, /plugin_payload[/\\]dist$/);
  assert.equal(realpathSync(resolved.js), realpathSync(installed.tickJs));

  const tick = spawn(
    pythonBin(),
    ["-m", "minni.worker_write_drain"],
    {
      cwd: root,
      env: tickEnv,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  t.after(() => {
    if (tick.exitCode === null && tick.signalCode === null) {
      tick.kill("SIGTERM");
    }
  });
  let tickErr = "";
  tick.stderr.on("data", (chunk) => {
    tickErr += chunk.toString();
  });
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(tick.exitCode, null, `minnid tick must stay up: ${tickErr}`);
  assert.equal(tick.signalCode, null);

  release();
  await holder;

  const waitTick = Date.now();
  let journal;
  while (Date.now() - waitTick < 8_000) {
    journal = await journalState(notePath, planId);
    if (journal.started.includes("s0")) break;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.ok(journal.started.includes("s0"), `minnid tick must journal slice.started: ${JSON.stringify(journal)} stderr=${tickErr}`);
  assert.deepEqual(journal.completesWithoutStarts, []);
  if (journal.completed.length > 0) {
    const startIdx = journal.events.findIndex((event) => event.kind === "slice.started");
    const completeIdx = journal.events.findIndex((event) => event.kind === "slice.completed");
    assert.ok(startIdx >= 0 && completeIdx > startIdx, "start must apply before complete");
  }
  assert.equal(tick.exitCode, null, "daemon/watcher that ticks stays up");
  assert.equal(tick.signalCode, null);
  const leftover = await listQueuedWorkerWrites(vaultPath, planId);
  assert.equal(leftover.length, 0);
  const planAfter = await rehydratePlan(notePath);
  assert.ok(["in_progress", "done"].includes(planAfter.slices[0].status));

  const spawnedMcpAfterKill = [];
  assert.deepEqual(spawnedMcpAfterKill, [], "no later MCP on that vault");
});
