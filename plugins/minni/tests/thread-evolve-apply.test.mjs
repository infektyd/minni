// Thread evolution apply: worker propose stays propose; orch replan
// (add/drop, not a kind enum) is the only apply surface. Wet MCP on one
// Thread per semantic. No new worker tool.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  createPlan,
  findPlanNote,
  rehydratePlan,
  structuralProposalDelta,
} from "../dist/plan.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;

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
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-evolve-"));
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
      clientInfo: { name: "thread-evolve-apply-test", version: "0.0.0" },
    },
  });
  await awaitResponse(1);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  try {
    return await fn({ root, vaultPath: root, call });
  } finally {
    await rm(root, { recursive: true, force: true }).catch(() => {});
  }
}

async function seedPlan(vaultPath, slices) {
  const created = await createPlan(
    { goal: "Thread evolution apply", slices, vaultPath },
    { vaultPath },
  );
  return created.plan.plan_id;
}

function sliceIds(plan) {
  return plan.slices.map((slice) => slice.id);
}

function liveSliceIds(plan) {
  return plan.slices
    .filter((slice) => slice.status !== "superseded")
    .map((slice) => slice.id)
    .sort();
}

async function claimWorker(call, plan_id, slice_id, worker_agent_id, key) {
  await call("minni_thread_assign", {
    plan_id,
    slice_id,
    worker_agent_id,
  });
  return call("minni_thread_claim", {
    plan_id,
    slice_id,
    worker_agent_id,
    idempotency_key: key,
  });
}

test("wet: propose expand leaves topology unchanged; orch add-only apply changes slice set + ready + journal; proposer stays", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "parent", title: "Parent slice" },
      { id: "blocked", title: "Blocked sibling", depends_on: ["parent"] },
    ]);
    const claim = await claimWorker(call, plan_id, "parent", "worker-a", "claim-expand");
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan note");
    const beforePropose = await rehydratePlan(notePath);
    const readyBeforePropose = await call("minni_thread_ready", { plan_id });

    const proposed = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "parent",
      worker_agent_id: "worker-a",
      claim_token: claim.token,
      idempotency_key: "propose-expand",
      action: "propose_structure",
      proposal: {
        kind: "expand",
        reason: "Need an independent verification branch",
        slices: [{ id: "extra", title: "Extra branch" }],
      },
    });

    assert.deepEqual(proposed.ready_before, proposed.ready_after);
    assert.deepEqual(proposed.ready_after, readyBeforePropose.ready.map((s) => s.id));
    const afterPropose = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(afterPropose), sliceIds(beforePropose));
    assert.equal(afterPropose.slices.find((s) => s.id === "parent").status, "pending");
    assert.ok(afterPropose.slices.find((s) => s.id === "parent").claim, "proposer claim stays");
    assert.deepEqual(afterPropose.slices.find((s) => s.id === "parent").proposals.at(-1), {
      kind: "expand",
      reason: "Need an independent verification branch",
      slices: [{ id: "extra", title: "Extra branch" }],
    });
    assert.equal(afterPropose.slices.some((s) => s.id === "extra"), false);

    const eventsAfterPropose = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    const proposedEvent = eventsAfterPropose.events.find((e) => e.kind === "structure.proposed");
    assert.ok(proposedEvent, `expected structure.proposed, got ${eventsAfterPropose.events.map((e) => e.kind)}`);
    assert.equal(proposedEvent.actor, "worker-a");
    assert.equal(proposedEvent.slice_id, "parent");
    assert.deepEqual(proposedEvent.payload, {
      kind: "expand",
      reason: "Need an independent verification branch",
      slices: [{ id: "extra", title: "Extra branch" }],
    });
    assert.equal(
      JSON.stringify(proposedEvent).includes(claim.token),
      false,
      "claim token must stay off the journal",
    );
    assert.equal(
      eventsAfterPropose.events.some((e) => e.kind === "replan"),
      false,
      "propose must not apply",
    );

    // Orch reconstructs add/drop from the journal event, not the plan note.
    const delta = structuralProposalDelta(proposedEvent.payload, proposedEvent.slice_id);
    assert.equal("drop_slice_ids" in delta, false);
    const applied = await call("minni_thread_replan", { plan_id, ...delta });
    assert.notEqual(applied.status, "error", JSON.stringify(applied));

    const afterApply = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(afterApply), ["parent", "blocked", "extra"]);
    const parent = afterApply.slices.find((s) => s.id === "parent");
    assert.equal(parent.status, "pending");
    assert.ok(parent.claim, "expand keeps the proposer claim");
    assert.equal(parent.assigned_to, "worker-a");
    assert.equal(afterApply.slices.find((s) => s.id === "extra").status, "pending");
    const readyAfter = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfter.ready.map((s) => s.id), ["extra"]);

    const eventsAfterApply = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    const kinds = eventsAfterApply.events.map((e) => e.kind);
    assert.ok(kinds.includes("replan"), `expected replan in ${kinds}`);
    assert.ok(kinds.includes("ready.changed"), `expected ready.changed in ${kinds}`);
    assert.ok(
      eventsAfterApply.events.some((e) => e.kind === "structure.proposed"),
      "attribution remains in the journal",
    );
    const replanEvent = eventsAfterApply.events.find((e) => e.kind === "replan");
    assert.ok(replanEvent?.payload, "replan event must carry landed add/drop");
    assert.deepEqual(replanEvent.payload.add_slices, [{ id: "extra", title: "Extra branch" }]);
    assert.equal("drop_slice_ids" in (replanEvent.payload ?? {}), false);
    assert.deepEqual(parent.proposals.at(-1).kind, "expand");
  });
});

