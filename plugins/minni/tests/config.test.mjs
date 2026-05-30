import assert from "node:assert/strict";
import test from "node:test";

const CONFIG_ENV_KEYS = [
  "SOVEREIGN_VAULT_PATH",
  "SOVEREIGN_CODEX_VAULT_PATH",
  "SOVEREIGN_SOCKET_PATH",
  "SOVEREIGN_AGENT_ID",
  "SOVEREIGN_CODEX_AGENT_ID",
  "SOVEREIGN_WORKSPACE_ID",
  "SOVEREIGN_CODEX_WORKSPACE_ID",
];

async function withConfigEnv(env, fn) {
  const saved = Object.fromEntries(
    CONFIG_ENV_KEYS.map((key) => [key, process.env[key]]),
  );
  for (const key of CONFIG_ENV_KEYS) delete process.env[key];
  Object.assign(process.env, env);

  try {
    const config = await import(`../dist/config.js?case=${Date.now()}-${Math.random()}`);
    await fn(config);
  } finally {
    for (const key of CONFIG_ENV_KEYS) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
}

test("generic SOVEREIGN env overrides Codex-specific defaults for Hermes", async () => {
  await withConfigEnv(
    {
      SOVEREIGN_VAULT_PATH: "/tmp/hermes-vault",
      SOVEREIGN_CODEX_VAULT_PATH: "/tmp/codex-vault",
      SOVEREIGN_SOCKET_PATH: "/tmp/hermes-sovereign.sock",
      SOVEREIGN_AGENT_ID: "hermes",
      SOVEREIGN_CODEX_AGENT_ID: "codex",
      SOVEREIGN_WORKSPACE_ID: "/tmp/hermes-workspace",
      SOVEREIGN_CODEX_WORKSPACE_ID: "/tmp/codex-workspace",
    },
    (config) => {
      assert.equal(config.DEFAULT_VAULT_PATH, "/tmp/hermes-vault");
      assert.equal(config.SOCKET_PATH, "/tmp/hermes-sovereign.sock");
      assert.equal(config.DEFAULT_AGENT_ID, "hermes");
      assert.equal(config.DEFAULT_WORKSPACE_ID, "/tmp/hermes-workspace");
    },
  );
});

test("Codex-specific env remains a compatibility fallback", async () => {
  await withConfigEnv(
    {
      SOVEREIGN_CODEX_VAULT_PATH: "/tmp/codex-vault",
      SOVEREIGN_CODEX_AGENT_ID: "codex-agent",
      SOVEREIGN_CODEX_WORKSPACE_ID: "/tmp/codex-workspace",
    },
    (config) => {
      assert.equal(config.DEFAULT_VAULT_PATH, "/tmp/codex-vault");
      assert.equal(config.DEFAULT_AGENT_ID, "codex-agent");
      assert.equal(config.DEFAULT_WORKSPACE_ID, "/tmp/codex-workspace");
    },
  );
});
