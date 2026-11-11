// Wave 1 v1 MCP surface E2E + Thread server journal/security invariants.
// Exercises the exact 12 v1 MCP tools over the REAL spawned MCP server (stdio)
// backed by a fake Unix-socket daemon handling gate.shared, daemon.handoff,
// minni_list_pending_handoffs, and minni_ack_handoff.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  createPlan,
  findPlanNote,
  journalPathFor,
  persistPlan,
  rehydratePlan,
} from "../dist/plan.js";
import { readThreadEvents } from "../dist/thread-events.js";
import { DEFAULT_AGENT_ID } from "../dist/config.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;
const SRC_PATH = new URL("../src/server.ts", import.meta.url);

const EXPECTED_12_V1_TOOLS = [
  "minni_recall",
  "minni_drill",
  "minni_learn",
  "minni_list_candidates",
  "minni_resolve_candidate",
  "minni_status",
  "minni_handoff_send",
  "minni_handoff_inbox",
  "minni_thread_create",
  "minni_thread_status",
  "minni_thread_update",
  "minni_thread_scar",
];

async function startFakeDaemon(socketPath, homeDir) {
  const rpcLog = [];
  const handoffs = new Map();

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
        const { id, method, params = {} } = request;
        rpcLog.push({ method, params });

        if (method === "daemon.handoff") {
          const packet = params.packet ?? {};
          const lease_id = packet.lease_id ?? `lease-${handoffs.size + 1}`;
          const expires_at =
            packet.expires_at ?? new Date(Date.now() + 86_400_000).toISOString();
          const packetPath = path.join(homeDir, `${lease_id}.packet.json`);
          void writeFile(packetPath, JSON.stringify(packet, null, 2), "utf8")
            .then(() => {
              handoffs.set(lease_id, {
                lease_id,
                from_agent: params.from_agent ?? packet.from_agent ?? null,
                to_agent: params.to_agent ?? packet.to_agent ?? null,
                task: packet.task ?? "",
                expires_at,
                path: packetPath,
                acked: false,
                ack_status: null,
              });
              socket.write(
                `${JSON.stringify({
                  jsonrpc: "2.0",
                  id,
                  result: {
                    ok: true,
                    delivered: true,
                    status: "ok",
                    lease_id,
                    expires_at,
                  },
                })}\n`,
              );
            })
            .catch((err) => {
              socket.write(
                `${JSON.stringify({
                  jsonrpc: "2.0",
                  id,
                  error: { code: -32000, message: String(err) },
                })}\n`,
              );
            });
          continue;
        }

        if (method === "minni_list_pending_handoffs") {
          const targetAgent = params.agent_id;
          const pending = [...handoffs.values()].filter(
            (h) => !h.acked && (!targetAgent || h.to_agent === targetAgent),
          );
          socket.write(
            `${JSON.stringify({
              jsonrpc: "2.0",
              id,
              result: { ok: true, handoffs: pending },
            })}\n`,
          );
          continue;
        }

        if (method === "minni_ack_handoff") {
          const item = handoffs.get(params.lease_id);
          if (!item) {
            socket.write(
              `${JSON.stringify({
                jsonrpc: "2.0",
                id,
                error: { code: -32004, message: `unknown lease: ${params.lease_id}` },
              })}\n`,
            );
            continue;
          }
          item.acked = true;
          item.ack_status = params.status ?? "accepted";
          socket.write(
            `${JSON.stringify({
              jsonrpc: "2.0",
              id,
              result: {
                ok: true,
                lease_id: item.lease_id,
                status: item.ack_status,
              },
            })}\n`,
          );
          continue;
        }

        if (method.startsWith("agent_ping")) {
          socket.write(
            `${JSON.stringify({
              jsonrpc: "2.0",
              id,
              error: { code: -32601, message: `Retired RPC: ${method}` },
            })}\n`,
          );
          continue;
        }

        socket.write(
          `${JSON.stringify({
            jsonrpc: "2.0",
            id,
            result: { ok: true, results: [] },
          })}\n`,
        );
      }
    });
  });

  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  return { daemon, rpcLog, handoffs };
}