test("wet: propose split leaves topology unchanged; orch drop+add supersedes claimed parent, adds children, never reuses parent id", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "parent", title: "Parent to split" },
      { id: "other", title: "Other ready slice" },
    ]);
    const claim = await claimWorker(call, plan_id, "parent", "worker-b", "claim-split");
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan note");
    const beforePropose = await rehydratePlan(notePath);

    await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "parent",
      worker_agent_id: "worker-b",
      claim_token: claim.token,
      idempotency_key: "propose-split",
      action: "propose_structure",
      proposal: {
        kind: "split",
        reason: "Two independently verifiable outputs",
        slices: [
          { id: "child-a", title: "Child A" },
          { id: "child-b", title: "Child B" },
        ],
      },
    });

    const afterPropose = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(afterPropose), sliceIds(beforePropose));
    assert.equal(afterPropose.slices.some((s) => s.id === "child-a"), false);
    assert.ok(afterPropose.slices.find((s) => s.id === "parent").claim);

    const reuse = await call("minni_thread_replan", {
      plan_id,
      drop_slice_ids: ["parent"],
      add_slices: [{ id: "parent", title: "Reused parent id" }],
    });
    assert.equal(reuse.status, "error");
    assert.equal(reuse.operation, "plan.replan");
    assert.match(reuse.error, /cannot add slice with id "parent"/);
    const afterRejected = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(afterRejected), sliceIds(beforePropose));
    assert.equal(afterRejected.slices.find((s) => s.id === "parent").status, "pending");

    const eventsAfterPropose = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    const proposedEvent = eventsAfterPropose.events.find((e) => e.kind === "structure.proposed");
    assert.ok(proposedEvent, `expected structure.proposed, got ${eventsAfterPropose.events.map((e) => e.kind)}`);
    assert.equal(proposedEvent.actor, "worker-b");
    assert.equal(proposedEvent.slice_id, "parent");
    assert.deepEqual(proposedEvent.payload, {
      kind: "split",
      reason: "Two independently verifiable outputs",
      slices: [
        { id: "child-a", title: "Child A" },
        { id: "child-b", title: "Child B" },
      ],
    });
    assert.equal(
      JSON.stringify(proposedEvent).includes(claim.token),
      false,
      "claim token must stay off the journal",
    );

    const delta = structuralProposalDelta(proposedEvent.payload, proposedEvent.slice_id);
    assert.deepEqual(delta.drop_slice_ids, ["parent"]);
    const applied = await call("minni_thread_replan", { plan_id, ...delta });
    assert.notEqual(applied.status, "error", JSON.stringify(applied));

    const afterApply = await rehydratePlan(notePath);
    const parent = afterApply.slices.find((s) => s.id === "parent");
    assert.ok(parent, "split never deletes the parent");
    assert.equal(parent.status, "superseded");
    assert.ok(parent.superseded_by);
    assert.equal(parent.claim, undefined, "claimed parent is revoked by supersession");
    assert.deepEqual(liveSliceIds(afterApply), ["child-a", "child-b", "other"]);
    assert.equal(afterApply.slices.filter((s) => s.id === "parent").length, 1);
    assert.deepEqual(parent.proposals.at(-1).kind, "split");

    const readyAfter = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfter.ready.map((s) => s.id).sort(), ["child-a", "child-b", "other"]);

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const kinds = events.events.map((e) => e.kind);
    assert.ok(kinds.includes("structure.proposed"));
    assert.ok(kinds.includes("replan"));
    assert.ok(kinds.includes("ready.changed"));
    assert.ok(kinds.includes("slice.claim_revoked"));
    const replanEvent = events.events.find((e) => e.kind === "replan");
    assert.deepEqual(replanEvent.payload.add_slices, [
      { id: "child-a", title: "Child A" },
      { id: "child-b", title: "Child B" },
    ]);
    assert.deepEqual(replanEvent.payload.drop_slice_ids, ["parent"]);
  });
});

