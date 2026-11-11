// Slice S1 (H6c, R1): Session-scoped recall guard, consult disarm, and lifecycle state.
// Spawns two real MCP server child processes under distinct live host PIDs against a single vault.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import net from "node:net";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os, { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  DEFAULT_VAULT_PATH,
  readProcessStartMarker,
  recordActiveSessionWorkspace,
  resolveMcpSessionIdentity,
} from "../dist/config.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire, cursorWire } from "../dist/hook-platform.js";
import {
  RECALL_STATE_RELPATH,
  readLifecycleState,
  readRecallState,
  sessionStateKey,
  writeRecallState,
} from "../dist/recall-state.js";
import { ensureVault } from "../dist/vault.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;

async function startFakeDaemon(socketPath) {
  const daemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        const result =
          request.method === "search"
            ? { results: [{ id: 1, title: "Hit", snippet: "ok", score: 0.9 }] }
            : { ok: true };
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      }
    });
  });
  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  return daemon;
}

async function spawnHostMcpSession(t, { home, socketPath, vaultPath, agentId }) {
  // Spawn a real host wrapper process so each MCP server child has its own distinct live process.ppid.
  const hostScript = [
    'const { spawn } = require("node:child_process");',
    "const child = spawn(process.execPath, [process.argv[1]], { stdio: 'inherit' });",
    "process.on('SIGTERM', () => child.kill('SIGKILL'));",
    "child.on('exit', (code) => process.exit(code ?? 0));",
  ].join(" ");
  const hostProc = spawn(process.execPath, ["-e", hostScript, SERVER_PATH], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: vaultPath,
      MINNI_CLAUDECODE_VAULT_PATH: vaultPath,
      ...(agentId ? { MINNI_AGENT_ID: agentId } : {}),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => hostProc.kill("SIGKILL"));

  const responses = new Map();
  const waiters = new Map();
  let buffered = "";
  hostProc.stdout.setEncoding("utf8");
  hostProc.stdout.on("data", (chunk) => {
    buffered += chunk;
    let nl;
    while ((nl = buffered.indexOf("\n")) >= 0) {
      const line = buffered.slice(0, nl).trim();
      buffered = buffered.slice(nl + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.id !== undefined) {
          responses.set(msg.id, msg);
          waiters.get(msg.id)?.(msg);
        }
      } catch {
        // ignore protocol noise
      }
    }
  });

  let nextId = 1;
  const send = (msg) => hostProc.stdin.write(`${JSON.stringify(msg)}\n`);
  const request = (method, params) => {
    const id = nextId++;
    send({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => {
      if (responses.has(id)) return resolve(responses.get(id));
      const timer = setTimeout(() => {
        waiters.delete(id);
        reject(new Error(`timeout waiting for MCP response id=${id} (${method})`));
      }, 15_000);
      waiters.set(id, (msg) => {
        clearTimeout(timer);
        waiters.delete(id);
        resolve(msg);
      });
    });
  };

  await request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "multi-session-recall-guard-test", version: "1.0.0" },
  });
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  return {
    hostPid: hostProc.pid,
    callTool: async (name, args = {}) => {
      const resp = await request("tools/call", { name, arguments: args });
      return resp.result;
    },
  };
}

