// Leftover consumed=false + daemon timeout on dropped-inject wires.
//
// The strong-turn path already clears leftover false when UPS inject is
// dropped. The timeout path used to KEEP the file (degraded ≠ weak), which
// re-armed UNCONSULTED on Grok against an envelope the model never saw.
// Gate timeout keep the same way as the plant: only if canInject(UPS).
//
// SOCKET_PATH is import-time (config.ts), so the black-hole socket must exist
// in the environment BEFORE hook-handlers is loaded — same pattern as
// hook-budget-shared.test.mjs. No vault note: a wiki substring match would
// take the strong path instead of daemonTimedOut.
import assert from "node:assert/strict";
import net from "node:net";
import { mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const SUITE_ROOT = await mkdtemp(path.join(tmpdir(), "sm-recall-dropped-timeout-"));
const SUITE_SOCKET = path.join(SUITE_ROOT, "d.sock");
process.env.MINNI_HOME = path.join(SUITE_ROOT, "home");
process.env.MINNI_SOCKET_PATH = SUITE_SOCKET;
process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
await mkdir(process.env.MINNI_HOME, { recursive: true });

const { createHookHandlers } = await import("../dist/hook-handlers.js");
const { claudeCodeWire, grokBuildWire } = await import("../dist/hook-platform.js");
const { RECALL_STATE_RELPATH, readRecallState } = await import("../dist/recall-state.js");
const { auditTail } = await import("../dist/vault.js");

async function startBlackHoleDaemon(socketPath) {
  const sockets = [];
  const server = net.createServer((socket) => sockets.push(socket));
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return {
    async close() {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function isDeny(output) {
  return output?.hookSpecificOutput?.permissionDecision === "deny";
}

async function fileExists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

function turnConfig(vaultPath, overrides = {}) {
  return {
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    recallGuardMode: "soft",
    promptHookTimeoutMs: 30_000,
    ...overrides,
  };
}

const LEFTOVER_FALSE = {
  task_signature: "leftover-from-dropped-ups",
  intent: "recall",
  top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
  top_score: 0.91,
  consumed: false,
  ts: "2026-08-30T00:00:00.000Z",
};

async function writeLeftoverFalse(vaultPath) {
  const statePath = path.join(vaultPath, RECALL_STATE_RELPATH);
  await mkdir(path.dirname(statePath), { recursive: true });
  await writeFile(statePath, JSON.stringify(LEFTOVER_FALSE), "utf8");
  return statePath;
}

/** Finds the last audit entry for `tool` and returns its parsed details JSON. */
async function lastAuditDetails(vaultPath, tool) {
  const tail = await auditTail(vaultPath, 20);
  for (let i = tail.entries.length - 1; i >= 0; i -= 1) {
    const entry = tail.entries[i];
    if (!entry.includes(`] ${tool} |`)) continue;
    const match = entry.match(/```json\n([\s\S]*?)\n```/);
    return match ? JSON.parse(match[1]) : undefined;
  }
  return undefined;
}

async function withFixture(run) {
  const vault = await mkdtemp(path.join(SUITE_ROOT, "vault-"));
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  const savedBudget = process.env.MINNI_HOOK_BUDGET_MS;
  const daemon = await startBlackHoleDaemon(SUITE_SOCKET);
  try {
    return await run({ vault });
  } finally {
    await daemon.close();
    if (savedBudget === undefined) delete process.env.MINNI_HOOK_BUDGET_MS;
    else process.env.MINNI_HOOK_BUDGET_MS = savedBudget;
    await rm(vault, { recursive: true, force: true });
  }
}

test.after(async () => {
  await rm(SUITE_ROOT, { recursive: true, force: true });
});

// One suite socket: tests must not bind it concurrently.
test.describe({ concurrency: 1 }, () => {
  test("leftover consumed=false + Grok + timeout UPS (no vault match) → no recall-state; PreToolUse Read allows", async () => {
    await withFixture(async ({ vault }) => {
      process.env.MINNI_HOOK_BUDGET_MS = "1200";
      const statePath = await writeLeftoverFalse(vault);
      const handlers = createHookHandlers(
        turnConfig(vault, { agentId: "grok-build", auditPrefix: "hook_grok", wire: grokBuildWire }),
      );
      await handlers.handleUserPromptSubmit({
        prompt: "an unrelated question with no vault substring match",
        workspace_id: "workspace-fixture",
      });
      const details = await lastAuditDetails(vault, "hook_grok_user_prompt_submit");
      assert.ok(details, "UserPromptSubmit must record an audit entry");
      assert.equal(details.daemon_timed_out, true, "precondition: this must be the timeout/degraded path this PR gates");
      assert.equal(details.recall_strong, false, "timeout path must not be a strong-recall plant");
      assert.equal(await fileExists(statePath), false, "leftover consumed=false must not survive a dropped timeout UPS");
      assert.equal(await readRecallState(vault), null);
      const pre = await handlers.handlePreToolUse({
        tool_name: "Read",
        tool_input: { file_path: "/tmp/work/README.md" },
      });
      assert.equal(isDeny(pre), false, "UNCONSULTED must not fire — Grok never received the envelope");
      assert.equal(pre.continue, true);
      assert.equal(pre.hookSpecificOutput, undefined);
    });
  });

  test("Claude degraded-turn-must-not-clear leftover consumed=false on timeout UPS", async () => {
    await withFixture(async ({ vault }) => {
      process.env.MINNI_HOOK_BUDGET_MS = "1200";
      const statePath = await writeLeftoverFalse(vault);
      const handlers = createHookHandlers(turnConfig(vault, { wire: claudeCodeWire }));
      await handlers.handleUserPromptSubmit({
        prompt: "an unrelated question with no vault substring match",
        workspace_id: "workspace-fixture",
      });
      const details = await lastAuditDetails(vault, "hook_test_user_prompt_submit");
      assert.ok(details, "UserPromptSubmit must record an audit entry");
      assert.equal(details.daemon_timed_out, true, "precondition: this must be the timeout/degraded path this PR gates");
      assert.equal(await fileExists(statePath), true, "degraded turn must not clear the recall-state file");
      const state = await readRecallState(vault);
      assert.equal(state.consumed, false);
      assert.equal(state.top_hits[0].title, "Prior fix");
    });
  });

  test("H2: dropped-inject timeout UPS does not unlink recall-state.json through a .runtime dir symlink", async () => {
    await withFixture(async ({ vault }) => {
      process.env.MINNI_HOOK_BUDGET_MS = "1200";
      await rm(path.join(vault, ".runtime"), { recursive: true, force: true });
      const outside = await mkdtemp(path.join(SUITE_ROOT, "outside-"));
      const escapedStatePath = path.join(outside, "recall-state.json");
      await writeFile(escapedStatePath, JSON.stringify(LEFTOVER_FALSE), "utf8");
      await symlink(outside, path.join(vault, ".runtime"), "dir");

      const handlers = createHookHandlers(
        turnConfig(vault, { agentId: "grok-build", auditPrefix: "hook_grok", wire: grokBuildWire }),
      );
      await handlers.handleUserPromptSubmit({
        prompt: "an unrelated question with no vault substring match",
        workspace_id: "workspace-fixture",
      });
      const details = await lastAuditDetails(vault, "hook_grok_user_prompt_submit");
      assert.ok(details, "UserPromptSubmit must record an audit entry");
      assert.equal(details.daemon_timed_out, true, "precondition: this must be the timeout/degraded path this PR gates");
      assert.equal(details.recall_strong, false, "timeout path must not be a strong-recall plant");
      assert.equal(
        await fileExists(escapedStatePath),
        true,
        "clearRecallState must not follow <vault>/.runtime dir symlink and wipe the escape target",
      );
      const escaped = JSON.parse(await readFile(escapedStatePath, "utf8"));
      assert.equal(escaped.consumed, false);
      assert.equal(escaped.task_signature, LEFTOVER_FALSE.task_signature);
      const pre = await handlers.handlePreToolUse({
        tool_name: "Read",
        tool_input: { file_path: "/tmp/work/README.md" },
      });
      assert.equal(isDeny(pre), false, "escaped leftover must not deny on a dropped-inject wire");
    });
  });
});
