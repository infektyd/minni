import assert from "node:assert/strict";
import test from "node:test";

import { harvestEvidence } from "../dist/team-harvest.js";
import {
  buildTeamEvidencePacket,
  buildTeamEvidencePacketWithHarvest,
} from "../dist/team.js";

function makeReports() {
  return [
    {
      agentId: "worker",
      status: "completed",
      summary: "Implemented foo",
      evidence: ["Inspected src/foo.ts"],
      changedFiles: ["src/foo.ts"],
      verification: ["npm test"],
    },
    {
      agentId: "explorer",
      status: "completed",
      summary: "Mapped patterns",
      evidence: ["Reviewed docs"],
    },
    {
      agentId: "reviewer",
      status: "blocked",
      summary: "Reviewer hit error path",
      evidence: ["Tried review"],
      blockers: ["AFM unreachable"],
    },
  ];
}

test("harvestEvidence writes one inbox entry per AFM LEARNING, skips SKIP, swallows errors", async () => {
  const inboxCalls = [];
  const audits = [];
  const callAfm = async (_system, user) => {
    if (user.includes("worker")) return "LEARNING: small backend changes win";
    if (user.includes("explorer")) return "  SKIP  ";
    if (user.includes("reviewer")) throw new Error("boom");
    return "";
  };
  const writeInbox = async (vaultPath, slug, payload) => {
    const entry = {
      slug,
      filePath: `${vaultPath}/inbox/2026-05-08-x-${slug}.json`,
      createdAt: "2026-05-08T00:00:00.000Z",
      payload: { slug, createdAt: "2026-05-08T00:00:00.000Z", ...payload },
    };
    inboxCalls.push(entry);
    return entry;
  };
  const audit = async (vaultPath, entry) => {
    audits.push({ vaultPath, entry });
  };

  const result = await harvestEvidence(
    {
      task: "Ship harvest loop",
      vaultPath: "/tmp/vault",
      runtimeId: "team-abc",
      reports: makeReports(),
    },
    { callAfm, writeInbox, audit },
  );

  assert.equal(result.length, 3);

  const written = result.filter((entry) => entry.source === "afm");
  const skipped = result.filter((entry) => entry.source === "skipped");
  assert.equal(written.length, 1);
  assert.equal(written[0].agentId, "worker");
  assert.equal(written[0].candidateText, "small backend changes win");
  assert.equal(written[0].slug, "harvest-worker");
  assert.ok(written[0].inboxFilePath, "afm entry should have inboxFilePath");
  assert.equal(skipped.length, 2);

  const skipReason = skipped.find((entry) => entry.agentId === "explorer");
  const errorReason = skipped.find((entry) => entry.agentId === "reviewer");
  assert.equal(skipReason.reason, "AFM returned SKIP");
  assert.equal(errorReason.reason, "boom");
  assert.equal(skipReason.slug, undefined, "skipped entries should not carry a slug");
  assert.equal(skipReason.inboxFilePath, undefined, "skipped entries should not carry an inboxFilePath");

  assert.equal(inboxCalls.length, 1);
  assert.equal(inboxCalls[0].slug, "harvest-worker");
  assert.equal(inboxCalls[0].payload.kind, "team-harvest");
  assert.equal(inboxCalls[0].payload.runtimeId, "team-abc");
  assert.equal(inboxCalls[0].payload.agentId, "worker");
  assert.equal(inboxCalls[0].payload.source, "afm");

  assert.equal(audits.length, 1);
  assert.equal(audits[0].entry.tool, "sovereign_team_harvest");
  assert.equal(audits[0].entry.details.runtimeId, "team-abc");
  assert.equal(audits[0].entry.details.totalReports, 3);
  assert.equal(audits[0].entry.details.written, 1);
  assert.equal(audits[0].entry.details.skipped, 2);
});

