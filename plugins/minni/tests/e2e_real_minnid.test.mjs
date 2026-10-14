import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chmod, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  formatDegradation,
  jsonRpcSocketRequest,
} from "../dist/daemon_transport.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../..");
const VENV_PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");

async function waitForDaemonReady(sockPath, proc, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) {
      throw new Error(`minnid exited prematurely with code ${proc.exitCode}`);
    }
    const res = await jsonRpcSocketRequest(sockPath, "ping", {}, { timeoutMs: 500 });
    if (res.ok && (res.data === "pong" || res.data?.status === "ok")) {
      return res.data;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`Timed out waiting for real minnid on ${sockPath}`);
}

test("Real minnid E2E over Unix socket: ping -> learn -> list_candidates -> resolve_candidate -> fast search (<15ms)", async (t) => {
  // Keep socket path well under the 104-byte macOS AF_UNIX sun_path limit
  const tmpRoot = await mkdtemp("/tmp/mn-e2e-");
  const sockPath = path.join(tmpRoot, "m.sock");
  const principalsDir = path.join(tmpRoot, "principals");
  const claudeVault = path.join(tmpRoot, "claudecode-vault");
  const sharedVault = path.join(tmpRoot, "shared");
  const defaultVault = path.join(tmpRoot, "vault");

  await mkdir(principalsDir, { recursive: true, mode: 0o700 });
  await chmod(principalsDir, 0o700);
  await mkdir(claudeVault, { recursive: true });
  await mkdir(sharedVault, { recursive: true });
  await mkdir(defaultVault, { recursive: true });

  const mainPrincipalPath = path.join(principalsDir, "main.json");
  await writeFile(
    mainPrincipalPath,
    JSON.stringify(
      {
        agent_id: "main",
        workspace_id: "default",
        capabilities: ["*", "resolve_candidate", "govern"],
        allowed_vault_roots: [claudeVault, sharedVault, defaultVault],
      },
      null,
      2,
    ) + "\n",
    { mode: 0o600 },
  );
  await chmod(mainPrincipalPath, 0o600);

  const claudePrincipalPath = path.join(principalsDir, "claude-code.json");
  await writeFile(
    claudePrincipalPath,
    JSON.stringify(
      {
        agent_id: "claude-code",
        workspace_id: "default",
        capabilities: [
          "search",
          "read",
          "learn",
          "feedback",
          "log_event",
          "handoff",
          "export",
        ],
        allowed_vault_roots: [claudeVault, sharedVault, defaultVault],
      },
      null,
      2,
    ) + "\n",
    { mode: 0o600 },
  );
  await chmod(claudePrincipalPath, 0o600);

  let stderrBuf = "";
  const proc = spawn(VENV_PYTHON, ["-m", "minni.minnid", "--socket", sockPath], {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      MINNI_HOME: tmpRoot,
      PYTHONPATH: path.join(REPO_ROOT, "src"),
      MINNI_WARMUP: "off",
      MINNI_VAULT_WATCH: "off",
      MINNI_DECAY: "off",
      MINNI_BACKFILL: "off",
      MINNI_FOOTPRINT_WATCHDOG: "off",
      MINNI_AFM_LOOP: "off",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  proc.stderr.on("data", (chunk) => {
    stderrBuf += chunk.toString("utf8");
  });

  t.after(async () => {
    if (proc.exitCode === null) {
      proc.kill("SIGTERM");
      await Promise.race([
        new Promise((resolve) => proc.once("exit", resolve)),
        new Promise((resolve) => setTimeout(resolve, 3000)),
      ]);
      if (proc.exitCode === null) {
        proc.kill("SIGKILL");
      }
    }
    await rm(tmpRoot, { recursive: true, force: true });
  });

  // 1. Wait for real daemon socket readiness
  const pingData = await waitForDaemonReady(sockPath, proc);
  assert.equal(pingData, "pong", `Expected healthy ping response, stderr: ${stderrBuf}`);

  // 2. Stage a candidate via `learn` as principal `claude-code`
  const uniqueToken = "InquestPhase5RealDaemonE2EToken";
  const learnRes = await jsonRpcSocketRequest(
    sockPath,
    "learn",
    {
      content: `${uniqueToken}: sub-15ms fast search and cross-principal candidate resolution over live Unix socket.`,
      category: "architecture",
      agent_id: "claude-code",
      workspace_id: "default",
    },
    { timeoutMs: 5000 },
  );
  assert.equal(learnRes.ok, true, `learn failed: ${JSON.stringify(learnRes)}`);
  assert.equal(learnRes.data.status, "proposed");
  const candidateId = learnRes.data.candidate_id;
  assert.equal(typeof candidateId, "number");

  // 3. List proposed candidates as operator `main` (omitting agent_id)
  const listRes = await jsonRpcSocketRequest(
    sockPath,
    "list_candidates",
    { status: "proposed" },
    { timeoutMs: 5000 },
  );
  assert.equal(listRes.ok, true, `list_candidates failed: ${JSON.stringify(listRes)}`);
  assert.ok(Array.isArray(listRes.data.candidates));
  const staged = listRes.data.candidates.find((c) => c.candidate_id === candidateId);
  assert.ok(staged, `Expected candidate ${candidateId} in operator list_candidates`);
  assert.equal(staged.principal, "claude-code");
  assert.equal(staged.category, "architecture");

  // 4. Resolve (accept) the candidate as operator `main`
  const resolveRes = await jsonRpcSocketRequest(
    sockPath,
    "resolve_candidate",
    {
      candidate_id: candidateId,
      decision: "accept",
    },
    { timeoutMs: 5000 },
  );
  assert.equal(resolveRes.ok, true, `resolve_candidate failed: ${JSON.stringify(resolveRes)}`);
  assert.equal(resolveRes.data.status, "resolved");
  assert.equal(resolveRes.data.new_status, "accepted");
  assert.equal(typeof resolveRes.data.learning_id, "number");

  // 5. Warm + timed `search` in `mode: "fast"` as `claude-code`
  await jsonRpcSocketRequest(
    sockPath,
    "search",
    {
      query: `${uniqueToken} sub-15ms fast search`,
      mode: "fast",
      agent_id: "claude-code",
      workspace_id: "default",
    },
    { timeoutMs: 5000 },
  );

  const t0 = performance.now();
  const searchRes = await jsonRpcSocketRequest(
    sockPath,
    "search",
    {
      query: `${uniqueToken} sub-15ms fast search`,
      mode: "fast",
      agent_id: "claude-code",
      workspace_id: "default",
    },
    { timeoutMs: 5000 },
  );
  const elapsedMs = performance.now() - t0;
  assert.equal(searchRes.ok, true, `search failed: ${JSON.stringify(searchRes)}`);
  assert.equal(searchRes.data.degraded, false);
  assert.ok(
    elapsedMs < 25,
    `Expected warm fast search over Unix socket < 25ms (engine < 15ms), got ${elapsedMs.toFixed(2)}ms`,
  );
  assert.equal(
    formatDegradation(searchRes.data),
    undefined,
    "Fast search against real daemon must not emit false degradation banners",
  );

  // Verify the accepted note is recalled in `learnings` with preserved provenance + category,
  // and also indexed in durable `results`
  assert.ok(Array.isArray(searchRes.data.learnings) && searchRes.data.learnings.length >= 1);
  const matchingLearning = searchRes.data.learnings.find((l) =>
    String(l.content ?? "").includes(uniqueToken),
  );
  assert.ok(matchingLearning, "Expected accepted candidate in learnings recall");
  assert.equal(matchingLearning.agent_id, "claude-code");
  assert.equal(matchingLearning.category, "architecture");

  assert.ok(Array.isArray(searchRes.data.results) && searchRes.data.results.length >= 1);
  const matchingDoc = searchRes.data.results.find((r) =>
    String(r.text ?? "").includes(uniqueToken),
  );
  assert.ok(matchingDoc, "Expected accepted candidate in durable FTS/Trie document results");
});
