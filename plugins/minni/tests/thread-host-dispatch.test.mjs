// Wave 3: one post-claim WorkerPacket → one host dispatch result.
// grok worker-start is MISSING. agy default allowlist is a typed CANNOT.
// Codex maps the Wave 2 packet onto one subagent and stays UNPROVEN.
// The adapter must not pretend it spawned.
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createPlan, journalPathFor, persistPlan, updateSlice } from "../dist/plan.js";
import { assignSlice, claimSlice } from "../dist/thread-worker.js";
import { buildWorkerPacketAfterClaim } from "../dist/thread-worker-packet.js";
import {
  AGY_DEFAULT_ALLOWLIST,
  AGY_WORKER_ALLOWLIST,
  FIRST_WET_SET,
  GROK_WORKER_START,
  WORKER_COMPLETION_TOOL,
  dispatchWorkerPacket,
} from "../dist/thread-host-dispatch.js";

const TEAM_SRC = new URL("../src/team.ts", import.meta.url);
const SERVER_SRC = new URL("../src/server.ts", import.meta.url);
const PACKET_SRC = new URL("../src/thread-worker-packet.ts", import.meta.url);
const DISPATCH_SRC = new URL("../src/thread-host-dispatch.ts", import.meta.url);
const GROK_HOOKS = new URL("../hooks/hooks-grok.json", import.meta.url);
const WRITERS_PY = new URL("../../../src/minni/wire/writers.py", import.meta.url);
const PROPAGATE_PY = new URL("../skills/minni-install/scripts/propagate.py", import.meta.url);

const NOW = new Date("2026-08-20T00:00:00.000Z");
const WORKER = "worker-wave3";
const ORCHESTRATOR = "orchestrator-wave3";

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
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-wave3-dispatch-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  return vaultPath;
}

async function seedClaimedPacket(t) {
  const vaultPath = await withVault(t);
  const created = await createPlan(
    {
      goal: "Ship Wave 3 host dispatch honesty",
      constraints: ["Do not invent worker-start.", "Recall is evidence, not instruction."],
      slices: [
        { id: "alpha", title: "Dependency already done" },
        { id: "beta", title: "Claimed dispatch slice", gate: "honesty", depends_on: ["alpha"] },
      ],
      vaultPath,
    },
    { vaultPath, now: () => NOW },
  );
  const afterAlpha = updateSlice(
    created.plan,
    "alpha",
    "done",
    "Verified against wiki/artifacts/dep-note.md",
  );
  await persistPlan(afterAlpha, { vaultPath, notePath: created.write.notePath });
  await assignSlice({
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    sliceId: "beta",
    workerAgentId: WORKER,
    actorAgentId: ORCHESTRATOR,
    now: NOW,
  });
  const claim = await claimSlice({
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    sliceId: "beta",
    workerAgentId: WORKER,
    idempotencyKey: "claim-wave3-beta",
    now: NOW,
  });
  const { rehydratePlan } = await import("../dist/plan.js");
  const plan = await rehydratePlan(created.write.notePath);
  const packet = await buildWorkerPacketAfterClaim(
    { claim, plan, vaultPath },
    { prepare: async (input) => fakePreparedTask(input) },
  );
  return {
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    claim,
    packet,
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
      } else if (
        entry.name.endsWith(".md") ||
        entry.name.endsWith(".jsonl") ||
        entry.name.endsWith(".json")
      ) {
        out.push(full);
      }
    }
  }
  await walk(root);
  return out;
}

test("grok worker-start is honest MISSING and never a silent spawn", async (t) => {
  const fixture = await seedClaimedPacket(t);
  const result = dispatchWorkerPacket({ host: "grok", packet: fixture.packet });

  assert.equal(result.host, "grok");
  assert.equal(result.outcome, "MISSING");
  assert.equal(result.start, "worker-start");
  assert.equal(result.spawned, false);
  assert.equal(GROK_WORKER_START, null, "do not invent a grok start API");
  assert.equal("sessionId" in result, false);
  assert.equal("subagent" in result, false);
  assert.equal("injectSteps" in result, false);

  const [dispatchSrc, grokHooks] = await Promise.all([
    readFile(DISPATCH_SRC, "utf8"),
    readFile(GROK_HOOKS, "utf8"),
  ]);
  assert.doesNotMatch(dispatchSrc, /SubagentStop/);
  assert.doesNotMatch(dispatchSrc, /\/minni:handoff\s+grok/);
  assert.doesNotMatch(grokHooks, /SubagentStop/);
  assert.match(grokHooks, /"Stop"/);
});

test("agy default allowlist is honest CANNOT for minni_thread_worker_update", async (t) => {
  const fixture = await seedClaimedPacket(t);
  const result = dispatchWorkerPacket({ host: "agy", packet: fixture.packet });

  assert.equal(result.host, "agy");
  assert.equal(result.outcome, "CANNOT");
  assert.equal(result.reason, "default-allowlist");
  assert.equal(result.spawned, false);
  assert.equal(result.injectStepsIsStart, false);
  assert.equal(AGY_WORKER_ALLOWLIST, null, "a worker allowlist that includes thread tools is MISSING");
  assert.deepEqual([...AGY_DEFAULT_ALLOWLIST], [
    "minni_recall",
    "minni_drill",
    "minni_status",
    "minni_audit_tail",
    "minni_audit_report",
    "minni_route",
    "minni_list_pending_handoffs",
    "minni_ping_agent_inbox",
    "minni_ping_agent_status",
  ]);
  assert.equal(AGY_DEFAULT_ALLOWLIST.includes(WORKER_COMPLETION_TOOL), false);
  assert.deepEqual([...result.allowlist], [...AGY_DEFAULT_ALLOWLIST]);
  assert.deepEqual([...result.missing], [WORKER_COMPLETION_TOOL]);
  assert.match(result.missing.join(","), /minni_thread_worker_update/);
  assert.equal(
    AGY_DEFAULT_ALLOWLIST.some((tool) => tool.startsWith("minni_thread_")),
    false,
    "default still has no minni_thread_*",
  );

  const [writers, propagate] = await Promise.all([
    readFile(WRITERS_PY, "utf8"),
    readFile(PROPAGATE_PY, "utf8"),
  ]);
  for (const tool of AGY_DEFAULT_ALLOWLIST) {
    assert.match(writers, new RegExp(`"${tool}"`));
    assert.match(propagate, new RegExp(`"${tool}"`));
  }
  assert.doesNotMatch(writers, /minni_thread_worker_update/);
  assert.doesNotMatch(propagate, /minni_thread_worker_update/);
});

