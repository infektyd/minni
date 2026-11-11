// Wave 1 (§2.1): Strictly non-blocking PreToolUse across ALL hosts.
// Hooks NEVER emit permissionDecision:"deny", { decision: "deny" }, { permission: "deny" },
// or throw a blocking Error on ANY host — even if MINNI_RECALL_GUARD_DELIVERY=deny or
// MINNI_GUARD_V2=off is set. Wires with preToolUseContext=true attach additionalContext
// (capped at 3 hits); other wires record non-blocking telemetry and return { continue: true }.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { adaptCursorOutput } from "../dist/cursor-adapter.js";
import { adaptPreToolUseOutput } from "../dist/gemini-adapter.js";
import { adaptGrokPreToolUseOutput } from "../dist/grok-adapter.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import {
  claudeCodeWire,
  codexWire,
  cursorWire,
  geminiWire,
  grokBuildWire,
  kilocodeWire,
  wireCapabilities,
  wireFor,
} from "../dist/hook-platform.js";
import { GUARD_CONTEXT_MAX_HITS, preToolUseContext } from "../dist/recall-guard.js";
import { readRecallState, writeRecallState } from "../dist/recall-state.js";
import MinniKiloPlugin from "../kilo/minni-plugin.js";

async function withVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s2b-ctx-delivery-"));
  const vault = path.join(root, "claudecode-vault");
  await mkdir(vault, { recursive: true });
  try {
    await fn({ root, vault });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

const FIVE_HITS = [
  { title: "Hit One", wikilink: "[[wiki/hit-1]]", score: 0.95 },
  { title: "Hit Two", wikilink: "[[wiki/hit-2]]", score: 0.90 },
  { title: "Hit Three", wikilink: "[[wiki/hit-3]]", score: 0.85 },
  { title: "Hit Four", wikilink: "[[wiki/hit-4]]", score: 0.80 },
  { title: "Hit Five", wikilink: "[[wiki/hit-5]]", score: 0.75 },
];

test("claude-code emits additionalContext without permissionDecision, marks consumed=true, and caps at 3 hits", async () => {
  await withVault(async ({ vault }) => {
    assert.equal(GUARD_CONTEXT_MAX_HITS, 3);
    await writeRecallState(vault, {
      task_signature: "sig-ctx",
      intent: "recall",
      top_hits: FIVE_HITS,
      top_score: 0.95,
      session_id: "sess-ctx",
    });

    const handlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "ws",
      contextWindow: 200_000,
      auditPrefix: "hook_ctx",
      hooksEnabled: true,
      recallGuardMode: "soft",
      wire: claudeCodeWire,
    });

    const out = await handlers.handlePreToolUse({
      session_id: "sess-ctx",
      tool_name: "Read",
      tool_input: { file_path: "/tmp/a.ts" },
    });

    assert.equal(out.continue, true);
    assert.equal(out.hookSpecificOutput?.hookEventName, "PreToolUse");
    assert.equal(out.hookSpecificOutput?.permissionDecision, undefined);
    assert.equal(out.hookSpecificOutput?.permissionDecisionReason, undefined);
    const ctx = out.hookSpecificOutput?.additionalContext ?? "";
    assert.match(ctx, /\[\[wiki\/hit-1\]\]/);
    assert.match(ctx, /\[\[wiki\/hit-2\]\]/);
    assert.match(ctx, /\[\[wiki\/hit-3\]\]/);
    assert.doesNotMatch(ctx, /\[\[wiki\/hit-4\]\]/, "4th hit must be excluded by 3-hit cap");
    assert.doesNotMatch(ctx, /\[\[wiki\/hit-5\]\]/, "5th hit must be excluded by 3-hit cap");

    const stateAfter = await readRecallState(vault, { sessionId: "sess-ctx", guardV2: true });
    assert.equal(stateAfter?.consumed, true, "context delivery must mark RecallState consumed=true");
  });
});

