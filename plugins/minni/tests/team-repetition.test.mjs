import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  DEFAULT_LOOKBACK_DAYS,
  DEFAULT_MIN_REPEATS,
  MAX_SUGGESTIONS,
  findRepeatedAgents,
} from "../dist/team-repetition.js";
import { recordAudit } from "../dist/vault.js";

// Hermetic guard: recordAudit writes per-agent rate-limit state under
// MINNI_HOME (falling back to ~/.minni) — point it at a temp dir so the
// suite never touches the real home (CI smoke asserts zero ~ pollution).
process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-test-home-"));

const NOW = new Date("2026-05-08T12:00:00.000Z");

function isoDaysAgo(days, time = "10:00:00.000Z") {
  const date = new Date(NOW);
  date.setUTCDate(date.getUTCDate() - days);
  return `${date.toISOString().slice(0, 10)}T${time}`;
}

function entry({ timestamp, tool = "minni_team_runtime", details, summary = "task" }) {
  const detailBlock = details === undefined
    ? ""
    : `\`\`\`json\n${typeof details === "string" ? details : JSON.stringify(details, null, 2)}\n\`\`\`\n\n`;
  return `## [${timestamp}] ${tool} | ${summary}\n\n${detailBlock}`;
}

function logFile(prefix, entries) {
  return `${prefix}\n\n${entries.join("")}`;
}

function makeAgentDetails(runtimeId, agents) {
  return {
    runtimeId,
    coordinatorAgentId: "codex",
    workspaceId: "/repo",
    agents,
    automaticLearning: false,
  };
}

function deps({ paths, files }) {
  return {
    readAuditLogPaths: async () => paths,
    readAuditFile: async (filePath) => {
      if (!(filePath in files)) throw new Error(`unexpected read of ${filePath}`);
      return files[filePath];
    },
  };
}

test("findRepeatedAgents returns empty when no audit logs are listed", async () => {
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    { readAuditLogPaths: async () => [], readAuditFile: async () => "" },
  );
  assert.deepEqual(result, []);
});