test("wet GO: exclusive split keeps depends_on blocked until orch remounts; propose does not apply", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "s0", title: "Claimed parent" },
      { id: "b", title: "Sibling depends on s0", depends_on: ["s0"] },
      { id: "indie", title: "Independent slice" },
    ]);
    const claim = await claimWorker(call, plan_id, "s0", "worker-split", "claim-s0-split");
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan note");

    const readyBefore = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyBefore.ready.map((s) => s.id).sort(), ["indie"]);

    const proposed = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "s0",
      worker_agent_id: "worker-split",
      claim_token: claim.token,
      idempotency_key: "propose-exclusive-split",
      action: "propose_structure",
      proposal: {
        kind: "split",
        reason: "Exclusive split into independently verifiable children",
        slices: [
          { id: "child-a", title: "Child A" },
          { id: "child-b", title: "Child B" },
        ],
      },
    });
    assert.deepEqual(proposed.ready_before, proposed.ready_after);
    const afterPropose = await rehydratePlan(notePath);
    assert.equal(afterPropose.slices.some((s) => s.id === "child-a"), false);
    assert.equal(afterPropose.slices.find((s) => s.id === "s0").status, "pending");
    assert.deepEqual(afterPropose.slices.find((s) => s.id === "b").depends_on, ["s0"]);

    const delta = structuralProposalDelta(
      afterPropose.slices.find((s) => s.id === "s0").proposals.at(-1),
      "s0",
    );
    assert.deepEqual(delta.drop_slice_ids, ["s0"]);
    assert.equal("depends_on" in delta, false, "delta does not remount dependents");
    const applied = await call("minni_thread_replan", { plan_id, ...delta });
    assert.notEqual(applied.status, "error", JSON.stringify(applied));

    const afterSplit = await rehydratePlan(notePath);
    const parent = afterSplit.slices.find((s) => s.id === "s0");
    assert.equal(parent.status, "superseded");
    assert.ok(parent.superseded_by);
    assert.ok(parent.replaced_by?.length, "split marks replacement");
    assert.equal(parent.claim, undefined);
    assert.deepEqual(afterSplit.slices.find((s) => s.id === "b").depends_on, ["s0"]);
    assert.equal(afterSplit.slices.find((s) => s.id === "child-a").assigned_to, undefined);
    assert.equal(afterSplit.slices.find((s) => s.id === "child-b").assigned_to, undefined);

    const readyAfterSplit = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(
      readyAfterSplit.ready.map((s) => s.id).sort(),
      ["child-a", "child-b", "indie"],
      "b must not become ready when s0 is replaced",
    );
    assert.equal(
      readyAfterSplit.ready.some((s) => s.id === "b"),
      false,
    );
    assert.equal(
      readyAfterSplit.ready.some((s) => s.id === "s0"),
      false,
    );

    const team = await call("minni_team_runtime", {
      task: "exclusive-split remount honesty",
      plan_id,
    });
    assert.deepEqual(
      team.ready.map((s) => s.id).sort(),
      ["child-a", "child-b", "indie"],
      "team_runtime ready must match thread ready after split",
    );
    assert.equal(team.ready.some((s) => s.id === "b"), false);

    const remount = await call("minni_thread_replan", {
      plan_id,
      new_slices: [
        { id: "b", title: "Sibling depends on s0", depends_on: ["child-a", "child-b"] },
        { id: "child-a", title: "Child A" },
        { id: "child-b", title: "Child B" },
        { id: "indie", title: "Independent slice" },
      ],
    });
    assert.notEqual(remount.status, "error", JSON.stringify(remount));
    const afterRemount = await rehydratePlan(notePath);
    assert.deepEqual(
      afterRemount.slices.find((s) => s.id === "b").depends_on,
      ["child-a", "child-b"],
    );
    const readyAfterRemount = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(
      readyAfterRemount.ready.map((s) => s.id).sort(),
      ["child-a", "child-b", "indie"],
      "after remount, b still waits on the live children",
    );
    assert.equal(readyAfterRemount.ready.some((s) => s.id === "b"), false);

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    assert.ok(events.events.some((e) => e.kind === "structure.proposed"));
    assert.ok(events.events.some((e) => e.kind === "replan"));
    const remountEvent = events.events
      .filter((e) => e.kind === "replan")
      .find((e) => e.payload?.depends_on_changed);
    assert.ok(remountEvent, "orch remount must journal depends_on_changed on existing replan surface");
  });
});