test("MINNI_RECALL_GUARD_DELIVERY=deny and MINNI_GUARD_V2=off NEVER produce permissionDecision:deny", async () => {
  await withVault(async ({ vault }) => {
    await writeRecallState(vault, {
      task_signature: "sig-deny-override",
      intent: "recall",
      top_hits: FIVE_HITS.slice(0, 2),
      top_score: 0.95,
      session_id: "sess-deny",
    });

    const prev = process.env.MINNI_RECALL_GUARD_DELIVERY;
    process.env.MINNI_RECALL_GUARD_DELIVERY = "deny";
    try {
      const handlers = createHookHandlers({
        agentId: "claude-code",
        vaultPath: vault,
        defaultWorkspaceId: "ws",
        contextWindow: 200_000,
        auditPrefix: "hook_ctx",
        hooksEnabled: true,
        recallGuardMode: "soft",
        wire: claudeCodeWire,
      });
      const out = await handlers.handlePreToolUse({
        session_id: "sess-deny",
        tool_name: "Grep",
        tool_input: { pattern: "foo" },
      });
      assert.equal(out.continue, true);
      assert.equal(out.hookSpecificOutput?.permissionDecision, undefined);
      assert.match(out.hookSpecificOutput?.additionalContext ?? "", /\[\[wiki\/hit-1\]\]/);
    } finally {
      if (prev === undefined) delete process.env.MINNI_RECALL_GUARD_DELIVERY;
      else process.env.MINNI_RECALL_GUARD_DELIVERY = prev;
    }

    // MINNI_GUARD_V2=off via .runtime/flags.json also stays strictly non-blocking
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    await writeFile(path.join(vault, ".runtime", "flags.json"), JSON.stringify({ MINNI_GUARD_V2: "off" }), "utf8");
    await writeRecallState(vault, {
      task_signature: "sig-v2-off",
      intent: "recall",
      top_hits: FIVE_HITS.slice(0, 1),
      top_score: 0.95,
    });
    const ccHandlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "ws",
      contextWindow: 200_000,
      auditPrefix: "hook_cc",
      hooksEnabled: true,
      recallGuardMode: "soft",
      wire: claudeCodeWire,
    });
    const v2OffOut = await ccHandlers.handlePreToolUse({
      tool_name: "Read",
      tool_input: { file_path: "/tmp/a.ts" },
    });
    assert.equal(v2OffOut.continue, true);
    assert.equal(v2OffOut.hookSpecificOutput?.permissionDecision, undefined);
    assert.match(v2OffOut.hookSpecificOutput?.additionalContext ?? "", /\[\[wiki\/hit-1\]\]/);
  });
});

