import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

// Exercise the alias table itself: `resolveAgentVaultPath` consults
// MINNI_AGENT_VAULTS first, so that override must be absent here or the
// fallback under test never runs. (agent-ping.test.mjs sets it, which is why
// this lives in its own file.)
delete process.env.MINNI_AGENT_VAULTS;

const { resolveAgentVaultPath } = await import("../dist/agent_ping.js");

test("resolveAgentVaultPath preserves hyphens in canonical agent ids", () => {
  // The alias fallback strips non-alphanumerics, so an unlisted hyphenated id
  // resolves to a vault that does not exist -- `claude-science` silently became
  // `claudescience-vault`, and pings/handoffs would materialize under a
  // directory the real agent never reads. This is the TypeScript twin of
  // `default_agent_vault` (src/minni/minnid_runtime/handoff.py); the two alias
  // tables must agree. Python side: test_default_agent_vault_matches_agent_vault_dirs.
  const expected = {
    "claude-code": "claudecode-vault",
    "claude-science": "claude-science-vault",
    "grok-build": "grok-build-vault",
    codex: "codex-vault",
    kilocode: "kilocode-vault",
  };
  for (const [agentId, vaultDir] of Object.entries(expected)) {
    assert.equal(
      path.basename(resolveAgentVaultPath(agentId)),
      vaultDir,
      `${agentId} must resolve to ${vaultDir}`,
    );
  }
});
