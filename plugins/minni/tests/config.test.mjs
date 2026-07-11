import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
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
      assert.equal(config.DEFAULT_WORKSPACE_ID, "workspace-hermes-workspace");
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
      assert.equal(config.DEFAULT_WORKSPACE_ID, "workspace-codex-workspace");
    },
  );
});

test("no env falls back to unknown deny identity, not codex vault", async () => {
  await withConfigEnv({}, (config) => {
    assert.equal(config.DEFAULT_AGENT_ID, "unknown-agent");
    assert.equal(
      config.DEFAULT_VAULT_PATH,
      path.join(os.homedir(), ".minni", "unknown-vault"),
    );
    assert.equal(config.CODEX_AGENT_ID, "codex");
    assert.equal(
      config.CODEX_VAULT_PATH,
      path.join(os.homedir(), ".minni", "codex-vault"),
    );
  });
});

test("Codex hook identity is independent from generic MCP identity", async () => {
  await withConfigEnv(
    {
      MINNI_AGENT_ID: "unknown-agent",
      MINNI_VAULT_PATH: "/tmp/unknown-vault",
      MINNI_CODEX_AGENT_ID: "codex",
      MINNI_CODEX_VAULT_PATH: "/tmp/codex-vault",
      MINNI_CODEX_WORKSPACE_ID: "/tmp/codex-workspace",
    },
    (config) => {
      assert.equal(config.DEFAULT_AGENT_ID, "unknown-agent");
      assert.equal(config.DEFAULT_VAULT_PATH, "/tmp/unknown-vault");
      assert.equal(config.CODEX_AGENT_ID, "codex");
      assert.equal(config.CODEX_VAULT_PATH, "/tmp/codex-vault");
      assert.equal(config.CODEX_WORKSPACE_ID, "workspace-codex-workspace");
    },
  );
});

test("Codex MCP manifest pins codex env explicitly", () => {
  const manifest = JSON.parse(readFileSync(new URL("../.mcp.json", import.meta.url), "utf8"));
  const env = manifest.mcpServers?.minni?.env;
  assert.equal(env?.MINNI_AGENT_ID, "codex");
  assert.equal(env?.MINNI_VAULT_PATH, "~/.minni/codex-vault");
  assert.equal(env?.MINNI_SOCKET_PATH, "~/.minni/run/minnid.sock");
});

test("Codex hook manifest uses only the native adapter with realistic timeouts", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../hooks/hooks-codex.json", import.meta.url), "utf8"),
  );
  const expected = {
    SessionStart: 30,
    UserPromptSubmit: 30,
    PreCompact: 20,
    Stop: 20,
  };
  for (const [event, timeout] of Object.entries(expected)) {
    const hook = manifest.hooks?.[event]?.[0]?.hooks?.[0];
    assert.equal(hook?.command, `node \${PLUGIN_ROOT}/dist/codex-hook.js ${event}`);
    assert.equal(hook?.timeout, timeout);
    assert.doesNotMatch(hook?.command ?? "", /\/dist\/hook\.js\s/);
  }
});