test("harvestEvidence treats off-contract AFM responses as skipped, not learnings", async () => {
  const inboxCalls = [];
  const callAfm = async () => "Sure! Here is what I think you should learn: write more tests.";
  const writeInbox = async (vaultPath, slug, payload) => {
    const entry = {
      slug,
      filePath: `${vaultPath}/${slug}.json`,
      createdAt: "now",
      payload: { slug, createdAt: "now", ...payload },
    };
    inboxCalls.push(entry);
    return entry;
  };
  const audit = async () => undefined;

  const result = await harvestEvidence(
    {
      task: "Off-contract test",
      vaultPath: "/tmp/vault",
      reports: [{ agentId: "agent-1", status: "completed", summary: "ok" }],
    },
    { callAfm, writeInbox, audit },
  );

  assert.equal(result.length, 1);
  assert.equal(result[0].source, "skipped");
  assert.equal(result[0].reason, "no LEARNING: prefix");
  assert.equal(inboxCalls.length, 0);
});

test("harvestEvidence redacts local paths and adapter filenames before inbox write", async () => {
  const inboxCalls = [];
  const callAfm = async () => "LEARNING: load adapter at /Users/foo/bar.fmadapter and /Volumes/Data/x.txt";
  const writeInbox = async (vaultPath, slug, payload) => {
    const entry = {
      slug,
      filePath: `${vaultPath}/inbox/${slug}.json`,
      createdAt: "now",
      payload: { slug, createdAt: "now", ...payload },
    };
    inboxCalls.push(entry);
    return entry;
  };
  const audit = async () => undefined;

  const result = await harvestEvidence(
    {
      task: "Privacy",
      vaultPath: "/tmp/vault",
      reports: [{ agentId: "agent-1", status: "completed", summary: "ok" }],
    },
    { callAfm, writeInbox, audit },
  );

  assert.equal(result.length, 1);
  const learning = result[0];
  assert.equal(learning.source, "afm");
  assert.ok(!learning.candidateText.includes("/Users/foo"), "candidateText leaked /Users path");
  assert.ok(!learning.candidateText.includes("bar.fmadapter"), "candidateText leaked .fmadapter filename");
  assert.ok(!learning.candidateText.includes("/Volumes/Data"), "candidateText leaked /Volumes path");

  const payload = inboxCalls[0].payload;
  assert.ok(!payload.candidateText.includes("/Users/foo"));
  assert.ok(!payload.candidateText.includes("bar.fmadapter"));
  assert.ok(!payload.candidateText.includes("/Volumes/Data"));
});

test("harvestEvidence enforces maxLearningsPerCall as a global cap", async () => {
  const inboxCalls = [];
  const callAfm = async () => "LEARNING: noteworthy thing";
  const writeInbox = async (vaultPath, slug, payload) => {
    const entry = {
      slug,
      filePath: `${vaultPath}/inbox/${slug}-${inboxCalls.length}.json`,
      createdAt: "now",
      payload: { slug, createdAt: "now", ...payload },
    };
    inboxCalls.push(entry);
    return entry;
  };
  const audit = async () => undefined;

  const reports = Array.from({ length: 5 }, (_, index) => ({
    agentId: `agent-${index + 1}`,
    status: "completed",
    summary: `Did thing ${index + 1}`,
  }));

  const result = await harvestEvidence(
    {
      task: "Cap enforcement",
      vaultPath: "/tmp/vault",
      reports,
      maxLearningsPerCall: 2,
    },
    { callAfm, writeInbox, audit },
  );

  assert.equal(result.length, 5);
  assert.equal(result.filter((entry) => entry.source === "afm").length, 2);
  const capped = result.filter((entry) => entry.reason === "per-call cap reached");
  assert.equal(capped.length, 3);
  assert.equal(inboxCalls.length, 2);
});

test("harvestEvidence reports audit failures to stderr without dropping the result", async () => {
  const originalWrite = process.stderr.write.bind(process.stderr);
  const captured = [];
  process.stderr.write = (chunk, ...rest) => {
    captured.push(typeof chunk === "string" ? chunk : chunk.toString());
    return originalWrite(chunk, ...rest);
  };
  try {
    const result = await harvestEvidence(
      {
        task: "Audit failure",
        vaultPath: "/tmp/vault",
        reports: [{ agentId: "agent-1", status: "completed", summary: "ok" }],
      },
      {
        callAfm: async () => "LEARNING: keep audit visible",
        writeInbox: async (vaultPath, slug, payload) => ({
          slug,
          filePath: `${vaultPath}/${slug}.json`,
          createdAt: "now",
          payload: { slug, createdAt: "now", ...payload },
        }),
        audit: async () => {
          throw new Error("audit-disk-full");
        },
      },
    );
    assert.equal(result.length, 1);
    assert.equal(result[0].source, "afm");
    const matched = captured.find((line) => line.includes("sovereign_team_harvest") && line.includes("audit-disk-full"));
    assert.ok(matched, "expected stderr line referencing failed audit, got: " + JSON.stringify(captured));
  } finally {
    process.stderr.write = originalWrite;
  }
});

