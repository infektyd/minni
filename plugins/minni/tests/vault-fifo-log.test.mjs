// A FIFO in place of <vault>/log.md must never block ensureVault, recordAudit,
// sessionReceipt, listSessions, or the Stop hook (backlog row i).
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import { mkdir, mkdtemp, open, rm, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { ensureVault, listSessions, recordAudit, sessionReceipt } from "../dist/vault.js";

async function unblockFifo(fifoPath) {
  try {
    const fh = await open(
      fifoPath,
      fs.constants.O_RDWR | (fs.constants.O_NONBLOCK ?? 0),
    );
    await fh.close();
  } catch {
    // Best-effort unblock for pre-fix blocking opens during teardown.
  }
}

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

test(
  "ensureVault and recordAudit refuse a FIFO log.md within 2s instead of blocking",
  { timeout: 10_000 },
  async () => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-fifo-log-write-"));
    const vault = path.join(root, "vault");
    const logPath = path.join(vault, "log.md");
    try {
      await ensureVault(vault);
      await unlink(logPath);
      execFileSync("mkfifo", [logPath]);

      await assert.rejects(
        () => withTimeout(ensureVault(vault, { agentId: "claude-code" }), 1_500, "ensureVault"),
        /non-regular file/,
      );
      await assert.rejects(
        () =>
          withTimeout(
            recordAudit(vault, { tool: "hook_test_stop", summary: "stop s1" }),
            1_500,
            "recordAudit",
          ),
        /non-regular file/,
      );
    } finally {
      await unblockFifo(logPath);
      await rm(root, { recursive: true, force: true });
    }
  },
);

test(
  "sessionReceipt and listSessions return empty results within 2s when log.md is a FIFO",
  { timeout: 10_000 },
  async () => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-fifo-log-read-"));
    const vault = path.join(root, "vault");
    const logPath = path.join(vault, "log.md");
    try {
      await ensureVault(vault);
      await unlink(logPath);
      execFileSync("mkfifo", [logPath]);

      const receipt = await withTimeout(sessionReceipt(vault, "s1"), 1_500, "sessionReceipt");
      assert.equal(receipt.recalls_strong, 0);
      assert.equal(receipt.recalls_weak, 0);
      assert.equal(receipt.learns, 0);

      const sessions = await withTimeout(listSessions(vault), 1_500, "listSessions");
      assert.deepEqual(sessions, []);
    } finally {
      await unblockFifo(logPath);
      await rm(root, { recursive: true, force: true });
    }
  },
);

test(
  "Stop hook process exits 0 with continue:true within 5s when log.md is a FIFO",
  { timeout: 10_000 },
  async () => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-fifo-log-stop-"));
    const home = path.join(root, "home");
    const vault = path.join(root, "vault");
    const logPath = path.join(vault, "log.md");
    await mkdir(home, { recursive: true });
    await ensureVault(vault, { agentId: "claude-code" });
    await unlink(logPath);
    execFileSync("mkfifo", [logPath]);

    try {
      const hookPath = new URL("../dist/hook.js", import.meta.url).pathname;
      const result = await new Promise((resolve) => {
        const child = spawn(process.execPath, [hookPath, "Stop"], {
          env: {
            ...process.env,
            MINNI_HOME: home,
            MINNI_SOCKET_PATH: path.join(home, "missing.sock"),
            MINNI_VAULT_PATH: vault,
            MINNI_CLAUDECODE_VAULT_PATH: vault,
          },
          stdio: ["pipe", "pipe", "pipe"],
        });
        let stdout = "";
        let stderr = "";
        child.stdout.setEncoding("utf8");
        child.stderr.setEncoding("utf8");
        child.stdout.on("data", (chunk) => (stdout += chunk));
        child.stderr.on("data", (chunk) => (stderr += chunk));
        const killer = setTimeout(() => {
          child.kill("SIGKILL");
        }, 5_000);
        child.on("close", (code, signal) => {
          clearTimeout(killer);
          resolve({ code, signal, stdout, stderr });
        });
        child.stdin.end(JSON.stringify({ session_id: "fifo-stop-s1" }));
      });

      assert.equal(result.signal, null, `Stop hook was killed (stderr: ${result.stderr})`);
      assert.equal(result.code, 0, `Stop hook exited non-zero (stderr: ${result.stderr})`);
      assert.equal(JSON.parse(result.stdout.trim()).continue, true);
    } finally {
      await unblockFifo(logPath);
      await rm(root, { recursive: true, force: true });
    }
  },
);
