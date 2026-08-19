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
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  createPlan,
  findPlanNote,
  historyPathFor,
  journalPathFor,
  persistPlan,
  rehydratePlan,
} from "../dist/plan.js";
import { DEFAULT_AGENT_ID } from "../dist/config.js";

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

    // Task 6 followup regression: "complete" clears the live claim once it
    // durably lands, so an identical retry has no live claim left to
    // authenticate the same token against — the OLD code threw "claim scope
    // mismatch" here instead of replaying. The private worker-update
    // receipt (keyed by plan/slice/worker/idempotency, holding a token
    // digest and the original public response) is what a retry
    // authenticates against instead.
    const eventsBeforeRetry = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    const retried = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
      claim_token: claim.token,
      idempotency_key: "done-research-1",
      action: "complete",
      evidence: "Verified against docs/source-a.md and docs/source-b.md",
    });
    assert.deepEqual(
      retried,
      done,
      "an identical retry after completion must replay the exact original response",
    );
    const eventsAfterRetry = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    assert.deepEqual(
      eventsAfterRetry.events,
      eventsBeforeRetry.events,
      "a same-key retry must never append a new journal event",
    );
    assert.equal(eventsAfterRetry.next_seq, eventsBeforeRetry.next_seq);

    // A wrong token against the same idempotency key must fail typed — the
    // receipt authenticates the retry via a timing-safe token digest, not
    // by trusting the idempotency key alone.
    const wrongToken = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
      claim_token: "wrong-token-does-not-match-the-original",
      idempotency_key: "done-research-1",
      action: "complete",
      evidence: "Verified against docs/source-a.md and docs/source-b.md",
    });
    assert.equal(wrongToken.status, "error");
    assert.match(wrongToken.error, /claim token mismatch/);

    // The same idempotency key reused for a DIFFERENT action is a genuine
    // identity conflict, not a replay — it must also fail typed.
    const conflictingAction = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
      claim_token: claim.token,
      idempotency_key: "done-research-1",
      action: "start",
    });
    assert.equal(conflictingAction.status, "error");
    assert.match(
      conflictingAction.error,
      /idempotency key .* already bound to a different operation/,
    );

    const eventsAfterConflicts = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 200,
    });
    assert.deepEqual(
      eventsAfterConflicts.events,
      eventsBeforeRetry.events,
      "a rejected wrong-token or conflicting-action call must never append a new journal event either",
    );
  });
});

test("minni_thread_assign stamps the server-side orchestrator actor on slice.assigned, never the assignment target worker", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "research", title: "Research the approach" },
    ]);

    await call("minni_thread_assign", {
      plan_id,
      slice_id: "research",
      worker_agent_id: "worker-a",
      // Even if a caller tries to smuggle an actor-shaped field alongside
      // the declared schema, zod's default "strip unknown keys" behavior
      // must drop it before it ever reaches assignSlice.
      actor_agent_id: "attempted-model-supplied-actor",
      agent_id: "attempted-model-supplied-actor",
    });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    const assigned = events.events.find((e) => e.kind === "slice.assigned");
    assert.ok(assigned, "expected a slice.assigned ordered event");
    assert.equal(assigned.actor, DEFAULT_AGENT_ID);
    assert.notEqual(assigned.actor, "worker-a");
    assert.notEqual(assigned.actor, "attempted-model-supplied-actor");

    const baseline = events.events.find((e) => e.kind === "state.baseline");
    assert.ok(baseline, "expected a state.baseline ordered event");
    assert.equal(baseline.actor, DEFAULT_AGENT_ID);
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

// Final-fix Important finding 2: structural/legacy orchestrator mutations
// (minni_thread_update/scar/replan/restore) previously never touched the
// ordered cursor at all — a caller consuming minni_thread_events had no way
// to see a status change, scar, replan, or restore alongside slice.*/claim
// events. These tests prove: (1) each of the four kinds appears via
// minni_thread_events in order with a server-stamped actor, (2) ready.changed
// is coalesced for update/replan/restore but never for scar, (3) reconcile-
// before/append-after crash-gap semantics apply to these tools too, and (4)
// no evidence/scar text/token/path ever leaks into an ordered payload.

