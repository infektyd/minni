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

test("SessionStart handoff acking now runs for every platform sharing the factory (codex) (#296)", async () => {
  // #296: was claude-only before this fix — an unstated cross-platform
  // inconsistency, not a deliberate design choice (see the issue). Every
  // platform's shim now sets ackPendingHandoffsAtBoot: true, so codex
  // (picked as the representative non-claude platform) must behave exactly
  // like claude's own "acks a real pending handoff lease" test above.
  await withFixture(async (fixture) => {
    const { server, calls } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respond }) => {
        respond({
          handoffs: [
            { lease_id: "handoff-296-abc", from_agent: "claude-code", to_agent: "codex", task: "test" },
          ],
        });
      },
      minni_ack_handoff: (request, { respond }) => {
        assert.equal(request.params.lease_id, "handoff-296-abc");
        assert.equal(request.params.status, "accepted");
        assert.equal(request.params.agent_id, "codex", "ack must stamp codex's own server-side identity");
        respond({ lease_id: request.params.lease_id, status: "accepted", updated_paths: [] });
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
      assert.deepEqual(body.handoff_acks, ["handoff-296-abc"]);
      assert.equal(body.degraded, undefined, "a clean ack must not degrade anything");
      assert.equal(
        calls.filter((c) => c.method === "minni_ack_handoff").length,
        1,
        "exactly one ack RPC must have been sent",
      );
    } finally {
      server.close();
    }
  });
});

test("SessionStart handoff acking deliberately does NOT run for grok-build (#296 carve-out)", async () => {
  // grok-build is the one platform sharing this factory that does NOT set
  // ackPendingHandoffsAtBoot (see grok-hook.ts's CONFIG comment): its wire
  // can only inject/note at Stop, so acking a lease at SessionStart would
  // tell the sending agent "grok accepted this" via the lease store before
  // grok has ever actually surfaced the content anywhere it could act on
  // it — a false acceptance signal. Confirms the carve-out is real and
  // load-bearing, not just documented.
  await withFixture(async (fixture) => {
    const { server, calls } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respond }) => {
        respond({ handoffs: [{ lease_id: "should-not-be-acked", to_agent: "grok-build" }] });
      },
    });
    try {
      const output = await runHook("SessionStart", {
        ...BASE_ENV(fixture),
        MINNI_GROK_VAULT_PATH: fixture.vault,
        MINNI_GROK_AGENT_ID: "grok-build",
        MINNI_GROK_HOOKS: "on",
      }, {}, "grok-hook.js");
      // grokBuildWire cannot inject at SessionStart at all — the real
      // observable output is the bare no-op, not an envelope.
      assert.deepEqual(output, { continue: true });
      assert.ok(calls.length > 0, "grok must actually reach the daemon for its other boot RPCs");
      assert.equal(
        calls.filter((c) => c.method === "minni_list_pending_handoffs").length,
        0,
        "grok must never even call minni_list_pending_handoffs",
      );
      assert.equal(
        calls.filter((c) => c.method === "minni_ack_handoff").length,
        0,
        "grok must never ack a lease it cannot show the model",
      );
    } finally {
      server.close();
    }
  });
});

