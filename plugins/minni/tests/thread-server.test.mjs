// Task 6: typed MCP worker surface. These tests exercise
// minni_thread_ready/assign/claim/worker_update/events over the REAL MCP
// server (stdio, same pattern as server-minni-recall-339.test.mjs and
// learn-gate-review-followups.test.mjs's fake-daemon test) rather than the
// underlying thread-worker.ts functions directly (already covered by
// thread-worker.test.mjs/thread-events.test.mjs). A fake gate daemon answers
// every gate.shared RPC with { ok: true } so requireSharedGate passes
// through to the handler under test.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createPlan } from "../dist/plan.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;
const SRC_PATH = new URL("../src/server.ts", import.meta.url);

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

async function withMcpSession(t, fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-server-"));
  const home = path.join(root, "home");
  await mkdir(home, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const daemon = await startFakeGateDaemon(socketPath);
  t.after(() => daemon.close());

  const child = spawn(process.execPath, [SERVER_PATH], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: root,
      MINNI_CLAUDECODE_VAULT_PATH: root,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => child.kill("SIGKILL"));

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
        // protocol noise surfaces via timeout below
      }
    }
  });

  let nextId = 1;
  const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
  const awaitResponse = (id, ms = 15000) =>
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

  const call = async (name, args) => {
    const id = nextId++;
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

  send({
    jsonrpc: "2.0",
    id: nextId++,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "thread-server-test", version: "0.0.0" },
    },
  });
  await awaitResponse(1);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  try {
    return await fn({ root, vaultPath: root, call, send, awaitResponse });
  } finally {
    await rm(root, { recursive: true, force: true }).catch(() => {});
  }
}

async function seedPlan(vaultPath, slices) {
  const created = await createPlan(
    { goal: "Task 6 typed worker surface", slices, vaultPath },
    { vaultPath },
  );
  return created.plan.plan_id;
}

test("minni_thread_assign -> claim -> worker_update completes a slice end to end", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "research", title: "Research the approach" },
    ]);

    await call("minni_thread_assign", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
    });

    const claim = await call("minni_thread_claim", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-research-1",
    });
    assert.ok(claim.token, "claim must return a one-time token");
    assert.equal(claim.slice_id, "research");
    assert.equal(claim.worker_agent_id, "worker-a");
    assert.equal(
      claim.filePath,
      undefined,
      "claim response must never leak the secret envelope's file path",
    );
    assert.deepEqual(
      Object.keys(claim).sort(),
      [
        "claim_id",
        "expires_at",
        "generation",
        "plan_id",
        "rev",
        "slice_id",
        "token",
        "worker_agent_id",
      ].sort(),
      "claim response must be exactly ThreadClaimResponse, nothing more",
    );

    const done = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
      claim_token: claim.token,
      idempotency_key: "done-research-1",
      action: "complete",
      evidence: "Verified against docs/source-a.md and docs/source-b.md",
    });
    assert.equal(done.slice.status, "done");
  });
});

test("minni_thread_ready reflects claim state and defaults plan_id to the active plan", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "alpha", title: "Alpha slice" }]);

    // createPlan auto-activates — plan_id is intentionally omitted here to
    // exercise the same id-less default every other optional-plan_id Thread
    // tool already honors (resolvePlanIdOrActive).
    const readyBeforeClaim = await call("minni_thread_ready", {});
    assert.deepEqual(readyBeforeClaim.ready.map((s) => s.id), ["alpha"]);

    await call("minni_thread_assign", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
    });
    await call("minni_thread_claim", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-alpha-1",
    });

    const readyAfterClaim = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(
      readyAfterClaim.ready.map((s) => s.id),
      [],
      "a live-claimed slice must not still be reported ready",
    );
  });
});

test("minni_thread_events is journal-backed and its cursor excludes seq at or below since_seq", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "alpha", title: "Alpha slice" }]);

    await call("minni_thread_assign", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
    });
    await call("minni_thread_claim", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-alpha-2",
    });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    assert.ok(events.events.length > 0, "expected at least one ordered event");
    const kinds = events.events.map((e) => e.kind);
    assert.ok(kinds.includes("slice.assigned"), `expected slice.assigned in ${kinds}`);
    assert.ok(kinds.includes("slice.claimed"), `expected slice.claimed in ${kinds}`);
    assert.equal(events.next_seq, events.events.at(-1).seq);

    const cursor = await call("minni_thread_events", { plan_id, since_seq: events.next_seq });
    assert.deepEqual(cursor.events, [], "cursor read must exclude seq at or below since_seq");
    assert.equal(cursor.next_seq, events.next_seq);
  });
});

test("minni_thread_claim surfaces a typed domain error instead of a transport crash", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "alpha", title: "Alpha slice" }]);

    const result = await call("minni_thread_claim", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      idempotency_key: "unassigned-claim",
    });
    assert.equal(result.status, "error");
    assert.equal(result.operation, "plan.claim");
    assert.match(result.error, /assigned/i);
    assert.equal(result.filePath, undefined);
    assert.equal(result.notePath, undefined);
  });
});

