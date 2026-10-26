// S3a (§5.2.1, §5.3): UserPromptSubmit skip-class envelope handling.
// A `<task-notification>` prompt on UserPromptSubmit:
// - issues zero daemon recall / vault search queries
// - emits no `recall_pointer`
// - neither writes nor clears `recall-state`
// - still emits the active plan pointer (`active_thread_ref`) and `lifecycle` line
// - records `envelope_kind: "skip"` and `subkind: "task_notification"` in audit details
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlan } from "../dist/plan.js";
import { readRecallState, writeRecallState } from "../dist/recall-state.js";
import { auditTail } from "../dist/vault.js";
import {
  additionalContext,
  daemonResult,
  exists,
  makeFixture,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

function parseEnvelopeBody(output) {
  const ctx = additionalContext(output);
  const match = /<minni:context\b[^>]*>\s*([\s\S]*?)\s*<\/minni:context>/.exec(ctx);
  assert.ok(match, `expected <minni:context> in additionalContext, got: ${ctx}`);
  return JSON.parse(match[1]);
}

test("UPS skip envelope: no recall, no recall_pointer, no recall-state write/clear, plan pointer + lifecycle still ride", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "ups-envelope-skip");
  let searchCalls = 0;
  await startFakeDaemon(fixture, {
    search: () => {
      searchCalls += 1;
      return {
        agent_id: "claude-code",
        results: [
          daemonResult({
            source: "/elsewhere/deploy.md",
            docId: 41,
            wikilink: "[[wiki/deploy]]",
            title: "Deploy procedure",
            confidence: 0.99,
          }),
        ],
      };
    },
  });

  // Create an active plan in the vault so active_thread_ref must ride on UPS.
  const { plan } = await createPlan({
    goal: "Ship S3a classifier actions without breaking plan pointer",
    slices: [{ id: "s3a", title: "Wire UPS classifier actions" }],
    vaultPath: fixture.vault,
  });

  const sessionId = "skip-session-1";
  const taskNotificationPrompt =
    "<task-notification>\n<task-id>task-77</task-id>\n<status>completed</status>\n<summary>Deploy procedure check completed</summary>\n</task-notification>";

  // Case A: no pre-existing recall-state on disk -> no recall-state file created, zero search calls, no recall_pointer.
  const resA = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "pid-skip-a",
    prompt: taskNotificationPrompt,
  });
  assert.equal(resA.code, 0);
  assert.equal(searchCalls, 0, "skip prompt must not call daemon search");

  const bodyA = parseEnvelopeBody(resA.output);
  assert.equal(bodyA.recall_pointer, undefined, "skip prompt must emit no recall_pointer");
  assert.equal(bodyA.recall_state, undefined, "skip prompt must emit no recall_state");
  assert.ok(bodyA.active_thread_ref, "active_thread_ref must still ride on skip prompt");
  assert.equal(bodyA.active_thread_ref.plan_id, plan.plan_id);
  assert.equal(typeof bodyA.lifecycle, "string", "lifecycle line must still ride on skip prompt");

  const stateAfterA = await readRecallState(fixture.vault, { sessionId, guardV2: true });
  assert.equal(stateAfterA, null, "skip prompt must not create recall-state when none existed");
  assert.equal(
    await exists(`${fixture.vault}/.runtime/recall-state.json`),
    false,
    "legacy recall-state.json must not be created either",
  );

  // Case B: pre-existing armed recall-state on disk -> untouched (neither overwritten nor cleared),
  // even when the skip payload contains write-intent words ("Please remember this:").
  await writeRecallState(
    fixture.vault,
    {
      task_signature: "t-armed-prior",
      intent: "recall",
      top_hits: [{ title: "Deploy procedure", wikilink: "[[wiki/deploy]]", score: 0.99 }],
      top_score: 0.99,
      session_id: sessionId,
      prompt_id: "pid-prior-human",
    },
    { sessionId, guardV2: true },
  );

  const taskNotificationWithWriteWords =
    "<task-notification>\n<task-id>task-78</task-id>\n<status>completed</status>\n<summary>Please remember this: deploy procedure finished</summary>\n</task-notification>";

  const resB = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "pid-skip-b",
    prompt: taskNotificationWithWriteWords,
  });
  assert.equal(resB.code, 0);
  assert.equal(searchCalls, 0, "skip prompt must still make zero daemon search calls");

  const bodyB = parseEnvelopeBody(resB.output);
  assert.equal(bodyB.recall_pointer, undefined, "skip prompt must not emit recall_pointer even when prior state exists");
  assert.ok(bodyB.active_thread_ref, "active_thread_ref still rides");
  assert.equal(bodyB.active_thread_ref.plan_id, plan.plan_id);
  assert.equal(typeof bodyB.lifecycle, "string", "lifecycle line still rides");

  const stateAfterB = await readRecallState(fixture.vault, { sessionId, guardV2: true });
  assert.ok(stateAfterB, "pre-existing armed recall-state must NOT be cleared by a skip prompt");
  assert.equal(stateAfterB.consumed, false, "pre-existing armed recall-state must stay unconsumed");
  assert.equal(stateAfterB.task_signature, "t-armed-prior", "pre-existing recall-state signature must be untouched");
  assert.equal(stateAfterB.prompt_id, "pid-prior-human", "pre-existing recall-state prompt_id must be untouched");

  // Audit check: envelope_kind: "skip", subkind: "task_notification"
  const logRaw = await readFile(`${fixture.vault}/log.md`, "utf8");
  assert.match(logRaw, /"envelope_kind":\s*"skip"/);
  assert.match(logRaw, /"subkind":\s*"task_notification"/);

  // Also verify with MINNI_USE_LEDGER=off: identical behavior.
  const resOff = await runHook(
    fixture,
    "UserPromptSubmit",
    {
      session_id: sessionId,
      prompt_id: "pid-skip-ledger-off",
      prompt: taskNotificationPrompt,
    },
    { env: { MINNI_USE_LEDGER: "off" } },
  );
  assert.equal(resOff.code, 0);
  assert.equal(searchCalls, 0, "skip prompt with MINNI_USE_LEDGER=off must also make zero daemon search calls");
  const stateAfterOff = await readRecallState(fixture.vault, { sessionId, guardV2: true });
  assert.ok(stateAfterOff && stateAfterOff.consumed === false, "armed state still untouched with ledger off");

  // Kill switch control: MINNI_PROMPT_CLASSES=off restores recall on the prompt.
  const resKillSwitch = await runHook(
    fixture,
    "UserPromptSubmit",
    {
      session_id: sessionId,
      prompt_id: "pid-skip-killswitch",
      prompt: "<task-notification>\n<status>check deploy procedure</status>\n</task-notification>",
    },
    { env: { MINNI_PROMPT_CLASSES: "off" } },
  );
  assert.equal(resKillSwitch.code, 0);
  assert.ok(searchCalls > 0, "with MINNI_PROMPT_CLASSES=off, daemon search runs as before");

  // Suspicious prefix audit check: human prompt starting like a harness tag stamps suspicious_prefix: true
  const resSuspicious = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "pid-suspicious",
    prompt: "<unrecognized-harness-tag> check deploy procedure",
  });
  assert.equal(resSuspicious.code, 0);
  const logAfterSuspicious = await readFile(`${fixture.vault}/log.md`, "utf8");
  assert.match(logAfterSuspicious, /"suspicious_prefix":\s*true/);
  const tail = await auditTail(fixture.vault, 5);
  assert.ok(tail.entries.length > 0);

  // Direct in-process handleUserPromptSubmit (use === undefined) records distinct
  // sessionStateKey prefixes even when session_ids share an 8-char prefix or contain ':'.
  const { createHookHandlers } = await import("../dist/hook-handlers.js");
  const { readPromptTemplates, templateHash } = await import("../dist/prompt-class.js");
  const directHandlers = createHookHandlers({
    agentId: "claude-code",
    vaultPath: fixture.vault,
    defaultWorkspaceId: "ws-direct",
    contextWindow: 200_000,
    hooksEnabled: true,
    runtime: "claude-code",
    auditPrefix: "hook",
  });
  const schedPrompt =
    "Recurring check-in 100: verify all deployment queues and confirm health status across clusters.";
  await directHandlers.handleUserPromptSubmit({
    session_id: "claude:session:001",
    prompt_id: "direct-p1",
    prompt: schedPrompt,
  });
  await directHandlers.handleUserPromptSubmit({
    session_id: "claude:session:002",
    prompt_id: "direct-p2",
    prompt: schedPrompt,
  });
  const counts = await readPromptTemplates(fixture.vault);
  const schedEntry = counts?.templates[templateHash(schedPrompt)];
  assert.ok(schedEntry, "direct handleUserPromptSubmit must record eligible template occurrence");
  assert.equal(
    Object.keys(schedEntry.sessions).length,
    2,
    "sessions sharing 'claude:s' prefix must hash to 2 distinct sessionStateKey prefixes",
  );
});
