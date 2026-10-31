import assert from "node:assert/strict";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
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

test("generic MINNI env overrides Codex-specific defaults for a peer agent", async () => {
  await withConfigEnv(
    {
      MINNI_VAULT_PATH: "/tmp/peer-vault",
      MINNI_CODEX_VAULT_PATH: "/tmp/codex-vault",
      MINNI_SOCKET_PATH: "/tmp/peer-sovereign.sock",
      MINNI_AGENT_ID: "peer",
      MINNI_CODEX_AGENT_ID: "codex",
      MINNI_WORKSPACE_ID: "/tmp/peer-workspace",
      MINNI_CODEX_WORKSPACE_ID: "/tmp/codex-workspace",
    },
    (config) => {
      assert.equal(config.DEFAULT_VAULT_PATH, "/tmp/peer-vault");
      assert.equal(config.SOCKET_PATH, "/tmp/peer-sovereign.sock");
      assert.equal(config.DEFAULT_AGENT_ID, "peer");
      assert.equal(config.DEFAULT_WORKSPACE_ID, "workspace-peer-workspace");
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
    assert.equal(config.CODEX_WORKSPACE_ID, "workspace-unknown");
  });
});

test("CODEX_WORKSPACE_ID ignores process.cwd when env and payload are unstamped", async () => {
  // Codex hooks often launch with cwd = plugin cache. That must never become
  // the workspace stamp; MCP fails closed to workspace-unknown the same way.
  const previousCwd = process.cwd();
  const fakeCache = path.join(
    os.tmpdir(),
    `minni-codex-cache-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  );
  mkdirSync(fakeCache, { recursive: true });
  try {
    process.chdir(fakeCache);
    await withConfigEnv({}, (config) => {
      assert.equal(config.CODEX_WORKSPACE_ID, "workspace-unknown");
      assert.equal(config.DEFAULT_WORKSPACE_ID, "workspace-unknown");
      assert.notEqual(
        config.CODEX_WORKSPACE_ID,
        `workspace-${path.basename(fakeCache).toLowerCase()}`,
      );
    });
  } finally {
    process.chdir(previousCwd);
    rmSync(fakeCache, { recursive: true, force: true });
  }
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
    PreToolUse: 10,
    PreCompact: 20,
    PostCompact: 20,
    Stop: 20,
  };
  for (const [event, timeout] of Object.entries(expected)) {
    const hook = manifest.hooks?.[event]?.[0]?.hooks?.[0];
    assert.equal(hook?.command, `node \${PLUGIN_ROOT}/dist/codex-hook.js ${event}`);
    assert.equal(hook?.timeout, timeout);
    assert.doesNotMatch(hook?.command ?? "", /\/dist\/hook\.js\s/);
  }
});

test("Class E: tilde expansion in resolveSessionHandoffsDir and active session workspace resolution from hooks", async () => {
  const { resolveSessionHandoffsDir } = await import("../dist/jev-harvest.js");
  const prevDir = process.env.MINNI_SESSION_HANDOFFS_DIR;
  try {
    process.env.MINNI_SESSION_HANDOFFS_DIR = "~/custom-handoffs";
    const resolved = resolveSessionHandoffsDir("/tmp/vault");
    assert.equal(
      resolved,
      path.join(os.homedir(), "custom-handoffs"),
      "MINNI_SESSION_HANDOFFS_DIR leading ~ must expand to os.homedir(), not process.cwd()",
    );
  } finally {
    if (prevDir === undefined) delete process.env.MINNI_SESSION_HANDOFFS_DIR;
    else process.env.MINNI_SESSION_HANDOFFS_DIR = prevDir;
  }

  const config = await import(`../dist/config.js?class-e=${Date.now()}`);
  assert.equal(
    typeof config.resolveSessionWorkspaceId,
    "function",
    "config.js must export resolveSessionWorkspaceId",
  );
  assert.equal(
    typeof config.recordActiveSessionWorkspace,
    "function",
    "config.js must export recordActiveSessionWorkspace",
  );

  const tmpVault = path.join(
    os.tmpdir(),
    `minni-class-e-vault-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  );
  mkdirSync(tmpVault, { recursive: true });
  try {
    const baseNow = Date.now();
    // Two concurrent sessions of the same agent in different workspaces (different host pids)
    config.recordActiveSessionWorkspace(tmpVault, "/tmp/Projects/Praxis", {
      agentId: "claude-code",
      hostPid: 41001,
      nowMs: baseNow,
    });
    config.recordActiveSessionWorkspace(tmpVault, "/tmp/Projects/Minni", {
      agentId: "claude-code",
      hostPid: 41002,
      nowMs: baseNow,
    });

    const wsSessionA = config.resolveSessionWorkspaceId(tmpVault, undefined, {
      agentId: "claude-code",
      hostPid: 41001,
      nowMs: baseNow + 1_000,
    });
    const wsSessionB = config.resolveSessionWorkspaceId(tmpVault, undefined, {
      agentId: "claude-code",
      hostPid: 41002,
      nowMs: baseNow + 1_000,
    });
    assert.equal(
      wsSessionA,
      "workspace-praxis",
      "Session A (hostPid 41001) must resolve its own workspace, not Session B's last write",
    );
    assert.equal(
      wsSessionB,
      "workspace-minni",
      "Session B (hostPid 41002) must resolve its own workspace",
    );

    // Missing entry (different hostPid or different agentId) must return DEFAULT_WORKSPACE_ID, never another session's file.
    // When no hint exists for own key, but a fresh same-agent hint exists under a different pid,
    // log exactly one warning to stderr per process.
    const stderrChunks = [];
    const origStderrWrite = process.stderr.write.bind(process.stderr);
    process.stderr.write = (chunk, ...args) => {
      stderrChunks.push(String(chunk));
      return true;
    };
    let wsMissingSession;
    let wsDifferentAgent;
    try {
      // Different agent with no same-agent hints must NOT warn
      wsDifferentAgent = config.resolveSessionWorkspaceId(tmpVault, undefined, {
        agentId: "codex",
        hostPid: 41001,
        nowMs: baseNow + 1_000,
      });
      assert.equal(
        stderrChunks.length,
        0,
        "No ppid warning when no fresh same-agent hint exists",
      );

      // Same agent with fresh hint under different pid (41001/41002 vs 49999) MUST warn once
      wsMissingSession = config.resolveSessionWorkspaceId(tmpVault, undefined, {
        agentId: "claude-code",
        hostPid: 49999,
        nowMs: baseNow + 1_000,
      });
      assert.equal(
        stderrChunks.length,
        1,
        "Must log one warning to stderr when a fresh same-agent hint exists under a different pid",
      );
      assert.match(stderrChunks[0], /claude-code/);
      assert.match(stderrChunks[0], /49999/);

      // Second miss in the same process must NOT log again
      config.resolveSessionWorkspaceId(tmpVault, undefined, {
        agentId: "claude-code",
        hostPid: 49998,
        nowMs: baseNow + 2_000,
      });
      assert.equal(
        stderrChunks.length,
        1,
        "Must log at most one ppid mismatch warning per process",
      );
    } finally {
      process.stderr.write = origStderrWrite;
    }
    assert.equal(
      wsMissingSession,
      config.DEFAULT_WORKSPACE_ID,
      "Missing session key must fall back to DEFAULT_WORKSPACE_ID, never another session's hint",
    );
    assert.equal(
      wsDifferentAgent,
      config.DEFAULT_WORKSPACE_ID,
      "Different agentId must not inherit claude-code's session workspace hint",
    );

    // Stale entry (older than TTL) must be ignored and return DEFAULT_WORKSPACE_ID
    const ttlMs = config.ACTIVE_SESSION_TTL_MS ?? 30 * 60 * 1000;
    const wsStale = config.resolveSessionWorkspaceId(tmpVault, undefined, {
      agentId: "claude-code",
      hostPid: 41001,
      nowMs: baseNow + ttlMs + 5_000,
    });
    assert.equal(
      wsStale,
      config.DEFAULT_WORKSPACE_ID,
      "Stale active-session entry older than TTL must be ignored",
    );

    // Deprecated MCP roots/list API must not be exported or honored
    assert.equal(
      config.workspaceIdFromMcpRoots,
      undefined,
      "deprecated workspaceIdFromMcpRoots must not be exported",
    );
    const wsIgnoringMcpRoots = config.resolveSessionWorkspaceId(tmpVault, undefined, {
      agentId: "codex",
      hostPid: 55555,
      mcpRootWorkspaceId: "workspace-praxis",
    });
    assert.equal(
      wsIgnoringMcpRoots,
      config.DEFAULT_WORKSPACE_ID,
      "mcpRootWorkspaceId option must not be honored",
    );

    // Explicit workspaceId overrides all hints
    const explicitWs = config.resolveSessionWorkspaceId(tmpVault, "/tmp/Projects/CustomRepo", {
      agentId: "claude-code",
      hostPid: 41001,
      nowMs: baseNow + 1_000,
    });
    assert.equal(explicitWs, "workspace-customrepo", "explicit workspaceId must override active session workspace");

    // Pruning on write: same-agent active-session-*.json files older than 24h are deleted;
    // newer same-agent files and other-agent files are preserved; pruning errors never throw.
    const dayMs = 24 * 60 * 60 * 1000;
    const runtimeDir = path.join(tmpVault, ".runtime");
    config.recordActiveSessionWorkspace(tmpVault, "/tmp/Projects/OldClaude", {
      agentId: "claude-code",
      hostPid: 30001,
      nowMs: baseNow - dayMs - 60_000,
    });
    config.recordActiveSessionWorkspace(tmpVault, "/tmp/Projects/RecentClaude", {
      agentId: "claude-code",
      hostPid: 30002,
      nowMs: baseNow - 2 * 60 * 60 * 1000,
    });
    config.recordActiveSessionWorkspace(tmpVault, "/tmp/Projects/OldCodex", {
      agentId: "codex",
      hostPid: 30003,
      nowMs: baseNow - dayMs - 60_000,
    });
    // Trigger write at baseNow for claude-code
    config.recordActiveSessionWorkspace(tmpVault, "/tmp/Projects/CurrentClaude", {
      agentId: "claude-code",
      hostPid: 30004,
      nowMs: baseNow,
    });

    const oldClaudeFile = path.join(runtimeDir, "active-session-claude-code-30001.json");
    const recentClaudeFile = path.join(runtimeDir, "active-session-claude-code-30002.json");
    const oldCodexFile = path.join(runtimeDir, "active-session-codex-30003.json");
    const currentClaudeFile = path.join(runtimeDir, "active-session-claude-code-30004.json");

    assert.throws(
      () => readFileSync(oldClaudeFile, "utf8"),
      /ENOENT/,
      "same-agent hint file older than 24h must be pruned on write",
    );
    assert.equal(
      JSON.parse(readFileSync(recentClaudeFile, "utf8")).workspace_id,
      "workspace-recentclaude",
      "same-agent hint file younger than 24h must be preserved",
    );
    assert.equal(
      JSON.parse(readFileSync(oldCodexFile, "utf8")).workspace_id,
      "workspace-oldcodex",
      "other-agent hint file must not be pruned by claude-code write",
    );
    assert.equal(
      JSON.parse(readFileSync(currentClaudeFile, "utf8")).workspace_id,
      "workspace-currentclaude",
      "newly written hint file must exist",
    );
  } finally {
    rmSync(tmpVault, { recursive: true, force: true });
  }
});