test("minni_thread_update appends an ordered status_changed mirror with the server actor and coalesces ready.changed", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "a", title: "Slice A" },
      { id: "b", title: "Slice B", depends_on: ["a"] },
    ]);

    const before = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    assert.equal(before.events.length, 0, "no ordered events exist before the first orchestrator mutation");

    await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "done",
      evidence: "Verified against docs/source-a.md",
    });

    const after = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    const kinds = after.events.map((e) => e.kind);
    assert.ok(kinds.includes("state.baseline"), `expected state.baseline in ${kinds}`);

    const statusChanged = after.events.find((e) => e.kind === "status_changed");
    assert.ok(statusChanged, `expected an ordered status_changed event, got kinds: ${kinds}`);
    assert.equal(statusChanged.actor, DEFAULT_AGENT_ID);
    assert.equal(statusChanged.slice_id, "a");
    assert.deepEqual(statusChanged.payload, { from: "pending", to: "done" });
    assert.doesNotMatch(
      JSON.stringify(statusChanged),
      /source-a\.md/,
      "evidence text must never leak into the ordered status_changed payload",
    );

    const readyChanged = after.events.find((e) => e.kind === "ready.changed");
    assert.ok(readyChanged, "expected a coalesced ready.changed event when b becomes ready");
    assert.deepEqual(readyChanged.payload, { slices: [{ id: "b", title: "Slice B" }] });

    const cursor = await call("minni_thread_events", { plan_id, since_seq: after.next_seq });
    assert.deepEqual(cursor.events, [], "cursor read must exclude seq at or below since_seq for these mirrored kinds too");
  });
});

test("minni_thread_scar appends an ordered scar_added mirror without emitting ready.changed or leaking the scar signal", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);

    await call("minni_thread_scar", {
      plan_id,
      kind: "dead_end",
      signal: "sentinel-scar-signal-do-not-leak-9f2c",
      resolution: "sentinel-scar-resolution-do-not-leak-4b1a",
    });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    const kinds = events.events.map((e) => e.kind);
    const scarAdded = events.events.find((e) => e.kind === "scar_added");
    assert.ok(scarAdded, `expected an ordered scar_added event, got kinds: ${kinds}`);
    assert.equal(scarAdded.actor, DEFAULT_AGENT_ID);

    const serialized = JSON.stringify(events.events);
    assert.doesNotMatch(serialized, /sentinel-scar-signal-do-not-leak-9f2c/);
    assert.doesNotMatch(serialized, /sentinel-scar-resolution-do-not-leak-4b1a/);

    assert.equal(
      kinds.includes("ready.changed"),
      false,
      "a scar never changes the ready set and must never emit ready.changed",
    );
  });
});

test("minni_thread_replan appends an ordered replan mirror and coalesces ready.changed when supersession frees a dependent", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "a", title: "Slice A" },
      { id: "b", title: "Slice B", depends_on: ["a"] },
    ]);

    const readyBefore = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyBefore.ready.map((s) => s.id), ["a"]);

    await call("minni_thread_replan", { plan_id, drop_slice_ids: ["a"] });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    const kinds = events.events.map((e) => e.kind);
    const replanEvent = events.events.find((e) => e.kind === "replan");
    assert.ok(replanEvent, `expected an ordered replan event, got kinds: ${kinds}`);
    assert.equal(replanEvent.actor, DEFAULT_AGENT_ID);
    assert.ok(
      replanEvent.payload?.depends_on_superseded,
      `expected depends_on_superseded in the replan payload, got: ${JSON.stringify(replanEvent)}`,
    );

    const readyChanged = events.events.find((e) => e.kind === "ready.changed");
    assert.ok(readyChanged, "expected a coalesced ready.changed event when b's dependency is superseded");
    assert.deepEqual(readyChanged.payload, { slices: [{ id: "b", title: "Slice B" }] });

    const readyAfter = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfter.ready.map((s) => s.id), ["b"]);
  });
});

