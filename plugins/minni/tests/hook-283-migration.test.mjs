// #283: claude-code's hook.ts migrated onto the shared createHookHandlers /
// runHookMain factory. Most of hook.ts's prior behavior is already covered
// end-to-end by the existing black-box suites (hook-behavior.test.mjs,
// lifecycle-hook.test.mjs, sessionstart-delivery.test.mjs, identity-body-
// delivery.test.mjs) — all of them continued to pass unmodified against the
// migrated hook.js, which is the strongest evidence the migration preserved
// behavior. This file covers the two pieces of claude-specific behavior that
// had NO existing end-to-end coverage before the migration:
//
//   1. handleSessionStart's PLUMB-only knob (ackPendingHandoffsAtBoot):
//      listing and acking pending handoff leases at boot, and surfacing
//      handoff_acks / the handoff_leases degraded section.
//   2. handlePostCompact (#227 near-free follow-through): claude-code's
//      native post-compaction event, now a real factory dispatch case
//      instead of falling through to the unrouted-event swallow.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { auditTail, ensureVault } from "../dist/vault.js";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function envelopeJson(additionalContext) {
  const match = additionalContext.match(/<minni:context [^>]*>\n([\s\S]*)\n<\/minni:context>/);
  assert.ok(match, `expected minni:context envelope, got: ${additionalContext.slice(0, 200)}`);
  return JSON.parse(match[1]);
}

/**
 * A minimal fake daemon (same style as identity-body-delivery.test.mjs):
 * a real Unix-socket JSON-RPC server, so SessionStart's real RPC client code
 * runs unmodified — only the RESPONSES are controlled. `handlers` overrides
 * the default per-method response; anything not overridden gets a bland
 * success so the rest of the boot sequence completes normally. Every
 * received request is pushed to `calls` so a test can assert what the hook
 * actually SENT (e.g. did it really ack the lease it claims to have acked).
 */
function startFakeDaemon(socketPath, handlers = {}) {
  const calls = [];
  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        calls.push(request);
        const respond = (result) => {
          socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
        };
        const respondError = (message, code = -32000) => {
          socket.write(
            `${JSON.stringify({ jsonrpc: "2.0", id: request.id, error: { code, message } })}\n`,
          );
        };
        const handler = handlers[request.method];
        if (handler) {
          handler(request, { respond, respondError });
          continue;
        }
        switch (request.method) {
          case "status":
            respond({ status: "ok" });
            break;
          case "search":
            respond({ agent_id: request.params?.agent_id, results: [] });
            break;
          case "read":
            respond({ agent_id: request.params?.agent_id, context: "" });
            break;
          case "minni_list_pending_handoffs":
            respond({ handoffs: [] });
            break;
          case "minni_subscribe_contradictions":
            respond({ events: [], status: "checked_no_match" });
            break;
          default:
            respond({ ok: true });
        }
      }
    });
  });
  return new Promise((resolve) => server.listen(socketPath, () => resolve({ server, calls })));
}

function runHook(event, env, payload = {}, bin = "hook.js") {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(PLUGIN_ROOT, "dist", bin), event], {
      env: { ...process.env, ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`hook ${event} timed out; stderr=${stderr}`));
    }, 30_000);
    child.on("close", () => {
      clearTimeout(timer);
      const line = stdout.trim().split("\n").at(-1) ?? "";
      try {
        resolve(JSON.parse(line));
      } catch {
        reject(new Error(`unparseable hook output: ${stdout} / ${stderr}`));
      }
    });
    child.stdin.write(JSON.stringify({ session_id: "hook-283-test", ...payload }));
    child.stdin.end();
  });
}

async function withFixture(fn) {
  const home = await mkdtemp(path.join(tmpdir(), "sm-hook283-home-"));
  const vault = await mkdtemp(path.join(tmpdir(), "sm-hook283-vault-"));
  try {
    await ensureVault(vault);
    return await fn({ home, vault, socketPath: path.join(home, "minnid.sock") });
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(vault, { recursive: true, force: true });
  }
}

const BASE_ENV = (fixture) => ({
  MINNI_HOME: fixture.home,
  MINNI_SOCKET_PATH: fixture.socketPath,
  MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
  MINNI_BYPASS_AUDIT_LIMIT: "true",
  MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
  MINNI_CLAUDECODE_AGENT_ID: "claude-code",
});

// ── ackPendingHandoffsAtBoot ────────────────────────────────────────────────

