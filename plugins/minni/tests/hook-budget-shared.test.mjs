// Prompt-time budget on the SHARED handler factory (codex / grok / cursor /
// gemini), mirroring hook-budget.test.mjs which covers the claude-code hook.
//
// Same bug, same contract: every host kills a prompt-time hook and discards its
// output, so an unbounded daemon wait costs the whole turn's injection silently.
// The extra constraint here is that each platform declares its own manifest
// timeout, and the budget must sit safely INSIDE the tightest one — gemini's
// PreInvocation is 10s, where the flat 8s default would leave no room to write
// the envelope and audit after paying the budget.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const CODEX_ENTRY = fileURLToPath(new URL("../dist/codex-hook.js", import.meta.url));

// The daemon socket path is resolved into a module-level const at IMPORT time
// (config.ts SOCKET_PATH), so the black-hole socket has to exist and be
// exported into the environment BEFORE the modules under test are loaded.
// Setting it inside a fixture is too late — the handler then points at a
// nonexistent default socket, fails instantly with "Socket not found", and the
// test measures the fast-fail path instead of the stall it means to bound.
// Hence: top-level await + dynamic import.
const SUITE_ROOT = await mkdtemp(path.join(tmpdir(), "sm-shared-budget-"));
const SUITE_SOCKET = path.join(SUITE_ROOT, "d.sock");
process.env.MINNI_HOME = path.join(SUITE_ROOT, "home");
process.env.MINNI_SOCKET_PATH = SUITE_SOCKET;
process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
await mkdir(process.env.MINNI_HOME, { recursive: true });

const { createHookHandlers } = await import("../dist/hook-handlers.js");
const { DEFAULT_HOOK_BUDGET_MS, HOOK_BUDGET_HARNESS_FRACTION, effectiveHookBudgetMs } =
  await import("../dist/hook-utils.js");

/** A daemon that accepts and never answers — the cold-start stall. */
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

/**
 * Each case gets its own vault under the suite root, but SHARES the suite's
 * black-hole socket — that path is baked into the imported modules and cannot
 * vary per test.
 */