test("ackPendingHandoffsAtBoot still gates correctly when a config genuinely omits it", async () => {
  // Regression guard for the underlying MECHANISM, now that no shipped
  // platform config exercises the off state: createHookHandlers itself must
  // still respect an omitted ackPendingHandoffsAtBoot, so a future change
  // can't silently make the ack sweep unconditional regardless of config.
  //
  // Must run as a genuinely separate PROCESS, not an in-process
  // createHookHandlers() call in this test file: config.ts's MINNI_HOME /
  // socket-path constants are computed ONCE at module import time from
  // process.env, and this test file's own top-level `import { auditTail,
  // ensureVault } from "../dist/vault.js"` transitively imports config.js
  // before any test body runs — so setting process.env.MINNI_SOCKET_PATH
  // inside a test has no effect on the already-frozen constant. (Caught by
  // running this exact scenario in-process first: the fake daemon received
  // ZERO calls of ANY kind, not just no ack call — the RPC layer never
  // reached it at all, which would have made this test pass for the wrong
  // reason regardless of whether the knob gate actually worked.) A driver
  // script run as its own `node` process gets its env at process start,
  // same as every other subprocess-based test in this file.
  await withFixture(async (fixture) => {
    const { server, calls } = await startFakeDaemon(fixture.socketPath, {
      minni_list_pending_handoffs: (_request, { respond }) => {
        respond({ handoffs: [{ lease_id: "should-not-be-seen", to_agent: "synthetic-agent" }] });
      },
    });
    const driverPath = path.join(fixture.home, "driver.mjs");
    await writeFile(
      driverPath,
      [
        `import { createHookHandlers } from ${JSON.stringify(path.join(PLUGIN_ROOT, "dist", "hook-handlers.js"))};`,
        "const handlers = createHookHandlers({",
        '  agentId: "synthetic-agent",',
        `  vaultPath: ${JSON.stringify(fixture.vault)},`,
        '  defaultWorkspaceId: "default",',
        "  contextWindow: 200000,",
        "  hooksEnabled: true,",
        '  auditPrefix: "hook_synthetic",',
        "  // ackPendingHandoffsAtBoot deliberately omitted.",
        "});",
        'const output = await handlers.handleSessionStart({ session_id: "s296" });',
        "process.stdout.write(JSON.stringify(output) + \"\\n\");",
      ].join("\n"),
      "utf8",
    );
    try {
      const output = await new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [driverPath], {
          env: {
            ...process.env,
            MINNI_HOME: fixture.home,
            MINNI_SOCKET_PATH: fixture.socketPath,
            MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
            MINNI_BYPASS_AUDIT_LIMIT: "true",
          },
          stdio: ["ignore", "pipe", "pipe"],
        });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (chunk) => (stdout += chunk));
        child.stderr.on("data", (chunk) => (stderr += chunk));
        const timer = setTimeout(() => {
          child.kill("SIGKILL");
          reject(new Error(`driver timed out; stderr=${stderr}`));
        }, 30_000);
        child.on("close", () => {
          clearTimeout(timer);
          try {
            resolve(JSON.parse(stdout.trim().split("\n").pop() ?? ""));
          } catch {
            reject(new Error(`unparseable driver output: ${stdout} / ${stderr}`));
          }
        });
      });
      assert.equal(output.continue, true);
      assert.equal(
        calls.filter((c) => c.method === "minni_list_pending_handoffs").length,
        0,
        "a config that omits the knob must never call minni_list_pending_handoffs",
      );
      // Confirms the daemon connection genuinely worked for OTHER boot RPCs
      // — otherwise "0 list calls" could just as easily mean the RPC layer
      // never reached the fake daemon at all (exactly the failure mode this
      // test's own comment above describes catching during development).
      assert.ok(calls.length > 0, "the driver must actually reach the daemon for its other boot RPCs");
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

test("UserPromptSubmit never surfaces a frontmatter-private vault note (SEC-006)", async () => {
  // Review round 2: the file/title MUST NOT contain any word
  // heuristicPrivacyForSource's regexes key on ("private", "raw session",
  // "secret", "token", "/users/", ...) — a fixture named e.g.
  // "private-note.md" gets excluded by the STRING HEURISTIC regardless of
  // whether frontmatter-authored privacy is parsed/applied at all, so it
  // cannot prove this test's actual claim (that the FRONTMATTER `privacy:`
  // field itself is respected). Neutral constellation names isolate the
  // frontmatter path specifically — a broken/no-op privacy parser passes the
  // heuristic-named case but fails this one.
  await withFixture(async (fixture) => {
    const dir = path.join(fixture.vault, "wiki", "concepts");
    await mkdir(dir, { recursive: true });
    const note = (name, privacy) =>
      writeFile(
        path.join(dir, name),
        `---\ntitle: ${name}\nprivacy: ${privacy}\nstatus: accepted\n---\n\n# ${name}\n\nshared hook283 sec006 marker phrase\n`,
        "utf8",
      );
    await note("borealis-topic.md", "safe");
    await note("atlas-topic.md", "private");
    await note("cygnus-topic.md", "local-only");

    const output = await runHook("UserPromptSubmit", BASE_ENV(fixture), {
      prompt: "shared hook283 sec006 marker phrase",
    });
    assert.equal(output.continue, true);

    // Whether or not this turn crosses the "strong recall" threshold, the
    // audit's vault_matches is built directly from the (post-filter)
    // vaultResults array on BOTH the salient and nothing-salient paths — the
    // most direct, threshold-independent signal that the filter actually ran.
    const tail = await auditTail(fixture.vault, 20);
    assert.match(tail.text, /borealis-topic\.md/, "the safe note must still be found and reported");
    assert.doesNotMatch(
      tail.text,
      /atlas-topic\.md/,
      "SEC-006: a privacy:private note must never reach vault_matches, let alone the model-facing envelope",
    );
    assert.doesNotMatch(
      tail.text,
      /cygnus-topic\.md/,
      "SEC-006: a privacy:local-only note must never reach vault_matches either",
    );

    // At equal vault-match scores the safe note alone still clears the
    // strong-recall threshold, so this branch is expected to run every time
    // — assert that positively (not just "if it happens to run") so the
    // check can't quietly rot into a no-op if scoring ever shifts.
    assert.ok(
      output.hookSpecificOutput?.additionalContext,
      "expected this turn to cross the strong-recall threshold on the safe note alone",
    );
    const body = envelopeJson(output.hookSpecificOutput.additionalContext);
    const pointer = body.recall_pointer ?? "";
    assert.match(pointer, /borealis-topic/, "the safe note's pointer must actually be present");
    assert.doesNotMatch(
      pointer,
      /atlas-topic|cygnus-topic/,
      "SEC-006: neither non-safe note may leak into the model-facing recall pointer",
    );
  });
});

test("UserPromptSubmit (#313): a private-heavy vault must not crowd a lower-ranked safe match out entirely", async () => {
  // searchVaultNotes scores and sorts across the WHOLE vault before slicing
  // to its `limit` argument — privacy is not part of that ordering (it only
  // drops `blocked` notes internally; `private`/`local-only` ride along).
  // Before the fix, this call site asked for exactly 6, so a vault where 6+
  // non-safe notes outscore a genuinely safe match meant the safe note never
  // even reached filterSafeVaultResults — not a leak (nothing unsafe
  // escaped) but a silent availability gap: the agent should have received
  // that memory and didn't. Eight decoys here each outrank the single safe
  // note (title repeats the query phrase for the +3-per-term title bonus the
  // safe note's plain title doesn't get), so under the pre-fix top-6 raw
  // fetch, all six of vaultResultsRaw's slots would have gone to decoys and
  // the safe note would never surface at all.
  await withFixture(async (fixture) => {
    const dir = path.join(fixture.vault, "wiki", "concepts");
    await mkdir(dir, { recursive: true });
    const phrase = "shared hook313 topn crowd marker phrase";
    for (let i = 0; i < 8; i++) {
      await writeFile(
        path.join(dir, `decoy-${i}.md`),
        `---\ntitle: Decoy ${phrase}\nprivacy: private\nstatus: accepted\n---\n\n# Decoy\n\n${phrase} ${phrase} ${phrase}\n`,
        "utf8",
      );
    }
    await writeFile(
      path.join(dir, "outranked-safe-note.md"),
      `---\ntitle: Outranked topic\nprivacy: safe\nstatus: accepted\n---\n\n# Outranked topic\n\n${phrase}\n`,
      "utf8",
    );

    const output = await runHook("UserPromptSubmit", BASE_ENV(fixture), {
      prompt: phrase,
    });
    assert.equal(output.continue, true);

    const tail = await auditTail(fixture.vault, 30);
    // Parse the actual vault_matches array rather than pattern-matching the
    // raw audit text — a filename substring could in principle match some
    // other field, so pin the assertion to the field this fix targets.
    const entry = tail.entries.find((e) => e.includes("hook_user_prompt_submit"));
    assert.ok(entry, "expected a hook_user_prompt_submit audit entry for this turn");
    const jsonBlock = entry.match(/```json\n([\s\S]*?)\n```/)?.[1];
    assert.ok(jsonBlock, "expected the audit entry to carry a JSON details block");
    const details = JSON.parse(jsonBlock);
    assert.ok(
      Array.isArray(details.vault_matches) && details.vault_matches.includes("wiki/concepts/outranked-safe-note.md"),
      `#313: the safe note must still surface even though 8 higher-scored private notes outrank it (got vault_matches: ${JSON.stringify(details.vault_matches)})`,
    );
    for (let i = 0; i < 8; i++) {
      assert.ok(
        !details.vault_matches.includes(`wiki/concepts/decoy-${i}.md`),
        `SEC-006: decoy-${i}.md is privacy:private and must never reach vault_matches`,
      );
    }
  });
});

test("UserPromptSubmit never surfaces a note the PRIVACY HEURISTIC flags (no authored privacy field)", async () => {
  // Complementary to the frontmatter-isolated test above: a note with NO
  // `privacy:` frontmatter at all still gets excluded when its content trips
  // heuristicPrivacyForSource's defense-in-depth string match (task.ts).
  //
  // Deliberately uses "raw session content" (-> heuristic level "private",
  // /private|raw session|session content/), NOT a secrets-style phrase like
  // "api_key"/"password" (-> heuristic level "blocked"). "blocked" results
  // are already excluded a layer earlier, inside searchVaultNotes itself
  // (see security-floor.test.mjs) — using that phrase here would test the
  // WRONG layer and pass even if filterSafeVaultResults (the function round
  // 1 fixed) were deleted entirely.
  await withFixture(async (fixture) => {
    const dir = path.join(fixture.vault, "wiki", "concepts");
    await mkdir(dir, { recursive: true });
    await writeFile(
      path.join(dir, "deploy-notes.md"),
      "# Deploy notes\n\nshared hook283 heuristic marker phrase — raw session content from a debug run\n",
      "utf8",
    );
    await writeFile(
      path.join(dir, "onboarding.md"),
      "# Onboarding\n\nshared hook283 heuristic marker phrase for new teammates\n",
      "utf8",
    );

    const output = await runHook("UserPromptSubmit", BASE_ENV(fixture), {
      prompt: "shared hook283 heuristic marker phrase",
    });
    assert.equal(output.continue, true);

    const tail = await auditTail(fixture.vault, 20);
    assert.match(tail.text, /onboarding\.md/, "the unflagged note must still be found and reported");
    assert.doesNotMatch(
      tail.text,
      /deploy-notes\.md/,
      "SEC-006: a note whose content trips the sensitive-content heuristic must never reach vault_matches",
    );
  });
});

// ── #312: handoff_context (SessionStart) privacy gate ───────────────────────

test("SessionStart's handoff_context never surfaces a privacy:private note's body text (#312)", async () => {
  // End-to-end version of the vault.test.mjs unit test for the same fix:
  // resolveInboxHandoffContext feeds SessionStart's envelope handoff_context
  // field with no filtering in between (a 1:1 map in hook-handlers.ts), so
  // this proves the gate holds all the way out to the real, model-facing
  // envelope a platform actually receives — not just the function in
  // isolation. Missing daemon socket (BASE_ENV) is fine: resolving a
  // handoff's wikilink_refs is pure local filesystem I/O, no RPC involved.
  await withFixture(async (fixture) => {
    const decisionDir = path.join(fixture.vault, "wiki", "decisions");
    await mkdir(decisionDir, { recursive: true });
    await writeFile(
      path.join(decisionDir, "safe-migration.md"),
      "---\ntitle: Safe Migration\nprivacy: safe\n---\n\nHandoff boot priming, safe note.",
      "utf8",
    );
    await writeFile(
      path.join(decisionDir, "private-migration.md"),
      "---\ntitle: Private Migration\nprivacy: private\n---\n\nHandoff boot priming, CONFIDENTIAL-296B body text.",
      "utf8",
    );
    const inboxDir = path.join(fixture.vault, "inbox");
    await mkdir(inboxDir, { recursive: true });
    // Fresh timestamp, not a hardcoded one: SessionStart TTL-reaps stale file
    // handoffs BEFORE reading the inbox (expireStaleInboxHandoffs runs
    // first) — an old-dated fixture gets silently archived as expired
    // before resolveInboxHandoffContext ever sees it, which would make this
    // test fail for the wrong reason entirely.
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "") + "Z";
    await writeFile(
      path.join(inboxDir, `${stamp}-mixed-handoff.json`),
      JSON.stringify({
        kind: "handoff",
        wikilink_refs: [
          "wiki/decisions/safe-migration",
          "wiki/decisions/private-migration",
        ],
      }),
      "utf8",
    );

    const output = await runHook("SessionStart", BASE_ENV(fixture));
    const body = envelopeJson(output.hookSpecificOutput.additionalContext);

    const refs = (body.handoff_context ?? []).map((entry) => entry.ref);
    assert.ok(refs.includes("wiki/decisions/safe-migration"), "the safe note must still resolve");
    assert.ok(
      !refs.includes("wiki/decisions/private-migration"),
      "SEC (#312): a privacy:private note's ref must not resolve in the real envelope either",
    );
    const rawEnvelope = JSON.stringify(body);
    assert.ok(
      !rawEnvelope.includes("CONFIDENTIAL-296B"),
      "SEC (#312): the private note's body text must not appear anywhere in the SessionStart envelope",
    );
  });
});
