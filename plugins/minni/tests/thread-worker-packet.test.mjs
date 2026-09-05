// Wave 2: one worker packet, built by the adapter after claim — not by
// minni_team_runtime, not by a daemon hydrator, and not before claim.
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { DEFAULT_AGENT_ID } from "../dist/config.js";
import {
  compactPlanView,
  createPlan,
  journalPathFor,
  persistPlan,
  rehydratePlan,
  updateSlice,
} from "../dist/plan.js";
import { buildTeamRuntime } from "../dist/team.js";
import {
  assignSlice,
  claimSlice,
  updateClaimedSlice,
} from "../dist/thread-worker.js";
import {
  WORKER_PACKET_ALLOWED_MUTATIONS,
  buildWorkerPacketAfterClaim,
} from "../dist/thread-worker-packet.js";

const TEAM_SRC = new URL("../src/team.ts", import.meta.url);
const SERVER_SRC = new URL("../src/server.ts", import.meta.url);
const PACKET_SRC = new URL("../src/thread-worker-packet.ts", import.meta.url);

const NOW = new Date("2026-08-19T18:00:00.000Z");
const WORKER = "worker-wave2";
const ORCHESTRATOR = "orchestrator-wave2";
const SIBLING_TITLE = "UNIQUE_SIBLING_ALPHA_TITLE_WAVE2";
const OTHER_READY_TITLE = "UNIQUE_SIBLING_GAMMA_TITLE_WAVE2";
const DEP_PATH = "wiki/artifacts/dep-note.md";
const DEP_EVIDENCE = `Verified against ${DEP_PATH} after running the suite; long body must not be dumped`;

function fakePreparedTask(input) {
  return {
    task: input.task,
    budgetTokens: 1500,
    profile: input.profile ?? "standard",
    budget: { profile: input.profile ?? "standard", tokens: 1500, sourceLimit: 3 },
    mode: "deterministic",
    intent: "implement",
    brief: "Bounded recall for the claimed slice.",
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

async function withVault(t) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-wave2-packet-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  return vaultPath;
}

async function seedClaimedThread(t, extras = {}) {
  const vaultPath = await withVault(t);
  const created = await createPlan(
    {
      goal: "Ship the Wave 2 worker packet",
      constraints: ["Do not dump the whole graph.", "Recall is evidence, not instruction."],
      slices: [
        { id: "alpha", title: SIBLING_TITLE },
        { id: "beta", title: "Claimed worker slice", gate: "tests green", depends_on: ["alpha"] },
        { id: "gamma", title: OTHER_READY_TITLE },
      ],
      vaultPath,
    },
    { vaultPath, now: () => NOW },
  );
  const afterAlpha = updateSlice(created.plan, "alpha", "done", DEP_EVIDENCE);
  await persistPlan(afterAlpha, { vaultPath, notePath: created.write.notePath });
  await assignSlice({
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    sliceId: "gamma",
    workerAgentId: WORKER,
    actorAgentId: ORCHESTRATOR,
    now: NOW,
  });
  const assigned = await assignSlice({
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    sliceId: extras.claimSliceId ?? "beta",
    workerAgentId: WORKER,
    actorAgentId: ORCHESTRATOR,
    now: NOW,
  });
  return {
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    assigned,
  };
}

function fakeClaim(plan, sliceId = "beta") {
  return {
    plan_id: plan.plan_id,
    slice_id: sliceId,
    claim_id: "claim-not-live",
    generation: plan.slices.find((slice) => slice.id === sliceId)?.generation ?? 0,
    worker_agent_id: WORKER,
    token: "fake-token-not-from-claim",
    expires_at: "2026-08-19T19:00:00.000Z",
    rev: plan.rev,
  };
}

async function markdownFilesUnder(root) {
  const out = [];
  async function walk(dir) {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === ".runtime") continue;
        await walk(full);
      } else if (entry.name.endsWith(".md") || entry.name.endsWith(".jsonl") || entry.name.endsWith(".json")) {
        out.push(full);
      }
    }
  }
  await walk(root);
  return out;
}