test("Slice S1 (H6c): two concurrent MCP sessions isolate recall guard arming, consult disarm, and lifecycle state", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-msp-s1-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "claudecode-vault");
  const home = path.join(root, "home");
  await mkdir(vault, { recursive: true });
  await mkdir(home, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const daemon = await startFakeDaemon(socketPath);
  t.after(() => daemon.close());

  const serverA = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });
  const serverB = await spawnHostMcpSession(t, { home, socketPath, vaultPath: vault });
  assert.notEqual(serverA.hostPid, serverB.hostPid);

  const sessionA = "session-alpha-111";
  const sessionB = "session-bravo-222";
  const keyA = sessionStateKey(sessionA);
  const keyB = sessionStateKey(sessionB);
  assert.ok(keyA && keyB && keyA !== keyB);

  // Record active-session hints for both live host PIDs
  recordActiveSessionWorkspace(vault, "/tmp/project-alpha", {
    agentId: "claude-code",
    hostPid: serverA.hostPid,
    sessionId: sessionA,
  });
  recordActiveSessionWorkspace(vault, "/tmp/project-bravo", {
    agentId: "claude-code",
    hostPid: serverB.hostPid,
    sessionId: sessionB,
  });

  // Verify R1 ancestor PID walk resolves even when an intermediate wrapper PID sits between MCP and host
  const ancestorResolved = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: 999991,
    checkHostPidAlive: false,
    parentPidResolver: (pid) => (pid === 999991 ? serverA.hostPid : undefined),
  });
  assert.equal(ancestorResolved.sessionId, sessionA);
  assert.equal(ancestorResolved.sessionKey, keyA);
  assert.equal(ancestorResolved.matchedHintPid, serverA.hostPid);

  // Arm both sessions with unconsumed recall states
  await writeRecallState(
    vault,
    {
      task_signature: "sig-alpha",
      intent: "recall",
      top_hits: [{ title: "Alpha Spec", wikilink: "[[wiki/alpha-spec]]", score: 0.92 }],
      top_score: 0.92,
      session_id: sessionA,
    },
    { sessionId: sessionA, guardV2: true },
  );
  await writeRecallState(
    vault,
    {
      task_signature: "sig-bravo",
      intent: "recall",
      top_hits: [{ title: "Bravo Spec", wikilink: "[[wiki/bravo-spec]]", score: 0.95 }],
      top_score: 0.95,
      session_id: sessionB,
    },
    { sessionId: sessionB, guardV2: true },
  );

  // Server A calls minni_recall -> disarms Session A ONLY, leaving Session B armed
  await serverA.callTool("minni_recall", { query: "alpha architecture" });

  const stateAAfterConsult = await readRecallState(vault, { sessionId: sessionA, guardV2: true });
  const stateBAfterConsult = await readRecallState(vault, { sessionId: sessionB, guardV2: true });
  assert.equal(stateAAfterConsult?.consumed, true, "Session A must be disarmed after serverA minni_recall");
  assert.equal(stateBAfterConsult?.consumed, false, "Session B must remain armed after serverA minni_recall");

  // PreToolUse in strict mode: Session A allows cold Grep; Session B denies with its own pointer
  const handlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "workspace-msp",
    contextWindow: 200_000,
    auditPrefix: "hook_msp_s1",
    hooksEnabled: true,
    recallGuardMode: "strict",
    lifecycleEnabled: true,
    wire: claudeCodeWire,
  });

  const preToolA = await handlers.handlePreToolUse({
    session_id: sessionA,
    tool_name: "Grep",
    tool_input: { pattern: "alpha" },
  });
  assert.equal(
    preToolA?.hookSpecificOutput,
    undefined,
    "Session A cold Grep must pass cleanly without guard surfacing because serverA already consulted minni_recall",
  );

  const preToolB = await handlers.handlePreToolUse({
    session_id: sessionB,
    tool_name: "Grep",
    tool_input: { pattern: "bravo" },
  });
  assert.equal(
    typeof preToolB?.hookSpecificOutput?.additionalContext,
    "string",
    "Session B cold Grep must still trigger recall guard because Session B never consulted minni_recall",
  );
  assert.match(
    String(preToolB?.hookSpecificOutput?.additionalContext ?? ""),
    /Bravo Spec|bravo-spec/,
  );

  // Lifecycle state isolation: SessionStart in Session B resets Session B's lifecycle state without wiping Session A's
  await handlers.handleSessionStart({ session_id: sessionA, cwd: "/tmp/project-alpha" });
  await handlers.handleUserPromptSubmit({
    session_id: sessionA,
    prompt: "implement the new storage layout parser",
  });
  const lifeABeforeBBoot = await readLifecycleState(vault, { sessionId: sessionA });
  assert.ok(
    Array.isArray(lifeABeforeBBoot?.emphasized) && lifeABeforeBBoot.emphasized.length > 0,
    "Session A should record emphasized lifecycle surface",
  );

  await handlers.handleSessionStart({ session_id: sessionB, cwd: "/tmp/project-bravo" });
  const lifeAAfterBBoot = await readLifecycleState(vault, { sessionId: sessionA });
  const lifeBAfterBBoot = await readLifecycleState(vault, { sessionId: sessionB });
  assert.deepEqual(
    lifeAAfterBBoot?.emphasized,
    lifeABeforeBBoot?.emphasized,
    "Session B boot must not wipe Session A lifecycle emphasis",
  );
  assert.deepEqual(lifeBAfterBBoot?.emphasized, []);

  // R1 (§9): Session C started outside any git repository (workspace-unknown / disallowed hint path)
  // still writes its session identity record and never resolves to Session A's session_key.
  const sessionC = "session-charlie-non-repo";
  const keyC = sessionStateKey(sessionC);
  await handlers.handleSessionStart({
    session_id: sessionC,
    cwd: "workspace-unknown",
    host_pid: 999993,
  });
  const resolvedC = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: 999993,
    checkHostPidAlive: false,
  });
  assert.equal(resolvedC.sessionId, sessionC);
  assert.equal(resolvedC.sessionKey, keyC);
  assert.notEqual(resolvedC.sessionKey, keyA, "Session C outside a repo must never borrow Session A's key");

  // R2 (§9): Foreign host environment variable (CLAUDE_CODE_SESSION_ID) is ignored when agent is codex
  const prevClaudeSession = process.env.CLAUDE_CODE_SESSION_ID;
  process.env.CLAUDE_CODE_SESSION_ID = "foreign-claude-session-999";
  try {
    const codexResolved = resolveMcpSessionIdentity(vault, {
      agentId: "codex",
      hostPid: 999994,
      checkHostPidAlive: false,
    });
    assert.notEqual(
      codexResolved.sessionId,
      "foreign-claude-session-999",
      "R2: codex agent must ignore inherited CLAUDE_CODE_SESSION_ID",
    );
  } finally {
    if (prevClaudeSession === undefined) delete process.env.CLAUDE_CODE_SESSION_ID;
    else process.env.CLAUDE_CODE_SESSION_ID = prevClaudeSession;
  }
});

