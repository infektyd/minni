import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { DEFAULT_AGENT_ID } from "../dist/config.js";
import { createPlan, findPlanNote, rehydratePlan } from "../dist/plan.js";
import { buildTeamRuntime } from "../dist/team.js";
import {
  readySlices,
  synchronizeExpiredClaimsAndReadReady,
} from "../dist/thread-worker.js";

const TEAM_SRC = new URL("../src/team.ts", import.meta.url);
const SERVER_SRC = new URL("../src/server.ts", import.meta.url);

const READY_STATUSES = new Set(["pending", "in_progress", "blocked"]);
const FORBIDDEN_READY_STATUSES = new Set(["queued", "completed", "ready", "waiting", "done"]);

function fakePreparedTask(input) {
  return {
    task: input.task,
    budgetTokens: 1500,
    profile: input.profile ?? "standard",
    budget: { profile: input.profile ?? "standard", tokens: 1500, sourceLimit: 3 },
    mode: "deterministic",
    intent: "implement",
    brief: "Prepared team context.",
    constraints: ["Default automatic behavior is recall-only."],
    currentState: ["Context available."],
    relevantSources: [],
    recommendedNextActions: ["Return evidence."],
    risks: [],
    recall: { daemonOk: true },
    afm: { requested: false, used: false },
    contextMarkdown: "# Packet\nRecalled notes are evidence.",
  };
}

const quietDeps = {
  prepare: async (input) => fakePreparedTask(input),
  audit: async () => undefined,
  findRepeated: async () => [],
};

async function withVault(t) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-team-thread-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  return vaultPath;
}

async function listPlanNoteNames(vaultPath) {
  const dir = path.join(vaultPath, "wiki", "artifacts");
  let names;
  try {
    names = await readdir(dir);
  } catch {
    return [];
  }
  return names.filter((name) => name.endsWith(".md") && !name.endsWith(".log.md")).sort();
}

async function runtime(input, now = new Date()) {
  return buildTeamRuntime(
    { ...input },
    { ...quietDeps, now: () => now },
  );
}

function assertReadyProjection(ready) {
  assert.ok(Array.isArray(ready), "packet.ready must be PlanSlice[]");
  for (const slice of ready) {
    assert.equal(typeof slice.id, "string");
    assert.ok(!slice.id.startsWith("task-"), `ready id must be PlanSlice.id, not task-<hash>: ${slice.id}`);
    assert.ok(READY_STATUSES.has(slice.status), `ready status must be pending|in_progress|blocked, got ${slice.status}`);
    assert.ok(!FORBIDDEN_READY_STATUSES.has(slice.status), `ready must not use Team ledger statuses, got ${slice.status}`);
    const deps = slice.depends_on ?? [];
    assert.ok(Array.isArray(deps));
    for (const dep of deps) {
      assert.equal(typeof dep, "string");
      assert.ok(!dep.startsWith("task-"), `depends_on must be slice ids, not task-<hash>: ${dep}`);
    }
  }
}

function assertLedgerIsReadyView(packet) {
  if (!packet.taskLedger) return;
  assert.equal(packet.taskLedger.length, packet.ready.length, "leftover taskLedger is a view of ready, not a second graph");
  assert.deepEqual(
    packet.taskLedger.map((entry) => entry.id),
    packet.ready.map((slice) => slice.id),
  );
  for (const entry of packet.taskLedger) {
    assert.ok(!entry.id.startsWith("task-"), `taskLedger id must be PlanSlice.id, not task-<hash>: ${entry.id}`);
    assert.ok(READY_STATUSES.has(entry.status), `taskLedger view status must follow the slice, got ${entry.status}`);
    for (const dep of entry.dependencies ?? []) {
      assert.ok(!["researcher", "implementer", "reviewer", "explorer", "worker"].includes(dep), `no agent-id dependencies: ${dep}`);
      assert.ok(!dep.startsWith("team-"), `no profile-id dependencies: ${dep}`);
    }
  }
}