test("findRepeatedAgents ignores entries from other tools", async () => {
  const file = logFile("# log", [
    entry({
      timestamp: isoDaysAgo(1),
      tool: "minni_vault_write",
      details: { foo: "bar" },
      summary: "wrote",
    }),
    entry({
      timestamp: isoDaysAgo(2),
      tool: "minni_team_harvest",
      details: { runtimeId: "x" },
      summary: "harvested",
    }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.deepEqual(result, []);
});

test("findRepeatedAgents promotes a signature that meets minRepeats", async () => {
  const agents = [{ agentId: "team-worker-1", role: "worker", focus: "Audit Swift concurrency" }];
  const file = logFile("# log", [
    entry({ timestamp: isoDaysAgo(1), details: makeAgentDetails("team-a", agents) }),
    entry({ timestamp: isoDaysAgo(3), details: makeAgentDetails("team-b", agents) }),
    entry({ timestamp: isoDaysAgo(5), details: makeAgentDetails("team-c", agents) }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 3);
  assert.equal(result[0].suggestPromotion, true);
  assert.equal(result[0].role, "worker");
  assert.equal(result[0].normalizedFocus, "audit swift concurrency");
  assert.equal(result[0].examples.length, 3);
  // Examples are sorted by timestamp ascending (earliest first).
  const timestamps = result[0].examples.map((ex) => ex.timestamp);
  assert.deepEqual(
    [...timestamps].sort(),
    timestamps,
    "examples should be in chronological order",
  );
  assert.equal(result[0].examples[0].runtimeId, "team-c");
});

test("findRepeatedAgents marks suggestPromotion=false below threshold", async () => {
  const agents = [{ agentId: "a1", role: "worker", focus: "Implement repetition counter" }];
  const file = logFile("# log", [
    entry({ timestamp: isoDaysAgo(1), details: makeAgentDetails("r1", agents) }),
    entry({ timestamp: isoDaysAgo(2), details: makeAgentDetails("r2", agents) }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 2);
  assert.equal(result[0].suggestPromotion, false);
});

test("findRepeatedAgents excludes entries outside the lookback window", async () => {
  const agents = [{ agentId: "a1", role: "worker", focus: "Implement counter" }];
  const file = logFile("# log", [
    entry({ timestamp: isoDaysAgo(1), details: makeAgentDetails("r1", agents) }),
    entry({ timestamp: isoDaysAgo(2), details: makeAgentDetails("r2", agents) }),
    entry({ timestamp: isoDaysAgo(20), details: makeAgentDetails("r3", agents) }),
    entry({ timestamp: isoDaysAgo(30), details: makeAgentDetails("r4", agents) }),
    entry({ timestamp: isoDaysAgo(60), details: makeAgentDetails("r5", agents) }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW, lookbackDays: 14 },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 2);
});

test("findRepeatedAgents normalizes focus aggressively before grouping", async () => {
  const file = logFile("# log", [
    entry({
      timestamp: isoDaysAgo(1),
      details: makeAgentDetails("r1", [
        { agentId: "a1", role: "worker", focus: "Audit Swift concurrency" },
      ]),
    }),
    entry({
      timestamp: isoDaysAgo(2),
      details: makeAgentDetails("r2", [
        { agentId: "a2", role: "worker", focus: "audit swift concurrency." },
      ]),
    }),
    entry({
      timestamp: isoDaysAgo(3),
      details: makeAgentDetails("r3", [
        { agentId: "a3", role: "worker", focus: "  AUDIT  SWIFT  concurrency" },
      ]),
    }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 3);
  assert.equal(result[0].normalizedFocus, "audit swift concurrency");
  assert.equal(result[0].suggestPromotion, true);
});

test("findRepeatedAgents sorts multiple signatures by count desc, signature asc", async () => {
  const file = logFile("# log", [
    entry({
      timestamp: isoDaysAgo(1),
      details: makeAgentDetails("r1", [{ agentId: "a", role: "worker", focus: "alpha" }]),
    }),
    entry({
      timestamp: isoDaysAgo(2),
      details: makeAgentDetails("r2", [{ agentId: "a", role: "worker", focus: "alpha" }]),
    }),
    entry({
      timestamp: isoDaysAgo(3),
      details: makeAgentDetails("r3", [{ agentId: "a", role: "worker", focus: "alpha" }]),
    }),
    entry({
      timestamp: isoDaysAgo(4),
      details: makeAgentDetails("r4", [{ agentId: "b", role: "worker", focus: "beta" }]),
    }),
    entry({
      timestamp: isoDaysAgo(5),
      details: makeAgentDetails("r5", [{ agentId: "b", role: "worker", focus: "beta" }]),
    }),
    entry({
      timestamp: isoDaysAgo(6),
      details: makeAgentDetails("r6", [{ agentId: "c", role: "worker", focus: "gamma" }]),
    }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 3);
  assert.equal(result[0].normalizedFocus, "alpha");
  assert.equal(result[0].count, 3);
  assert.equal(result[0].suggestPromotion, true);
  assert.equal(result[1].normalizedFocus, "beta");
  assert.equal(result[1].count, 2);
  assert.equal(result[1].suggestPromotion, false);
  assert.equal(result[2].normalizedFocus, "gamma");
  assert.equal(result[2].count, 1);
  assert.equal(result[2].suggestPromotion, false);
});

test("findRepeatedAgents counts repetitions across multiple daily files", async () => {
  const dayOnePath = "/tmp/v/logs/2026-05-04.md";
  const dayFivePath = "/tmp/v/logs/2026-05-08.md";
  const rollingPath = "/tmp/v/log.md";
  const dayOne = logFile("# 2026-05-04", [
    entry({
      timestamp: "2026-05-04T09:00:00.000Z",
      details: makeAgentDetails("r1", [{ agentId: "a", role: "worker", focus: "shared focus" }]),
    }),
  ]);
  const dayFive = logFile("# 2026-05-08", [
    entry({
      timestamp: "2026-05-08T09:00:00.000Z",
      details: makeAgentDetails("r2", [{ agentId: "b", role: "worker", focus: "shared focus" }]),
    }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({
      paths: [dayOnePath, dayFivePath, rollingPath],
      files: { [dayOnePath]: dayOne, [dayFivePath]: dayFive, [rollingPath]: "# log\n\n" },
    }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 2);
  assert.equal(result[0].suggestPromotion, false);
});

test("findRepeatedAgents skips entries with malformed JSON details", async () => {
  const malformed = entry({
    timestamp: isoDaysAgo(1),
    details: "{ this is not valid json",
  });
  const valid = entry({
    timestamp: isoDaysAgo(2),
    details: makeAgentDetails("r2", [{ agentId: "a", role: "worker", focus: "valid focus" }]),
  });
  const file = logFile("# log", [malformed, valid]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 1);
  assert.equal(result[0].normalizedFocus, "valid focus");
});

test("findRepeatedAgents skips entries with no agents array and agents missing required fields", async () => {
  const noAgents = entry({
    timestamp: isoDaysAgo(1),
    details: { runtimeId: "r1", coordinatorAgentId: "codex", workspaceId: "/r", automaticLearning: false },
  });
  const missingFocus = entry({
    timestamp: isoDaysAgo(2),
    details: makeAgentDetails("r2", [{ agentId: "a", role: "worker" }]),
  });
  const valid = entry({
    timestamp: isoDaysAgo(3),
    details: makeAgentDetails("r3", [{ agentId: "a", role: "worker", focus: "valid focus" }]),
  });
  const file = logFile("# log", [noAgents, missingFocus, valid]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].count, 1);
  assert.equal(result[0].normalizedFocus, "valid focus");
});

test("findRepeatedAgents trims to MAX_SUGGESTIONS=20 after sorting", async () => {
  const entries = [];
  for (let i = 0; i < 25; i += 1) {
    entries.push(
      entry({
        timestamp: isoDaysAgo(1, `${String(i % 24).padStart(2, "0")}:00:00.000Z`),
        details: makeAgentDetails(`r${i}`, [
          { agentId: `a${i}`, role: "worker", focus: `focus number ${i.toString().padStart(2, "0")}` },
        ]),
      }),
    );
  }
  const file = logFile("# log", entries);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    deps({ paths: ["/tmp/v/log.md"], files: { "/tmp/v/log.md": file } }),
  );
  assert.equal(result.length, MAX_SUGGESTIONS);
  // Stable sort by signature ascending when counts tie at 1.
  const signatures = result.map((suggestion) => suggestion.signature);
  assert.deepEqual([...signatures].sort(), signatures);
});

test("findRepeatedAgents returns [] when readAuditLogPaths throws", async () => {
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    {
      readAuditLogPaths: async () => {
        throw new Error("listing failed");
      },
      readAuditFile: async () => "",
    },
  );
  assert.deepEqual(result, []);
});

test("findRepeatedAgents continues past a single readAuditFile failure", async () => {
  const goodFile = logFile("# log", [
    entry({
      timestamp: isoDaysAgo(1),
      details: makeAgentDetails("r1", [{ agentId: "a", role: "worker", focus: "still counted" }]),
    }),
  ]);
  const result = await findRepeatedAgents(
    { vaultPath: "/tmp/v", now: NOW },
    {
      readAuditLogPaths: async () => ["/tmp/v/logs/2026-05-08.md", "/tmp/v/log.md"],
      readAuditFile: async (filePath) => {
        if (filePath.endsWith(".md") && filePath.includes("logs/")) {
          throw new Error("file unreadable");
        }
        return goodFile;
      },
    },
  );
  assert.equal(result.length, 1);
  assert.equal(result[0].normalizedFocus, "still counted");
});

test("findRepeatedAgents exposes documented defaults", () => {
  assert.equal(DEFAULT_LOOKBACK_DAYS, 14);
  assert.equal(DEFAULT_MIN_REPEATS, 3);
  assert.equal(MAX_SUGGESTIONS, 20);
});

// Regression guard for the double-counting bug: recordAudit writes every entry
// to BOTH log.md AND logs/<date>.md. The default reader must prefer dailies and
// only fall back to log.md when no dailies exist, otherwise every observation
// would be counted twice and a single repetition would trip suggestPromotion.
test("findRepeatedAgents integration — real recordAudit writes are not double-counted", async () => {
  const tmpVault = await mkdtemp(path.join(tmpdir(), "sm-team-repetition-"));
  try {
    const sharedAgents = [
      { agentId: "team-worker-1", role: "worker", focus: "Audit Swift concurrency" },
    ];
    for (let i = 0; i < 3; i += 1) {
      await recordAudit(tmpVault, {
        tool: "minni_team_runtime",
        summary: `spawn ${i}`,
        details: {
          runtimeId: `team-int-${i}`,
          coordinatorAgentId: "codex",
          workspaceId: "/repo",
          agents: sharedAgents,
          automaticLearning: false,
        },
      });
    }
    const result = await findRepeatedAgents({ vaultPath: tmpVault });
    assert.equal(result.length, 1, "expected a single signature");
    assert.equal(result[0].count, 3, "count must be 3, not 6 (double-counting regression)");
    assert.equal(result[0].suggestPromotion, true);
    assert.equal(result[0].normalizedFocus, "audit swift concurrency");
  } finally {
    await rm(tmpVault, { recursive: true, force: true });
  }
});
