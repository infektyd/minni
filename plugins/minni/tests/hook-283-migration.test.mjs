// #283: claude-code's hook.ts migrated onto the shared createHookHandlers /
// runHookMain factory. Most of hook.ts's prior behavior is already covered
// end-to-end by the existing black-box suites (hook-behavior.test.mjs,
// lifecycle-hook.test.mjs, sessionstart-delivery.test.mjs, identity-body-
// delivery.test.mjs) — all of them continued to pass unmodified against the
// migrated hook.js, which is the strongest evidence the migration preserved
// behavior. This file covers the pieces of behavior that had NO existing
// end-to-end coverage before the migration:
//
//   1. handleSessionStart's PLUMB-only knob (ackPendingHandoffsAtBoot):
//      listing and acking pending handoff leases at boot, and surfacing
//      handoff_acks / the handoff_leases degraded section.
//   2. handlePostCompact (#227 near-free follow-through): claude-code's
//      native post-compaction event, now a real factory dispatch case
//      instead of falling through to the unrouted-event swallow.
//   3. SEC-006 (found during review round 1 of this same PR): the shared
//      factory's handleUserPromptSubmit imported filterSafeVaultResults but
//      never called it — searchVaultNotes' raw output (including
//      private/local-only notes and privacy-heuristic escalations) fed
//      straight into the recall pointer, the persisted recall-state top
//      hits, and the audit vault_matches list. This was ALREADY true for
//      codex/grok-build/cursor/gemini/kilocode before this PR; migrating
//      claude-code onto this factory would have been what took away
//      claude's own (correct) filtering. Fixed once, in the factory, for
//      every platform.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
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
        let request;
        try {
          request = JSON.parse(line);
        } catch (error) {
          // Fail loudly with a diagnosable message rather than an opaque
          // uncaught exception inside a socket 'data' handler (which would
          // otherwise crash the whole test runner, not just this test).
          throw new Error(`fake daemon received non-JSON line: ${line} (${error.message})`);
        }
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
    const { server, calls } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respondError }) => {
        respondError("handoff lease store failed");
      },
    });
    try {
      const output = await runHook("SessionStart", BASE_ENV(fixture));
      assert.equal(output.continue, true, "a broken handoff list must not fail the whole boot");
      const body = envelopeJson(output.hookSpecificOutput.additionalContext);

      // Confirms the RPC error path was actually exercised, not e.g. a
      // socket-connection failure that would ALSO leave handoff_acks
      // trivially empty and degraded.sections populated for an unrelated
      // reason.
      assert.equal(
        calls.filter((c) => c.method === "minni_list_pending_handoffs").length,
        1,
        "the list RPC must actually have been attempted",
      );

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
      // Prove the daemon connection actually worked (codex's other boot RPCs
      // landed) — otherwise "0 list calls" could just as easily mean the
      // socket never connected at all, which would pass this assertion
      // vacuously regardless of whether the knob-off gate works.
      assert.ok(calls.length > 0, "codex must actually reach the daemon for its other boot RPCs");
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
    // "hook_compact_harvest" alone doesn't distinguish a real harvest from
    // the empty_summary no-op branch (both share the same tool name). Only
    // the success branch's audit details carry summary_sha1; the
    // empty_summary branch's details carry `"reason": "empty_summary"` and
    // nothing else. Assert both to pin the actual success path, not just
    // "some compact_harvest audit row exists".
    assert.match(tail.text, /summary_sha1/, "a real harvest must record the content-dedup key");
    assert.doesNotMatch(
      tail.text,
      /empty_summary/,
      "an 80-char summary must not take the too-short no-op branch",
    );
  });
});

test("PostCompact is a genuine no-op (not an unrouted-event drop) when the summary is empty", async () => {
  // harvestSummaryText's no_summary_found branch returns before writing
  // ANYTHING to the audit log — so a fresh fixture's tail is empty on this
  // path by design, not just free of an "hook_intent_dropped" string. Assert
  // the tail is exactly empty (review round 1: asserting doesNotMatch alone
  // against an already-empty tail is vacuous — it would pass identically if
  // the hook crashed before touching the vault at all, not just on the
  // intended no-op path). The "PostCompact is genuinely routed" claim itself
  // is covered positively by the harvest test above and by the mutant check
  // in this PR (removing the dispatch case makes both this and the harvest
  // test fail with hook_intent_dropped in the tail).
  await withFixture(async (fixture) => {
    const output = await runHook("PostCompact", BASE_ENV(fixture), { session_id: "pc-283-empty" });
    assert.equal(output.continue, true);

    const tail = await auditTail(fixture.vault, 20);
    assert.equal(
      tail.text.trim(),
      "",
      "an empty compact_summary must write no audit row at all (no_summary_found is a silent no-op by design)",
    );
  });
});

// ── SEC-006: filterSafeVaultResults must actually be applied ───────────────

test("UserPromptSubmit never surfaces a privacy:private vault note (SEC-006)", async () => {
  await withFixture(async (fixture) => {
    const dir = path.join(fixture.vault, "wiki", "concepts");
    await mkdir(dir, { recursive: true });
    const note = (name, privacy) =>
      writeFile(
        path.join(dir, name),
        `---\ntitle: ${name}\nprivacy: ${privacy}\nstatus: accepted\n---\n\n# ${name}\n\nshared hook283 sec006 marker phrase\n`,
        "utf8",
      );
    await note("safe-note.md", "safe");
    await note("private-note.md", "private");

    const output = await runHook("UserPromptSubmit", BASE_ENV(fixture), {
      prompt: "shared hook283 sec006 marker phrase",
    });
    assert.equal(output.continue, true);

    // Whether or not this turn crosses the "strong recall" threshold, the
    // audit's vault_matches is built directly from the (post-filter)
    // vaultResults array on BOTH the salient and nothing-salient paths — the
    // most direct, threshold-independent signal that the filter actually ran.
    const tail = await auditTail(fixture.vault, 20);
    assert.match(tail.text, /safe-note\.md/, "the safe note must still be found and reported");
    assert.doesNotMatch(
      tail.text,
      /private-note\.md/,
      "SEC-006: a privacy:private note must never reach vault_matches, let alone the model-facing envelope",
    );

    // If this turn happened to cross the strong-recall threshold on the safe
    // note alone, the envelope's recall pointer / persisted recall-state must
    // not name the private note either.
    if (output.hookSpecificOutput?.additionalContext) {
      const body = envelopeJson(output.hookSpecificOutput.additionalContext);
      const pointer = body.recall_pointer ?? "";
      assert.doesNotMatch(
        pointer,
        /private-note/,
        "SEC-006: the private note must not leak into the model-facing recall pointer",
      );
    }
  });
});