async function withFixture(run) {
  const vault = await mkdtemp(path.join(SUITE_ROOT, "vault-"));
  const home = process.env.MINNI_HOME;
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  const savedBudget = process.env.MINNI_HOOK_BUDGET_MS;
  const daemon = await startBlackHoleDaemon(SUITE_SOCKET);
  try {
    return await run({ vault, home, socketPath: SUITE_SOCKET });
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

function sharedConfig(vaultPath, overrides = {}) {
  return {
    agentId: "codex",
    vaultPath,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_codex_test",
    promptHookTimeoutMs: 30_000,
    ...overrides,
  };
}

async function writeStaleState(vaultPath, title) {
  await writeFile(
    path.join(vaultPath, ".runtime", "recall-state.json"),
    JSON.stringify({
      task_signature: "previous-turn",
      intent: "recall",
      top_hits: [{ title, wikilink: `[[wiki/decisions/${title.replace(/\s+/g, "-")}]]`, score: 0.9 }],
      top_score: 0.9,
      consumed: false,
      ts: new Date().toISOString(),
    }),
    "utf8",
  );
}

// ---- budget clamping (pure) ---------------------------------------------

test("the budget is clamped to a fraction of the platform's manifest timeout", () => {
  // gemini PreInvocation is 10s — the tightest, and below the flat default.
  assert.equal(
    effectiveHookBudgetMs(10_000, {}),
    Math.floor(10_000 * HOOK_BUDGET_HARNESS_FRACTION),
  );
  // codex / grok / cursor are 30s, so the configured default wins.
  assert.equal(effectiveHookBudgetMs(30_000, {}), DEFAULT_HOOK_BUDGET_MS);
  // No declared bound (kilocode) → the configured budget, unchanged.
  assert.equal(effectiveHookBudgetMs(undefined, {}), DEFAULT_HOOK_BUDGET_MS);
});

test("MINNI_HOOK_BUDGET_MS can shrink the budget but never exceed the harness", () => {
  assert.equal(effectiveHookBudgetMs(30_000, { MINNI_HOOK_BUDGET_MS: "2000" }), 2000);
  // The whole point: an operator raising the budget must not push it past the
  // deadline that will actually kill the hook.
  assert.equal(
    effectiveHookBudgetMs(10_000, { MINNI_HOOK_BUDGET_MS: "600000" }),
    Math.floor(10_000 * HOOK_BUDGET_HARNESS_FRACTION),
  );
});

// ---- shared handler behavior --------------------------------------------

test("shared handleUserPromptSubmit fails open under its budget", async () => {
  await withFixture(async ({ vault }) => {
    process.env.MINNI_HOOK_BUDGET_MS = "1200";
    const handlers = createHookHandlers(sharedConfig(vault));
    const started = Date.now();
    const output = await handlers.handleUserPromptSubmit({
      prompt: "what did we decide about the retrieval reranker",
      session_id: "shared-budget-fixture",
    });
    const elapsedMs = Date.now() - started;
    assert.ok(elapsedMs < 8000, `shared handler took ${elapsedMs}ms under a 1200ms budget`);
    assert.ok(output, "handler must return an output, not throw");
  });
});

test("a budget overrun serves the STALE pointer and marks the turn degraded", async () => {
  await withFixture(async ({ vault }) => {
    process.env.MINNI_HOOK_BUDGET_MS = "1200";
    await writeStaleState(vault, "reranker decision");
    const handlers = createHookHandlers(sharedConfig(vault));
    const output = await handlers.handleUserPromptSubmit({
      prompt: "what did we decide about the retrieval reranker",
      session_id: "shared-budget-fixture",
    });
    const rendered = JSON.stringify(output);
    assert.match(rendered, /reranker decision/, "stale pointer content must be injected");
    assert.match(rendered, /STALE recall pointer/, "the pointer must be marked stale");
    assert.match(rendered, /timed_out/, "the turn must be marked degraded");
  });
});

test("a degraded shared turn does NOT clear recall-state", async () => {
  await withFixture(async ({ vault }) => {
    process.env.MINNI_HOOK_BUDGET_MS = "1200";
    await writeStaleState(vault, "kept hit");
    const handlers = createHookHandlers(sharedConfig(vault));
    await handlers.handleUserPromptSubmit({
      prompt: "an unrelated question entirely",
      session_id: "shared-budget-fixture",
    });
    const raw = await readFile(path.join(vault, ".runtime", "recall-state.json"), "utf8");
    assert.match(raw, /kept hit/, "degraded turn must preserve the s6 guard's pointer");
  });
});

test("gemini's tight 10s manifest still leaves headroom after the budget", async () => {
  await withFixture(async ({ vault }) => {
    delete process.env.MINNI_HOOK_BUDGET_MS; // default 8000, clamped to 6000
    const handlers = createHookHandlers(
      sharedConfig(vault, { agentId: "gemini", promptHookTimeoutMs: 10_000 }),
    );
    const started = Date.now();
    await handlers.handleUserPromptSubmit({
      prompt: "what did we decide about the retrieval reranker",
      session_id: "gemini-budget-fixture",
    });
    const elapsedMs = Date.now() - started;
    // Must finish inside the 10s kill with room to spare for envelope + audit.
    assert.ok(elapsedMs < 8000, `gemini path took ${elapsedMs}ms against a 10s manifest kill`);
    assert.ok(elapsedMs >= 5500, `expected the ~6000ms clamp to apply, got ${elapsedMs}ms`);
  });
});

// ---- end-to-end: a real non-Claude entrypoint process ---------------------

test("the codex hook PROCESS exits under budget against a black-hole daemon", async () => {
  await withFixture(async ({ vault, home, socketPath }) => {
    const budgetMs = 1500;
    const started = Date.now();
    const result = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [CODEX_ENTRY, "UserPromptSubmit"], {
        env: {
          ...process.env,
          MINNI_HOME: home,
          MINNI_SOCKET_PATH: socketPath,
          MINNI_CODEX_VAULT_PATH: vault,
          MINNI_BYPASS_AUDIT_LIMIT: "true",
          MINNI_HOOK_BUDGET_MS: String(budgetMs),
        },
        stdio: ["pipe", "pipe", "pipe"],
      });
      let stdout = "";
      child.stdout.on("data", (chunk) => {
        stdout += chunk.toString("utf8");
      });
      child.on("error", reject);
      child.on("close", (code) => resolve({ stdout, code, elapsedMs: Date.now() - started }));
      child.stdin.end(
        JSON.stringify({
          hook_event_name: "UserPromptSubmit",
          prompt: "what did we decide about the retrieval reranker",
          session_id: "codex-budget-fixture",
          cwd: home,
        }),
      );
    });

    // The process must EXIT — a lingering socket handle is the difference
    // between "failed open" and "killed by the harness, output discarded".
    assert.equal(result.code, 0, "codex hook must exit cleanly");
    assert.ok(
      result.elapsedMs < budgetMs + 6000,
      `codex hook took ${result.elapsedMs}ms with a ${budgetMs}ms budget`,
    );
    assert.ok(
      result.elapsedMs < 30_000,
      `codex hook took ${result.elapsedMs}ms — still inside the kill window`,
    );
  });
});