test("minni_thread_restore appends an ordered restored mirror reflecting the ready-set delta", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "a", title: "Slice A" },
      { id: "b", title: "Slice B", depends_on: ["a"] },
    ]);

    const doneA = await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "done",
      evidence: "Verified against docs/source-a.md",
    });
    const revAfterADone = doneA.plan.rev;
    // b is ready now that a is done.

    await call("minni_thread_replan", { plan_id, drop_slice_ids: ["b"] });
    const readyAfterDrop = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfterDrop.ready.map((s) => s.id), []);

    await call("minni_thread_restore", { plan_id, rev: revAfterADone });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 50 });
    const kinds = events.events.map((e) => e.kind);
    const restoredEvent = events.events.find((e) => e.kind === "restored");
    assert.ok(restoredEvent, `expected an ordered restored event, got kinds: ${kinds}`);
    assert.equal(restoredEvent.actor, DEFAULT_AGENT_ID);
    assert.deepEqual(restoredEvent.payload, { from_rev: revAfterADone });

    const readyChangedEvents = events.events.filter((e) => e.kind === "ready.changed");
    assert.ok(
      readyChangedEvents.some((e) => e.payload?.slices?.some((s) => s.id === "b")),
      "expected restore's ready.changed to show b becoming ready again",
    );

    const readyAfterRestore = await call("minni_thread_ready", { plan_id });
    assert.deepEqual(readyAfterRestore.ready.map((s) => s.id), ["b"]);
  });
});

test("minni_thread_update recovers a note-ahead-of-journal gap via state.recovered", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    await call("minni_thread_update", { plan_id, slice_id: "a", status: "in_progress" });

    const notePath = await findPlanNote(vaultPath, plan_id);
    const planBeforeGap = await rehydratePlan(notePath);
    planBeforeGap.next_action = "simulate note-ahead crash gap";
    await persistPlan(planBeforeGap, { vaultPath, notePath });
    const planAhead = await rehydratePlan(notePath);

    await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "blocked",
      evidence: "blocked for recovery check",
    });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const kinds = events.events.map((e) => e.kind);
    const recovered = events.events.find((e) => e.kind === "state.recovered");
    assert.ok(recovered, `expected a state.recovered event, got kinds: ${kinds}`);
    assert.equal(recovered.rev, planAhead.rev);
    assert.ok(events.events.some((e) => e.kind === "status_changed" && e.payload?.to === "blocked"));
  });
});

test("minni_thread_scar recovers a note-ahead-of-journal gap via state.recovered", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    await call("minni_thread_scar", { plan_id, kind: "dead_end", signal: "first scar before gap" });

    const notePath = await findPlanNote(vaultPath, plan_id);
    const planBeforeGap = await rehydratePlan(notePath);
    planBeforeGap.next_action = "simulate note-ahead crash gap";
    await persistPlan(planBeforeGap, { vaultPath, notePath });
    const planAhead = await rehydratePlan(notePath);

    await call("minni_thread_scar", { plan_id, kind: "failed_command", signal: "second scar after gap" });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const kinds = events.events.map((e) => e.kind);
    const recovered = events.events.find((e) => e.kind === "state.recovered");
    assert.ok(recovered, `expected a state.recovered event, got kinds: ${kinds}`);
    assert.equal(recovered.rev, planAhead.rev);
    assert.equal(events.events.filter((e) => e.kind === "scar_added").length, 2);
  });
});

test("minni_thread_replan recovers a note-ahead-of-journal gap via state.recovered", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    await call("minni_thread_replan", { plan_id, add_slices: [{ title: "Slice B" }] });

    const notePath = await findPlanNote(vaultPath, plan_id);
    const planBeforeGap = await rehydratePlan(notePath);
    planBeforeGap.next_action = "simulate note-ahead crash gap";
    await persistPlan(planBeforeGap, { vaultPath, notePath });
    const planAhead = await rehydratePlan(notePath);

    await call("minni_thread_replan", { plan_id, add_slices: [{ title: "Slice C" }] });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const kinds = events.events.map((e) => e.kind);
    const recovered = events.events.find((e) => e.kind === "state.recovered");
    assert.ok(recovered, `expected a state.recovered event, got kinds: ${kinds}`);
    assert.equal(recovered.rev, planAhead.rev);
    assert.equal(events.events.filter((e) => e.kind === "replan").length, 2);
  });
});