test("Read, Grep, Glob, and Bash are NEVER denied across Claude Code, Gemini/Antigravity, Codex, Cursor, Grok, and KiloCode", async () => {
  await withVault(async ({ root, vault }) => {
    const prevDelivery = process.env.MINNI_RECALL_GUARD_DELIVERY;
    const prevKiloVault = process.env.MINNI_KILOCODE_VAULT_PATH;
    const prevHome = process.env.MINNI_HOME;
    const prevSocket = process.env.MINNI_SOCKET_PATH;
    process.env.MINNI_RECALL_GUARD_DELIVERY = "deny";
    process.env.MINNI_KILOCODE_VAULT_PATH = vault;
    process.env.MINNI_HOME = root;
    process.env.MINNI_SOCKET_PATH = path.join(root, "missing.sock");

    try {
      const tools = [
        ["Read", { file_path: "/tmp/a.ts" }],
        ["Grep", { pattern: "needle" }],
        ["Glob", { pattern: "**/*.ts" }],
        ["Bash", { command: "rg needle ." }],
      ];

      const hosts = [
        ["claude-code", claudeCodeWire],
        ["gemini", geminiWire],
        ["codex", codexWire],
        ["cursor", cursorWire],
        ["grok-build", grokBuildWire],
        ["kilocode", kilocodeWire],
      ];

      for (const [hostLabel, wire] of hosts) {
        for (const [toolName, toolInput] of tools) {
          const sess = `sess-${hostLabel}-${toolName}`;
          await writeRecallState(vault, {
            task_signature: `sig-${hostLabel}-${toolName}`,
            intent: "recall",
            top_hits: FIVE_HITS.slice(0, 2),
            top_score: 0.95,
            session_id: sess,
          });

          const handlers = createHookHandlers({
            agentId: hostLabel,
            vaultPath: vault,
            defaultWorkspaceId: "ws",
            contextWindow: 200_000,
            auditPrefix: `hook_${hostLabel}`,
            hooksEnabled: true,
            recallGuardMode: "strict",
            wire,
          });

          const out = await handlers.handlePreToolUse({
            session_id: sess,
            tool_name: toolName,
            tool_input: toolInput,
          });

          assert.equal(out.continue, true, `${hostLabel} ${toolName} must continue`);
          assert.equal(
            out.hookSpecificOutput?.permissionDecision,
            undefined,
            `${hostLabel} ${toolName} must never emit permissionDecision`,
          );

          if (wireCapabilities(wire).preToolUseContext) {
            assert.match(out.hookSpecificOutput?.additionalContext ?? "", /\[\[wiki\/hit-1\]\]/);
          } else {
            assert.equal(out.hookSpecificOutput, undefined, `${hostLabel} non-context wire returns plain allow`);
          }

          if (hostLabel === "gemini") {
            assert.deepEqual(adaptPreToolUseOutput(out), { decision: "allow" });
          } else if (hostLabel === "cursor") {
            assert.deepEqual(adaptCursorOutput("PreToolUse", out), { permission: "allow" });
          } else if (hostLabel === "grok-build") {
            assert.deepEqual(adaptGrokPreToolUseOutput(out), { decision: "allow" });
          }
        }
      }

      // Exercise real KiloCode plugin tool.execute.before bridge directly on Read/Grep/Glob/Bash:
      // must never throw a blocking Error even with armed recall-state and MINNI_RECALL_GUARD_DELIVERY=deny.
      const kiloPlugin = await MinniKiloPlugin({ directory: "/work/repo", client: {} });
      for (const [toolName, args] of tools) {
        const sess = `sess-kilo-live-${toolName}`;
        await writeRecallState(vault, {
          task_signature: `sig-kilo-live-${toolName}`,
          intent: "recall",
          top_hits: FIVE_HITS.slice(0, 1),
          top_score: 0.95,
          session_id: sess,
        });
        await assert.doesNotReject(
          () =>
            kiloPlugin["tool.execute.before"](
              { sessionID: sess, tool: toolName },
              { args },
            ),
          `KiloCode tool.execute.before must never throw on ${toolName}`,
        );
      }
    } finally {
      if (prevDelivery === undefined) delete process.env.MINNI_RECALL_GUARD_DELIVERY;
      else process.env.MINNI_RECALL_GUARD_DELIVERY = prevDelivery;
      if (prevKiloVault === undefined) delete process.env.MINNI_KILOCODE_VAULT_PATH;
      else process.env.MINNI_KILOCODE_VAULT_PATH = prevKiloVault;
      if (prevHome === undefined) delete process.env.MINNI_HOME;
      else process.env.MINNI_HOME = prevHome;
      if (prevSocket === undefined) delete process.env.MINNI_SOCKET_PATH;
      else process.env.MINNI_SOCKET_PATH = prevSocket;
    }
  });
});

test("grok-build, cursor, and unprofiled wires declare preToolUseContext=false, and cursor/gemini/grok adapters map preToolUseContext and legacy deny shapes to allow", () => {
  assert.equal(wireCapabilities(grokBuildWire).preToolUseContext, false);
  assert.equal(wireCapabilities(cursorWire).preToolUseContext, false);
  const unprofiled = wireFor("unprofiled-test-host");
  assert.equal(wireCapabilities(unprofiled).preToolUseContext, false);

  const ctxShape = preToolUseContext({
    task_signature: "sig",
    intent: "recall",
    top_hits: FIVE_HITS,
    top_score: 0.95,
    consumed: false,
    ts: new Date().toISOString(),
  });
  assert.ok(ctxShape.hookSpecificOutput?.additionalContext);
  assert.equal(ctxShape.hookSpecificOutput?.permissionDecision, undefined);

  assert.deepEqual(adaptCursorOutput("PreToolUse", ctxShape), { permission: "allow" });
  assert.deepEqual(adaptPreToolUseOutput(ctxShape), { decision: "allow" });
  assert.deepEqual(adaptGrokPreToolUseOutput(ctxShape), { decision: "allow" });
});
