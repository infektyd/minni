// Issue #121 / PR #132 P1: a daemon identity-recovery denial must surface as a
// FAILED call on the plugin surface — never fake success ("No recall results",
// "learned"). Covers both wire shapes: a success-wrapped recovery envelope
// (gate.shared) and a JSON-RPC error carrying the machine route in error.data
// (gated method capability denials).

import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  identityDenialFrom,
  isCrossAgentCapabilityDenial,
  jsonRpcSocketRequest,
  recallCrossAgentDegrade,
  recallMemory,
  recallResponseText,
  recoveryRouteFrom,
} from "../dist/sovereign.js";

const RECOVERY_ROUTE = {
  ok: false,
  status: "recovery_required",
  reason: "unknown_identity",
  identity: null,
  caller: { method: "search", supplied_agent_id: "claude-code" },
  route: { zone: "pre_identity", surface: "diagnostic" },
  remediation: [
    "Stamp this runtime surface with MINNI_AGENT_ID.",
    "Author the matching operator-owned principals/<agent>.json file and chmod it 0600.",
  ],
};

function withFakeDaemon(replyFor, run) {
  return (async () => {
    const dir = await mkdtemp(path.join(tmpdir(), "minni-recovery-"));
    const socketPath = path.join(dir, "minnid.sock");
    const server = net.createServer((client) => {
      client.on("data", (chunk) => {
        const request = JSON.parse(chunk.toString("utf8"));
        client.write(`${JSON.stringify(replyFor(request))}\n`);
      });
    });
    await new Promise((resolve) => server.listen(socketPath, resolve));
    try {
      return await run(socketPath);
    } finally {
      server.close();
      await rm(dir, { recursive: true, force: true });
    }
  })();
}

test("recoveryRouteFrom extracts a success-wrapped recovery envelope", () => {
  assert.deepEqual(recoveryRouteFrom(RECOVERY_ROUTE), RECOVERY_ROUTE);
});

test("recoveryRouteFrom extracts the route from JSON-RPC error.data", () => {
  const envelope = {
    jsonrpc: "2.0",
    id: 1,
    error: { code: -32004, message: "recovery route", data: RECOVERY_ROUTE },
  };
  assert.deepEqual(recoveryRouteFrom(envelope), RECOVERY_ROUTE);
});

test("recoveryRouteFrom ignores ordinary payloads", () => {
  assert.equal(recoveryRouteFrom(undefined), undefined);
  assert.equal(recoveryRouteFrom({ results: "No recall results." }), undefined);
  assert.equal(
    recoveryRouteFrom({ error: { code: -32004, message: "capability_denied" } }),
    undefined,
  );
});

test("jsonRpcSocketRequest reports a success-wrapped recovery envelope as a failed call", async () => {
  const result = await withFakeDaemon(
    (request) => ({ jsonrpc: "2.0", id: request.id, result: RECOVERY_ROUTE }),
    (socketPath) => jsonRpcSocketRequest(socketPath, "search", { query: "x" }),
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /recovery_required \(unknown_identity\)/);
  assert.match(result.error, /principals\/<agent>\.json/);
  // The route stays on data for shape-aware callers (requireSharedGate).
  assert.deepEqual(recoveryRouteFrom(result.data), RECOVERY_ROUTE);
});

test("jsonRpcSocketRequest surfaces a recovery JSON-RPC error with its route", async () => {
  const result = await withFakeDaemon(
    (request) => ({
      jsonrpc: "2.0",
      id: request.id,
      error: {
        code: -32004,
        message: "Minni provenance recovery required for method 'search'",
        data: RECOVERY_ROUTE,
      },
    }),
    (socketPath) => jsonRpcSocketRequest(socketPath, "search", { query: "x" }),
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /recovery required/);
  assert.deepEqual(recoveryRouteFrom(result.data), RECOVERY_ROUTE);
});

test("jsonRpcSocketRequest still reports ordinary results as ok", async () => {
  const result = await withFakeDaemon(
    (request) => ({ jsonrpc: "2.0", id: request.id, result: { results: "hit" } }),
    (socketPath) => jsonRpcSocketRequest(socketPath, "search", { query: "x" }),
  );
  assert.equal(result.ok, true);
  assert.deepEqual(result.data, { results: "hit" });
});