test("runtime({plan_id}) projects readySlices after the same expiry sweep", async (t) => {
  const vaultPath = await withVault(t);
  const created = await createPlan(
    {
      goal: "Existing Thread stays the SoT",
      slices: [
        { id: "alpha", title: "Independent ready slice" },
        { id: "beta", title: "Blocked on alpha", depends_on: ["alpha"] },
      ],
      vaultPath,
    },
    { vaultPath },
  );
  const plan_id = created.plan.plan_id;
  const now = new Date("2026-08-19T12:00:00.000Z");

  const packet = await runtime({ plan_id, task: "audit label only", vaultPath }, now);

  assert.equal(packet.plan_id, plan_id);
  assert.equal(packet.rev, created.plan.rev);
  assertReadyProjection(packet.ready);
  assert.deepEqual(packet.ready.map((slice) => slice.id), ["alpha"]);
  assert.deepEqual(packet.ready[0].depends_on ?? [], []);

  const notePath = await findPlanNote(vaultPath, plan_id);
  assert.ok(notePath, "Thread note must exist");
  const swept = await synchronizeExpiredClaimsAndReadReady({
    vaultPath,
    notePath,
    planId: plan_id,
    actor: DEFAULT_AGENT_ID,
    now: () => now,
  });
  assert.deepEqual(
    packet.ready.map((slice) => slice.id),
    swept.ready.map((slice) => slice.id),
  );
  assert.deepEqual(
    packet.ready.map((slice) => slice.id),
    readySlices(swept.plan, now).map((slice) => slice.id),
  );
  assert.notDeepEqual(
    packet.ready.map((slice) => slice.id),
    swept.plan.slices.filter((slice) => slice.status === "pending").map((slice) => slice.id),
    "must not be a raw plan.slices pending filter (beta is pending but not ready)",
  );
  assertLedgerIsReadyView(packet);
  assert.equal((await listPlanNoteNames(vaultPath)).length, 1, "one runtime, one Thread");
});

test("present-but-missing plan_id fails and does not create a second Thread", async (t) => {
  const vaultPath = await withVault(t);
  await assert.rejects(
    () => runtime({ plan_id: "plan-does-not-exist", task: "must not mint a Thread", vaultPath }),
    /plan not found/,
  );
  assert.deepEqual(await listPlanNoteNames(vaultPath), []);
});

test("absent plan_id creates one Thread from the task; second call with plan_id does not mint another", async (t) => {
  const vaultPath = await withVault(t);
  const first = await runtime({ task: "  Ship the widget  ", vaultPath });

  assert.equal(typeof first.plan_id, "string");
  assert.match(first.plan_id, /^plan-[0-9a-f]{16}$/);
  assert.equal(typeof first.rev, "number");
  assertReadyProjection(first.ready);
  assert.equal(first.ready.length, 1, "no agents => one pending slice from the task, not DEFAULT_TEAM");
  assert.equal(first.ready[0].title, "Ship the widget");
  assert.equal(first.ready[0].status, "pending");
  assert.deepEqual(first.ready[0].depends_on ?? [], []);
  assertLedgerIsReadyView(first);

  const notePath = await findPlanNote(vaultPath, first.plan_id);
  assert.ok(notePath, "created Thread note must exist");
  const note = await rehydratePlan(notePath);
  assert.equal(note.goal, "Ship the widget");
  assert.equal(note.slices.length, 1);
  assert.deepEqual(
    first.ready.map((slice) => slice.id),
    note.slices.map((slice) => slice.id),
  );

  const notesAfterFirst = await listPlanNoteNames(vaultPath);
  assert.equal(notesAfterFirst.length, 1, "one runtime, one Thread");

  const second = await runtime({ plan_id: first.plan_id, task: "later audit label", vaultPath });
  assert.equal(second.plan_id, first.plan_id);
  assert.deepEqual(
    second.ready.map((slice) => slice.id),
    first.ready.map((slice) => slice.id),
  );
  assert.deepEqual(await listPlanNoteNames(vaultPath), notesAfterFirst, "second call must not create another Thread");
});

