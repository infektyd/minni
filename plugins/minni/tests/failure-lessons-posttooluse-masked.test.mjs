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

test("hooks/hooks.json declares PostToolUse and widened PostToolUseFailure matcher", async () => {
  const raw = await readFile(path.join(PLUGIN_ROOT, "hooks", "hooks.json"), "utf8");
  const manifest = JSON.parse(raw);
  assert.ok(Array.isArray(manifest?.hooks?.PostToolUse), "PostToolUse must be declared in hooks.json");
  assert.ok(Array.isArray(manifest?.hooks?.PostToolUseFailure), "PostToolUseFailure must be declared in hooks.json");
  assert.equal(manifest.hooks.PostToolUse[0].matcher, "^(?:Bash|run_terminal_command)$");
  const failureMatcher = manifest.hooks.PostToolUseFailure[0].matcher;
  assert.ok(
    failureMatcher === "" ||
      failureMatcher === "^(?:Bash|run_terminal_command|Agent|Task|update_plan|mcp__.*|.*)$",
    `expected widened PostToolUseFailure matcher, got: ${JSON.stringify(failureMatcher)}`,
  );
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

test("B4: Swift/Praxis antipattern rules (m:7a11e510..m:7a11e513) respect repo-scope, ignore read-only exit_code=0 search/diff and doc comments, fire on non-Bash PostToolUseFailure, and persist .handles.json", async (t) => {
  const fixture = await makeFixture(t, "posttooluse-b4");
  await startFakeDaemon(fixture);

  // 1. Read-only `rg @unchecked Sendable` with exit_code=0 in Praxis MUST NOT false-fire
  const readOnlyRg = await runHook(fixture, "PostToolUse", {
    session_id: "sess-b4-1",
    cwd: "/workspace/Projects/Praxis",
    tool_name: "Bash",
    tool_input: { command: "rg '@unchecked Sendable' Sources/" },
    is_error: false,
    tool_response: {
      stdout: "Sources/Worker.swift:12:final class Worker: @unchecked Sendable {}",
      stderr: "",
      exit_code: 0,
    },
  });
  assert.deepEqual(
    readOnlyRg.output,
    { continue: true },
    "read-only rg with exit_code=0 must not trigger Swift antipattern failure lesson",
  );

  // 2. Non-Swift repo (`cwd: /workspace/Projects/PythonApp`) running pytest MUST NOT trigger Swift-scoped rule even if output mentions Foo.swift and @unchecked Sendable
  const nonSwiftRepo = await runHook(fixture, "PostToolUse", {
    session_id: "sess-b4-2",
    cwd: "/workspace/Projects/PythonApp",
    tool_name: "Bash",
    tool_input: { command: "pytest" },
    is_error: true,
    tool_response: {
      stdout: "AssertionError in test_miner: Foo.swift '@unchecked Sendable' not allowed",
      stderr: "",
      exit_code: 1,
    },
  });
  assert.deepEqual(
    nonSwiftRepo.output,
    { continue: true },
    "Swift-scoped rule must not fire in a non-Swift repo during pytest even if test output mentions Foo.swift",
  );

  // 3. Doc-comment-only match (`/// Do not use @unchecked Sendable`) in Praxis MUST NOT fire
  const commentOnly = await runHook(fixture, "PostToolUse", {
    session_id: "sess-b4-3",
    cwd: "/workspace/Projects/Praxis",
    tool_name: "Bash",
    tool_input: { command: "swift build" },
    is_error: false,
    tool_response: {
      stdout: "/// Warning: avoid @unchecked Sendable in actor code",
      stderr: "",
      exit_code: 0,
    },
  });
  assert.deepEqual(
    commentOnly.output,
    { continue: true },
    "comment-only mention of @unchecked Sendable must not fire",
  );

  // 4. Real Swift/Praxis violation (`@unchecked Sendable` on code line during build/check) fires m:7a11e510
  const swiftUnchecked = await runHook(fixture, "PostToolUse", {
    session_id: "sess-b4-4",
    cwd: "/workspace/Projects/Praxis",
    tool_name: "Bash",
    tool_input: { command: "swift build" },
    is_error: true,
    tool_response: {
      stdout: "Sources/SyncEngine.swift:44:1: error: final class SyncEngine: @unchecked Sendable",
      stderr: "",
      exit_code: 1,
    },
  });
  assert.match(additionalContext(swiftUnchecked.output), /m:7a11e510/);

  // 5. PostToolUseFailure on `update_plan` fires m:7a11e508
  const updatePlanFail = await runHook(fixture, "PostToolUseFailure", {
    session_id: "sess-b4-5",
    tool_name: "update_plan",
    tool_input: {},
    is_error: true,
    error: "update_plan is a tool Only available in Plan mode",
  });
  assert.match(additionalContext(updatePlanFail.output), /m:7a11e508/);

  // 6. PostToolUseFailure on unknown MCP tool fires m:7a11e509
  const mcpFail = await runHook(fixture, "PostToolUseFailure", {
    session_id: "sess-b4-6",
    tool_name: "mcp__stale__tool",
    tool_input: {},
    is_error: true,
    error: "MCP tool `stale/tool` not found on server",
  });
  assert.match(additionalContext(mcpFail.output), /m:7a11e509/);

  // 7. <vaultPath>/.handles.json AND .runtime/recall-state.d/default.handles.json contain all 13 approved lesson handles
  const handlesRaw = JSON.parse(await readFile(path.join(fixture.vault, ".handles.json"), "utf8"));
  const defaultHandlesRaw = JSON.parse(
    await readFile(path.join(fixture.vault, ".runtime", "recall-state.d", "default.handles.json"), "utf8"),
  );
  for (const h of [
    "m:7a11e501",
    "m:7a11e502",
    "m:7a11e503",
    "m:7a11e504",
    "m:7a11e505",
    "m:7a11e506",
    "m:7a11e507",
    "m:7a11e508",
    "m:7a11e509",
    "m:7a11e510",
    "m:7a11e511",
    "m:7a11e512",
    "m:7a11e513",
  ]) {
    assert.ok(handlesRaw[h], `expected ${h} registered in <vault>/.handles.json`);
    assert.ok(defaultHandlesRaw?.handles?.[h], `expected ${h} registered in .runtime/recall-state.d/default.handles.json`);
  }

  const { isSwiftOrPraxisContext } = await import("../dist/failure-lessons.js");
  assert.equal(typeof isSwiftOrPraxisContext, "function", "isSwiftOrPraxisContext must be exported");
});

