// A UserPromptSubmit must not leave work running, and writing into the vault,
// after it has answered.
//
// withBudget races rather than cancels. Two abandoned writers caused the
// ENOTEMPTY teardown flake in hook-behavior.test.mjs ("spent budget does not
// wait out a held lock", "held lock is degraded active_thread"):
// - the active-plan read kept polling `mkdir(lockDir)` for up to
//   DEFAULT_WAIT_MS, and re-created the lock dir once the fixture's
//   depth-first `rm` had deleted it
// - a raced ensureVault, and the cold vault scan's own ensureVault, re-seeded
//   vault files (`open(…, "ax")`) after they had been deleted
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { createPlan } from "../dist/plan.js";
import { ensureVault, searchVaultNotes } from "../dist/vault.js";

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
  const locksRoot = path.dirname(path.dirname(ownerPath));
  const baselineMtimeNs = existsSync(locksRoot) ? statSync(locksRoot, { bigint: true }).mtimeNs : 0n;
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
    if (existsSync(locksRoot) && statSync(locksRoot, { bigint: true }).mtimeNs !== baselineMtimeNs) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  return false;
}

/** Delete the vault seed files, then report any that reappear within `ms`. */
async function watchForReseededVault(vault, ms) {
  const seeds = ["log.md", "index.md", path.join("schema", "AGENTS.md")].map((rel) =>
    path.join(vault, rel),
  );
  await Promise.all(seeds.map((seed) => rm(seed, { force: true })));
  const until = Date.now() + ms;
  while (Date.now() < until) {
    const back = seeds.filter((seed) => existsSync(seed));
    if (back.length > 0) return back.map((seed) => path.relative(vault, seed));
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  return [];
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
    assert.ok(elapsedMs < 2_500, `UPS must answer inside its 1.8 s budget; took ${elapsedMs}ms`);

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
    const body = parseUpsEnvelope(output);
    assert.ok(body, "held lock degrades active_thread, making the turn salient");
    assert.equal(body.degraded?.daemon_recall, "timed_out");

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

function parseUpsEnvelope(output) {
  const ctx = output.hookSpecificOutput?.additionalContext ?? "";
  const raw = ctx.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/)?.[1];
  return raw ? JSON.parse(raw) : undefined;
}

test("spent-budget UPS seeds the vault before answering and not after", { timeout: 10_000 }, async () => {
  await withFixture(async ({ vault }) => {
    // No ensureVault here: the handler's own call is what is under test.
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
      prompt: "an utterly novel question with zero prior memory reseed-zzz",
      workspace_id: "workspace-fixture",
    });
    assert.equal(output.continue, true);
    assert.ok(existsSync(path.join(vault, "log.md")), "the vault must be seeded before UPS answers");

    const reseeded = await watchForReseededVault(vault, 400);
    assert.deepEqual(reseeded, [], "no vault file may be re-seeded after UPS has answered");
  });
});

// Pins the branch that still runs: with budget and a free lock, the plan read
// happens and its pointer rides the envelope.
test("UPS with budget and a free lock still injects the active thread pointer", { timeout: 15_000 }, async () => {
  await withFixture(async ({ vault }) => {
    await ensureVault(vault);
    const { plan } = await createPlan(
      { goal: "the happy path keeps its plan pointer", vaultPath: vault },
      { vaultPath: vault },
    );
    const handlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "workspace-fixture",
      contextWindow: 200_000,
      hooksEnabled: true,
      auditPrefix: "hook_test",
      alwaysWriteStopInbox: false,
    });
    const output = await handlers.handleUserPromptSubmit({
      prompt: "an utterly novel question with zero prior memory happy-path-zzz",
      workspace_id: "workspace-fixture",
    });
    const body = parseUpsEnvelope(output);
    assert.ok(body, "an active plan must make the turn salient");
    assert.equal(body.active_thread_ref?.plan_id, plan.plan_id);
    assert.ok(
      !(body.degraded?.sections ?? []).includes("active_thread"),
      `a free lock must not degrade active_thread, got ${JSON.stringify(body.degraded)}`,
    );
  });
});

async function runHeldLockSessionStart(vault, sessionStartHookTimeoutMs) {
  const { plan } = await createPlan(
    { goal: "SessionStart must not linger on the plan lock", vaultPath: vault },
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
    sessionStartHookTimeoutMs,
  });
  const started = Date.now();
  const output = await handlers.handleSessionStart({
    workspace_id: "workspace-fixture",
  });
  return { output, elapsedMs: Date.now() - started, lockDir, ownerPath };
}

test("mid-budget SessionStart gives up the plan lock before it answers", { timeout: 15_000 }, async () => {
  await withFixture(async ({ vault }) => {
    await ensureVault(vault);
    const { output, elapsedMs, lockDir, ownerPath } = await runHeldLockSessionStart(vault, 3_000);
    assert.equal(output.continue, true);
    assert.ok(elapsedMs < 2_500, `SessionStart must answer inside its 1.8 s budget; took ${elapsedMs}ms`);

    await rm(lockDir, { recursive: true, force: true });
    const acquired = await watchForActivePlanAcquire(ownerPath, 400);
    assert.equal(
      acquired,
      false,
      "the plan read must not still be waiting on the lock after SessionStart answered",
    );
  });
});

test("spent-budget SessionStart leaves no active-plan read waiting on the lock", { timeout: 10_000 }, async () => {
  await withFixture(async ({ vault }) => {
    await ensureVault(vault);
    const { output, lockDir, ownerPath } = await runHeldLockSessionStart(vault, 2);
    assert.equal(output.continue, true);

    await rm(lockDir, { recursive: true, force: true });
    const acquired = await watchForActivePlanAcquire(ownerPath, 400);
    assert.equal(
      acquired,
      false,
      "a SessionStart that answered with its budget spent must not still be waiting on the plan lock",
    );
  });
});

test(
  "searchVaultNotes on the scan path or without an explicit agentId is strictly read-only and does not seed a missing vault",
  { timeout: 10_000 },
  async () => {
    await withFixture(async ({ vault }) => {
      const missingVaultA = path.join(vault, "missing-a");
      const missingVaultB = path.join(vault, "missing-b");
      assert.deepEqual(await searchVaultNotes(missingVaultA, "cache policy", 5), []);
      assert.deepEqual(
        await searchVaultNotes(missingVaultB, "cache policy", 5, "claude-code", { candidates: "scan" }),
        [],
      );
      assert.equal(existsSync(missingVaultA), false, "read-only search without agentId must not create vault files");
      assert.equal(existsSync(missingVaultB), false, "scan-path search must not create vault files");
    });
  },
);
