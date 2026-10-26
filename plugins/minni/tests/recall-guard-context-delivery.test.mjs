// S2b (§6.3.4): PreToolUse context delivery on wires with preToolUseContext=true,
// fallback to permissionDecision:"deny" on other wires or when MINNI_RECALL_GUARD_DELIVERY=deny
// or MINNI_GUARD_V2=off, and 3-hit cap on context delivery.
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

async function withVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s2b-ctx-delivery-"));
  const vault = path.join(root, "claudecode-vault");
  await mkdir(vault, { recursive: true });
  try {
    await fn({ vault });
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

test("MINNI_RECALL_GUARD_DELIVERY=deny forces permissionDecision:deny even on claude-code", async () => {
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
      assert.equal(out.hookSpecificOutput?.permissionDecision, "deny");
      assert.match(out.hookSpecificOutput?.permissionDecisionReason ?? "", /Re-issue this exact call/);
      assert.equal(out.hookSpecificOutput?.additionalContext, undefined);
    } finally {
      if (prev === undefined) delete process.env.MINNI_RECALL_GUARD_DELIVERY;
      else process.env.MINNI_RECALL_GUARD_DELIVERY = prev;
    }
  });
});

test("codex, kilocode, and agy (geminiWire) deny once per arming and allow on second call; MINNI_GUARD_V2=off falls back to deny", async () => {
  await withVault(async ({ vault }) => {
    for (const [label, wire] of [
      ["codex", codexWire],
      ["kilocode", kilocodeWire],
      ["agy", geminiWire],
    ]) {
      const sess = `sess-${label}`;
      await writeRecallState(vault, {
        task_signature: `sig-${label}`,
        intent: "recall",
        top_hits: FIVE_HITS.slice(0, 1),
        top_score: 0.95,
        session_id: sess,
      });

      const handlers = createHookHandlers({
        agentId: label,
        vaultPath: vault,
        defaultWorkspaceId: "ws",
        contextWindow: 200_000,
        auditPrefix: `hook_${label}`,
        hooksEnabled: true,
        recallGuardMode: "soft",
        wire,
      });

      const firstOut = await handlers.handlePreToolUse({
        session_id: sess,
        tool_name: "Read",
        tool_input: { file_path: "/tmp/a.ts" },
      });
      assert.equal(firstOut.hookSpecificOutput?.permissionDecision, "deny", `${label} must deny on first call`);

      const secondOut = await handlers.handlePreToolUse({
        session_id: sess,
        tool_name: "Read",
        tool_input: { file_path: "/tmp/a.ts" },
      });
      assert.equal(secondOut.hookSpecificOutput, undefined, `${label} must allow on second call`);
    }

    // Now test MINNI_GUARD_V2=off via .runtime/flags.json on claudeCodeWire
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
    assert.equal(v2OffOut.hookSpecificOutput?.permissionDecision, "deny");
  });
});

test("grok-build, cursor, and unprofiled wires declare preToolUseContext=false, and cursor/gemini/grok adapters map preToolUseContext shape to allow", () => {
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