test("setup-env.mjs hermeticity & unkeyed recall-state mirror isolation", async (t) => {
  const realTmp = realpathSync(tmpdir());
  const homeReal = path.join(os.homedir(), ".minni");
  assert.ok(process.env.MINNI_HOME, "setup-env.mjs must populate process.env.MINNI_HOME");
  const envMinniHomeReal = realpathSync(process.env.MINNI_HOME);
  assert.ok(
    envMinniHomeReal.startsWith(realTmp + path.sep) || envMinniHomeReal === realTmp,
    `MINNI_HOME (${envMinniHomeReal}) must reside inside os.tmpdir() (${realTmp})`,
  );
  assert.equal(
    envMinniHomeReal.startsWith(homeReal),
    false,
    "MINNI_HOME must never point into ~/.minni",
  );
  assert.equal(
    path.resolve(DEFAULT_VAULT_PATH).startsWith(path.resolve(homeReal)),
    false,
    `DEFAULT_VAULT_PATH (${DEFAULT_VAULT_PATH}) must not fall back to ~/.minni`,
  );
  assert.ok(
    path.resolve(DEFAULT_VAULT_PATH).startsWith(path.resolve(tmpdir())) ||
      path.resolve(DEFAULT_VAULT_PATH).startsWith(realTmp),
    `DEFAULT_VAULT_PATH (${DEFAULT_VAULT_PATH}) must stay inside os.tmpdir()`,
  );

  // Unkeyed recall-state.json mirror (!mirror.session_id) must NEVER bleed into a keyed session lookup.
  const root = await mkdtemp(path.join(tmpdir(), "minni-unkeyed-mirror-"));
  t.after(async () => {
    await rm(root, { recursive: true, force: true });
  });
  const vault = path.join(root, "vault");
  const mirrorPath = path.join(vault, RECALL_STATE_RELPATH);
  await mkdir(path.dirname(mirrorPath), { recursive: true });
  await writeFile(
    mirrorPath,
    JSON.stringify({
      task_signature: "unkeyed-legacy-sig",
      intent: "recall",
      top_hits: [{ title: "Unkeyed Note", wikilink: "[[unkeyed]]", score: 0.92 }],
      top_score: 0.92,
      consumed: false,
      ts: new Date().toISOString(),
    }),
    "utf8",
  );
  const keyedRead = await readRecallState(vault, {
    sessionId: "keyed-session-isolated",
    guardV2: true,
  });
  assert.equal(
    keyedRead,
    null,
    "Unkeyed recall-state.json mirror must not bleed into a keyed session",
  );
});