test("minni_thread_restore recovers a note-ahead-of-journal gap via state.recovered", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const doneA = await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "done",
      evidence: "Verified against docs/source-a.md",
    });
    const targetRev = doneA.plan.rev;
    await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "blocked",
      evidence: "reopen for restore recovery test",
    });

    const notePath = await findPlanNote(vaultPath, plan_id);
    const planBeforeGap = await rehydratePlan(notePath);
    planBeforeGap.next_action = "simulate note-ahead crash gap";
    await persistPlan(planBeforeGap, { vaultPath, notePath });
    const planAhead = await rehydratePlan(notePath);

    await call("minni_thread_restore", { plan_id, rev: targetRev });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const kinds = events.events.map((e) => e.kind);
    const recovered = events.events.find((e) => e.kind === "state.recovered");
    assert.ok(recovered, `expected a state.recovered event, got kinds: ${kinds}`);
    assert.equal(recovered.rev, planAhead.rev);
    assert.ok(events.events.some((e) => e.kind === "restored"));
  });
});

test("journal-ahead-of-note blocks minni_thread_update, scar, replan, and restore as thread_inconsistent", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    const journalPath = journalPathFor(notePath, plan_id);
    const plan = await rehydratePlan(notePath);

    await writeFile(
      journalPath,
      `# Minni Plan Journal\n\n## events\n${JSON.stringify({
        thread_event_batch: [{
          seq: 99,
          rev: plan.rev + 5,
          event_id: "ahead",
          idempotency_key: "ahead",
          actor: "test",
          kind: "slice.completed",
          at: new Date().toISOString(),
        }],
      })}\n`,
      "utf8",
    );

    for (const tool of [
      {
        name: "minni_thread_update",
        operation: "plan.update",
        args: { slice_id: "a", status: "in_progress" },
      },
      {
        name: "minni_thread_scar",
        operation: "plan.scar",
        args: { kind: "dead_end", signal: "must not commit" },
      },
      {
        name: "minni_thread_replan",
        operation: "plan.replan",
        args: { add_slices: [{ title: "Slice C" }] },
      },
    ]) {
      const result = await call(tool.name, { plan_id, ...tool.args });
      assert.equal(result.status, "error", `${tool.name}: ${JSON.stringify(result)}`);
      assert.equal(result.operation, tool.operation);
      assert.equal(result.code, "THREAD_INCONSISTENT");
      assert.match(result.error, /thread_inconsistent/);
    }
    // minni_thread_restore pre-existing behavior (unrelated to this fix):
    // it wraps every thrown error from its withThreadLock body — including
    // "revision not found" — into a soft { error } result rather than an
    // MCP-level isError response, so it never rejects via call().
    const restoreResult = await call("minni_thread_restore", { plan_id, rev: plan.rev });
    assert.match(restoreResult.error, /thread_inconsistent/);
  });
});

test("no evidence, scar text, claim token, or private path leaks into ordered payloads from update/scar/replan/restore", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "a", title: "Slice A" },
      { id: "b", title: "Slice B" },
    ]);
    const SENTINEL_EVIDENCE = "sentinel-evidence-9f2c-do-not-leak";
    const SENTINEL_SIGNAL = "sentinel-signal-4b1a-do-not-leak";
    const SENTINEL_RESOLUTION = "sentinel-resolution-77aa-do-not-leak";

    const doneA = await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "done",
      evidence: SENTINEL_EVIDENCE,
    });
    await call("minni_thread_scar", {
      plan_id,
      kind: "dead_end",
      signal: SENTINEL_SIGNAL,
      resolution: SENTINEL_RESOLUTION,
    });
    await call("minni_thread_replan", { plan_id, add_slices: [{ title: "Slice C" }] });
    await call("minni_thread_restore", { plan_id, rev: doneA.plan.rev });

    const events = await call("minni_thread_events", { plan_id, since_seq: 0, limit: 200 });
    const serialized = JSON.stringify(events.events);
    for (const sentinel of [SENTINEL_EVIDENCE, SENTINEL_SIGNAL, SENTINEL_RESOLUTION]) {
      assert.doesNotMatch(serialized, new RegExp(sentinel), `ordered payloads leaked sentinel text: ${sentinel}`);
    }
    assert.doesNotMatch(
      serialized,
      /\.runtime[\\/]thread-claims/,
      "ordered payloads must never reference the private claim envelope path",
    );
    assert.doesNotMatch(
      serialized,
      /\btoken\b/i,
      "ordered payloads must never reference a claim token field",
    );
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

