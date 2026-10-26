// Phase 1b (S4c, §7): PostToolUse and PostToolUseFailure failure-lesson
// surfacing. Solves the §7 open blocker where 350/352 Swift test-failure Bash
// results had `is_error: false` due to exit-code masking (`| tail`,
// `echo exit=$?`) by matching `PostToolUse` `tool_response` output text on
// every Bash call, plus `PostToolUseFailure` for real failures.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { PLUGIN_ROOT, additionalContext, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("hooks/hooks.json declares PostToolUse and PostToolUseFailure for Bash and run_terminal_command", async () => {
  const raw = await readFile(path.join(PLUGIN_ROOT, "hooks", "hooks.json"), "utf8");
  const manifest = JSON.parse(raw);
  assert.ok(Array.isArray(manifest?.hooks?.PostToolUse), "PostToolUse must be declared in hooks.json");
  assert.ok(Array.isArray(manifest?.hooks?.PostToolUseFailure), "PostToolUseFailure must be declared in hooks.json");
  assert.equal(manifest.hooks.PostToolUse[0].matcher, "^(?:Bash|run_terminal_command)$");
  assert.equal(manifest.hooks.PostToolUseFailure[0].matcher, "^(?:Bash|run_terminal_command)$");
});

test("PostToolUse matches exit-masked Bash output (is_error: false with | tail / echo exit=$?) and surfaces the matching lesson", async (t) => {
  const fixture = await makeFixture(t, "posttooluse-masked");
  await startFakeDaemon(fixture);

  // 1. Swift Testing failure with exit code masked by `| tail -n 20; echo exit=$?` (`is_error: false`)
  const maskedSwift = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-1",
    tool_name: "Bash",
    tool_input: { command: "swift test 2>&1 | tail -n 20; echo exit=$?" },
    is_error: false,
    tool_response: {
      stdout: '✘ Test "Inspector layout bounds" failed after 0.042 seconds.\nExpectation failed: (frame.width → 0.0) == 320.0\nexit=0',
      stderr: "",
      exit_code: 0,
    },
  });
  assert.equal(maskedSwift.code, 0);
  assert.equal(maskedSwift.output.continue, true);
  assert.equal(maskedSwift.output.hookSpecificOutput?.hookEventName, "PostToolUse");
  const ctx1 = additionalContext(maskedSwift.output);
  assert.match(ctx1, /📓 Minni failure lesson/);
  assert.match(ctx1, /Expectation failed/i);
  assert.match(ctx1, /<EVIDENCE\b[\s\S]*<\/EVIDENCE>/);

  // 2. verify_source_baseline.sh failure with `is_error: false`
  const maskedBaseline = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-1",
    tool_name: "Bash",
    tool_input: { command: "./scripts/verify_source_baseline.sh; echo exit=$?" },
    is_error: false,
    tool_response: "verify_source_baseline.sh: FAIL: unregistered test file NewFeatureTests.swift\nexit=1",
  });
  assert.equal(maskedBaseline.code, 0);
  assert.equal(maskedBaseline.output.continue, true);
  const ctx2 = additionalContext(maskedBaseline.output);
  assert.match(ctx2, /📓 Minni failure lesson/);
  assert.match(ctx2, /project\.pbxproj|membershipExceptions/);

  // 3. PostToolUseFailure with real non-zero exit / error (`is_error: true`)
  const realFailure = await runHook(fixture, "PostToolUseFailure", {
    session_id: "sess-post-1",
    tool_name: "Bash",
    tool_input: { command: "swift build" },
    is_error: true,
    error: "/workspace/App/Sources/View.swift:88:14: error: no such module 'MissingKit'",
  });
  assert.equal(realFailure.code, 0);
  assert.equal(realFailure.output.continue, true);
  assert.equal(realFailure.output.hookSpecificOutput?.hookEventName, "PostToolUseFailure");
  assert.match(additionalContext(realFailure.output), /📓 Minni failure lesson/);

  // 4. Clean Bash output returns plain `{ continue: true }`
  const cleanBash = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-1",
    tool_name: "Bash",
    tool_input: { command: "swift test" },
    is_error: false,
    tool_response: "Test run with 128 tests in 14 suites passed after 0.812 seconds.",
  });
  assert.deepEqual(cleanBash.output, { continue: true });
});
