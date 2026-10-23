// A UserPromptSubmit whose budget is already spent must not leave an
// active-plan read waiting on the plan's thread lock after it returns.
//
// withBudget races rather than cancels, so a plan read started with no
// budget left kept polling `mkdir(lockDir)` for up to DEFAULT_WAIT_MS after
// the handler had answered. In a test that is a teardown race: the fixture's
// depth-first `rm` deleted the held lock dir, the abandoned waiter's next
// poll re-created it, and the parent `rmdir` failed with ENOTEMPTY
// (hook-behavior.test.mjs "spent budget does not wait out a held lock").
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { createPlan } from "../dist/plan.js";
import { ensureVault } from "../dist/vault.js";

async function withFixture(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-ups-no-linger-"));
  const vault = path.join(root, "vault");
  const home = path.join(root, "home");
  const saved = {
    home: process.env.MINNI_HOME,
    socket: process.env.MINNI_SOCKET_PATH,
    afm: process.env.MINNI_AFM_HEALTH_URL,
    bypass: process.env.MINNI_BYPASS_AUDIT_LIMIT,
  };
  process.env.MINNI_HOME = home;
  process.env.MINNI_SOCKET_PATH = path.join(home, "missing.sock");
  process.env.MINNI_AFM_HEALTH_URL = "http://127.0.0.1:1/health";
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  await mkdir(home, { recursive: true });
  try {
    await run({ vault });
  } finally {
    for (const [key, value] of [
      ["MINNI_HOME", saved.home],
      ["MINNI_SOCKET_PATH", saved.socket],
      ["MINNI_AFM_HEALTH_URL", saved.afm],
      ["MINNI_BYPASS_AUDIT_LIMIT", saved.bypass],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(root, { recursive: true, force: true, maxRetries: 5 });
  }
}

function lockPaths(vault, planId) {
  const key = createHash("sha256").update(planId).digest("hex").slice(0, 32);
  const lockDir = path.join(vault, ".runtime", "thread-locks", `${key}.lock`);
  return { lockDir, ownerPath: path.join(lockDir, "owner.json") };
}

/** Watch, for `ms`, whether any active-plan-view operation takes the lock. */
async function watchForActivePlanAcquire(ownerPath, ms) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    if (existsSync(ownerPath)) {
      try {
        const owner = JSON.parse(readFileSync(ownerPath, "utf8"));
        if (String(owner.operationId ?? "").startsWith("active-plan-view:")) return true;
      } catch {
        // Owner file mid-write: look again.
      }
    }
    await new Promise((resolve) => setImmediate(resolve));
  }
  return false;
}

async function runHeldLockUps(vault, promptHookTimeoutMs) {
  const { plan } = await createPlan(
    { goal: "UPS must not linger on the plan lock", vaultPath: vault },
    { vaultPath: vault },
  );
  const { lockDir, ownerPath } = lockPaths(vault, plan.plan_id);
  await mkdir(lockDir, { recursive: true });
  await writeFile(
    ownerPath,
    `${JSON.stringify({ pid: process.pid, operationId: "test-live-holder", acquiredAt: new Date().toISOString() })}\n`,
    { mode: 0o600 },
  );
  const handlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    alwaysWriteStopInbox: false,
    promptHookTimeoutMs,
  });
  const started = Date.now();
  const output = await handlers.handleUserPromptSubmit({
    prompt: "an utterly novel question with zero prior memory linger-mid-zzz",
    workspace_id: "workspace-fixture",
  });
  return { output, elapsedMs: Date.now() - started, lockDir, ownerPath };
}

// A budget that is not spent but is shorter than the lock's default wait:
// the plan read must give up inside the budget, not keep polling after it.
test("mid-budget UPS gives up the plan lock before it answers", { timeout: 15_000 }, async () => {
  await withFixture(async ({ vault }) => {
    await ensureVault(vault);
    // effectiveHookBudgetMs takes a fraction of the manifest timeout; 3 s of
    // manifest leaves a budget well under the 5 s default lock wait.
    const { output, elapsedMs, lockDir, ownerPath } = await runHeldLockUps(vault, 3_000);
    assert.equal(output.continue, true);
    assert.ok(elapsedMs < 4_000, `UPS must answer inside its budget; took ${elapsedMs}ms`);

    await rm(lockDir, { recursive: true, force: true });
    const acquired = await watchForActivePlanAcquire(ownerPath, 400);
    assert.equal(acquired, false, "the plan read must not still be waiting on the lock after UPS answered");
  });
});

test("spent-budget UPS leaves no active-plan read waiting on the lock", { timeout: 10_000 }, async () => {
  await withFixture(async ({ vault }) => {
    await ensureVault(vault);
    const { plan } = await createPlan(
      { goal: "spent budget must not linger on the plan lock", vaultPath: vault },
      { vaultPath: vault },
    );
    const { lockDir, ownerPath } = lockPaths(vault, plan.plan_id);
    await mkdir(lockDir, { recursive: true });
    await writeFile(
      ownerPath,
      `${JSON.stringify({ pid: process.pid, operationId: "test-live-holder", acquiredAt: new Date().toISOString() })}\n`,
      { mode: 0o600 },
    );

    const handlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "workspace-fixture",
      contextWindow: 200_000,
      hooksEnabled: true,
      auditPrefix: "hook_test",
      alwaysWriteStopInbox: false,
      promptHookTimeoutMs: 2,
    });
    const output = await handlers.handleUserPromptSubmit({
      prompt: "an utterly novel question with zero prior memory linger-zzz",
      workspace_id: "workspace-fixture",
    });
    assert.equal(output.continue, true);

    // Release the holder the way a fixture teardown would. A plan read still
    // waiting on the lock takes it on its next poll (every 25 ms by default).
    await rm(lockDir, { recursive: true, force: true });
    const acquired = await watchForActivePlanAcquire(ownerPath, 400);
    assert.equal(
      acquired,
      false,
      "a UPS that answered with its budget spent must not still be waiting on the plan lock",
    );
  });
});