test("minni_thread_worker_update history-append error never leaks the vault note path", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    await call("minni_thread_assign", {
      plan_id,
      slice_id: "a",
      worker_agent_id: "worker-a",
    });
    const claim = await call("minni_thread_claim", {
      plan_id,
      slice_id: "a",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-for-path-leak",
    });
    assert.ok(
      claim.token,
      `claim must return a token before the history-append probe, got ${JSON.stringify(claim)}`,
    );
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan must have a vault note");
    const historyFile = historyPathFor(notePath);
    await rm(historyFile, { force: true });
    await mkdir(historyFile);

    const result = await call("minni_thread_worker_update", {
      plan_id,
      slice_id: "a",
      worker_agent_id: "worker-a",
      claim_token: claim.token,
      idempotency_key: "complete-history-eisdir",
      action: "complete",
      evidence: "Completed despite a broken history append",
    });
    assert.equal(result.status, "error");
    assert.equal(result.operation, "plan.worker_update");
    assert.equal(result.code, "PLAN_HISTORY_APPEND_FAILED");
    assert.equal(result.notePath, undefined);
    assert.equal(result.filePath, undefined);
    assert.match(result.error, /note committed at rev/);
    assert.match(result.error, /EISDIR/);
    assert.equal(
      result.error.includes(notePath),
      false,
      "model-facing MCP error must not embed PlanHistoryAppendError.notePath",
    );
    assert.equal(
      result.error.includes(historyFile),
      false,
      "model-facing MCP error must not embed the history file path from a real Node EISDIR",
    );
    assert.doesNotMatch(
      JSON.stringify(result),
      /wiki\/artifacts/,
      "model-facing MCP error payload must not contain a vault artifacts path",
    );
    assert.equal(
      JSON.stringify(result).includes(historyFile),
      false,
      "model-facing MCP JSON must not embed the history file path",
    );
  });
});

test("minni_thread_claim EISDIR journal is a typed MCP error, not a path-bearing transport crash", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan must have a vault note");
    const journalPath = journalPathFor(notePath, plan_id);
    await rm(journalPath, { force: true });
    await mkdir(journalPath);

    const result = await call("minni_thread_claim", {
      plan_id,
      slice_id: "a",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-eisdir-journal",
    });
    assert.equal(result.status, "error");
    assert.equal(result.operation, "plan.claim");
    assert.equal(result.code, "THREAD_JOURNAL_UNREADABLE");
    assert.match(result.error, /EISDIR|unreadable/);
    assert.equal(result.error.includes(journalPath), false);
    assert.equal(result.error.includes(notePath), false);
    assert.equal(result.journalPath, undefined);
    assert.equal(result.notePath, undefined);
    assert.equal(result.filePath, undefined);
    assert.doesNotMatch(
      JSON.stringify(result),
      /wiki\/artifacts/,
      "model-facing claim error must not embed a vault artifacts path",
    );
  });
});