test("SessionStart acks a real pending handoff lease and reports handoff_acks", async () => {
  await withFixture(async (fixture) => {
    const { server, calls } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respond }) => {
        respond({
          handoffs: [
            { lease_id: "handoff-283-abc", from_agent: "codex", to_agent: "claude-code", task: "test" },
          ],
        });
      },
      minni_ack_handoff: (request, { respond }) => {
        assert.equal(request.params.lease_id, "handoff-283-abc");
        assert.equal(request.params.status, "accepted");
        assert.equal(request.params.agent_id, "claude-code", "ack must stamp the server-side identity");
        respond({ lease_id: request.params.lease_id, status: "accepted", updated_paths: [] });
      },
    });
    try {
      const output = await runHook("SessionStart", BASE_ENV(fixture));
      const body = envelopeJson(output.hookSpecificOutput.additionalContext);

      assert.deepEqual(body.handoff_acks, ["handoff-283-abc"]);
      assert.equal(body.degraded, undefined, "a clean ack must not degrade anything");

      const ackCalls = calls.filter((c) => c.method === "minni_ack_handoff");
      assert.equal(ackCalls.length, 1, "exactly one ack RPC must have been sent");
    } finally {
      server.close();
    }
  });
});

test("SessionStart degrades handoff_leases when the list RPC fails, without breaking the boot", async () => {
  await withFixture(async (fixture) => {
    const { server } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respondError }) => {
        respondError("handoff lease store failed");
      },
    });
    try {
      const output = await runHook("SessionStart", BASE_ENV(fixture));
      assert.equal(output.continue, true, "a broken handoff list must not fail the whole boot");
      const body = envelopeJson(output.hookSpecificOutput.additionalContext);

      // handoff_acks is unconditionally present when the knob is on (matches
      // the pre-migration hook.ts exactly) — empty because the list RPC
      // failed before any lease could be acked, not absent.
      assert.deepEqual(body.handoff_acks, [], "no acks were attempted, but the key stays present");
      assert.ok(body.degraded, "the boot must say it degraded");
      assert.ok(
        body.degraded.sections.includes("handoff_leases"),
        `degraded.sections must include handoff_leases, got: ${JSON.stringify(body.degraded.sections)}`,
      );
    } finally {
      server.close();
    }
  });
});

test("SessionStart handoff acking does not run for a platform without the knob (codex)", async () => {
  // Regression guard for the knob's default-off contract: codex's config
  // never sets ackPendingHandoffsAtBoot, so a fake daemon that WOULD answer
  // list/ack RPCs must never actually be asked.
  await withFixture(async (fixture) => {
    const { server, calls } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respond }) => {
        respond({ handoffs: [{ lease_id: "should-not-be-seen", to_agent: "codex" }] });
      },
    });
    try {
      const output = await runHook("SessionStart", {
        ...BASE_ENV(fixture),
        MINNI_CODEX_VAULT_PATH: fixture.vault,
        MINNI_CODEX_AGENT_ID: "codex",
        MINNI_CODEX_HOOKS: "on",
      }, {}, "codex-hook.js");
      assert.equal(output.continue, true);
      const body = envelopeJson(output.hookSpecificOutput.additionalContext);
      assert.equal(body.handoff_acks, undefined, "codex must never surface handoff_acks");
      assert.equal(
        calls.filter((c) => c.method === "minni_list_pending_handoffs").length,
        0,
        "codex must never even call minni_list_pending_handoffs",
      );
    } finally {
      server.close();
    }
  });
});

// ── handlePostCompact (#227 near-free follow-through) ───────────────────────

test("PostCompact harvests compact_summary into the vault (primary delivery path)", async () => {
  await withFixture(async (fixture) => {
    const summary = "x".repeat(80); // clears SUMMARY_TEXT_MIN_CHARS (40)
    const output = await runHook(
      "PostCompact",
      BASE_ENV(fixture),
      { compact_summary: summary, session_id: "pc-283" },
    );
    assert.equal(output.continue, true);

    const tail = await auditTail(fixture.vault, 20);
    assert.match(
      tail.text,
      /hook_compact_harvest/,
      "a real, long-enough summary must be recorded as harvested",
    );
    assert.match(tail.text, /pc-283/);
  });
});

test("PostCompact is a genuine no-op (not an unrouted-event drop) when the summary is empty", async () => {
  await withFixture(async (fixture) => {
    const output = await runHook("PostCompact", BASE_ENV(fixture), { session_id: "pc-283-empty" });
    assert.equal(output.continue, true);

    const tail = await auditTail(fixture.vault, 20);
    assert.doesNotMatch(
      tail.text,
      /hook_intent_dropped/,
      "PostCompact has a real dispatch case now — an empty summary is a no-op harvest, not an unrouted event",
    );
  });
});
