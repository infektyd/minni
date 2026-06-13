import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { spawn } from "node:child_process";
import net from "node:net";
import { fileURLToPath } from "node:url";

import { envelopeBudgetFor } from "../dist/agent_envelope.js";
import {
  extractIdentityBody,
  truncateToTokenCharBudget,
} from "../dist/sovereign.js";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const IDENTITY_MARKER = "verification_expectation: recall before guessing";
const IDENTITY_CONTEXT = [
  "## Agent Identity: claude-code",
  "Loaded whole (not chunked).",
  "### CLAUDE-CODE_HOSTED_AGENT_ENVELOPE",
  IDENTITY_MARKER,
  "shelf contract token-budget table lives here",
  "",
  "## Prior Context (claude-code)",
  "  - **note.md** accessed 1x",
  "",
  "## Learnings (claude-code)",
  "  - [fix] Correction: service X moved to port 9090 (conf=1.0)",
].join("\n");

test("extractIdentityBody returns the whole Layer 1 block before Prior Context", () => {
  const body = extractIdentityBody(IDENTITY_CONTEXT);
  assert.ok(body?.startsWith("## Agent Identity: claude-code"));
  assert.match(body, new RegExp(IDENTITY_MARKER));
  assert.ok(!body.includes("## Prior Context"));
  assert.ok(!body.includes("## Learnings"));
});

test("extractIdentityBody handles missing input without throwing", () => {
  assert.equal(extractIdentityBody(undefined), undefined);
  assert.equal(extractIdentityBody(""), undefined);
  assert.equal(extractIdentityBody("## Learnings\n- x"), undefined);
});

test("truncateToTokenCharBudget respects char estimate", () => {
  const text = "a".repeat(100);
  assert.equal(truncateToTokenCharBudget(text, 10).length, 40);
  assert.equal(truncateToTokenCharBudget(text, 100), text);
});

function envelopeJson(additionalContext) {
  const match = additionalContext.match(/<minni:context [^>]*>\n([\s\S]*)\n<\/minni:context>/);
  assert.ok(match, `expected minni:context envelope, got: ${additionalContext.slice(0, 200)}`);
  return JSON.parse(match[1]);
}

function startFakeDaemon(socketPath, context = IDENTITY_CONTEXT) {
  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.split("\n")[0]);
      const respond = (result) => {
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      };
      switch (request.method) {
        case "status":
          respond({ status: "ok" });
          break;
        case "search":
          respond({ agent_id: request.params.agent_id, results: [] });
          break;
        case "read":
          respond({ agent_id: request.params.agent_id, context });
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
    });
  });
  return new Promise((resolve) => server.listen(socketPath, () => resolve(server)));
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
      } catch (error) {
        reject(new Error(`unparseable hook output: ${stdout} / ${stderr}`));
      }
    });
    child.stdin.write(JSON.stringify({ session_id: "identity-body-test", ...payload }));
    child.stdin.end();
  });
}

test("SessionStart envelope includes whole-document identity_body (claude-code hook)", async (t) => {
  const home = await mkdtemp(path.join(tmpdir(), "sm-identity-home-"));
  const vault = await mkdtemp(path.join(tmpdir(), "sm-identity-vault-"));
  const socketPath = path.join(home, "minnid.sock");
  const server = await startFakeDaemon(socketPath);
  t.after(async () => {
    server.close();
    await rm(home, { recursive: true, force: true });
    await rm(vault, { recursive: true, force: true });
  });

  const output = await runHook(
    "SessionStart",
    {
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
      MINNI_CLAUDECODE_VAULT_PATH: vault,
      MINNI_CLAUDECODE_AGENT_ID: "claude-code",
    },
    {},
    "hook.js",
  );

  const context = output.hookSpecificOutput.additionalContext;
  const body = envelopeJson(context);

  assert.equal(typeof body.identity_body, "string");
  assert.match(body.identity_body, new RegExp(IDENTITY_MARKER));
  assert.match(body.identity_body, /HOSTED_AGENT_ENVELOPE/);
  assert.ok(
    !String(body.identity_body).includes("## Learnings"),
    "identity_body must not include the Learnings slice",
  );

  const budget = envelopeBudgetFor(200_000);
  const maxChars = Math.max(budget - 500, 0) * 4;
  assert.ok(body.identity_body.length <= maxChars, "identity_body must respect Layer-1 budget");

  const tokensMatch = context.match(/tokens="(\d+)"/);
  assert.ok(tokensMatch, "envelope must report token estimate");
  assert.ok(Number(tokensMatch[1]) <= budget, "envelope tokens must not exceed budget");
});

test("SessionStart agent-context boot includes identity_body (codex hook)", async (t) => {
  const home = await mkdtemp(path.join(tmpdir(), "sm-identity-codex-home-"));
  const vault = await mkdtemp(path.join(tmpdir(), "sm-identity-codex-vault-"));
  const socketPath = path.join(home, "minnid.sock");
  const server = await startFakeDaemon(socketPath);
  t.after(async () => {
    server.close();
    await rm(home, { recursive: true, force: true });
    await rm(vault, { recursive: true, force: true });
  });

  const output = await runHook(
    "SessionStart",
    {
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
      MINNI_VAULT_PATH: vault,
      MINNI_AGENT_ID: "codex",
    },
    {},
    "codex-hook.js",
  );

  const context = output.hookSpecificOutput.additionalContext;
  const body = envelopeJson(context);

  assert.match(String(body.identity_body), new RegExp(IDENTITY_MARKER));
  assert.equal(
    body.recent_learnings,
    undefined,
    "agent-context must not duplicate recent_learnings",
  );
  assert.match(context, /## Agent Identity: claude-code/, "native layer 1 prefix must remain");
});