test("minni_thread_status EISDIR journal does not leak a vault path as a transport crash", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan must have a vault note");
    const journalPath = journalPathFor(notePath, plan_id);
    await rm(journalPath, { force: true });
    await mkdir(journalPath);

    const result = await call("minni_thread_status", { plan_id });
    assert.equal(typeof result.error, "undefined", `status must not fail at plan-note discovery: ${JSON.stringify(result)}`);
    assert.ok(result.view, "status must still resolve the plan note beside an unreadable journal");
    assert.equal(result.rev !== undefined, true);
    assert.equal(
      JSON.stringify(result).includes(journalPath),
      false,
      "status payload must not embed the journal path",
    );
    assert.doesNotMatch(
      JSON.stringify(result),
      /wiki\/artifacts/,
      "status payload must not embed a vault artifacts path",
    );
  });
});

test("minni_thread_events fails closed on an unreadable journal instead of an empty cursor", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan must have a vault note");
    const journalPath = journalPathFor(notePath, plan_id);
    await rm(journalPath, { force: true });
    await mkdir(journalPath);

    const result = await call("minni_thread_events", {
      plan_id,
      since_seq: 0,
      limit: 50,
    });
    assert.notEqual(
      result.status,
      undefined,
      "unreadable journal must not look like a successful empty cursor",
    );
    assert.equal(result.status, "error");
    assert.equal(result.operation, "plan.events");
    assert.equal(Array.isArray(result.events), false);
    assert.match(result.error, /EISDIR|unreadable/);
    assert.equal(result.error.includes(journalPath), false);
    assert.equal(result.error.includes(notePath), false);
    assert.equal(result.journalPath, undefined);
    assert.doesNotMatch(
      JSON.stringify(result),
      /wiki\/artifacts/,
      "model-facing events error must not embed the journal path",
    );
  });
});

test("minni_thread_restore catch never forwards a path-bearing Error.message", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan must have a vault note");
    const journalPath = journalPathFor(notePath, plan_id);
    await rm(journalPath, { force: true });
    await mkdir(journalPath);

    const result = await call("minni_thread_restore", {
      plan_id,
      rev: 1,
    });
    assert.equal(typeof result.error, "string");
    assert.equal(result.error.includes(notePath), false);
    assert.equal(result.error.includes(journalPath), false);
    assert.doesNotMatch(
      JSON.stringify(result),
      /wiki\/artifacts/,
      "restore catch must not embed a vault artifacts path",
    );
  });
});

// Cassandra PR #371 round 4: update/scar/replan write the ordered journal via
// prepareThreadMutation / appendJournal / appendJournalLine but were not
// wrapped in threadWorkerErrorResult. A raw Node EISDIR/EACCES from those
// writes becomes a transport-level isError whose .message embeds the vault
// journal path. Sanitizer unit tests never ran on this MCP path.
const ORCHESTRATOR_JOURNAL_MUTATIONS = [
  {
    name: "minni_thread_update",
    operation: "plan.update",
    args: { slice_id: "a", status: "in_progress" },
  },
  {
    name: "minni_thread_scar",
    operation: "plan.scar",
    args: { kind: "dead_end", signal: "must not leak a journal path" },
  },
  {
    name: "minni_thread_replan",
    operation: "plan.replan",
    args: { add_slices: [{ title: "Slice C" }] },
  },
];

function assertPathFreeOrchestratorJournalError(result, { name, operation, journalPath, notePath }) {
  assert.equal(
    result.status,
    "error",
    `${name} must return a typed MCP error, not a transport-level isError: ${JSON.stringify(result)}`,
  );
  assert.equal(result.operation, operation);
  assert.equal(typeof result.error, "string");
  assert.equal(result.error.includes(journalPath), false, `${name} leaked journalPath`);
  assert.equal(result.error.includes(notePath), false, `${name} leaked notePath`);
  assert.equal(result.journalPath, undefined);
  assert.equal(result.notePath, undefined);
  assert.equal(result.filePath, undefined);
  assert.doesNotMatch(
    JSON.stringify(result),
    /wiki\/artifacts/,
    `${name} must not embed a vault artifacts path`,
  );
}

