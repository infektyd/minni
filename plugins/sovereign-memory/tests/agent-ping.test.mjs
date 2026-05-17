import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { after } from "node:test";

const root = await mkdtemp(path.join(tmpdir(), "sm-agent-ping-"));
const codexVault = path.join(root, "codex-vault");
const claudeVault = path.join(root, "claude-vault");

process.env.SOVEREIGN_CODEX_AGENT_ID = "codex";
process.env.SOVEREIGN_CODEX_VAULT_PATH = codexVault;
process.env.SOVEREIGN_AGENT_VAULTS = JSON.stringify({
  codex: codexVault,
  "claude-code": claudeVault,
});

const {
  createAgentPingRequest,
  decideAgentPingRequest,
  getAgentPingStatus,
  listAgentPingInbox,
} = await import("../dist/agent_ping.js");

after(async () => {
  await rm(root, { recursive: true, force: true });
});

test("agent ping request stays pending until the recipient approves", async () => {
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "What is the safest interface for cross-agent recall?",
      purpose: "Need architecture guidance without exposing private memory.",
      allowedTopics: ["architecture", "security"],
      maxResponseChars: 160,
    },
    "codex",
  );

  assert.equal(created.contract.status, "pending");
  assert.equal(created.contract.fromAgent, "codex");
  assert.equal(created.contract.toAgent, "claude-code");
  assert.equal(created.contract.response, undefined);
  assert.match(created.senderPath, /outbox\/agent-pings\/.+\.json$/);
  assert.match(created.recipientPath, /inbox\/agent-pings\/.+\.json$/);

  const recipientInbox = await listAgentPingInbox("claude-code");
  assert.equal(recipientInbox.requests.length, 1);
  assert.equal(recipientInbox.requests[0].requestId, created.contract.requestId);
  assert.equal(recipientInbox.requests[0].status, "pending");

  const decided = await decideAgentPingRequest(
    {
      requestId: created.contract.requestId,
      decision: "approve",
      answer:
        "Use attributed inbox contracts only. Never return raw private memory or secrets like api_key=abcdef123456 from /Users/alice/private/vault.",
      reason: "Safe to share architectural guidance.",
    },
    "claude-code",
  );

  assert.equal(decided.contract.status, "approved");
  assert.equal(decided.contract.response.decision, "approve");
  assert.equal(decided.contract.response.decidedBy, "claude-code");
  assert.match(decided.contract.response.answer, /api_key=\[REDACTED\]/);
  assert.match(decided.contract.response.answer, /\[local-path\]/);
  assert.equal(decided.contract.response.redacted, true);

  const requesterStatus = await getAgentPingStatus(created.contract.requestId, "codex");
  assert.equal(requesterStatus.contract.status, "approved");
  assert.equal(requesterStatus.contract.response.answer, decided.contract.response.answer);

  const senderCopy = JSON.parse(await readFile(created.senderPath, "utf8"));
  const recipientCopy = JSON.parse(await readFile(created.recipientPath, "utf8"));
  assert.equal(senderCopy.status, "approved");
  assert.equal(recipientCopy.status, "approved");
});

test("only the recipient can decide and terminal decisions cannot replay", async () => {
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Can I import the latest handoff summary?",
      ttlMinutes: 10,
    },
    "codex",
  );

  await assert.rejects(
    () => decideAgentPingRequest({ requestId: created.contract.requestId, decision: "deny", reason: "spoof" }, "codex"),
    /Only the recipient agent/,
  );

  await decideAgentPingRequest(
    { requestId: created.contract.requestId, decision: "deny", reason: "Not enough context." },
    "claude-code",
  );

  await assert.rejects(
    () => decideAgentPingRequest({ requestId: created.contract.requestId, decision: "approve", answer: "retry" }, "claude-code"),
    /only pending requests/,
  );
});

test("expired requests cannot be approved", async () => {
  const now = new Date("2026-05-02T00:00:00.000Z");
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Short lived request",
      ttlMinutes: 1,
      now,
    },
    "codex",
  );

  await assert.rejects(
    () =>
      decideAgentPingRequest(
        {
          requestId: created.contract.requestId,
          decision: "approve",
          answer: "too late",
          now: new Date("2026-05-02T00:02:00.000Z"),
        },
        "claude-code",
      ),
    /Request is expired/,
  );

  const status = await getAgentPingStatus(created.contract.requestId, "codex", new Date("2026-05-02T00:02:00.000Z"));
  assert.equal(status.contract.status, "expired");
});
