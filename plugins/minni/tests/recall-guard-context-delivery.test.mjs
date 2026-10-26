// S2b (§6.3.4): PreToolUse context delivery on wires with preToolUseContext=true,
// fallback to permissionDecision:"deny" on other wires or when MINNI_RECALL_GUARD_DELIVERY=deny
// or MINNI_GUARD_V2=off, and 3-hit cap on context delivery.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire, codexWire } from "../dist/hook-platform.js";
import { GUARD_CONTEXT_MAX_HITS, preToolUseContext } from "../dist/recall-guard.js";
import { writeRecallState } from "../dist/recall-state.js";

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

test("claude-code emits additionalContext without permissionDecision and caps at 3 hits", async () => {
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

test("non-preToolUseContext wire (codexWire) and MINNI_GUARD_V2=off fallback to permissionDecision:deny", async () => {
  await withVault(async ({ vault }) => {
    await writeRecallState(vault, {
      task_signature: "sig-codex",
      intent: "recall",
      top_hits: FIVE_HITS.slice(0, 1),
      top_score: 0.95,
      session_id: "sess-codex",
    });

    const codexHandlers = createHookHandlers({
      agentId: "codex",
      vaultPath: vault,
      defaultWorkspaceId: "ws",
      contextWindow: 200_000,
      auditPrefix: "hook_codex",
      hooksEnabled: true,
      recallGuardMode: "soft",
      wire: codexWire,
    });
    const codexOut = await codexHandlers.handlePreToolUse({
      session_id: "sess-codex",
      tool_name: "Read",
      tool_input: { file_path: "/tmp/a.ts" },
    });
    assert.equal(codexOut.hookSpecificOutput?.permissionDecision, "deny");

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
    assert.ok(preToolUseContext({
      task_signature: "sig",
      intent: "recall",
      top_hits: FIVE_HITS,
      top_score: 0.95,
      consumed: false,
      ts: new Date().toISOString(),
    }).hookSpecificOutput?.additionalContext);
  });
});
