import assert from "node:assert/strict";
import test from "node:test";

const CONFIG_ENV_KEYS = [
  "MINNI_VAULT_PATH",
  "MINNI_CODEX_VAULT_PATH",
  "MINNI_SOCKET_PATH",
  "MINNI_AGENT_ID",
  "MINNI_CODEX_AGENT_ID",
  "MINNI_WORKSPACE_ID",
  "MINNI_CODEX_WORKSPACE_ID",
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

test("generic MINNI env overrides Codex-specific defaults for Hermes", async () => {
  await withConfigEnv(
    {
      MINNI_VAULT_PATH: "/tmp/hermes-vault",
      MINNI_CODEX_VAULT_PATH: "/tmp/codex-vault",
      MINNI_SOCKET_PATH: "/tmp/hermes-sovereign.sock",
      MINNI_AGENT_ID: "hermes",
      MINNI_CODEX_AGENT_ID: "codex",
      MINNI_WORKSPACE_ID: "/tmp/hermes-workspace",
      MINNI_CODEX_WORKSPACE_ID: "/tmp/codex-workspace",
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
      MINNI_CODEX_VAULT_PATH: "/tmp/codex-vault",
      MINNI_CODEX_AGENT_ID: "codex-agent",
      MINNI_CODEX_WORKSPACE_ID: "/tmp/codex-workspace",
    },
    (config) => {
      assert.equal(config.DEFAULT_VAULT_PATH, "/tmp/codex-vault");
      assert.equal(config.DEFAULT_AGENT_ID, "codex-agent");
      assert.equal(config.DEFAULT_WORKSPACE_ID, "/tmp/codex-workspace");
    },
  );
});