async function withMcpSession(t, fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-server-"));
  const home = path.join(root, "home");
  await mkdir(home, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const { daemon, rpcLog, handoffs } = await startFakeDaemon(socketPath, home);
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

  const callRaw = async (name, args) => {
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
    const text = reply.result.content[0].text;
    return {
      text,
      bytes: Buffer.byteLength(text, "utf8"),
      data: JSON.parse(text),
    };
  };

  const call = async (name, args) => (await callRaw(name, args)).data;

  const listTools = async () => {
    const id = nextId++;
    send({ jsonrpc: "2.0", id, method: "tools/list", params: {} });
    const reply = await awaitResponse(id);
    if (reply.error) {
      throw new Error(`tools/list: ${JSON.stringify(reply.error)}`);
    }
    return reply.result.tools;
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
    return await fn({
      root,
      vaultPath: root,
      call,
      callRaw,
      listTools,
      rpcLog,
      handoffs,
      send,
      awaitResponse,
    });
  } finally {
    await rm(root, { recursive: true, force: true }).catch(() => {});
  }
}

async function seedPlan(vaultPath, slices) {
  const created = await createPlan(
    { goal: "Wave 1 v1 thread surface", slices, vaultPath },
    { vaultPath },
  );
  return created.plan.plan_id;
}

async function readOrderedJournal(vaultPath, planId, sinceSeq = 0, limit = 200) {
  const notePath = await findPlanNote(vaultPath, planId);
  assert.ok(notePath, `expected plan note for ${planId}`);
  const journalPath = journalPathFor(notePath, planId);
  return readThreadEvents(journalPath, sinceSeq, limit);
}

test("Wave 1 E2E: tools/list registers the exact 12 v1 MCP tools and nothing else", async (t) => {
  await withMcpSession(t, async ({ listTools }) => {
    const tools = await listTools();
    assert.deepEqual(
      tools.map((tool) => tool.name),
      EXPECTED_12_V1_TOOLS,
    );
  });
});

test("Wave 1 E2E: minni_handoff_send + minni_handoff_inbox handle self and cross-agent handoffs in <1 KB without agent_ping diversion", async (t) => {
  await withMcpSession(t, async ({ callRaw, rpcLog }) => {
    // 1. Self-handoff (to_agent omitted -> defaults to DEFAULT_AGENT_ID)
    const selfSend = await callRaw("minni_handoff_send", {
      task: "Continue Wave 1 verification in the next session",
      summary: "All 12 MCP tools wired; verify final test suite",
      files: ["plugins/minni/src/server.ts", "plugins/minni/src/plan.ts"],
      next_steps: ["Run npm test", "Run pytest"],
      open_questions: ["Should we prune stale branches after attest?"],
    });
    assert.ok(
      selfSend.bytes < 1024,
      `minni_handoff_send receipt must be < 1024 bytes, got ${selfSend.bytes}`,
    );
    assert.equal(selfSend.data.status, "ok");
    assert.equal(selfSend.data.from_agent, DEFAULT_AGENT_ID);
    assert.equal(selfSend.data.to_agent, DEFAULT_AGENT_ID);
    assert.equal(typeof selfSend.data.lease_id, "string");
    assert.equal(typeof selfSend.data.expires_at, "string");

    // 2. Cross-agent handoff with "ping ... question" text must NEVER divert to agent_ping
    const crossSend = await callRaw("minni_handoff_send", {
      to_agent: "codex",
      task: "ping codex with a question: should we merge Slice 1B now?",
      summary: "Cross-agent review handoff",
    });
    assert.ok(
      crossSend.bytes < 1024,
      `cross-agent minni_handoff_send receipt must be < 1024 bytes, got ${crossSend.bytes}`,
    );
    assert.equal(crossSend.data.status, "ok");
    assert.equal(crossSend.data.from_agent, DEFAULT_AGENT_ID);
    assert.equal(crossSend.data.to_agent, "codex");
    assert.equal(
      rpcLog.some((entry) => entry.method.includes("ping")),
      false,
      "minni_handoff_send must never divert to retired agent_ping RPCs",
    );

    // 3. Listing pending inbox without ack leaves item pending and hydrates packet preview
    const listOnly = await callRaw("minni_handoff_inbox", {});
    assert.equal(listOnly.data.status, "ok");
    assert.equal(listOnly.data.count, 1);
    assert.equal(listOnly.data.acked_count, undefined);
    assert.equal(listOnly.data.handoffs[0].lease_id, selfSend.data.lease_id);
    assert.equal(
      listOnly.data.handoffs[0].summary,
      "All 12 MCP tools wired; verify final test suite",
    );
    assert.deepEqual(listOnly.data.handoffs[0].files, [
      "plugins/minni/src/server.ts",
      "plugins/minni/src/plan.ts",
    ]);

    // 4. Listing with ack: true reads AND clears all pending items for this agent in one call
    const readAndClear = await callRaw("minni_handoff_inbox", {
      ack: true,
      note: "Picked up cleanly in follow-up session",
    });
    assert.equal(readAndClear.data.status, "ok");
    assert.equal(readAndClear.data.count, 1);
    assert.equal(readAndClear.data.acked_count, 1);
    assert.equal(readAndClear.data.handoffs[0].ack_status, "accepted");

    const afterClear = await callRaw("minni_handoff_inbox", {});
    assert.equal(afterClear.data.count, 0);

    // 5. Direct single-lease acknowledgment with explicit rejection status
    const secondSelf = await callRaw("minni_handoff_send", {
      task: "Stale follow-up task",
    });
    const directAck = await callRaw("minni_handoff_inbox", {
      lease_id: secondSelf.data.lease_id,
      status: "rejected_stale",
      note: "Already superseded by newer slice",
    });
    assert.equal(directAck.data.status, "rejected_stale");
    assert.equal(directAck.data.lease_id, secondSelf.data.lease_id);
    assert.equal(directAck.data.acked, true);

    const afterDirectAck = await callRaw("minni_handoff_inbox", {});
    assert.equal(afterDirectAck.data.count, 0);
  });
});

test("Wave 1 E2E: 4 Thread tools stay <2 KB (<1 KB for scar) even on a >54 KB PlanArtifact and support inline activate", async (t) => {
  await withMcpSession(t, async ({ vaultPath, callRaw }) => {
    const heavySlices = Array.from({ length: 15 }, (_, idx) => ({
      id: `s${idx + 1}`,
      title: `Slice ${idx + 1}: consolidate subsystem surface #${idx + 1}`,
      gate: `npm --prefix plugins/minni test -- slice-${idx + 1}`,
      evidence: `Initial seeded context ${idx + 1}: ${"E".repeat(2200)}`,
    }));

    const created = await callRaw("minni_thread_create", {
      goal: "Consolidate MCP surface and keep all thread tool responses strictly under 2 KB",
      constraints: ["Never exceed 2 KB response budget", "Preserve full plan on disk"],
      slices: heavySlices,
      activate: true,
    });
    assert.ok(
      created.bytes < 2048,
      `minni_thread_create response must be < 2048 bytes, got ${created.bytes}`,
    );
    assert.equal(created.data.active, true);
    assert.equal(created.data.scar_tissue, undefined);
    assert.equal(created.data.slices.length, 15);
    const plan_id = created.data.plan_id;

    // Grow the persisted PlanArtifact well past 54 KB with 25 large scars
    for (let i = 1; i <= 25; i++) {
      const scarRes = await callRaw("minni_thread_scar", {
        plan_id,
        slice_id: `s${((i - 1) % 15) + 1}`,
        kind: "failed_command",
        signal: `scar-signal-${i}-${"S".repeat(600)}`,
        resolution: `scar-resolution-${i}-${"R".repeat(600)}`,
      });
      assert.ok(
        scarRes.bytes < 1024,
        `minni_thread_scar response must be < 1024 bytes, got ${scarRes.bytes}`,
      );
      assert.equal(scarRes.data.scar_count, i);
      assert.ok(
        scarRes.data.scar.signal.length <= 200,
        `scar signal preview must be <= 200 chars, got ${scarRes.data.scar.signal.length}`,
      );
      assert.equal(scarRes.data.plan, undefined);
    }

    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath);
    const noteStat = await stat(notePath);
    assert.ok(
      noteStat.size > 54 * 1024,
      `persisted plan note should exceed 54 KB for stress proof, got ${noteStat.size} bytes`,
    );

    // minni_thread_status must stay < 2048 bytes and cap each scar string to <= 160 chars
    const statusRes = await callRaw("minni_thread_status", { plan_id });
    assert.ok(
      statusRes.bytes < 2048,
      `minni_thread_status response must be < 2048 bytes on >54 KB plan, got ${statusRes.bytes}`,
    );
    assert.equal(statusRes.data.active, true);
    assert.ok(Array.isArray(statusRes.data.view.scars));
    assert.ok(statusRes.data.view.scars.length > 0);
    for (const scarLine of statusRes.data.view.scars) {
      assert.ok(
        scarLine.length <= 160,
        `compactPlanView scar line must be <= 160 chars, got ${scarLine.length}`,
      );
    }

    // minni_thread_update defaults compact to true (< 2048 bytes, no full plan dump)
    const updateDefault = await callRaw("minni_thread_update", {
      plan_id,
      slice_id: "s1",
      status: "done",
      evidence: `Verified s1 with huge evidence payload: ${"V".repeat(1500)}`,
    });
    assert.ok(
      updateDefault.bytes < 2048,
      `default minni_thread_update response must be < 2048 bytes, got ${updateDefault.bytes}`,
    );
    assert.equal(updateDefault.data.plan, undefined);
    assert.equal(updateDefault.data.updated_slice.id, "s1");
    assert.equal(updateDefault.data.updated_slice.status, "done");
    assert.ok(updateDefault.data.updated_slice.evidence.length <= 240);
    for (const scarLine of updateDefault.data.scars) {
      assert.ok(scarLine.length <= 160);
    }

    // Inline deactivate & re-activate via minni_thread_update
    const deactivated = await callRaw("minni_thread_update", {
      plan_id,
      activate: false,
    });
    assert.ok(deactivated.bytes < 2048);
    assert.equal(deactivated.data.active, false);
    const statusAfterDeactivate = await callRaw("minni_thread_status", { plan_id });
    assert.equal(statusAfterDeactivate.data.active, false);

    const reactivated = await callRaw("minni_thread_update", {
      plan_id,
      activate: true,
    });
    assert.ok(reactivated.bytes < 2048);
    assert.equal(reactivated.data.active, true);

    // Explicit opt-in include_full_plan: true still returns the full persisted PlanArtifact
    const updateFull = await callRaw("minni_thread_update", {
      plan_id,
      slice_id: "s2",
      status: "done",
      evidence: "Verified s2 with opt-in full plan",
      include_full_plan: true,
    });
    assert.ok(updateFull.data.plan);
    assert.equal(updateFull.data.plan.scar_tissue.length, 25);
  });
});

