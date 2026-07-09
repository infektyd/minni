import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldOfflineVaultPrescan, shouldPrescanVault } from "../dist/sovereign.js";

test("hook/MCP: no offline vault pre-scan when daemon recall succeeded", () => {
  assert.equal(shouldOfflineVaultPrescan({ ok: true, data: { results: [] } }), false);
  assert.equal(shouldPrescanVault(true, true), false);
});

test("hook/MCP: no offline vault pre-scan on identity/auth denial", () => {
  const capabilityDenied = {
    ok: false,
    data: {
      error: { code: -32004, message: "capability_denied: search" },
    },
  };
  assert.equal(shouldOfflineVaultPrescan(capabilityDenied), false);

  const reserved = {
    ok: false,
    data: {
      error: { code: -32004, message: "reserved_agent_id: main" },
    },
  };
  assert.equal(shouldOfflineVaultPrescan(reserved), false);
});

test("hook/MCP: offline vault pre-scan only on true daemon outage", () => {
  assert.equal(
    shouldOfflineVaultPrescan({ ok: false, error: "ECONNREFUSED", data: undefined }),
    true,
  );
  assert.equal(
    shouldOfflineVaultPrescan({ ok: false, error: "ECONNREFUSED" }, false),
    false,
  );
});
