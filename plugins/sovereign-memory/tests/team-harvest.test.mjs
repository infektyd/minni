import assert from "node:assert/strict";
import test from "node:test";

import { harvestEvidence } from "../dist/team-harvest.js";
import { buildTeamEvidencePacket } from "../dist/team.js";

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
  assert.equal(skipped.length, 2);

  const skipReason = skipped.find((entry) => entry.agentId === "explorer");
  const errorReason = skipped.find((entry) => entry.agentId === "reviewer");
  assert.match(skipReason.reason ?? "", /skip/i);
  assert.match(errorReason.reason ?? "", /boom/);

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

test("buildTeamEvidencePacket wires harvest:true through to harvestEvidence", async () => {
  const inboxCalls = [];
  const audits = [];
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
  const audit = async (vaultPath, entry) => {
    audits.push(entry);
  };

  const packet = await buildTeamEvidencePacket(
    {
      runtimeId: "team-xyz",
      task: "End-to-end harvest",
      harvest: true,
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

test("buildTeamEvidencePacket without harvest flag is unchanged", async () => {
  const packet = await buildTeamEvidencePacket({
    task: "Plain evidence",
    results: [
      { agentId: "worker", status: "completed", summary: "Done" },
    ],
  });
  assert.equal(packet.harvestedLearnings, undefined);
  assert.ok(!packet.contextMarkdown.includes("Harvested Candidates"));
});

test("buildTeamEvidencePacket with harvest:true throws when vaultPath missing", () => {
  assert.throws(
    () =>
      buildTeamEvidencePacket({
        task: "missing path",
        harvest: true,
        results: [{ agentId: "worker", status: "completed", summary: "Done" }],
      }),
    /vaultPath/,
  );
});