test("minni_thread_update appends an ordered status_changed mirror with the server actor and coalesces ready.changed", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "a", title: "Slice A" },
      { id: "b", title: "Slice B", depends_on: ["a"] },
    ]);

    const before = await readOrderedJournal(vaultPath, plan_id, 0, 50);
    assert.equal(
      before.events.length,
      0,
      "no ordered events exist before the first orchestrator mutation",
    );

    await call("minni_thread_update", {
      plan_id,
      slice_id: "a",
      status: "done",
      evidence: "Verified against docs/source-a.md",
    });

    const after = await readOrderedJournal(vaultPath, plan_id, 0, 50);
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

    const cursor = await readOrderedJournal(vaultPath, plan_id, after.next_seq, 50);
    assert.deepEqual(
      cursor.events,
      [],
      "cursor read must exclude seq at or below since_seq for mirrored kinds",
    );
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

    const events = await readOrderedJournal(vaultPath, plan_id, 0, 50);
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

    const events = await readOrderedJournal(vaultPath, plan_id, 0, 200);
    const kinds = events.events.map((e) => e.kind);
    const recovered = events.events.find((e) => e.kind === "state.recovered");
    assert.ok(recovered, `expected a state.recovered event, got kinds: ${kinds}`);
    assert.equal(recovered.rev, planAhead.rev);
    assert.ok(
      events.events.some((e) => e.kind === "status_changed" && e.payload?.to === "blocked"),
    );
  });
});