test("recallResponseText prints the recovery route, not fake success or offline fallback", () => {
  const text = recallResponseText(
    "socket health",
    { ok: false, data: RECOVERY_ROUTE, error: "recovery_required (unknown_identity)" },
    [],
  );
  assert.match(text, /identity recovery required/);
  assert.match(text, /unknown_identity/);
  assert.match(text, /principals\/<agent>\.json/);
  assert.doesNotMatch(text, /No recall results/);
  assert.doesNotMatch(text, /Daemon unavailable/);
});

test("recallResponseText keeps the ok and offline-fallback paths unchanged", () => {
  const okText = recallResponseText(
    "socket health",
    { ok: true, data: { results: "### daemon.md (score=1.0)", agent_id: "codex" } },
    [],
  );
  assert.match(okText, /Query: socket health/);

  const offlineText = recallResponseText(
    "socket health",
    { ok: false, error: "Socket not found: /tmp/nope.sock" },
    [
      {
        notePath: "/tmp/vault/wiki/sessions/socket-health.md",
        relativePath: "wiki/sessions/socket-health.md",
        wikilink: "[[wiki/sessions/socket-health]]",
        title: "Socket health",
        snippet: "check the socket",
        score: 61,
      },
    ],
  );
  assert.match(offlineText, /Daemon unavailable — offline vault fallback/);

  const failedText = recallResponseText(
    "socket health",
    { ok: false, error: "Socket not found: /tmp/nope.sock" },
    [],
  );
  assert.match(failedText, /Recall failed: Socket not found/);
});

// ── PR #132 P2: reserved-id (and other -32004) denials are live daemon answers,
// not outages — they must never be mislabeled as "Daemon unavailable" nor be
// masked by the offline vault fallback. ─────────────────────────────────────

const RESERVED_ID_MESSAGE =
  "reserved_agent_id: reserved agent_id 'main' requires operator context for 'search' — " +
  "omit agent_id for the zero-config operator path, or set MINNI_LOCAL_OPERATOR in the " +
  "daemon environment (operator-controlled, never wire-supplied)";

const VAULT_MATCH = {
  notePath: "/tmp/vault/wiki/sessions/socket-health.md",
  relativePath: "wiki/sessions/socket-health.md",
  wikilink: "[[wiki/sessions/socket-health]]",
  title: "Socket health",
  snippet: "check the socket",
  score: 61,
};

test("identityDenialFrom extracts a live -32004 diagnostic, ignores transport failures", () => {
  const envelope = {
    jsonrpc: "2.0",
    id: 1,
    error: { code: -32004, message: RESERVED_ID_MESSAGE },
  };
  assert.equal(identityDenialFrom(envelope), RESERVED_ID_MESSAGE);
  assert.equal(identityDenialFrom(undefined), undefined);
  assert.equal(identityDenialFrom({ results: "hit" }), undefined);
  // Other JSON-RPC errors (e.g. -32602 invalid params) are not identity denials.
  assert.equal(
    identityDenialFrom({ error: { code: -32602, message: "Invalid params" } }),
    undefined,
  );
});

test("minni_recall surfaces the reserved-id diagnostic, never the offline fallback", async () => {
  const result = await withFakeDaemon(
    (request) => ({
      jsonrpc: "2.0",
      id: request.id,
      error: { code: -32004, message: RESERVED_ID_MESSAGE },
    }),
    (socketPath) => jsonRpcSocketRequest(socketPath, "search", { query: "x" }),
  );
  assert.equal(result.ok, false);
  // Even with a local vault match on hand, the denial wins — the daemon
  // ANSWERED; this is a misconfiguration, not an outage.
  const text = recallResponseText("socket health", result, [VAULT_MATCH]);
  assert.match(text, /reserved_agent_id/);
  assert.match(text, /MINNI_LOCAL_OPERATOR/);
  assert.doesNotMatch(text, /Daemon unavailable/);
  assert.doesNotMatch(text, /offline vault fallback/);
  assert.doesNotMatch(text, /socket-health/);
});

test("a true transport failure still falls back to the offline vault scan", () => {
  const text = recallResponseText(
    "socket health",
    { ok: false, error: "connect ECONNREFUSED /tmp/nope.sock" },
    [VAULT_MATCH],
  );
  assert.match(text, /Daemon unavailable — offline vault fallback/);
  assert.match(text, /socket-health/);
});

