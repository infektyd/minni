// S2b (§6.3.5): PreToolUse on minni_recall / minni_drill (bare or MCP-prefixed)
// returns allow, marks RecallState consumed, and claims the arming so a
// subsequent Read/Grep in the same turn is allowed without firing the guard.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import { isConsultTool } from "../dist/recall-guard.js";
import { readRecallState, writeRecallState } from "../dist/recall-state.js";

async function withVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s2b-consult-"));
  const vault = path.join(root, "claudecode-vault");
  await mkdir(vault, { recursive: true });
  try {
    await fn({ vault });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

const CONSULT_TOOL_NAMES = [
  "minni_recall",
  "minni_drill",
  "mcp__minni__minni_recall",
  "mcp__minni__minni_drill",
  "mcp__plugin_minni_minni__minni_recall",
  "mcp__plugin_minni_minni__minni_drill",
];

test("isConsultTool recognizes bare and MCP-prefixed minni_recall and minni_drill", () => {
  for (const name of CONSULT_TOOL_NAMES) {
    assert.equal(isConsultTool(name), true, `expected ${name} to be a consult tool`);
  }
  for (const nonConsult of ["Read", "Grep", "minni_learn", "mcp__minni__minni_status", "Agent"]) {
    assert.equal(isConsultTool(nonConsult), false, `expected ${nonConsult} not to be a consult tool`);
  }
});

test("PreToolUse on each consult tool consumes arming so a subsequent Read in the same turn is allowed", async () => {
  for (const consultTool of CONSULT_TOOL_NAMES) {
    await withVault(async ({ vault }) => {
      const sessionId = `sess-${consultTool.replace(/[^a-z0-9]/gi, "-")}`;
      await writeRecallState(
        vault,
        {
          task_signature: "sig-consult",
          intent: "recall",
          top_hits: [{ title: "Consulted Note", wikilink: "[[wiki/consulted]]", score: 0.94 }],
          top_score: 0.94,
          session_id: sessionId,
        },
        { sessionId, guardV2: true },
      );

      const handlers = createHookHandlers({
        agentId: "claude-code",
        vaultPath: vault,
        defaultWorkspaceId: "ws",
        contextWindow: 200_000,
        auditPrefix: "hook_consult",
        hooksEnabled: true,
        recallGuardMode: "strict",
        wire: claudeCodeWire,
      });

      const consultOut = await handlers.handlePreToolUse({
        session_id: sessionId,
        tool_name: consultTool,
        tool_input: { query: "consulted" },
      });
      assert.equal(consultOut.hookSpecificOutput, undefined, `${consultTool} must be allowed`);

      const stateAfterConsult = await readRecallState(vault, { sessionId, guardV2: true });
      assert.equal(stateAfterConsult?.consumed, true, `${consultTool} must flip consumed=true`);

      // Subsequent Read in the same turn must now be allowed without surfacing the guard.
      const readOut = await handlers.handlePreToolUse({
        session_id: sessionId,
        tool_name: "Read",
        tool_input: { file_path: "/tmp/target.ts" },
      });
      assert.equal(readOut.hookSpecificOutput, undefined, `Read after ${consultTool} must be allowed`);
    });
  }
});