test("wet: propose contract leaves topology unchanged; orch drop supersedes named ids and never deletes", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "keep", title: "Keep working" },
      { id: "drop-me", title: "No longer needed" },
    ]);
    const claim = await claimWorker(call, plan_id, "keep", "worker-c", "claim-contract");
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan note");
    const beforePropose = await rehydratePlan(notePath);
    const readyBefore = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyBefore.ready.map((s) => s.id), ["drop-me"]);

    await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "keep",
      worker_agent_id: "worker-c",
      claim_token: claim.token,
      idempotency_key: "propose-contract",
      action: "propose_structure",
      proposal: {
        kind: "contract",
        reason: "drop-me is no longer in scope",
        slice_ids: ["drop-me"],
      },
    });

    const afterPropose = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(afterPropose), sliceIds(beforePropose));
    assert.equal(afterPropose.slices.find((s) => s.id === "drop-me").status, "pending");
    const readyAfterPropose = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfterPropose.ready.map((s) => s.id), ["drop-me"]);

    const eventsAfterPropose = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    const proposedEvent = eventsAfterPropose.events.find((e) => e.kind === "structure.proposed");
    assert.ok(proposedEvent, `expected structure.proposed, got ${eventsAfterPropose.events.map((e) => e.kind)}`);
    assert.equal(proposedEvent.actor, "worker-c");
    assert.equal(proposedEvent.slice_id, "keep");
    assert.deepEqual(proposedEvent.payload, {
      kind: "contract",
      reason: "drop-me is no longer in scope",
      slice_ids: ["drop-me"],
    });
    assert.equal(
      JSON.stringify(proposedEvent).includes(claim.token),
      false,
      "claim token must stay off the journal",
    );

    const delta = structuralProposalDelta(proposedEvent.payload, proposedEvent.slice_id);
    assert.deepEqual(delta, { drop_slice_ids: ["drop-me"] });
    const applied = await call("minni_thread_replan", { plan_id, ...delta });
    assert.notEqual(applied.status, "error", JSON.stringify(applied));

    const afterApply = await rehydratePlan(notePath);
    const dropped = afterApply.slices.find((s) => s.id === "drop-me");
    assert.ok(dropped, "contract never deletes");
    assert.equal(dropped.status, "superseded");
    assert.ok(dropped.superseded_by);
    assert.equal(afterApply.slices.find((s) => s.id === "keep").status, "pending");
    assert.ok(afterApply.slices.find((s) => s.id === "keep").claim);
    const readyAfter = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfter.ready.map((s) => s.id), []);

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const kinds = events.events.map((e) => e.kind);
    assert.ok(kinds.includes("structure.proposed"));
    assert.ok(kinds.includes("replan"));
    assert.ok(kinds.includes("ready.changed"));
    const replanEvent = events.events.find((e) => e.kind === "replan");
    assert.deepEqual(replanEvent.payload.drop_slice_ids, ["drop-me"]);
    assert.equal("add_slices" in (replanEvent.payload ?? {}), false);
    assert.deepEqual(
      afterApply.slices.find((s) => s.id === "keep").proposals.at(-1),
      {
        kind: "contract",
        reason: "drop-me is no longer in scope",
        slice_ids: ["drop-me"],
      },
    );
  });
});