test("buildTeamEvidencePacketWithHarvest wires harvest end-to-end", async () => {
  const inboxCalls = [];
  const callAfm = async () => "LEARNING: end-to-end works";
  const writeInbox = async (vaultPath, slug, payload) => {
    const entry = {
      slug,
      filePath: `${vaultPath}/inbox/${slug}.json`,
      createdAt: "now",
      payload: { slug, createdAt: "now", ...payload },
    };
    inboxCalls.push(entry);
    return entry;
  };
  const audit = async () => undefined;

  const packet = await buildTeamEvidencePacketWithHarvest(
    {
      runtimeId: "team-xyz",
      task: "End-to-end harvest",
      vaultPath: "/tmp/vault",
      results: [
        {
          agentId: "worker",
          status: "completed",
          summary: "Did the thing.",
          evidence: ["a", "b"],
          changedFiles: ["x.ts"],
          verification: ["npm test"],
        },
      ],
    },
    { callAfm, writeInbox, audit },
  );

  assert.ok(Array.isArray(packet.harvestedLearnings));
  assert.equal(packet.harvestedLearnings.length, 1);
  assert.equal(packet.harvestedLearnings[0].source, "afm");
  assert.equal(packet.harvestedLearnings[0].candidateText, "end-to-end works");
  assert.equal(inboxCalls.length, 1);
  assert.match(packet.contextMarkdown, /Harvested Candidates/);
});

test("buildTeamEvidencePacket without harvest stays sync and unchanged", () => {
  const packet = buildTeamEvidencePacket({
    task: "Plain evidence",
    results: [
      { agentId: "worker", status: "completed", summary: "Done" },
    ],
  });
  assert.equal(packet.harvestedLearnings, undefined);
  assert.ok(!packet.contextMarkdown.includes("Harvested Candidates"));
});

test("buildTeamEvidencePacketWithHarvest only renders afm rows in markdown", async () => {
  const callAfm = async (_system, user) => (user.includes("worker") ? "LEARNING: kept" : "SKIP");
  const writeInbox = async (vaultPath, slug, payload) => ({
    slug,
    filePath: `${vaultPath}/inbox/${slug}.json`,
    createdAt: "now",
    payload: { slug, createdAt: "now", ...payload },
  });
  const audit = async () => undefined;

  const packet = await buildTeamEvidencePacketWithHarvest(
    {
      task: "Mixed harvest",
      vaultPath: "/tmp/vault",
      results: [
        { agentId: "worker", status: "completed", summary: "Did" },
        { agentId: "explorer", status: "completed", summary: "Read" },
      ],
    },
    { callAfm, writeInbox, audit },
  );

  assert.equal(packet.harvestedLearnings.length, 2);
  const harvestedSection = packet.contextMarkdown.split("## Harvested Candidates")[1] ?? "";
  assert.ok(harvestedSection.length > 0, "expected a Harvested Candidates section");
  assert.match(harvestedSection, /worker: kept/);
  assert.ok(!harvestedSection.includes("explorer"), "skipped rows must not appear in Harvested Candidates");
  assert.ok(!harvestedSection.includes("AFM returned SKIP"), "skip reasons must not leak into operator markdown");
  assert.ok(!packet.contextMarkdown.includes("AFM returned SKIP"));
});

test("buildTeamEvidencePacketWithHarvest throws when vaultPath missing", async () => {
  await assert.rejects(
    () =>
      buildTeamEvidencePacketWithHarvest({
        task: "missing path",
        results: [{ agentId: "worker", status: "completed", summary: "Done" }],
      }),
    /vaultPath/,
  );
});
