import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_TEAM_TTL_SECONDS,
  MAX_TEAM_TTL_SECONDS,
  MIN_TEAM_TTL_SECONDS,
  buildTeamEvidencePacket,
  buildTeamEvidencePacketWithHarvest,
  buildTeamPromotionPacket,
  buildTeamRuntime,
} from "../dist/team.js";

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
      findRepeated: async () => [],
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
  // Task 3: audit detail now records focus per agent so repetition signatures can be derived later.
  assert.equal(audits[0].details.agents[0].focus, "Map prior decisions.");
  assert.equal(audits[0].details.agents[1].focus, "Implement runtime.");
  assert.equal(audits[0].details.agents[2].focus, "Review privacy and tests.");
  assert.deepEqual(packet.repeatedAgentSuggestions, []);
});

test("buildTeamRuntime attaches repeatedAgentSuggestions from findRepeated and renders them", async () => {
  const stubSuggestions = [
    {
      signature: "worker::audit swift concurrency",
      role: "worker",
      normalizedFocus: "audit swift concurrency",
      count: 4,
      examples: [
        {
          runtimeId: "team-old",
          timestamp: "2026-04-30T10:00:00.000Z",
          agentId: "team-worker-1",
          rawFocus: "Audit Swift concurrency",
        },
      ],
      suggestPromotion: true,
    },
  ];
  let findRepeatedCalls = 0;
  const packet = await buildTeamRuntime(
    {
      task: "Repetition wiring",
      vaultPath: "/tmp/vault",
    },
    {
      prepare: async (input) => fakePreparedTask(input),
      audit: async () => undefined,
      findRepeated: async () => {
        findRepeatedCalls += 1;
        return stubSuggestions;
      },
    },
  );

  assert.equal(findRepeatedCalls, 1);
  assert.deepEqual(packet.repeatedAgentSuggestions, stubSuggestions);
  assert.match(packet.contextMarkdown, /## Repeated Agent Patterns/);
  assert.match(packet.contextMarkdown, /worker::audit swift concurrency/);
  assert.match(packet.contextMarkdown, /observed 4 times/);
  assert.match(packet.contextMarkdown, /promotion candidate: yes/);
});

test("buildTeamRuntime omits Repeated Agent Patterns section when no suggestions", async () => {
  const packet = await buildTeamRuntime(
    {
      task: "No repetitions",
      vaultPath: "/tmp/vault",
    },
    {
      prepare: async (input) => fakePreparedTask(input),
      audit: async () => undefined,
      findRepeated: async () => [],
    },
  );
  assert.deepEqual(packet.repeatedAgentSuggestions, []);
  assert.equal(packet.contextMarkdown.includes("## Repeated Agent Patterns"), false);
});

test("buildTeamRuntime never breaks when findRepeated throws", async () => {
  const packet = await buildTeamRuntime(
    {
      task: "Resilient against repetition failure",
      vaultPath: "/tmp/vault",
    },
    {
      prepare: async (input) => fakePreparedTask(input),
      audit: async () => undefined,
      findRepeated: async () => {
        throw new Error("repetition computation exploded");
      },
    },
  );
  assert.deepEqual(packet.repeatedAgentSuggestions, []);
  assert.equal(packet.contextMarkdown.includes("## Repeated Agent Patterns"), false);
  assert.ok(packet.runtimeId.startsWith("team-"));
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

const FIXED_NOW_ISO = "2026-05-08T22:30:00.000Z";
const FIXED_NOW_MS = Date.parse(FIXED_NOW_ISO);

function fixedClock(iso = FIXED_NOW_ISO) {
  return () => new Date(iso);
}

async function buildRuntimeAt(iso, overrides = {}) {
  return buildTeamRuntime(
    {
      task: "Lifecycle test",
      vaultPath: "/tmp/vault",
      ...overrides,
    },
    {
      prepare: async (input) => fakePreparedTask(input),
      audit: async () => undefined,
      now: fixedClock(iso),
    },
  );
}

test("buildTeamRuntime defaults TTL to 24h and stamps createdAt + expiresAt", async () => {
  const packet = await buildRuntimeAt(FIXED_NOW_ISO);

  assert.equal(packet.createdAt, FIXED_NOW_ISO);
  assert.equal(packet.ttlSeconds, DEFAULT_TEAM_TTL_SECONDS);
  assert.equal(packet.expiresAt, new Date(FIXED_NOW_MS + DEFAULT_TEAM_TTL_SECONDS * 1000).toISOString());
  assert.match(packet.contextMarkdown, new RegExp(`Created: ${FIXED_NOW_ISO}`));
  assert.match(packet.contextMarkdown, new RegExp(`Expires: ${packet.expiresAt}`));
});

test("buildTeamRuntime accepts a custom ttlSeconds", async () => {
  const packet = await buildRuntimeAt(FIXED_NOW_ISO, { ttlSeconds: 3600 });
  assert.equal(packet.ttlSeconds, 3600);
  assert.equal(packet.expiresAt, new Date(FIXED_NOW_MS + 3600 * 1000).toISOString());
});

test("buildTeamRuntime clamps ttlSeconds below the floor up to MIN_TEAM_TTL_SECONDS", async () => {
  const packet = await buildRuntimeAt(FIXED_NOW_ISO, { ttlSeconds: MIN_TEAM_TTL_SECONDS - 30 });
  assert.equal(packet.ttlSeconds, MIN_TEAM_TTL_SECONDS);
  assert.equal(packet.expiresAt, new Date(FIXED_NOW_MS + MIN_TEAM_TTL_SECONDS * 1000).toISOString());
});

test("buildTeamRuntime clamps ttlSeconds above the ceiling down to MAX_TEAM_TTL_SECONDS", async () => {
  const packet = await buildRuntimeAt(FIXED_NOW_ISO, { ttlSeconds: 999_999_999 });
  assert.equal(packet.ttlSeconds, MAX_TEAM_TTL_SECONDS);
  assert.equal(packet.expiresAt, new Date(FIXED_NOW_MS + MAX_TEAM_TTL_SECONDS * 1000).toISOString());
});

test("buildTeamEvidencePacket without a runtime emits promotion candidates and no expiration blocker", () => {
  const packet = buildTeamEvidencePacket({
    task: "Implement runtime",
    results: [
      {
        agentId: "worker",
        status: "completed",
        summary: "Did it.",
        evidence: ["a", "b"],
        changedFiles: ["x.ts"],
        verification: ["npm test"],
      },
    ],
  });

  assert.equal(packet.runtimeExpired, undefined);
  assert.equal(packet.expiredRuntimeId, undefined);
  assert.equal(packet.expiredAt, undefined);
  assert.ok(packet.promotionCandidates.length > 0);
  assert.ok(!packet.unresolvedBlockers.some((entry) => entry.includes("expired")));
  assert.ok(!packet.contextMarkdown.includes("## Expiration"));
});

test("buildTeamEvidencePacket with a fresh runtime marks runtimeExpired === false", async () => {
  const runtime = await buildRuntimeAt(FIXED_NOW_ISO, { ttlSeconds: 3600 });
  const packet = buildTeamEvidencePacket({
    task: "Implement runtime",
    runtime,
    now: fixedClock("2026-05-08T22:45:00.000Z"),
    results: [
      {
        agentId: "worker",
        status: "completed",
        summary: "Did it.",
        evidence: ["a", "b"],
        changedFiles: ["x.ts"],
        verification: ["npm test"],
      },
    ],
  });

  assert.equal(packet.runtimeExpired, false);
  assert.equal(packet.expiredRuntimeId, undefined);
  assert.equal(packet.expiredAt, undefined);
  assert.ok(packet.promotionCandidates.length > 0);
  assert.ok(!packet.unresolvedBlockers.some((entry) => entry.includes("expired")));
  assert.ok(!packet.contextMarkdown.includes("## Expiration"));
});

test("buildTeamEvidencePacket with an expired runtime suppresses promotion and adds a blocker", async () => {
  const runtime = await buildRuntimeAt(FIXED_NOW_ISO, { ttlSeconds: 3600 });
  const oneSecondPastExpiry = new Date(Date.parse(runtime.expiresAt) + 1000).toISOString();
  const packet = buildTeamEvidencePacket({
    task: "Implement runtime",
    runtime,
    now: fixedClock(oneSecondPastExpiry),
    results: [
      {
        agentId: "worker",
        status: "completed",
        summary: "Did it.",
        evidence: ["a", "b"],
        changedFiles: ["x.ts"],
        verification: ["npm test"],
      },
    ],
  });

  assert.equal(packet.runtimeExpired, true);
  assert.equal(packet.expiredRuntimeId, runtime.runtimeId);
  assert.equal(packet.expiredAt, runtime.expiresAt);
  assert.equal(packet.promotionCandidates.length, 0);
  const expirationBlocker = packet.unresolvedBlockers.find((entry) => entry.includes("expired"));
  assert.ok(expirationBlocker, "expected an expiration blocker");
  assert.match(
    expirationBlocker,
    new RegExp(`Runtime ${runtime.runtimeId} expired at ${runtime.expiresAt} \\(now ${oneSecondPastExpiry}\\); evidence ignored for promotion\\.`),
  );
  assert.match(packet.contextMarkdown, /## Expiration/);
  assert.ok(packet.contextMarkdown.includes(runtime.runtimeId));
});

test("buildTeamEvidencePacketWithHarvest refuses to harvest when runtime expired", async () => {
  const runtime = await buildRuntimeAt(FIXED_NOW_ISO, { ttlSeconds: 3600 });
  const oneSecondPastExpiry = new Date(Date.parse(runtime.expiresAt) + 1000).toISOString();
  let harvestCalled = false;
  const callAfm = async () => {
    harvestCalled = true;
    throw new Error("harvest must not be invoked when runtime expired");
  };
  const writeInbox = async () => {
    harvestCalled = true;
    throw new Error("harvest must not be invoked when runtime expired");
  };
  const audit = async () => {
    harvestCalled = true;
    throw new Error("harvest must not be invoked when runtime expired");
  };

  const packet = await buildTeamEvidencePacketWithHarvest(
    {
      task: "Implement runtime",
      vaultPath: "/tmp/vault",
      runtime,
      now: fixedClock(oneSecondPastExpiry),
      results: [
        {
          agentId: "worker",
          status: "completed",
          summary: "Did it.",
          evidence: ["a", "b"],
          changedFiles: ["x.ts"],
          verification: ["npm test"],
        },
      ],
    },
    { callAfm, writeInbox, audit },
  );

  assert.equal(harvestCalled, false, "harvest deps must not be called when runtime expired");
  assert.equal(packet.runtimeExpired, true);
  assert.deepEqual(packet.harvestedLearnings, []);
  assert.equal(packet.promotionCandidates.length, 0);
  assert.ok(packet.unresolvedBlockers.some((entry) => entry.includes("expired")));
  assert.match(packet.contextMarkdown, /## Expiration/);
});