test("wet GO: new_slices replan that adds and supersedes journals landed add/drop; propose still does not apply", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "keep", title: "Keep" },
      { id: "old", title: "Supersede me" },
    ]);
    const claim = await claimWorker(call, plan_id, "keep", "worker-ns", "claim-newslices");
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan note");
    const beforePropose = await rehydratePlan(notePath);

    await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "keep",
      worker_agent_id: "worker-ns",
      claim_token: claim.token,
      idempotency_key: "propose-newslices",
      action: "propose_structure",
      proposal: {
        kind: "expand",
        reason: "need a replacement branch via full-set replan",
        slices: [{ id: "new-a", title: "New A" }],
      },
    });

    const afterPropose = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(afterPropose), sliceIds(beforePropose));
    assert.equal(afterPropose.slices.some((s) => s.id === "new-a"), false);
    const eventsAfterPropose = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    assert.equal(
      eventsAfterPropose.events.some((e) => e.kind === "replan"),
      false,
      "propose must not apply",
    );

    // Full-set replan surface (#30 remount path): MCP args are new_slices
    // only — no add_slices / drop_slice_ids. Journal must still carry what
    // landed.
    const applied = await call("minni_thread_replan", {
      plan_id,
      new_slices: [
        { id: "keep", title: "Keep" },
        { id: "new-a", title: "New A" },
      ],
    });
    assert.notEqual(applied.status, "error", JSON.stringify(applied));

    const afterApply = await rehydratePlan(notePath);
    assert.equal(afterApply.slices.find((s) => s.id === "old").status, "superseded");
    assert.equal(afterApply.slices.find((s) => s.id === "new-a").status, "pending");
    assert.equal(afterApply.slices.find((s) => s.id === "keep").status, "pending");
    assert.ok(afterApply.slices.find((s) => s.id === "keep").claim, "proposer claim stays");

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const replanEvent = events.events.find((e) => e.kind === "replan");
    assert.ok(replanEvent?.payload, "replan event must carry landed add/drop");
    assert.deepEqual(replanEvent.payload.add_slices, [{ id: "new-a", title: "New A" }]);
    assert.deepEqual(replanEvent.payload.drop_slice_ids, ["old"]);
    assert.equal(
      JSON.stringify(replanEvent).includes(claim.token),
      false,
      "claim token must stay off the journal",
    );
    const readyAfter = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfter.ready.map((s) => s.id), ["new-a"]);
  });
});