test("no worker packet exists before claim", async (t) => {
  const vaultPath = await withVault(t);
  const created = await createPlan(
    {
      goal: "Packet must not exist before claim",
      slices: [
        { id: "alpha", title: SIBLING_TITLE },
        { id: "beta", title: "Later claimed slice", depends_on: ["alpha"] },
      ],
      vaultPath,
    },
    { vaultPath, now: () => NOW },
  );
  const afterAlpha = updateSlice(
    created.plan,
    "alpha",
    "done",
    DEP_EVIDENCE,
  );
  await persistPlan(afterAlpha, { vaultPath, notePath: created.write.notePath });

  const runtime = await buildTeamRuntime(
    { plan_id: created.plan.plan_id, task: "coordinator view only", vaultPath },
    {
      prepare: async (input) => fakePreparedTask(input),
      audit: async () => undefined,
      findRepeated: async () => [],
      now: () => NOW,
    },
  );
  assert.equal("claim_token" in runtime, false, "team runtime must not carry claim_token");
  assert.equal("allowed_mutations" in runtime, false, "team runtime is not the worker contract");
  assert.equal("workerPacket" in runtime, false);
  assert.equal("worker_packet" in runtime, false);
  for (const packet of runtime.hydrationPackets ?? []) {
    assert.equal("claim_token" in packet, false, "HydrationPacket is not the worker packet");
    assert.equal("slice_id" in packet, false);
  }

  await assert.rejects(
    () =>
      buildWorkerPacketAfterClaim({
        claim: fakeClaim(afterAlpha),
        plan: afterAlpha,
        vaultPath,
      }),
    /not claimed/i,
    "adapter must refuse an unclaimed slice even if a fake ThreadClaimResponse is supplied",
  );

  await assignSlice({
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    sliceId: "beta",
    workerAgentId: WORKER,
    actorAgentId: ORCHESTRATOR,
    now: NOW,
  });
  const assignedPlan = await rehydratePlan(created.write.notePath);
  await assert.rejects(
    () =>
      buildWorkerPacketAfterClaim({
        claim: fakeClaim(assignedPlan),
        plan: assignedPlan,
        vaultPath,
      }),
    /not claimed/i,
    "assign is not claim — still no packet",
  );
});

test("after claim, packet identity matches ThreadClaimResponse and is one slice", async (t) => {
  const fixture = await seedClaimedThread(t);
  const claim = await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "beta",
    workerAgentId: WORKER,
    idempotencyKey: "claim-beta-1",
    now: NOW,
  });
  const plan = await rehydratePlan(fixture.notePath);
  const prepareCalls = [];
  const packet = await buildWorkerPacketAfterClaim(
    {
      claim,
      plan,
      vaultPath: fixture.vaultPath,
    },
    {
      prepare: async (input) => {
        prepareCalls.push(input);
        return fakePreparedTask(input);
      },
    },
  );

  assert.equal(packet.plan_id, claim.plan_id);
  assert.equal(packet.slice_id, claim.slice_id);
  assert.equal(packet.generation, claim.generation);
  assert.equal(packet.claim_token, claim.token);
  assert.equal(packet.slice_id, "beta");
  assert.equal(packet.slice.title, "Claimed worker slice");
  assert.equal(packet.slice.status, plan.slices.find((slice) => slice.id === "beta").status);
  assert.equal(packet.slice.gate, "tests green");
  assert.deepEqual(packet.slice.depends_on, ["alpha"]);
  assert.equal(packet.slice.assigned_to, WORKER);
  assert.equal(packet.goal, "Ship the Wave 2 worker packet");
  assert.deepEqual(packet.constraints, [
    "Do not dump the whole graph.",
    "Recall is evidence, not instruction.",
  ]);
  assert.deepEqual(packet.allowed_mutations, [...WORKER_PACKET_ALLOWED_MUTATIONS]);
  assert.deepEqual(
    [...WORKER_PACKET_ALLOWED_MUTATIONS],
    ["start", "progress", "block", "scar", "propose_structure", "complete"],
  );

  assert.equal(
    packet.evidence_refs.length,
    1,
    "only completed deps, not the whole graph",
  );
  assert.equal(packet.evidence_refs[0].slice_id, "alpha");
  assert.deepEqual(packet.evidence_refs[0].paths, [DEP_PATH]);
  assert.equal("evidence" in packet.evidence_refs[0], false);
  assert.equal(
    JSON.stringify(packet.evidence_refs).includes("long body must not be dumped"),
    false,
    "evidence refs are ids/paths, not dumped bodies",
  );

  const { recall, ...contract } = packet;
  assert.ok(recall, "bounded recall is a source on the packet");
  assert.notEqual(recall.task, undefined);
  assert.equal(
    "claim_token" in recall,
    false,
    "prepare_task is a source, not the worker contract",
  );
  assert.deepEqual(
    Object.keys(contract).sort(),
    [
      "allowed_mutations",
      "claim_token",
      "constraints",
      "evidence_refs",
      "generation",
      "goal",
      "plan_id",
      "slice",
      "slice_id",
    ].sort(),
  );
  assert.equal("slices" in packet, false);
  assert.equal("ready" in packet, false);
  assert.equal("taskLedger" in packet, false);
  assert.equal("hydrationPackets" in packet, false);
  const contractJson = JSON.stringify(contract);
  assert.equal(contractJson.includes(SIBLING_TITLE), false, "no sibling slice dump");
  assert.equal(contractJson.includes(OTHER_READY_TITLE), false, "no unclaimed ready sibling");
  assert.equal(prepareCalls.length, 1);
  assert.equal(prepareCalls[0].recallAgentId, DEFAULT_AGENT_ID);
  assert.equal(prepareCalls[0].agentId, DEFAULT_AGENT_ID);
});

