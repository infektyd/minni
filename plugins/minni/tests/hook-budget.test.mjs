// UserPromptSubmit hook time budget (fail-open deadline).
//
// The bug this pins: Claude Code kills a UserPromptSubmit hook at 30s and
// DISCARDS its output ("hook timed out after 30s — output discarded"), so the
// turn silently loses recall injection, corrections matching and the active-plan
// pointer. The hook's only deadline was the JSON-RPC socket timeout, which was
// itself 30000ms — the same value — and was applied PER SOCKET CANDIDATE inside
// a sequential loop, so it could never fail open before the harness killed it.
//
// The contract now: the hook owns an internal budget (MINNI_HOOK_BUDGET_MS,
// default 8000) and emits whatever it has when the daemon overruns it.
//
// These are end-to-end SUBPROCESS tests on purpose. Racing a promise is not
// enough to make a node process exit — an in-flight socket handle keeps the
// event loop alive — so the only honest assertion is that the real
// `dist/hook.js` process writes its envelope AND exits, well under 30s, while
// a fake daemon holds the connection open forever.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const HOOK_ENTRY = fileURLToPath(new URL("../dist/hook.js", import.meta.url));

/** A daemon that accepts the connection and NEVER answers — the cold-start stall. */
async function startBlackHoleDaemon(socketPath) {
  const sockets = [];
  const server = net.createServer((socket) => {
    sockets.push(socket);
    // deliberately no response, ever
  });
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

/** A daemon that answers immediately — the healthy path must stay untouched. */
async function startResponsiveDaemon(socketPath, results) {
  const server = net.createServer((socket) => {
    socket.on("data", (chunk) => {
      const request = JSON.parse(chunk.toString("utf8").split("\n")[0]);
      socket.write(
        `${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { results, layer: "knowledge" } })}\n`,
      );
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return { close: () => new Promise((resolve) => server.close(resolve)) };
}

async function withFixture(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-budget-"));
  const vault = path.join(root, "claudecode-vault");
  const home = path.join(root, "home");
  // Unix socket paths are capped near 104 bytes on darwin; keep it short.
  const socketPath = path.join(root, "d.sock");
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  await mkdir(home, { recursive: true });
  const daemon = await startBlackHoleDaemon(socketPath);
  try {
    return await run({ vault, home, socketPath });
  } finally {
    await daemon.close();
    await rm(root, { recursive: true, force: true });
  }
}

/**
 * Run the real hook entrypoint as a subprocess. Resolves with the parsed stdout
 * envelope and the wall-clock the PROCESS took to exit (not just to print) —
 * a hook that prints and then hangs on its socket is still a hook Claude Code
 * kills and discards.
 */
function runHook({ vault, home, socketPath, budgetMs, prompt }) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const child = spawn(process.execPath, [HOOK_ENTRY, "UserPromptSubmit"], {
      env: {
        ...process.env,
        MINNI_HOME: home,
        MINNI_SOCKET_PATH: socketPath,
        MINNI_CLAUDECODE_VAULT_PATH: vault,
        MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
        MINNI_BYPASS_AUDIT_LIMIT: "true",
        MINNI_HOOK_BUDGET_MS: String(budgetMs),
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", reject);
    child.on("close", (code) => {
      const elapsedMs = Date.now() - started;
      let parsed;
      try {
        parsed = JSON.parse(stdout.trim().split("\n").filter(Boolean).pop() ?? "{}");
      } catch (error) {
        reject(new Error(`unparseable hook stdout (${error.message}): ${stdout}\n${stderr}`));
        return;
      }
      resolve({ output: parsed, elapsedMs, code, stderr });
    });
    child.stdin.end(
      JSON.stringify({
        hook_event_name: "UserPromptSubmit",
        prompt,
        session_id: "budget-fixture",
        cwd: home,
      }),
    );
  });
}

test("UserPromptSubmit fails open under its budget when the daemon never answers", async () => {
  await withFixture(async (fixture) => {
    const budgetMs = 1500;
    const { output, elapsedMs, code } = await runHook({
      ...fixture,
      budgetMs,
      prompt: "what did we decide about the retrieval reranker",
    });

    // The process must EXIT, not merely print: a lingering socket handle is the
    // difference between "failed open" and "killed at 30s, output discarded".
    assert.equal(code, 0, "hook must exit cleanly");
    assert.ok(
      elapsedMs < budgetMs + 6000,
      `hook took ${elapsedMs}ms with a ${budgetMs}ms budget — it did not fail open`,
    );
    // Far under Claude Code's 30s UserPromptSubmit limit, which is the whole point.
    assert.ok(elapsedMs < 30_000, `hook took ${elapsedMs}ms — still inside the kill window`);
    assert.equal(output.continue, true);
  });
});

test("a budget overrun serves the STALE recall pointer, marked stale", async () => {
  await withFixture(async (fixture) => {
    // A previous (fast) turn left a strong pointer behind. When the daemon
    // stalls we serve that instead of injecting nothing.
    await writeFile(
      path.join(fixture.vault, ".runtime", "recall-state.json"),
      JSON.stringify({
        task_signature: "previous-turn",
        intent: "recall",
        top_hits: [
          { title: "reranker decision", wikilink: "[[wiki/decisions/reranker]]", score: 0.91 },
        ],
        top_score: 0.91,
        consumed: false,
        ts: new Date().toISOString(),
      }),
      "utf8",
    );

    const { output, elapsedMs } = await runHook({
      ...fixture,
      budgetMs: 1500,
      prompt: "what did we decide about the retrieval reranker",
    });

    assert.ok(elapsedMs < 30_000, `hook took ${elapsedMs}ms`);
    const context = output.hookSpecificOutput?.additionalContext ?? "";
    assert.match(context, /reranker decision/, "stale pointer content must be injected");
    assert.match(context, /stale/i, "the envelope must mark the pointer as stale");
  });
});

test("a stale pointer is NOT silently cleared by the degraded turn", async () => {
  await withFixture(async (fixture) => {
    const statePath = path.join(fixture.vault, ".runtime", "recall-state.json");
    await writeFile(
      statePath,
      JSON.stringify({
        task_signature: "previous-turn",
        intent: "recall",
        top_hits: [{ title: "kept hit", wikilink: "[[wiki/decisions/kept]]", score: 0.9 }],
        top_score: 0.9,
        consumed: false,
        ts: new Date().toISOString(),
      }),
      "utf8",
    );

    await runHook({ ...fixture, budgetMs: 1500, prompt: "unrelated question about kept hit" });

    // The weak-turn path clears recall-state; a DEGRADED turn must not, because
    // "the daemon did not answer" is not evidence that the recall was weak.
    const { readFile } = await import("node:fs/promises");
    const raw = await readFile(statePath, "utf8");
    assert.match(raw, /kept hit/, "degraded turn must not clear the recall-state file");
  });
});

test("a responsive daemon is NOT marked degraded and answers fast", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-ok-"));
  const vault = path.join(root, "claudecode-vault");
  const home = path.join(root, "home");
  const socketPath = path.join(root, "d.sock");
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  await mkdir(home, { recursive: true });
  const daemon = await startResponsiveDaemon(socketPath, [
    {
      title: "reranker decision",
      wikilink: "[[wiki/decisions/reranker]]",
      confidence: 0.93,
      layer: "knowledge",
    },
  ]);
  try {
    const { output, elapsedMs } = await runHook({
      vault,
      home,
      socketPath,
      budgetMs: 8000,
      prompt: "what did we decide about the retrieval reranker",
    });
    // A healthy daemon must not pay any part of the budget.
    assert.ok(elapsedMs < 5000, `healthy path took ${elapsedMs}ms`);
    const context = output.hookSpecificOutput?.additionalContext ?? "";
    assert.match(context, /reranker decision/, "live recall must be injected");
    assert.doesNotMatch(context, /STALE recall pointer/, "healthy turn must not read as stale");
    assert.doesNotMatch(context, /timed_out/, "healthy turn must not be marked degraded");
  } finally {
    await daemon.close();
    await rm(root, { recursive: true, force: true });
  }
});