// ── W5 (punch-list #4): cross_agent deny ergonomics ─────────────────────────
// allows_cross_agent_recall() (principal.py:798-803) is a correct-by-design
// default-deny gate, not a misconfiguration. The plugin must (b) stop calling
// it one, naming the capability + remedy instead, and (c) gracefully degrade
// in-band to a personal-scope retry rather than returning a bare error — but
// ONLY for this exact capability_denied shape, and only when the ORIGINAL
// request actually asked for cross_agent (belt-and-suspenders against a
// misfiring classifier or an already-personal-scope call retrying itself).

const CROSS_AGENT_DENIED_MESSAGE =
  "capability_denied: 'cross_agent' required for 'search' (principal='codex-main')";

const GENERIC_CAPABILITY_DENIED_MESSAGE =
  "capability_denied: 'govern' required for 'plan_activate' (principal='x')";

test("identityDenialFrom + isCrossAgentCapabilityDenial classify the cross_agent capability_denied message", () => {
  const envelope = {
    jsonrpc: "2.0",
    id: 1,
    error: { code: -32004, message: CROSS_AGENT_DENIED_MESSAGE },
  };
  const denial = identityDenialFrom(envelope);
  assert.equal(denial, CROSS_AGENT_DENIED_MESSAGE);
  assert.equal(isCrossAgentCapabilityDenial(denial), true);
});

test("isCrossAgentCapabilityDenial ignores other capability_denied / reserved_agent_id messages", () => {
  assert.equal(isCrossAgentCapabilityDenial(RESERVED_ID_MESSAGE), false);
  assert.equal(isCrossAgentCapabilityDenial(GENERIC_CAPABILITY_DENIED_MESSAGE), false);
});

test("recallResponseText drops 'misconfiguration' wording for cross_agent denials and names the capability + remedy", () => {
  const result = {
    ok: false,
    data: { error: { code: -32004, message: CROSS_AGENT_DENIED_MESSAGE } },
    error: CROSS_AGENT_DENIED_MESSAGE,
  };
  const text = recallResponseText("team status", result, []);
  assert.match(text, /default-deny/);
  assert.match(text, /cross_agent/);
  assert.doesNotMatch(text, /misconfiguration/);
});

test("recallResponseText keeps the 'misconfiguration' framing for non-cross_agent -32004s (reserved_agent_id)", () => {
  const result = {
    ok: false,
    data: { error: { code: -32004, message: RESERVED_ID_MESSAGE } },
    error: RESERVED_ID_MESSAGE,
  };
  const text = recallResponseText("socket health", result, []);
  assert.match(text, /misconfiguration/);
});

test("cross_agent deny degrades in-band to personal scope with a note, not a bare error", async () => {
  const text = await withFakeDaemon(
    (request) =>
      request.params && request.params.cross_agent === true
        ? {
            jsonrpc: "2.0",
            id: request.id,
            error: { code: -32004, message: CROSS_AGENT_DENIED_MESSAGE },
          }
        : {
            jsonrpc: "2.0",
            id: request.id,
            result: {
              results: [{ wikilink: "[[wiki/team-status]]", score: 42, snippet: "personal hit" }],
              agent_id: "codex-main",
              count: 1,
            },
          },
    async (socketPath) => {
      const requester = (_socketPath, method, params) => jsonRpcSocketRequest(socketPath, method, params);
      const denied = await recallMemory({ query: "team status", crossAgent: true }, requester);
      const denial = identityDenialFrom(denied.data);
      assert.equal(isCrossAgentCapabilityDenial(denial), true);
      return recallCrossAgentDegrade({ query: "team status" }, true, denial, requester);
    },
  );
  assert.ok(text, "expected a degraded result, not undefined");
  assert.match(text, /team-status/);
  assert.match(text, /personal hit/);
  assert.match(text, /cross-agent/i);
  assert.doesNotMatch(text, /^Recall failed/);
});

test("cross_agent degrade is a no-op when the original request did not ask for cross_agent (belt-and-suspenders)", async () => {
  const result = await recallCrossAgentDegrade(
    { query: "team status" },
    /* crossAgentRequested */ false,
    CROSS_AGENT_DENIED_MESSAGE,
  );
  assert.equal(result, undefined);
});

test("cross_agent degrade is a no-op for non-cross_agent denials even if somehow invoked", async () => {
  const result = await recallCrossAgentDegrade(
    { query: "team status" },
    true,
    RESERVED_ID_MESSAGE,
  );
  assert.equal(result, undefined);
});
