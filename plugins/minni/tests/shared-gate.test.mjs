import assert from "node:assert/strict";
import test from "node:test";

import { gateSharedOperation, isSharedGateUnavailable } from "../dist/sovereign.js";

test("gateSharedOperation routes shared plugin work through minnid gate", async () => {
  const calls = [];
  const result = await gateSharedOperation(
    {
      operation: "plan.update",
      agentId: "codex",
      workspaceId: "workspace-minni",
      details: { slice: "route-shared-through-gate" },
    },
    async (socketPath, method, params) => {
      calls.push({ socketPath, method, params });
      return { ok: true, data: { status: "ok", principal: params.agent_id } };
    },
  );

  assert.equal(result.ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "gate.shared");
  assert.equal(calls[0].params.operation, "plan.update");
  assert.equal(calls[0].params.agent_id, "codex");
  assert.equal(calls[0].params.workspace_id, "workspace-minni");
  assert.deepEqual(calls[0].params.details, { slice: "route-shared-through-gate" });
});

test("isSharedGateUnavailable classifies old/down daemon errors as degraded gate availability", () => {
  assert.equal(isSharedGateUnavailable("Method not found: gate.shared"), true);
  assert.equal(isSharedGateUnavailable("connect ECONNREFUSED /tmp/minnid.sock"), true);
  assert.equal(isSharedGateUnavailable("Socket not found: /tmp/minnid.sock"), true);
  assert.equal(isSharedGateUnavailable("identity unresolved: unknown agent codex"), false);
  assert.equal(isSharedGateUnavailable("gate rejected: principal mismatch"), false);
});

test("isSharedGateUnavailable keeps identity rejections loud even when reason text mentions a transport code", () => {
  // Regression: an IDENTITY/authz rejection whose reason merely *mentions* an
  // error code must NOT be classified as an availability degrade — otherwise
  // fail-loud-on-identity inverts into fail-open. Codes only count when they
  // anchor the start of the message (the shape Node surfaces transport errors).
  assert.equal(isSharedGateUnavailable("identity unresolved: vault path ENOENT under root"), false);
  assert.equal(isSharedGateUnavailable("gate rejected: principal ECONNREFUSED-lookalike"), false);
  assert.equal(isSharedGateUnavailable("recovery_required: ETIMEDOUT mentioned in reason"), false);
  // ...but genuine transport errors at the start still degrade.
  assert.equal(isSharedGateUnavailable("ENOENT: no such file or directory, /tmp/minnid.sock"), true);
  assert.equal(isSharedGateUnavailable("Error: connect ECONNREFUSED /tmp/minnid.sock"), true);
});
