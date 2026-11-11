// Wave 1 (§2.2): Early return & paired envelope skip rules for <task-notification> / system wakeups.
// When isSkip === true (<task-notification>, <SYSTEM_MESSAGE>, The following is a <SYSTEM_MESSAGE>,
// [Message] timestamp=, <background-task), handleUserPromptSubmit returns { continue: true }
// IMMEDIATELY before resolveActivePlanView (withThreadLock), recordAudit (hook_user_prompt_submit),
// or <minni:context> / <MINNI_ENVELOPE> injection, and leaves armed recall-state untouched.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlan } from "../dist/plan.js";
import { readRecallState, writeRecallState } from "../dist/recall-state.js";
import { withThreadLock } from "../dist/thread-lock.js";
import { auditTail } from "../dist/vault.js";
import {
  additionalContext,
  daemonResult,
  exists,
  makeFixture,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

test("UPS skip wakeups return { continue: true } immediately with zero plan lock, zero audit spam, and zero envelope injection", { timeout: 120_000 }, async (t) => {
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

  // Create an active plan in the vault so non-skip prompts would touch the plan lock + emit active_thread_ref.
  const { plan } = await createPlan(
    {
      goal: "Ship Wave 1 wakeup skip early return without touching plan lock",
      slices: [{ id: "s1b", title: "Immediate return on system wakeups" }],
      vaultPath: fixture.vault,
    },
    { vaultPath: fixture.vault },
  );

  const sessionId = "skip-session-1";
  const wakeupPayloads = [
    "<task-notification>\n<task-id>task-77</task-id>\n<status>completed</status>\n<summary>Deploy procedure check completed</summary>\n</task-notification>",
    "Result ready:\n<task-notification>\n<task-id>task-79</task-id>\n<status>completed</status>\n</task-notification>",
    "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-11T02:24:21Z sender=subagent-1 content=done\n</SYSTEM_MESSAGE>",
    "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n<SYSTEM_MESSAGE>Task finished</SYSTEM_MESSAGE>",
    "[Message] timestamp=2026-11-11T02:24:21Z sender=worker-2 priority=HIGH content=Completed check",
    "<background-task id=\"bg-42\" status=\"completed\">All checks green</background-task>",
  ];

  // Case A: Hold the active plan lock exclusively while firing all wakeup skip variants!
  // If handleUserPromptSubmit attempted resolveActivePlanView (withThreadLock), it would stall/contend.
  await withThreadLock(fixture.vault, plan.plan_id, "ups-skip-lock-test", async () => {
    for (let i = 0; i < wakeupPayloads.length; i += 1) {
      const res = await runHook(fixture, "UserPromptSubmit", {
        session_id: sessionId,
        prompt_id: `pid-skip-${i}`,
        prompt: wakeupPayloads[i],
      });
      assert.equal(res.code, 0);
      assert.deepEqual(res.output, { continue: true }, `wakeup variant ${i} must return bare { continue: true }`);
      assert.equal(additionalContext(res.output), "", `wakeup variant ${i} must inject zero envelope`);
    }
  });

  assert.equal(searchCalls, 0, "skip wakeups must make zero daemon search calls");
  const logAfterA = (await exists(`${fixture.vault}/log.md`))
    ? await readFile(`${fixture.vault}/log.md`, "utf8")
    : "";
  assert.doesNotMatch(
    logAfterA,
    /hook_user_prompt_submit/,
    "skip wakeups must emit zero hook_user_prompt_submit audit spam",
  );

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
  assert.deepEqual(resB.output, { continue: true });
  assert.equal(searchCalls, 0, "skip prompt must still make zero daemon search calls");
  const logAfterB = (await exists(`${fixture.vault}/log.md`))
    ? await readFile(`${fixture.vault}/log.md`, "utf8")
    : "";
  assert.doesNotMatch(logAfterB, /hook_user_prompt_submit/, "still zero hook_user_prompt_submit audit spam after Case B");

  const stateAfterB = await readRecallState(fixture.vault, { sessionId, guardV2: true });
  assert.ok(stateAfterB, "pre-existing armed recall-state must NOT be cleared by a skip prompt");
  assert.equal(stateAfterB.consumed, false, "pre-existing armed recall-state must stay unconsumed");
  assert.equal(stateAfterB.task_signature, "t-armed-prior", "pre-existing recall-state signature must be untouched");
  assert.equal(stateAfterB.prompt_id, "pid-prior-human", "pre-existing recall-state prompt_id must be untouched");

  // Kill switch control: MINNI_PROMPT_CLASSES=off restores full recall, envelope, and audit on the prompt.
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
  assert.match(additionalContext(resKillSwitch.output), /<minni:context/);

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