test("minni_thread_scar recovers a note-ahead-of-journal gap via state.recovered", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    await call("minni_thread_scar", {
      plan_id,
      kind: "dead_end",
      signal: "first scar before gap",
    });

    const notePath = await findPlanNote(vaultPath, plan_id);
    const planBeforeGap = await rehydratePlan(notePath);
    planBeforeGap.next_action = "simulate note-ahead crash gap";
    await persistPlan(planBeforeGap, { vaultPath, notePath });
    const planAhead = await rehydratePlan(notePath);

    await call("minni_thread_scar", {
      plan_id,
      kind: "failed_command",
      signal: "second scar after gap",
    });

    const events = await readOrderedJournal(vaultPath, plan_id, 0, 200);
    const kinds = events.events.map((e) => e.kind);
    const recovered = events.events.find((e) => e.kind === "state.recovered");
    assert.ok(recovered, `expected a state.recovered event, got kinds: ${kinds}`);
    assert.equal(recovered.rev, planAhead.rev);
    assert.equal(events.events.filter((e) => e.kind === "scar_added").length, 2);
  });
});

test("journal-ahead-of-note blocks minni_thread_update and minni_thread_scar as thread_inconsistent", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    const journalPath = journalPathFor(notePath, plan_id);
    const plan = await rehydratePlan(notePath);

    await writeFile(
      journalPath,
      `# Minni Plan Journal\n\n## events\n${JSON.stringify({
        thread_event_batch: [
          {
            seq: 99,
            rev: plan.rev + 5,
            event_id: "ahead",
            idempotency_key: "ahead",
            actor: "test",
            kind: "slice.completed",
            at: new Date().toISOString(),
          },
        ],
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
    ]) {
      const result = await call(tool.name, { plan_id, ...tool.args });
      assert.equal(result.status, "error", `${tool.name}: ${JSON.stringify(result)}`);
      assert.equal(result.operation, tool.operation);
      assert.equal(result.code, "THREAD_INCONSISTENT");
      assert.match(result.error, /thread_inconsistent/);
    }
  });
});

test("no evidence, scar text, claim token, or private path leaks into ordered payloads from update or scar", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [
      { id: "a", title: "Slice A" },
      { id: "b", title: "Slice B" },
    ]);
    const SENTINEL_EVIDENCE = "sentinel-evidence-9f2c-do-not-leak";
    const SENTINEL_SIGNAL = "sentinel-signal-4b1a-do-not-leak";
    const SENTINEL_RESOLUTION = "sentinel-resolution-77aa-do-not-leak";

    await call("minni_thread_update", {
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

    const events = await readOrderedJournal(vaultPath, plan_id, 0, 200);
    const serialized = JSON.stringify(events.events);
    for (const sentinel of [SENTINEL_EVIDENCE, SENTINEL_SIGNAL, SENTINEL_RESOLUTION]) {
      assert.doesNotMatch(
        serialized,
        new RegExp(sentinel),
        `ordered payloads leaked sentinel text: ${sentinel}`,
      );
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

test("minni_thread_status EISDIR journal does not leak a vault path as a transport crash", async (t) => {
  await withMcpSession(t, async ({ vaultPath, call }) => {
    const plan_id = await seedPlan(vaultPath, [{ id: "a", title: "Slice A" }]);
    const notePath = await findPlanNote(vaultPath, plan_id);
    assert.ok(notePath, "seeded plan must have a vault note");
    const journalPath = journalPathFor(notePath, plan_id);
    await rm(journalPath, { force: true });
    await mkdir(journalPath);

    const result = await call("minni_thread_status", { plan_id });
    assert.equal(
      typeof result.error,
      "undefined",
      `status must not fail at plan-note discovery: ${JSON.stringify(result)}`,
    );
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
];

function assertPathFreeOrchestratorJournalError(
  result,
  { name, operation, journalPath, notePath },
) {
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

test("minni_thread_update and minni_thread_scar EISDIR journal is a typed MCP error, not a path-bearing transport crash", async (t) => {
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

test("minni_thread_update and minni_thread_scar EACCES journal append is a typed MCP error, not a path-bearing transport crash", async (t) => {
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

function toolBlock(source, toolName) {
  const start = source.indexOf(`server.registerTool(\n  "${toolName}"`);
  assert.notEqual(start, -1, `${toolName} registration not found`);
  const next = source.indexOf("server.registerTool(", start + 1);
  return source.slice(start, next === -1 ? undefined : next);
}

test("minni_thread_status, minni_thread_update, and minni_thread_scar funnel errors through threadWorkerErrorResult and never accept vaultPath", async () => {
  const source = await readFile(SRC_PATH, "utf8");
  for (const { name, operation } of [
    { name: "minni_thread_status", operation: "plan.status" },
    ...ORCHESTRATOR_JOURNAL_MUTATIONS,
  ]) {
    const block = toolBlock(source, name);
    const schemaStart = block.indexOf("inputSchema:");
    const handlerStart = block.indexOf("async (");
    const schema = block.slice(schemaStart, handlerStart);
    assert.doesNotMatch(
      schema,
      /vaultPath|vault_path/,
      `${name} must not accept a caller-supplied vault path`,
    );
    assert.match(
      block,
      /resolvePlanTarget\(planIdInput\)/,
      `${name} must resolve plan_id through server-pinned resolvePlanTarget`,
    );
    assert.match(
      block,
      /try \{/,
      `${name} must catch journal/storage I/O instead of leaking a raw JSON-RPC error`,
    );
    assert.match(
      block,
      new RegExp(`threadWorkerErrorResult\\(\\s*"${operation.replace(".", "\\.")}"`),
      `${name} must sanitize throws through threadWorkerErrorResult("${operation}")`,
    );
  }
});
