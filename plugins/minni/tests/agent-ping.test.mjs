import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { after } from "node:test";

const root = await mkdtemp(path.join(tmpdir(), "sm-agent-ping-"));
const codexVault = path.join(root, "codex-vault");
const claudeVault = path.join(root, "claude-vault");
const geminiVault = path.join(root, "gemini-vault");

process.env.MINNI_CODEX_AGENT_ID = "codex";
process.env.MINNI_CODEX_VAULT_PATH = codexVault;
process.env.MINNI_HOME = path.join(root, "minni-home");
process.env.MINNI_AGENT_VAULTS = JSON.stringify({
  codex: codexVault,
  "claude-code": claudeVault,
  gemini: geminiVault,
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

  await assert.rejects(
    () => getAgentPingStatus(created.contract.requestId, "codex", new Date("2026-05-02T00:02:00.000Z")),
    /Request not found/
  );
});

test("RCM-004: ping request does not create recipient inbox on disk", async () => {
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Will you see this before polling?",
    },
    "codex"
  );

  // Recipient inbox file should NOT exist yet
  const inboxFile = path.join(claudeVault, "inbox", "agent-pings", `${created.contract.requestId}.json`);
  await assert.rejects(readFile(inboxFile, "utf8"), /ENOENT/);
});

test("RCM-004: ping materializes on recipient poll (listAgentPingInbox)", async () => {
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Polled request",
    },
    "codex"
  );

  const inboxFile = path.join(claudeVault, "inbox", "agent-pings", `${created.contract.requestId}.json`);
  await assert.rejects(readFile(inboxFile, "utf8"), /ENOENT/);

  // Recipient polls
  const inbox = await listAgentPingInbox("claude-code");
  assert.ok(inbox.requests.some(r => r.requestId === created.contract.requestId));

  // Now it MUST exist on disk
  const content = await readFile(inboxFile, "utf8");
  assert.ok(content.includes("Polled request"));
});

test("RCM-004: recipient getAgentPingStatus sees pending request without polling first", async () => {
  // Regression for Codex P2 on PR #23: getAgentPingStatus must materialize
  // from the live lease so the recipient can query status before any inbox
  // poll has happened.
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Status-before-poll request",
    },
    "codex"
  );

  // Recipient inbox copy must NOT exist yet (precondition).
  const inboxFile = path.join(claudeVault, "inbox", "agent-pings", `${created.contract.requestId}.json`);
  await assert.rejects(readFile(inboxFile, "utf8"), /ENOENT/);

  // Recipient calls status directly, with no prior listAgentPingInbox call.
  const status = await getAgentPingStatus(created.contract.requestId, "claude-code");
  assert.equal(status.contract.requestId, created.contract.requestId);
  assert.equal(status.contract.status, "pending");
  assert.equal(status.contract.toAgent, "claude-code");

  // Side-effect: status call materialized the recipient inbox copy.
  const content = await readFile(inboxFile, "utf8");
  assert.ok(content.includes("Status-before-poll request"));
});

test("RCM-004: ping materializes on recipient decide", async () => {
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Decide request",
    },
    "codex"
  );

  const inboxFile = path.join(claudeVault, "inbox", "agent-pings", `${created.contract.requestId}.json`);
  await assert.rejects(readFile(inboxFile, "utf8"), /ENOENT/);

  // Recipient decides without polling first
  await decideAgentPingRequest(
    {
      requestId: created.contract.requestId,
      decision: "deny",
      reason: "No poll decide"
    },
    "claude-code"
  );

  // Now it MUST exist on disk and be decided
  const content = JSON.parse(await readFile(inboxFile, "utf8"));
  assert.equal(content.status, "denied");
});

test("RCM-004: ping lease expires after TTL and reaps files", async () => {
  const now = new Date("2026-05-02T00:00:00.000Z");
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Short lease",
      ttlMinutes: 5,
      now,
    },
    "codex"
  );

  const leaseFile = path.join(process.env.MINNI_HOME, "pings", "leases", `${created.contract.requestId}.json`);
  const senderFile = created.senderPath;

  // Verify they exist initially
  await readFile(leaseFile, "utf8");
  await readFile(senderFile, "utf8");

  // Advance time past TTL (5 min)
  const queryTime = new Date("2026-05-02T00:06:00.000Z");

  // Status check reaps it. #297: the first status check after expiry must
  // report "expired", distinguishable from a request that never existed —
  // not the generic "Request not found." the reap used to leave behind.
  await assert.rejects(
    () => getAgentPingStatus(created.contract.requestId, "codex", queryTime),
    /Request expired/
  );

  // Files should be reaped/removed
  await assert.rejects(readFile(leaseFile, "utf8"), /ENOENT/);
});

