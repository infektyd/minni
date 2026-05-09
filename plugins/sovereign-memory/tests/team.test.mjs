import assert from "node:assert/strict";
import test from "node:test";

import { buildTeamEvidencePacket, buildTeamPromotionPacket, buildTeamRuntime } from "../dist/team.js";

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

test("buildTeamRuntime creates temporary agent profiles, task ledger, and hydration packets", async () => {
  const prepareCalls = [];
  const audits = [];
  const packet = await buildTeamRuntime(
    {
      task: "Implement Sovereign Team Runtime",
      coordinatorAgentId: "codex",
      workspaceId: "/repo",
      vaultPath: "/tmp/vault",
      agents: [
        { agentId: "researcher", role: "explorer", focus: "Map prior decisions." },
        { agentId: "implementer", role: "worker", focus: "Implement runtime.", ownership: ["src/team.ts"] },
        { agentId: "reviewer", role: "reviewer", focus: "Review privacy and tests." },
      ],
    },
    {
      prepare: async (input) => {
        prepareCalls.push(input);
        return fakePreparedTask(input);
      },
      audit: async (_vaultPath, entry) => {
        audits.push(entry);
      },
    },
  );

  assert.equal(packet.runtimeId.startsWith("team-"), true);
  assert.equal(packet.temporaryProfiles.length, 3);
  assert.equal(packet.temporaryProfiles[0].lifetime, "temporary");
  assert.equal(packet.temporaryProfiles[0].memoryPolicy.learn, "manual-only");
  assert.deepEqual(packet.temporaryProfiles[0].permissions, ["read", "memory-recall"]);
  assert.ok(packet.temporaryProfiles[1].permissions.includes("write"));
  assert.equal(packet.taskLedger.length, 3);
  assert.equal(packet.taskLedger[0].status, "queued");
  assert.deepEqual(packet.taskLedger[1].dependencies, ["researcher"]);
  assert.deepEqual(packet.taskLedger[1].ownership, ["src/team.ts"]);
  assert.equal(packet.hydrationPackets.length, 3);
  assert.equal(packet.hydrationPackets[0].agentId, "researcher");
  assert.equal(packet.hydrationPackets[0].context.task.includes("Assigned role: explorer"), true);
  assert.ok(packet.hydrationPackets[2].instructions.includes("Do not edit files for this assignment."));
  assert.equal(packet.memoryPolicy.automaticLearning, false);
  assert.equal(packet.memoryPolicy.durableWrites, "explicit-only");
  assert.match(packet.contextMarkdown, /Sovereign Team Runtime/);
  assert.equal(prepareCalls.length, 3);
  assert.equal(audits[0].tool, "sovereign_team_runtime");
});

test("buildTeamRuntime defaults to a complete explorer, worker, reviewer team", async () => {
  const packet = await buildTeamRuntime(
    { task: "Ship runtime", vaultPath: "/tmp/vault" },
    { prepare: async (input) => fakePreparedTask(input), audit: async () => undefined },
  );

  assert.deepEqual(packet.temporaryProfiles.map((profile) => profile.role), ["explorer", "worker", "reviewer"]);
  assert.equal(packet.taskLedger.length, 3);
  assert.match(packet.gates.join("\n"), /Durable learning requires an explicit user request/);
  assert.match(packet.nonGoals.join("\n"), /No automatic spawning/);
});

test("buildTeamEvidencePacket separates complete evidence from blockers", () => {
  const packet = buildTeamEvidencePacket({
    runtimeId: "team-abc",
    task: "Implement runtime",
    results: [
      {
        agentId: "worker",
        status: "completed",
        summary: "Implemented the runtime module.",
        evidence: ["Specific files inspected: src/team.ts"],
        changedFiles: ["src/team.ts"],
        verification: ["npm run build:server"],
      },
      {
        agentId: "reviewer",
        status: "blocked",
        summary: "Review waiting on failing test output.",
        evidence: ["Reviewed privacy boundary."],
        blockers: ["Need final npm test output."],
      },
    ],
  });

  assert.equal(packet.reports[0].evidenceStatus, "complete");
  assert.equal(packet.reports[0].risks.length, 0);
  assert.equal(packet.reports[1].evidenceStatus, "partial");
  assert.match(packet.reports[1].risks.join("\n"), /Blockers remain unresolved/);
  assert.deepEqual(packet.unresolvedBlockers, ["reviewer: Need final npm test output."]);
  assert.match(packet.doNotStore.join("\n"), /raw transcripts/);
});

test("buildTeamEvidencePacket makes promotion candidates human-review only", () => {
  const packet = buildTeamEvidencePacket({
    task: "Implement runtime",
    results: [
      {
        agentId: "worker",
        status: "completed",
        summary: "Reusable implementation lane.",
        evidence: ["Specific APIs inspected.", "Concrete diff summary."],
        changedFiles: ["src/team.ts"],
        verification: ["node --test tests/team.test.mjs"],
      },
      {
        agentId: "scribe",
        status: "completed",
        summary: "No verification yet.",
        evidence: ["Docs inspected."],
      },
    ],
  });

  assert.equal(packet.promotionCandidates[0].recommended, true);
  assert.match(packet.promotionCandidates[0].nextStep, /do not promote automatically/);
  assert.equal(packet.promotionCandidates[1].recommended, false);
  assert.match(packet.contextMarkdown, /Promotion Candidates/);
});

test("buildTeamPromotionPacket drafts permanent profiles only after explicit approval", () => {
  const pending = buildTeamPromotionPacket({
    agent: {
      agentId: "team-worker-2",
      role: "worker",
      focus: "Implement scoped backend changes.",
      ownership: ["src/team.ts"],
      permissions: ["read", "write", "test", "memory-recall"],
      memoryPolicy: { recall: "allowed", learn: "manual-only", vaultWrites: "manual-only" },
      lifetime: "temporary",
      promotionRule: "Promote only after completed evidence, repeatable value, and explicit operator approval.",
    },
    evidence: {
      agentId: "team-worker-2",
      recommended: true,
      score: 5,
      reasons: ["submitted evidence plus verification"],
      nextStep: "Eligible for human review; do not promote automatically.",
    },
    requestedPermissions: ["read", "write", "test", "memory-recall", "network"],
    approved: false,
  });

  assert.equal(pending.status, "needs-approval");
  assert.equal(pending.autoWrite, false);
  assert.equal(pending.permanentProfile, undefined);
  assert.match(pending.contextMarkdown, /requires explicit operator approval/i);

  const approved = buildTeamPromotionPacket({
    agent: pending.temporaryProfile,
    evidence: pending.evidence,
    requestedPermissions: ["read", "write", "test", "memory-recall", "network"],
    approved: true,
    permanentAgentId: "agent-sovereign-worker",
  });

  assert.equal(approved.status, "promoted-draft");
  assert.equal(approved.autoWrite, false);
  assert.equal(approved.permanentProfile.agentId, "agent-sovereign-worker");
  assert.equal(approved.permanentProfile.lifetime, "permanent");
  assert.ok(approved.permissionDelta.added.includes("network"));
  assert.match(approved.nextStep, /review and persist/i);
});
