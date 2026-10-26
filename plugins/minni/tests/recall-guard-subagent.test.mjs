import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import { readRecallState, sessionStateKey, writeRecallState } from "../dist/recall-state.js";

async function makeTempDir(prefix) {
  return fsp.mkdtemp(path.join(os.tmpdir(), prefix));
}

function makeHandlers(vaultPath) {
  return createHookHandlers({
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "ws-subagent-test",
    contextWindow: 12000,
    hooksEnabled: true,
    auditPrefix: "hook",
    wire: claudeCodeWire,
  });
}

test("PreToolUse with agent_id returns allow and does NOT consume parent's armed RecallState", async () => {
  const vaultPath = await makeTempDir("minni-s2b-subagent-guard-");
  try {
    const handlers = makeHandlers(vaultPath);
    const sessionId = "sess-parent-subagent-01";
    await writeRecallState(
      vaultPath,
      {
        task_signature: "sig-parent-01",
        intent: "recall",
        top_score: 0.91,
        session_id: sessionId,
        top_hits: [
          {
            wikilink: "[[layer2/notes/parent_memory]]",
            title: "Parent Memory",
            score: 0.91,
            snippet: "Parent context hit",
          },
        ],
      },
      { sessionId, guardV2: true },
    );

    // Subagent tool call carries agent_id: must bypass parent guard and leave parent state unconsumed.
    const subagentOut = await handlers.handlePreToolUse({
      session_id: sessionId,
      agent_id: "subagent-worker-99",
      agent_type: "Explore",
      tool_name: "Grep",
      tool_input: { pattern: "foo" },
    });
    assert.deepEqual(subagentOut, { continue: true });

    const afterSubagent = await readRecallState(vaultPath, { sessionId, guardV2: true });
    assert.equal(afterSubagent?.consumed, false, "subagent PreToolUse must not consume parent RecallState");

    // Parent's own subsequent cold search still triggers the guard.
    const parentOut = await handlers.handlePreToolUse({
      session_id: sessionId,
      tool_name: "Grep",
      tool_input: { pattern: "foo" },
    });
    assert.equal(parentOut.hookSpecificOutput?.hookEventName, "PreToolUse");
    assert.ok(typeof parentOut.hookSpecificOutput?.additionalContext === "string");
    const afterParent = await readRecallState(vaultPath, { sessionId, guardV2: true });
    assert.equal(afterParent?.consumed, true);
  } finally {
    await fsp.rm(vaultPath, { recursive: true, force: true });
  }
});

test("PreToolUse on Agent and Task stashes prompt in subagent-pending.json and leaves parent RecallState unconsumed", async () => {
  const vaultPath = await makeTempDir("minni-s2b-agent-stash-");
  try {
    const handlers = makeHandlers(vaultPath);
    const sessionId = "sess-parent-stash-02";
    await writeRecallState(
      vaultPath,
      {
        task_signature: "sig-parent-02",
        intent: "recall",
        top_score: 0.88,
        session_id: sessionId,
        top_hits: [
          {
            wikilink: "[[layer2/notes/parent_hit]]",
            title: "Parent Hit",
            score: 0.88,
            snippet: "Parent hit snippet",
          },
        ],
      },
      { sessionId, guardV2: true },
    );

    const agentOut = await handlers.handlePreToolUse({
      session_id: sessionId,
      tool_name: "Agent",
      tool_use_id: "toolu_agent_01",
      tool_input: {
        subagent_type: "Explore",
        prompt: "Audit recall-state session isolation and claim files",
      },
    });
    assert.deepEqual(agentOut, { continue: true });

    const taskOut = await handlers.handlePreToolUse({
      session_id: sessionId,
      tool_name: "Task",
      tool_use_id: "toolu_task_02",
      tool_input: {
        subagent_type: "general-purpose",
        description: "Verify subagent brief token budget",
      },
    });
    assert.deepEqual(taskOut, { continue: true });

    const stateAfter = await readRecallState(vaultPath, { sessionId, guardV2: true });
    assert.equal(stateAfter?.consumed, false, "Agent/Task PreToolUse must not consume parent RecallState");

    const sessionKey = sessionStateKey(sessionId);
    const pendingPath = path.join(
      vaultPath,
      ".runtime",
      "recall-session.d",
      sessionKey,
      "subagent-pending.json",
    );
    assert.equal(fs.existsSync(pendingPath), true, "subagent-pending.json must be written");
    const parsed = JSON.parse(fs.readFileSync(pendingPath, "utf8"));
    assert.equal(parsed.v, 1);
    assert.equal(parsed.entries.length, 2);
    assert.equal(parsed.entries[0].tool_use_id, "toolu_agent_01");
    assert.equal(parsed.entries[0].subagent_type, "Explore");
    assert.match(parsed.entries[0].prompt, /Audit recall-state session isolation/);
    assert.equal(parsed.entries[1].tool_use_id, "toolu_task_02");
    assert.equal(parsed.entries[1].subagent_type, "general-purpose");
    assert.match(parsed.entries[1].prompt, /Verify subagent brief token budget/);
  } finally {
    await fsp.rm(vaultPath, { recursive: true, force: true });
  }
});