test(">30-minute live hostPid retention, process_start_marker PID-reuse guard, PreToolUse refresh, and R2 env priority", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-long-turn-"));
  t.after(async () => {
    await rm(root, { recursive: true, force: true });
  });
  const vault = path.join(root, "claudecode-vault");
  await mkdir(path.join(vault, ".runtime"), { recursive: true });

  const sessionId = "session-long-turn-45m";
  const expectedKey = sessionStateKey(sessionId);
  const liveMarker = readProcessStartMarker(process.pid);
  assert.ok(liveMarker && liveMarker.length > 0, "readProcessStartMarker(process.pid) must return lstart");

  recordActiveSessionWorkspace(vault, "/tmp/project-alpha", {
    agentId: "claude-code",
    hostPid: process.pid,
    sessionId,
  });

  const hintPath = path.join(vault, ".runtime", `active-session-claude-code-${process.pid}.json`);
  const recorded = JSON.parse(await readFile(hintPath, "utf8"));
  assert.equal(recorded.process_start_marker, liveMarker);
  assert.equal(recorded.workspace_id, "workspace-project-alpha");

  // 1. Backdate updated_at to 45 minutes ago (past 30-min ACTIVE_SESSION_TTL_MS) on live process.pid
  const fortyFiveMinAgo = new Date(Date.now() - 45 * 60 * 1000).toISOString();
  recorded.updated_at = fortyFiveMinAgo;
  await writeFile(hintPath, JSON.stringify(recorded, null, 2), "utf8");

  const resolvedLongTurn = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: process.pid,
  });
  assert.equal(
    resolvedLongTurn.source,
    "pid_exact",
    "Live hostPid with matching process_start_marker must retain session identity past 30 minutes",
  );
  assert.equal(resolvedLongTurn.sessionId, sessionId);
  assert.equal(resolvedLongTurn.sessionKey, expectedKey);

  // 2. Tamper process_start_marker to simulate OS PID reuse -> must reject stale record!
  recorded.process_start_marker = "Mon Jan  1 00:00:00 1999";
  await writeFile(hintPath, JSON.stringify(recorded, null, 2), "utf8");
  const resolvedRecycled = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: process.pid,
  });
  assert.equal(
    resolvedRecycled.source,
    "ppid_derived",
    "Mismatched process_start_marker must reject recycled-PID active-session hint",
  );
  assert.notEqual(resolvedRecycled.sessionKey, expectedKey);

  // 3. Restore valid marker + 45m-old updated_at, run PreToolUse with cwd=/tmp -> refreshes updated_at AND preserves workspace_id!
  recorded.process_start_marker = liveMarker;
  recorded.updated_at = fortyFiveMinAgo;
  await writeFile(hintPath, JSON.stringify(recorded, null, 2), "utf8");

  const handlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "workspace-unknown",
    contextWindow: 200_000,
    auditPrefix: "hook_long_turn",
    hooksEnabled: true,
    wire: claudeCodeWire,
  });
  await handlers.handlePreToolUse({
    session_id: sessionId,
    cwd: tmpdir(),
    host_pid: process.pid,
    tool_name: "Bash",
    tool_input: { command: "ls -la" },
  });

  const afterPreTool = JSON.parse(await readFile(hintPath, "utf8"));
  assert.ok(
    Date.now() - Date.parse(afterPreTool.updated_at) < 15_000,
    `PreToolUse must refresh active-session updated_at (got ${afterPreTool.updated_at})`,
  );
  assert.equal(
    afterPreTool.workspace_id,
    "workspace-project-alpha",
    "PreToolUse running in /tmp must preserve existing workspace_id on the same live process",
  );
  assert.equal(afterPreTool.process_start_marker, liveMarker);

  // 4. R2: DEVIN_SESSION_ID support, agent-specific env precedence over MINNI_SESSION_ID, and once-per-process MINNI_DEBUG foreign env log
  const savedDevin = process.env.DEVIN_SESSION_ID;
  const savedClaude = process.env.CLAUDE_CODE_SESSION_ID;
  const savedGeneric = process.env.MINNI_SESSION_ID;
  try {
    process.env.DEVIN_SESSION_ID = "devin-session-42";
    const devinRes = resolveMcpSessionIdentity(vault, {
      agentId: "devin",
      hostPid: 999991,
      checkHostPidAlive: false,
    });
    assert.equal(devinRes.source, "explicit_or_env");
    assert.equal(devinRes.sessionId, "devin-session-42");

    process.env.CLAUDE_CODE_SESSION_ID = "claude-specific-win";
    process.env.MINNI_SESSION_ID = "generic-minni-fallback";
    const claudeRes = resolveMcpSessionIdentity(vault, {
      agentId: "claude-code",
      hostPid: 999992,
      checkHostPidAlive: false,
    });
    assert.equal(claudeRes.sessionId, "claude-specific-win");
  } finally {
    if (savedDevin === undefined) delete process.env.DEVIN_SESSION_ID;
    else process.env.DEVIN_SESSION_ID = savedDevin;
    if (savedClaude === undefined) delete process.env.CLAUDE_CODE_SESSION_ID;
    else process.env.CLAUDE_CODE_SESSION_ID = savedClaude;
    if (savedGeneric === undefined) delete process.env.MINNI_SESSION_ID;
    else process.env.MINNI_SESSION_ID = savedGeneric;
  }

  const configUrl = new URL("../dist/config.js", import.meta.url).href;
  const stderrOut = execFileSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      `import { resolveMcpSessionIdentity } from ${JSON.stringify(configUrl)};
       resolveMcpSessionIdentity(${JSON.stringify(vault)}, { agentId: "codex", hostPid: 999995, checkHostPidAlive: false });
       resolveMcpSessionIdentity(${JSON.stringify(vault)}, { agentId: "codex", hostPid: 999995, checkHostPidAlive: false });`,
    ],
    {
      env: {
        ...process.env,
        MINNI_DEBUG: "1",
        CLAUDE_CODE_SESSION_ID: "foreign-claude-debug-test",
        CODEX_THREAD_ID: "",
        MINNI_SESSION_ID: "",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  // Capture stderr via spawnSync check to verify once-per-process warning
  void stderrOut;
  const { spawnSync } = await import("node:child_process");
  const debugRun = spawnSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      `import { resolveMcpSessionIdentity } from ${JSON.stringify(configUrl)};
       resolveMcpSessionIdentity(${JSON.stringify(vault)}, { agentId: "codex", hostPid: 999995, checkHostPidAlive: false });
       resolveMcpSessionIdentity(${JSON.stringify(vault)}, { agentId: "codex", hostPid: 999995, checkHostPidAlive: false });`,
    ],
    {
      env: {
        ...process.env,
        MINNI_DEBUG: "1",
        CLAUDE_CODE_SESSION_ID: "foreign-claude-debug-test",
        CODEX_THREAD_ID: "",
        MINNI_SESSION_ID: "",
      },
      encoding: "utf8",
    },
  );
  assert.equal(debugRun.status, 0);
  const debugMatches = (debugRun.stderr || "").match(/CLAUDE_CODE_SESSION_ID/g) ?? [];
  assert.equal(
    debugMatches.length,
    1,
    `MINNI_DEBUG must log ignored foreign session env var exactly once per process, got stderr: ${debugRun.stderr}`,
  );
});

