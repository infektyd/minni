import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

// Exercise the alias table itself. `resolveAgentVaultPath` consults
// MINNI_AGENT_VAULTS and then MINNI_<AGENT>_VAULT_PATH before falling through
// to the alias table, so every one of those overrides must be absent or the
// fallback under test never runs -- on an operator shell they are often set.
// (agent-ping.test.mjs sets them too, which is why this lives in its own file.)
delete process.env.MINNI_AGENT_VAULTS;
for (const key of Object.keys(process.env)) {
  if (/^MINNI_.*_VAULT_PATH$/.test(key)) delete process.env[key];
}

const { resolveAgentVaultPath } = await import("../dist/agent_ping.js");

const repoRoot = path.resolve(fileURLToPath(new URL("../../..", import.meta.url)));

/**
 * Parse `AGENT_VAULT_DIRS` out of author_principals.py.
 *
 * Read from source rather than restated here: a hardcoded list would be a third
 * copy of the same table, and this test exists because copies drift.
 */
async function agentVaultDirs() {
  const source = await readFile(
    path.join(repoRoot, "src", "minni", "tools", "author_principals.py"),
    "utf8",
  );
  const body = /AGENT_VAULT_DIRS[^=]*=\s*\{(.*?)\n\}/s.exec(source);
  assert.ok(body, "AGENT_VAULT_DIRS literal not found in author_principals.py");
  return Object.fromEntries(
    [...body[1].matchAll(/"([^"]+)"\s*:\s*"([^"]+)"/g)].map((m) => [m[1], m[2]]),
  );
}

test("resolveAgentVaultPath resolves every authored agent to its real vault dir", async () => {
  // The alias fallback strips non-alphanumerics, so a hyphenated id missing from
  // the table resolves to a vault that does not exist -- `claude-science`
  // silently became `claudescience-vault`, and pings/handoffs would materialize
  // under a directory the real agent never reads. This is the TypeScript twin of
  // `default_agent_vault` (src/minni/minnid_runtime/handoff.py), which is gated
  // against the same table by test_default_agent_vault_matches_agent_vault_dirs.
  // Driving both sides off AGENT_VAULT_DIRS is what keeps the pair from drifting.
  const expected = await agentVaultDirs();
  assert.ok(Object.keys(expected).length > 0, "parsed AGENT_VAULT_DIRS is empty");

  const mismatched = {};
  for (const [agentId, vaultDir] of Object.entries(expected)) {
    const resolved = path.basename(resolveAgentVaultPath(agentId));
    if (resolved !== vaultDir) mismatched[agentId] = { expected: vaultDir, resolved };
  }

  assert.deepEqual(
    mismatched,
    {},
    "TS agent->vault aliases disagree with AGENT_VAULT_DIRS; pings and handoffs " +
      "for these agents would land in a directory they never read",
  );
});