test("minni_thread_update/scar/replan EISDIR journal is a typed MCP error, not a path-bearing transport crash", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    for (const tool of ORCHESTRATOR_JOURNAL_MUTATIONS) {
      const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
      const notePath = await findPlanNote(vaultPath, plan_id);
      assert.ok(notePath, "seeded plan must have a vault note");
      const journalPath = journalPathFor(notePath, plan_id);
      await rm(journalPath, { force: true });
      await mkdir(journalPath);

      const result = await call(tool.name, { plan_id, ...tool.args });
      assertPathFreeOrchestratorJournalError(result, {
        name: tool.name,
        operation: tool.operation,
        journalPath,
        notePath,
      });
      assert.match(result.error, /EISDIR|unreadable/);
    }
  });
});

test("minni_thread_update/scar/replan EACCES journal append is a typed MCP error, not a path-bearing transport crash", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    for (const tool of ORCHESTRATOR_JOURNAL_MUTATIONS) {
      const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
      const notePath = await findPlanNote(vaultPath, plan_id);
      assert.ok(notePath, "seeded plan must have a vault note");
      const journalPath = journalPathFor(notePath, plan_id);
      await chmod(journalPath, 0o444);
      try {
        const result = await call(tool.name, { plan_id, ...tool.args });
        assertPathFreeOrchestratorJournalError(result, {
          name: tool.name,
          operation: tool.operation,
          journalPath,
          notePath,
        });
        assert.match(result.error, /EACCES|unreadable|thread worker failed/);
      } finally {
        await chmod(journalPath, 0o644).catch(() => {});
      }
    }
  });
});

test("minni_thread_update/scar/replan funnel journal I/O failures through threadWorkerErrorResult", async () => {
  const source = await readFile(SRC_PATH, "utf8");
  for (const { name, operation } of ORCHESTRATOR_JOURNAL_MUTATIONS) {
    const block = toolBlock(source, name);
    assert.match(
      block,
      /try \{/,
      `${name} must catch journal I/O instead of leaking a raw JSON-RPC error`,
    );
    assert.match(
      block,
      new RegExp(`threadWorkerErrorResult\\(\\s*"${operation.replace(".", "\\.")}"`),
      `${name} must sanitize throws through threadWorkerErrorResult("${operation}")`,
    );
  }
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

test("minni_thread_worker_update and minni_thread_claim reject a whitespace-only idempotency_key at the SDK layer", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call, send, awaitResponse }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "alpha", title: "Alpha slice" }]);
    await call("minni_thread_assign", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
    });

    const claimId = 9101;
    send({
      jsonrpc: "2.0",
      id: claimId,
      method: "tools/call",
      params: {
        name: "minni_thread_claim",
        arguments: {
          plan_id,
          slice_id: "alpha",
          worker_agent_id: "worker-a",
          idempotency_key: "   ",
        },
      },
    });
    const claimReply = await awaitResponse(claimId);
    assert.equal(
      claimReply.result?.isError,
      true,
      `a whitespace-only idempotency_key must be rejected before reaching thread-worker: ${JSON.stringify(claimReply)}`,
    );

    const claim = await call("minni_thread_claim", {
      plan_id,
      slice_id: "alpha",
      worker_agent_id: "worker-a",
      idempotency_key: "claim-alpha-blank-check",
    });

    const updateId = 9102;
    send({
      jsonrpc: "2.0",
      id: updateId,
      method: "tools/call",
      params: {
        name: "minni_thread_worker_update",
        arguments: {
          plan_id,
          slice_id: "alpha",
          worker_agent_id: "worker-a",
          claim_token: claim.token,
          idempotency_key: "\t\n ",
          action: "start",
        },
      },
    });
    const updateReply = await awaitResponse(updateId);
    assert.equal(
      updateReply.result?.isError,
      true,
      `a whitespace-only idempotency_key must be rejected before reaching thread-worker: ${JSON.stringify(updateReply)}`,
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
    /idempotency_key:\s*nonBlankIdempotencyKey/,
    "idempotency_key must be the shared, whitespace-rejecting schema",
  );
  assert.match(
    source,
    /const nonBlankIdempotencyKey\s*=\s*z\s*\n?\s*\.string\(\)\s*\n?\s*\.min\(1\)\s*\n?\s*\.refine\(/,
    "the shared idempotency_key schema must reject whitespace-only values, not just length 0",
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