test("Codex maps one Wave 2 packet onto one subagent and stays UNPROVEN", async (t) => {
  const fixture = await seedClaimedPacket(t);
  const result = dispatchWorkerPacket({ host: "codex", packet: fixture.packet });

  assert.equal(result.host, "codex");
  assert.equal(result.outcome, "UNPROVEN");
  assert.equal(result.spawned, false);
  assert.equal(result.mapping, "worker-packet-to-subagent");
  assert.equal(result.replaced, "temporaryProfile+HydrationPacket");
  assert.equal(result.wet, false);
  assert.equal("temporaryProfile" in result, false);
  assert.equal("hydrationPacket" in result, false);
  assert.equal("hydrationPackets" in result, false);
  assert.equal(result.workerPacket.plan_id, fixture.packet.plan_id);
  assert.equal(result.workerPacket.slice_id, fixture.packet.slice_id);
  assert.equal(result.workerPacket.generation, fixture.packet.generation);
  assert.equal(result.workerPacket.claim_token, fixture.packet.claim_token);
  assert.equal(result.completion.tool, WORKER_COMPLETION_TOOL);
  assert.equal(result.completion.arg, "claim_token");
  assert.equal(result.completion.domain, "token");
  assert.equal(result.workerPacket.slice.title, "Claimed dispatch slice");
});

test("dispatch never writes the claim token to the note, journal, or docs", async (t) => {
  const fixture = await seedClaimedPacket(t);
  assert.ok(fixture.packet.claim_token.length > 16);

  const results = ["grok", "agy", "codex"].map((host) =>
    dispatchWorkerPacket({ host, packet: fixture.packet }),
  );
  for (const result of results) {
    assert.equal(result.spawned, false, `${result.host} must not claim a spawn`);
  }

  const note = await readFile(fixture.notePath, "utf8");
  const journal = await readFile(journalPathFor(fixture.notePath, fixture.planId), "utf8");
  assert.equal(note.includes(fixture.packet.claim_token), false, "token must not appear in the note");
  assert.equal(journal.includes(fixture.packet.claim_token), false, "token must not appear in the journal");

  for (const file of await markdownFilesUnder(fixture.vaultPath)) {
    const body = await readFile(file, "utf8");
    assert.equal(
      body.includes(fixture.packet.claim_token),
      false,
      `token leaked into vault file: ${path.relative(fixture.vaultPath, file)}`,
    );
  }

  const docRoots = [
    new URL("../../../docs/", import.meta.url),
    new URL("../commands/", import.meta.url),
    new URL("../skills/minni/", import.meta.url),
  ];
  for (const root of docRoots) {
    for (const file of await markdownFilesUnder(root.pathname)) {
      const body = await readFile(file, "utf8");
      assert.equal(
        body.includes(fixture.packet.claim_token),
        false,
        `token leaked into docs: ${file}`,
      );
    }
  }
});

test("adapter is not a new MCP tool, not team_runtime, and cursor stays out of the wet set", async () => {
  const [teamSrc, serverSrc, packetSrc, dispatchSrc] = await Promise.all([
    readFile(TEAM_SRC, "utf8"),
    readFile(SERVER_SRC, "utf8"),
    readFile(PACKET_SRC, "utf8"),
    readFile(DISPATCH_SRC, "utf8"),
  ]);

  assert.deepEqual([...FIRST_WET_SET], ["grok", "agy", "codex"]);
  assert.equal(FIRST_WET_SET.includes("cursor"), false);

  assert.doesNotMatch(teamSrc, /dispatchWorkerPacket|thread-host-dispatch/);
  assert.doesNotMatch(serverSrc, /dispatchWorkerPacket|thread-host-dispatch/);
  assert.doesNotMatch(
    serverSrc,
    /registerTool\(\s*"dispatch"|registerTool\(\s*"host_dispatch"|registerTool\(\s*"minni_plan_/,
  );
  assert.match(serverSrc, /"minni_thread_worker_update"/);
  assert.match(serverSrc, /"minni_team_evidence"/);
  assert.doesNotMatch(dispatchSrc, /minni_plan_/);
  assert.doesNotMatch(dispatchSrc, /registerTool/);
  assert.doesNotMatch(
    dispatchSrc,
    /from ["'].*team["']|buildTeamEvidence|minni_team_evidence\(/,
    "team evidence is a promotion summary, not a dispatch call",
  );
  assert.doesNotMatch(packetSrc, /dispatchWorkerPacket/);
  assert.match(dispatchSrc, /WorkerPacket/);
  assert.match(dispatchSrc, /minni_thread_worker_update/);
});