test("wet: worker cannot replan; live worker tool stays worker_update only", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const claim = await claimWorker(call, plan_id, "a", "worker-a", "claim-no-replan");
    const notePath = await findPlanNote(vaultPath, plan_id);
    const before = await rehydratePlan(notePath);

    await assert.rejects(
      () => call("minni_thread_worker_update", {
        plan_id,
        slice_id: "a",
        worker_agent_id: "worker-a",
        claim_token: claim.token,
        idempotency_key: "worker-replan-reject",
        action: "replan",
        proposal: {
          kind: "expand",
          reason: "worker must not apply",
          slices: [{ id: "injected", title: "Injected" }],
        },
      }),
      /expected one of "start"\|"progress"\|"block"\|"scar"\|"propose_structure"\|"complete" at action/,
    );

    const after = await rehydratePlan(notePath);
    assert.deepEqual(sliceIds(after), sliceIds(before));
    assert.equal(after.slices.some((s) => s.id === "injected"), false);
  });

  const writers = await readFile(
    new URL("../../../src/minni/wire/writers.py", import.meta.url),
    "utf8",
  );
  assert.match(
    writers,
    /MINNI_WORKER_TOOLS = \(\s*"minni_thread_worker_update",\s*\)/,
  );
  assert.equal(writers.includes("minni_thread_replan"), false);
  assert.equal(writers.includes("minni_thread_create"), false);
  assert.equal(writers.includes("minni_thread_assign"), false);
  assert.equal(writers.includes("minni_thread_claim"), false);

  const serverSource = await readFile(
    new URL("../src/server.ts", import.meta.url),
    "utf8",
  );
  const workerBlockStart = serverSource.indexOf('"minni_thread_worker_update"');
  const workerBlock = serverSource.slice(
    workerBlockStart,
    serverSource.indexOf("server.registerTool", workerBlockStart + 1),
  );
  assert.match(workerBlock, /propose_structure/);
  assert.doesNotMatch(
    workerBlock,
    /action: z\.enum\(\[[^\]]*replan/,
    "worker_update action enum must not include replan",
  );
  assert.equal(
    serverSource.includes('server.registerTool(\n  "minni_thread_propose'),
    false,
    "no new worker MCP tool",
  );
});

test("wet: replan journals generated add ids when request add_slices omit id", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "keep", title: "Keep working" }]);
    const applied = await call("minni_thread_replan", {
      plan_id,
      add_slices: [{ title: "Generated Child", depends_on: ["keep"] }],
    });
    assert.notEqual(applied.status, "error", JSON.stringify(applied));

    const notePath = await findPlanNote(vaultPath, plan_id);
    const after = await rehydratePlan(notePath);
    const generated = after.slices.find((s) => s.id !== "keep");
    assert.ok(generated, "applySliceDelta must land a generated-id slice");
    assert.equal(generated.title, "Generated Child");
    assert.notEqual(generated.id, "Generated Child");

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const replanEvent = events.events.find((e) => e.kind === "replan");
    assert.ok(replanEvent?.payload?.add_slices, "replan journal must carry landed add_slices");
    assert.deepEqual(replanEvent.payload.add_slices, [
      { id: generated.id, title: "Generated Child", depends_on: ["keep"] },
    ]);
    assert.equal(
      JSON.stringify(replanEvent.payload).includes('"title":"Generated Child"') &&
        !JSON.stringify(replanEvent.payload.add_slices[0]).includes('"id":undefined'),
      true,
    );
  });
});