test("minni_thread_worker_update rejects an empty idempotency_key before it reaches thread-worker", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call, send, awaitResponse }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "alpha", title: "Alpha slice" }]);
    await call("minni_thread_assign", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
    });
    const claim = await call("minni_thread_claim", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-alpha-3",
    });

    const id = 9001;
    send({
      jsonrpc: "2.0",
      id,
      method: "tools/call",
      params: {
        name: "minni_thread_worker_update",
        arguments: {
          plan_id,
          slice_id: "alpha",
          worker_agent_id: "worker-a",
          claim_token: claim.token,
          idempotency_key: "",
          action: "start",
        },
      },
    });
    const reply = await awaitResponse(id);
    assert.equal(
      reply.result?.isError,
      true,
      `an empty idempotency_key must be rejected at the schema layer, not reach thread-worker: ${JSON.stringify(reply)}`,
    );
    assert.match(
      reply.result.content[0].text,
      /idempotency_key/,
      "the validation error must name the offending field",
    );
  });
});

test("minni_thread_worker_update's discriminated union strips fields that do not belong to the given action, never applies them", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "alpha", title: "Alpha slice" }]);
    await call("minni_thread_assign", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
    });
    const claim = await call("minni_thread_claim", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-alpha-4",
    });

    // "start" declares no evidence field. The discriminated union's "start"
    // branch is a closed z.object({action: literal("start")}), so zod strips
    // evidence rather than smuggling it into the persisted slice.
    const started = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      claim_token: claim.token,
      idempotency_key: "mismatched-action-fields",
      action: "start",
      evidence: "must be stripped, not attached to the start action",
    });
    assert.equal(started.slice.status, "in_progress");
    assert.equal(
      started.slice.evidence,
      undefined,
      "a stray evidence field alongside action:start must never reach the persisted slice",
    );
  });
});

function toolBlock(source, toolName) {
  const start = source.indexOf(`server.registerTool(\n  "${toolName}"`);
  assert.notEqual(start, -1, `${toolName} registration not found`);
  const next = source.indexOf("server.registerTool(", start + 1);
  return source.slice(start, next === -1 ? undefined : next);
}

test("minni_thread_worker_update schema exposes no topology, assignment, or force field", async () => {
  const source = await readFile(SRC_PATH, "utf8");
  const block = toolBlock(source, "minni_thread_worker_update");
  const schemaStart = block.indexOf("inputSchema:");
  const handlerStart = block.indexOf("async (");
  const schema = block.slice(schemaStart, handlerStart);

  for (const forbidden of [
    "depends_on",
    "dependency",
    "\\bgate\\s*:",
    "assigned_to",
    "assignee",
    "constraints",
    "sibling",
    "\\bforce\\b",
    "force_reason",
    "\\breplan\\b",
    "new_slices",
    "add_slices",
    "drop_slice_ids",
    "z\\.record",
  ]) {
    assert.doesNotMatch(
      schema,
      new RegExp(forbidden),
      `minni_thread_worker_update schema must not expose ${forbidden}`,
    );
  }
  assert.match(
    schema,
    /idempotency_key:\s*z\.string\(\)\.min\(1\)/,
    "idempotency_key must be a required, non-empty field",
  );
});

test("minni_thread_worker_update validates action as a real discriminated union and never spreads the raw request", async () => {
  const source = await readFile(SRC_PATH, "utf8");
  // The discriminated union is a module-scoped const consumed by (not
  // inlined into) the tool block, so this checks the whole file rather than
  // just the registerTool block.
  assert.match(
    source,
    /workerUpdateActionSchema\s*=\s*z\.discriminatedUnion\(\s*"action"/,
    "must validate action as a discriminated union, not a loosely-typed object",
  );
  const block = toolBlock(source, "minni_thread_worker_update");
  assert.doesNotMatch(
    block,
    /updateClaimedSlice\(\{\s*\n\s*\.\.\./,
    "must not spread the raw parsed request into thread-worker's updateClaimedSlice",
  );
});

test("every Task 6 worker tool resolves plan_id server-side and never accepts a caller-supplied vault path", async () => {
  const source = await readFile(SRC_PATH, "utf8");
  for (const toolName of [
    "minni_thread_ready",
    "minni_thread_assign",
    "minni_thread_claim",
    "minni_thread_worker_update",
    "minni_thread_events",
  ]) {
    const block = toolBlock(source, toolName);
    const schemaStart = block.indexOf("inputSchema:");
    const handlerStart = block.indexOf("async (");
    const schema = block.slice(schemaStart, handlerStart);
    assert.doesNotMatch(schema, /vaultPath|vault_path/, `${toolName} must not accept a caller-supplied vault path`);
    assert.match(
      block,
      /resolvePlanTarget\(planIdInput\)/,
      `${toolName} must resolve plan_id through the shared, server-pinned resolvePlanTarget helper`,
    );
  }
});