test("#297: expired-and-reaped is distinguishable from never-existed", async () => {
  const now = new Date("2026-05-03T00:00:00.000Z");
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Short lease for #297",
      ttlMinutes: 5,
      now,
    },
    "codex",
  );

  const queryTime = new Date("2026-05-03T00:06:00.000Z");

  // The request genuinely existed and expired: distinguishable message.
  await assert.rejects(
    () => getAgentPingStatus(created.contract.requestId, "codex", queryTime),
    /Request expired/,
  );

  // A requestId that was never created at all: the other message, not
  // conflated with the expired case above.
  const neverExisted = "00000000-0000-4000-8000-000000000000";
  await assert.rejects(
    () => getAgentPingStatus(neverExisted, "codex", queryTime),
    /Request not found/,
  );

  // A second status check on the now fully-reaped (lease file gone) request
  // falls back to "not found" — this is the honest limit of the fix (the
  // pre-reap snapshot only survives the FIRST post-expiry call); it must
  // not be conflated with "Request expired." either.
  await assert.rejects(
    () => getAgentPingStatus(created.contract.requestId, "codex", queryTime),
    /Request not found/,
  );
});

test("#297: an unauthorized actor cannot learn an expired request exists", async () => {
  // Review round on this fix's first draft: it threw the normal-path
  // "Only the requester or recipient..." authorization error for an
  // unauthorized actor probing an EXPIRED request — a new existence oracle
  // a third party didn't have before (pre-#297, they always landed on the
  // same "Request not found." a genuinely nonexistent id produces, because
  // the request is never materialized into a non-party's own vault).
  // Assert message EQUALITY between the two cases, not just "isn't the
  // distinguishing message" — a weaker assertion here already gave a false
  // pass against that regression once.
  const now = new Date("2026-05-04T00:00:00.000Z");
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Short lease, third party should not see it",
      ttlMinutes: 5,
      now,
    },
    "codex",
  );

  const queryTime = new Date("2026-05-04T00:06:00.000Z");
  const neverExisted = "11111111-1111-4111-8111-111111111111";

  const expiredThirdPartyMessage = await getAgentPingStatus(
    created.contract.requestId,
    "some-other-agent",
    queryTime,
  ).then(
    () => { throw new Error("expected getAgentPingStatus to reject"); },
    (error) => error.message,
  );
  const neverExistedMessage = await getAgentPingStatus(
    neverExisted,
    "some-other-agent",
    queryTime,
  ).then(
    () => { throw new Error("expected getAgentPingStatus to reject"); },
    (error) => error.message,
  );

  assert.equal(expiredThirdPartyMessage, neverExistedMessage);
  assert.equal(expiredThirdPartyMessage, "Request not found.");
});

test("RCM-004: ping materialization rejects wrong principal", async () => {
  const created = await createAgentPingRequest(
    {
      toAgent: "claude-code",
      question: "Intruder test",
    },
    "codex"
  );

  // If wrong agent (e.g. "gemini") tries to decide or poll, the request is not materialized for them
  // and they cannot access it.
  const inbox = await listAgentPingInbox("gemini");
  assert.ok(!inbox.requests.some(r => r.requestId === created.contract.requestId));
  const wrongPrincipalInboxFile = path.join(geminiVault, "inbox", "agent-pings", `${created.contract.requestId}.json`);
  await assert.rejects(readFile(wrongPrincipalInboxFile, "utf8"), /ENOENT/);

  await assert.rejects(
    () => decideAgentPingRequest(
      {
        requestId: created.contract.requestId,
        decision: "approve",
        answer: "intruder answer"
      },
      "gemini"
    ),
    /Only the recipient agent/
  );
  await assert.rejects(readFile(wrongPrincipalInboxFile, "utf8"), /ENOENT/);
});
