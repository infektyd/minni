// S2b (§6.3.4): Already-surfaced rule.
// In "soft" mode on the main thread with MINNI_GUARD_V2=on, when pointer_named
// is non-empty, the pointer was already delivered so the guard returns allow.
// In "strict" mode (or when pointer_named is empty / MINNI_GUARD_V2=off), it surfaces.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import { decideGuard } from "../dist/recall-guard.js";
import { writeRecallState } from "../dist/recall-state.js";

async function withVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s2b-already-surfaced-"));
  const vault = path.join(root, "claudecode-vault");
  await mkdir(vault, { recursive: true });
  try {
    await fn({ vault });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

const NAMED_STATE = {
  task_signature: "sig-named",
  intent: "recall",
  top_hits: [{ title: "Already Named Hit", wikilink: "[[wiki/already-named]]", score: 0.92 }],
  top_score: 0.92,
  pointer_named: ["[[wiki/already-named]]"],
  session_id: "sess-named",
};

test("soft mode on main thread allows without re-surfacing when pointer_named is non-empty", async () => {
  await withVault(async ({ vault }) => {
    await writeRecallState(vault, NAMED_STATE, { sessionId: "sess-named", guardV2: true });
    const handlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "ws",
      contextWindow: 200_000,
      auditPrefix: "hook_named",
      hooksEnabled: true,
      recallGuardMode: "soft",
      wire: claudeCodeWire,
    });

    const out = await handlers.handlePreToolUse({
      session_id: "sess-named",
      tool_name: "Read",
      tool_input: { file_path: "/tmp/some-file.ts" },
    });
    assert.equal(out.hookSpecificOutput, undefined, "soft mode must allow when pointer_named is non-empty");
  });
});

test("strict mode still surfaces even when pointer_named is non-empty", async () => {
  await withVault(async ({ vault }) => {
    await writeRecallState(vault, NAMED_STATE, { sessionId: "sess-named", guardV2: true });
    const handlers = createHookHandlers({
      agentId: "claude-code",
      vaultPath: vault,
      defaultWorkspaceId: "ws",
      contextWindow: 200_000,
      auditPrefix: "hook_named_strict",
      hooksEnabled: true,
      recallGuardMode: "strict",
      wire: claudeCodeWire,
    });

    const out = await handlers.handlePreToolUse({
      session_id: "sess-named",
      tool_name: "Read",
      tool_input: { file_path: "/tmp/some-file.ts" },
    });
    assert.equal(typeof out.hookSpecificOutput?.additionalContext, "string", "strict mode surfaces even when pointer_named is non-empty");
    assert.match(out.hookSpecificOutput.additionalContext, /Already Named Hit/);
  });
});

test("decideGuard unit: pointer_named empty or guardV2=false still surfaces in soft mode", () => {
  const baseState = {
    ...NAMED_STATE,
    consumed: false,
    ts: new Date().toISOString(),
  };

  assert.equal(
    decideGuard({
      state: baseState,
      mode: "soft",
      threshold: 0.45,
      toolName: "Grep",
      toolInput: { pattern: "x" },
      guardV2: true,
    }),
    "allow",
  );

  assert.equal(
    decideGuard({
      state: { ...baseState, pointer_named: [] },
      mode: "soft",
      threshold: 0.45,
      toolName: "Grep",
      toolInput: { pattern: "x" },
      guardV2: true,
    }),
    "deny",
  );

  assert.equal(
    decideGuard({
      state: baseState,
      mode: "soft",
      threshold: 0.45,
      toolName: "Grep",
      toolInput: { pattern: "x" },
      guardV2: false,
    }),
    "deny",
  );
});