test("R1.3 + R1.1 genuine outside-repo sessions (including Cursor host_pid-only) and 2-server minni_drill per-session handles", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-r1-drill-"));
  const home = path.join(root, "home");
  const vault = path.join(home, "claudecode-vault");
  const cursorVault = path.join(home, "cursor-vault");
  const socketPath = path.join(root, "minnid.sock");
  await ensureVault(vault, { agentId: "claude-code" });
  await ensureVault(cursorVault, { agentId: "cursor" });

  const daemon = await startFakeDaemon(socketPath);
  t.after(async () => {
    daemon.close();
    await rm(root, { recursive: true, force: true });
  });

  // Spawn two real MCP server processes with distinct live host PIDs
  const serverA = await spawnHostMcpSession(t, {
    home,
    socketPath,
    vaultPath: vault,
    agentId: "claude-code",
  });
  const serverB = await spawnHostMcpSession(t, {
    home,
    socketPath,
    vaultPath: vault,
    agentId: "claude-code",
  });

  // Part 1 (R1.3 + R1.1): ONLY Session A is inside a repo; Session B runs outside any repo (cwd = os.tmpdir()),
  // including Cursor Window B with host_pid ONLY and NO conversation_id/session_id!
  const sessionA = "claude-repo-win-a";
  const keyA = sessionStateKey(sessionA);
  recordActiveSessionWorkspace(vault, "/Users/example/project-alpha", {
    agentId: "claude-code",
    hostPid: serverA.hostPid,
    sessionId: sessionA,
  });

  const cursorHandlers = createHookHandlers({
    agentId: "cursor",
    vaultPath: cursorVault,
    defaultWorkspaceId: "workspace-unknown",
    contextWindow: 200_000,
    auditPrefix: "hook_cursor_r1",
    hooksEnabled: true,
    wire: cursorWire,
  });
  // Cursor Window 1 in a repo with conversation_id
  recordActiveSessionWorkspace(cursorVault, "/Users/example/project-alpha", {
    agentId: "cursor",
    hostPid: serverA.hostPid,
    sessionId: "cursor-conv-win-1",
  });
  const cursorKey1 = sessionStateKey("cursor-conv-win-1");

  // Cursor Window 2 opened in os.tmpdir() WITHOUT conversation_id/session_id, only host_pid = serverB.hostPid!
  await cursorHandlers.handleSessionStart({
    cwd: tmpdir(),
    host_pid: serverB.hostPid,
  });
  const cursorHintBPath = path.join(
    cursorVault,
    ".runtime",
    `active-session-cursor-${serverB.hostPid}.json`,
  );
  const cursorHintB = JSON.parse(await readFile(cursorHintBPath, "utf8"));
  assert.equal(
    cursorHintB.workspace_id,
    undefined,
    "Outside-repo Cursor session must omit disallowed workspace_id",
  );
  assert.equal(cursorHintB.host_pid, serverB.hostPid);

  // Direct child of Window 2 AND shared Cursor MCP helper (unrelated PID -> Priority 3) must NEVER borrow Window 1's key!
  const cursorResolvedDirectB = resolveMcpSessionIdentity(cursorVault, {
    agentId: "cursor",
    hostPid: serverB.hostPid,
  });
  assert.notEqual(
    cursorResolvedDirectB.sessionKey,
    cursorKey1,
    "Cursor Window 2 outside repo must never borrow Window 1 session_key",
  );
  const cursorResolvedSharedHelper = resolveMcpSessionIdentity(cursorVault, {
    agentId: "cursor",
    hostPid: 999988,
    checkHostPidAlive: false,
  });
  assert.notEqual(
    cursorResolvedSharedHelper.sessionKey,
    cursorKey1,
    "With 2 live Cursor hints (Window 1 in repo + Window 2 host_pid-only outside repo), Priority 3 must NOT borrow Window 1's session_key",
  );

  // Claude Session B outside repo (cwd = tmpdir()) with session_id
  const claudeHandlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "workspace-unknown",
    contextWindow: 200_000,
    auditPrefix: "hook_claude_r1",
    hooksEnabled: true,
    wire: claudeCodeWire,
  });
  const sessionB = "claude-outside-repo-win-b";
  const keyB = sessionStateKey(sessionB);
  await claudeHandlers.handleSessionStart({
    session_id: sessionB,
    cwd: tmpdir(),
    host_pid: serverB.hostPid,
  });
  const claudeResolvedB = resolveMcpSessionIdentity(vault, {
    agentId: "claude-code",
    hostPid: serverB.hostPid,
  });
  assert.equal(claudeResolvedB.sessionKey, keyB);
  assert.notEqual(claudeResolvedB.sessionKey, keyA);

  // Part 2: Two real dist/server.js processes resolve distinct per-session handles via minni_drill!
  const alphaRel = "wiki/decisions/alpha-drill-target.md";
  const bravoRel = "wiki/decisions/bravo-drill-target.md";
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });
  await writeFile(
    path.join(vault, alphaRel),
    [
      "---",
      "title: Alpha Drill Decision",
      "type: decision",
      "privacy: safe",
      "status: active",
      "---",
      "# Alpha Drill Decision",
      "",
      "Distinct payload exclusively for Session Alpha.",
    ].join("\n"),
    "utf8",
  );
  await writeFile(
    path.join(vault, bravoRel),
    [
      "---",
      "title: Bravo Drill Decision",
      "type: decision",
      "privacy: safe",
      "status: active",
      "---",
      "# Bravo Drill Decision",
      "",
      "Distinct payload exclusively for Session Bravo.",
    ].join("\n"),
    "utf8",
  );

  const stateDir = path.join(vault, ".runtime", "recall-state.d");
  await mkdir(stateDir, { recursive: true });
  await writeFile(
    path.join(stateDir, `${keyA}.handles.json`),
    JSON.stringify({
      v: 1,
      updated_at: new Date().toISOString(),
      handles: {
        "m:aaaa1111": {
          handle: "m:aaaa1111",
          mkey: `vault:${alphaRel}`,
          wikilink: "[[wiki/decisions/alpha-drill-target]]",
          title: "Alpha Drill Decision",
          relativePath: alphaRel,
          ts: Date.now(),
        },
        "m:cccc3333": {
          handle: "m:cccc3333",
          mkey: `vault:${alphaRel}`,
          wikilink: "[[wiki/decisions/alpha-drill-target]]",
          title: "Alpha Drill Decision",
          relativePath: alphaRel,
          ts: Date.now(),
        },
      },
    }),
    "utf8",
  );
  await writeFile(
    path.join(stateDir, `${keyB}.handles.json`),
    JSON.stringify({
      v: 1,
      updated_at: new Date().toISOString(),
      handles: {
        "m:bbbb2222": {
          handle: "m:bbbb2222",
          mkey: `vault:${bravoRel}`,
          wikilink: "[[wiki/decisions/bravo-drill-target]]",
          title: "Bravo Drill Decision",
          relativePath: bravoRel,
          ts: Date.now(),
        },
        "m:cccc3333": {
          handle: "m:cccc3333",
          mkey: `vault:${bravoRel}`,
          wikilink: "[[wiki/decisions/bravo-drill-target]]",
          title: "Bravo Drill Decision",
          relativePath: bravoRel,
          ts: Date.now(),
        },
      },
    }),
    "utf8",
  );

  const parseToolJson = (res) => JSON.parse(res.content[0].text);

  const drillARes = parseToolJson(
    await serverA.callTool("minni_drill", { handle: "m:aaaa1111" }),
  );
  assert.equal(drillARes.ok, true);
  assert.match(drillARes.data.content, /Distinct payload exclusively for Session Alpha/);

  const drillBRes = parseToolJson(
    await serverB.callTool("minni_drill", { handle: "m:bbbb2222" }),
  );
  assert.equal(drillBRes.ok, true);
  assert.match(drillBRes.data.content, /Distinct payload exclusively for Session Bravo/);

  // Even on a colliding handle m:cccc3333, each real MCP server prioritizes its OWN session's .handles.json!
  const collideA = parseToolJson(
    await serverA.callTool("minni_drill", { handle: "m:cccc3333" }),
  );
  const collideB = parseToolJson(
    await serverB.callTool("minni_drill", { handle: "m:cccc3333" }),
  );
  assert.match(collideA.data.content, /Distinct payload exclusively for Session Alpha/);
  assert.match(collideB.data.content, /Distinct payload exclusively for Session Bravo/);
});
