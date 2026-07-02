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
  jsonRpcSocketRequest,
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