test("absent plan_id with agents[] seeds independent pending slices, never agent-id deps or a compat chain", async (t) => {
  const vaultPath = await withVault(t);
  const packet = await runtime({
    task: "Implement Sovereign Team Runtime",
    vaultPath,
    agents: [
      { agentId: "researcher", role: "explorer", focus: "Map prior decisions." },
      { agentId: "implementer", role: "worker", focus: "Implement runtime." },
      { agentId: "reviewer", role: "reviewer", focus: "Review privacy and tests." },
    ],
  });

  assertReadyProjection(packet.ready);
  assert.equal(packet.ready.length, 3);
  assert.deepEqual(
    packet.ready.map((slice) => slice.title).sort(),
    ["Implement runtime.", "Map prior decisions.", "Review privacy and tests."].sort(),
  );
  for (const slice of packet.ready) {
    assert.equal(slice.status, "pending");
    assert.deepEqual(slice.depends_on ?? [], []);
  }
  assertLedgerIsReadyView(packet);
  const dumped = JSON.stringify(packet);
  assert.doesNotMatch(dumped, /"dependencies":\s*\[\s*"researcher"/);
  assert.doesNotMatch(dumped, /task-[0-9a-f]{10}/);
  assert.equal((await listPlanNoteNames(vaultPath)).length, 1);
});

test("compat door also consumes a Thread; it does not invent ready or a dependsOn chain", async (t) => {
  const vaultPath = await withVault(t);
  const packet = await runtime({
    task: "Compat must not keep a second ledger",
    ownerAgentId: DEFAULT_AGENT_ID,
    workspaceId: "workspace-test",
    vaultPath,
    agents: [
      { id: "hosted-a", role: "explorer" },
      { id: "hosted-b", role: "worker" },
    ],
  });

  assert.equal(typeof packet.plan_id, "string");
  assert.match(packet.plan_id, /^plan-[0-9a-f]{16}$/);
  assertReadyProjection(packet.ready);
  assert.equal(packet.ready.length, 2);
  for (const slice of packet.ready) {
    assert.equal(slice.status, "pending");
    assert.deepEqual(slice.depends_on ?? [], []);
  }
  if (packet.ledger) {
    assert.equal(packet.ledger.length, packet.ready.length);
    assert.deepEqual(
      packet.ledger.map((item) => item.id),
      packet.ready.map((slice) => slice.id),
    );
    for (const item of packet.ledger) {
      assert.ok(!item.id.startsWith("task-"), `compat ledger id must be PlanSlice.id: ${item.id}`);
      assert.ok(READY_STATUSES.has(item.status), `compat ledger must not invent status ready, got ${item.status}`);
      assert.deepEqual(item.dependsOn ?? [], []);
    }
  }
  const notes = await listPlanNoteNames(vaultPath);
  assert.equal(notes.length, 1, "compat door creates one Thread, not a parallel ledger");

  const again = await runtime({
    plan_id: packet.plan_id,
    task: "Compat must not keep a second ledger",
    ownerAgentId: DEFAULT_AGENT_ID,
    workspaceId: "workspace-test",
    vaultPath,
    agents: [
      { id: "hosted-a", role: "explorer" },
      { id: "hosted-b", role: "worker" },
    ],
  });
  assert.equal(again.plan_id, packet.plan_id);
  assert.deepEqual(await listPlanNoteNames(vaultPath), notes);
});

test("both fake Team ledger doors are gone from the tree", async () => {
  const source = await readFile(TEAM_SRC, "utf8");
  assert.doesNotMatch(source, /function ledgerFor\b/, "live MCP door: ledgerFor() must die");
  assert.doesNotMatch(source, /profiles\[0\]\.agentId/, "no star on profiles[0].agentId");
  assert.doesNotMatch(
    source,
    /status:\s*"ready"/,
    "compat door must not invent status ready",
  );
  assert.doesNotMatch(
    source,
    /dependsOn:\s*index === 0 \? \[\] : \[stableId/,
    "compat door must not seed a dependsOn chain",
  );
  assert.match(source, /synchronizeExpiredClaimsAndReadReady/, "ready comes from the Thread after the expiry sweep");
  assert.match(source, /readySlices/, "projection is readySlices, not a raw slices filter");
});

test("MCP team runtime accepts plan_id and keeps G11/G12 shut", async () => {
  const source = await readFile(SERVER_SRC, "utf8");
  const start = source.indexOf('"minni_team_runtime"');
  assert.notEqual(start, -1);
  const nextTool = source.indexOf("server.registerTool(", start + 1);
  const block = source.slice(start, nextTool === -1 ? undefined : nextTool);
  const schemaStart = block.indexOf("inputSchema:");
  const handlerStart = block.indexOf("async");
  const schema = block.slice(schemaStart, handlerStart);
  assert.match(schema, /plan_id:\s*z\.string\(\)\.min\(1\)\.optional\(\)/);
  assert.doesNotMatch(schema, /coordinatorAgentId\s*:/);
  assert.doesNotMatch(schema, /vaultPath\s*:/);
  assert.match(block, /vaultPath:\s*DEFAULT_VAULT_PATH/);
  assert.match(block, /coordinatorAgentId:\s*DEFAULT_AGENT_ID/);
});

test("create while a non-terminal active Thread exists surfaces displaced_active; no incumbent leaves the field absent", async (t) => {
  const emptyVault = await withVault(t);
  const noIncumbent = await runtime({ task: "First Thread has no incumbent", vaultPath: emptyVault });
  assert.equal(noIncumbent.displaced_active, undefined, "no incumbent must not mint a fake displaced_active");
  assert.equal("displaced_active" in noIncumbent, false, "field must be absent, not a dummy");

  const vaultPath = await withVault(t);
  const incumbent = await createPlan(
    {
      goal: "Non-terminal active Thread",
      slices: [{ title: "Still open" }],
      vaultPath,
    },
    { vaultPath },
  );
  assert.equal(incumbent.displaced_active, undefined, "first createPlan auto-activates silently");

  const live = await runtime({ task: "Displace the in-flight Thread", vaultPath });
  assert.equal(live.displaced_active, incumbent.plan.plan_id, "live packet must name the displaced incumbent");
  assert.notEqual(live.plan_id, incumbent.plan.plan_id);

  const present = await runtime({ plan_id: live.plan_id, task: "read path must not create", vaultPath });
  assert.equal(present.plan_id, live.plan_id);
  assert.equal(present.displaced_active, undefined, "present plan_id path does not create and must not invent displaced_active");
  assert.equal("displaced_active" in present, false);

  const compatVault = await withVault(t);
  const compatIncumbent = await createPlan(
    {
      goal: "Compat incumbent",
      slices: [{ title: "Open" }],
      vaultPath: compatVault,
    },
    { vaultPath: compatVault },
  );
  const compat = await runtime({
    task: "Compat must also surface displacement",
    ownerAgentId: DEFAULT_AGENT_ID,
    workspaceId: "workspace-test",
    vaultPath: compatVault,
    agents: [{ id: "hosted-a", role: "explorer" }],
  });
  assert.equal(compat.displaced_active, compatIncumbent.plan.plan_id, "compat packet uses the same displaced_active field");

  const compatNoneVault = await withVault(t);
  const compatNone = await runtime({
    task: "Compat with no incumbent",
    ownerAgentId: DEFAULT_AGENT_ID,
    workspaceId: "workspace-test",
    vaultPath: compatNoneVault,
    agents: [{ id: "hosted-a", role: "explorer" }],
  });
  assert.equal(compatNone.displaced_active, undefined, "compat no-incumbent must not mint a fake displaced_active");
  assert.equal("displaced_active" in compatNone, false);
});
