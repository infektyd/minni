// S2b (§6.3.1): session-scoped RecallState under <vault>/.runtime/recall-state.d/<session_key>.json.
// Arming in session A never fires or gets cleared by session B; unlabeled sessions stay isolated.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { claudeCodeWire } from "../dist/hook-platform.js";
import {
  clearRecallState,
  markRecallConsumed,
  readRecallState,
  recallStateSessionPath,
  sessionStateKey,
  writeRecallState,
} from "../dist/recall-state.js";

async function withVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s2b-session-scope-"));
  const vault = path.join(root, "claudecode-vault");
  await mkdir(vault, { recursive: true });
  try {
    await fn({ root, vault });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function makeConfig(vault) {
  return {
    agentId: "claude-code",
    vaultPath: vault,
    defaultWorkspaceId: "ws-s2b",
    contextWindow: 200_000,
    auditPrefix: "hook_s2b",
    hooksEnabled: true,
    recallGuardMode: "soft",
    wire: claudeCodeWire,
  };
}

const HITS_A = [{ title: "Session A Note", wikilink: "[[wiki/session-a]]", score: 0.91 }];
const HITS_B = [{ title: "Session B Note", wikilink: "[[wiki/session-b]]", score: 0.88 }];

test("session A arming does not fire in session B or unlabeled session, and session B weak turn does not clear session A", async () => {
  await withVault(async ({ vault }) => {
    const handlers = createHookHandlers(makeConfig(vault));

    await writeRecallState(
      vault,
      {
        task_signature: "sig-a",
        intent: "recall",
        top_hits: HITS_A,
        top_score: 0.91,
        session_id: "session-alpha",
      },
      { sessionId: "session-alpha", guardV2: true },
    );

    const sessionAPath = recallStateSessionPath(vault, "session-alpha");
    assert.ok(sessionAPath && sessionAPath.endsWith(`${sessionStateKey("session-alpha")}.json`));

    // Session B PreToolUse must NOT see Session A's armed state.
    const bOut = await handlers.handlePreToolUse({
      session_id: "session-beta",
      tool_name: "Read",
      tool_input: { file_path: "/tmp/file.txt" },
    });
    assert.equal(bOut.hookSpecificOutput, undefined, "Session B must allow when only Session A is armed");

    // Unlabeled PreToolUse must NOT see Session A's armed state.
    const unlabeledOut = await handlers.handlePreToolUse({
      tool_name: "Grep",
      tool_input: { pattern: "foo" },
    });
    assert.equal(unlabeledOut.hookSpecificOutput, undefined, "unlabeled PreToolUse must not read Session A state");

    // Session B clearing state (e.g. weak UPS) must NOT clear Session A's state.
    await clearRecallState(vault, { sessionId: "session-beta", guardV2: true });
    const stateAStillThere = await readRecallState(vault, { sessionId: "session-alpha", guardV2: true });
    assert.ok(stateAStillThere, "Session A state must survive Session B clearRecallState");
    assert.equal(stateAStillThere.consumed, false);

    // Session A's own PreToolUse surfaces once and consumes Session A's state.
    const aOut = await handlers.handlePreToolUse({
      session_id: "session-alpha",
      tool_name: "Read",
      tool_input: { file_path: "/tmp/file.txt" },
    });
    assert.equal(typeof aOut.hookSpecificOutput?.additionalContext, "string", "Session A PreToolUse surfaces");
    assert.match(aOut.hookSpecificOutput.additionalContext, /Session A Note/);

    const stateAAfter = await readRecallState(vault, { sessionId: "session-alpha", guardV2: true });
    assert.equal(stateAAfter?.consumed, true);
  });
});

test("two concurrent sessions maintain independent arming and consumption lifecycles", async () => {
  await withVault(async ({ vault }) => {
    await writeRecallState(
      vault,
      {
        task_signature: "sig-a",
        intent: "recall",
        top_hits: HITS_A,
        top_score: 0.91,
        session_id: "sess-1",
      },
      { sessionId: "sess-1", guardV2: true },
    );
    await writeRecallState(
      vault,
      {
        task_signature: "sig-b",
        intent: "recall",
        top_hits: HITS_B,
        top_score: 0.88,
        session_id: "sess-2",
      },
      { sessionId: "sess-2", guardV2: true },
    );

    await markRecallConsumed(vault, { sessionId: "sess-1", guardV2: true });
    const s1 = await readRecallState(vault, { sessionId: "sess-1", guardV2: true });
    const s2 = await readRecallState(vault, { sessionId: "sess-2", guardV2: true });
    assert.equal(s1?.consumed, true);
    assert.equal(s2?.consumed, false, "consuming sess-1 must leave sess-2 unconsumed");
    assert.equal(s2?.top_hits[0]?.title, "Session B Note");
  });
});

test("symlink escape on recall-state.d is refused by containment checks", async () => {
  await withVault(async ({ root, vault }) => {
    const outside = await mkdtemp(path.join(root, "outside-"));
    const runtimeDir = path.join(vault, ".runtime");
    await mkdir(runtimeDir, { recursive: true });
    await symlink(outside, path.join(runtimeDir, "recall-state.d"));

    await assert.rejects(() =>
      writeRecallState(
        vault,
        {
          task_signature: "sig-escape",
          intent: "recall",
          top_hits: HITS_A,
          top_score: 0.91,
          session_id: "sess-escape",
        },
        { sessionId: "sess-escape", guardV2: true },
      ),
    );
    const readEscaped = await readRecallState(vault, { sessionId: "sess-escape", guardV2: true });
    assert.equal(readEscaped, null);
  });
});