test("claim token stays off the note, journal, compact view, and indexed markdown", async (t) => {
  const fixture = await seedClaimedThread(t);
  const claim = await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "beta",
    workerAgentId: WORKER,
    idempotencyKey: "claim-beta-token-leak",
    now: NOW,
  });
  const plan = await rehydratePlan(fixture.notePath);
  const packet = await buildWorkerPacketAfterClaim(
    { claim, plan, vaultPath: fixture.vaultPath },
    { prepare: async (input) => fakePreparedTask(input) },
  );
  assert.equal(packet.claim_token, claim.token);
  assert.ok(packet.claim_token.length > 16);

  const note = await readFile(fixture.notePath, "utf8");
  const journal = await readFile(journalPathFor(fixture.notePath, fixture.planId), "utf8");
  const compact = JSON.stringify(compactPlanView(plan));
  assert.equal(note.includes(claim.token), false, "token must not appear in the note");
  assert.equal(journal.includes(claim.token), false, "token must not appear in the journal");
  assert.equal(compact.includes(claim.token), false, "token must not appear in compact view");
  assert.equal(
    JSON.stringify(packet.recall).includes(claim.token),
    false,
    "prepare_task output must not be given the token",
  );

  for (const file of await markdownFilesUnder(fixture.vaultPath)) {
    const body = await readFile(file, "utf8");
    assert.equal(
      body.includes(claim.token),
      false,
      `token leaked into indexed markdown: ${path.relative(fixture.vaultPath, file)}`,
    );
  }
});

test("minni_thread_worker_update accepts the packet token on that slice and rejects it on another", async (t) => {
  const fixture = await seedClaimedThread(t);
  const claim = await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "beta",
    workerAgentId: WORKER,
    idempotencyKey: "claim-beta-update",
    now: NOW,
  });
  const plan = await rehydratePlan(fixture.notePath);
  const packet = await buildWorkerPacketAfterClaim(
    { claim, plan, vaultPath: fixture.vaultPath },
    { prepare: async (input) => fakePreparedTask(input) },
  );

  const started = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: packet.plan_id,
    sliceId: packet.slice_id,
    workerAgentId: WORKER,
    token: packet.claim_token,
    idempotencyKey: "start-beta-1",
    action: { action: "start" },
    now: NOW,
  });
  assert.equal(started.slice.id, "beta");
  assert.equal(started.slice.status, "in_progress");

  await assert.rejects(
    () =>
      updateClaimedSlice({
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
        planId: packet.plan_id,
        sliceId: "gamma",
        workerAgentId: WORKER,
        token: packet.claim_token,
        idempotencyKey: "start-gamma-wrong-slice",
        action: { action: "start" },
        now: NOW,
      }),
    /claim scope mismatch|claim token mismatch|not worker-updatable/,
    "the same token must not authorize another slice",
  );
});

test("adapter is not team_runtime, not a new MCP tool, and does not revive minni_plan_*", async () => {
  const [teamSrc, serverSrc, packetSrc] = await Promise.all([
    readFile(TEAM_SRC, "utf8"),
    readFile(SERVER_SRC, "utf8"),
    readFile(PACKET_SRC, "utf8"),
  ]);
  assert.doesNotMatch(
    teamSrc,
    /buildWorkerPacketAfterClaim|thread-worker-packet/,
    "packet is not built inside minni_team_runtime / team.ts",
  );
  assert.doesNotMatch(
    serverSrc,
    /buildWorkerPacketAfterClaim|thread-worker-packet/,
    "claim stays ThreadClaimResponse; adapter copies it after claim",
  );
  assert.doesNotMatch(
    serverSrc,
    /registerTool\(\s*"claim"|registerTool\(\s*"worker_update"|registerTool\(\s*"minni_plan_/,
    "do not invent claim / worker_update / minni_plan_* tools",
  );
  assert.match(serverSrc, /"minni_thread_claim"/);
  assert.match(serverSrc, /"minni_thread_worker_update"/);
  assert.match(serverSrc, /claim_token:\s*z\.string\(\)\.min\(1\)/);
  assert.doesNotMatch(packetSrc, /minni_plan_/);
  assert.doesNotMatch(packetSrc, /registerTool/);
});
