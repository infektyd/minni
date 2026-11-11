// Wave 1 (§2.3): PostToolUse and PostToolUseFailure failure-lesson surfacing.
// PostToolUse checks fire STRICTLY on:
//   (a) file-edit tools (Edit, Write, MultiEdit, NotebookEdit, edit_file, write_file, apply_patch), or
//   (b) commands that failed (isError === true || (exitCode !== null && exitCode !== 0)).
// Clean-exit shell commands (exitCode === 0 or exitCode === null && !isError) return
// { continue: true } immediately without scanning stdout for scar keywords.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { PLUGIN_ROOT, additionalContext, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("hooks/hooks.json declares PostToolUse for failed commands + file-edit tools and widened PostToolUseFailure matcher", async () => {
  const raw = await readFile(path.join(PLUGIN_ROOT, "hooks", "hooks.json"), "utf8");
  const manifest = JSON.parse(raw);
  assert.ok(Array.isArray(manifest?.hooks?.PostToolUse), "PostToolUse must be declared in hooks.json");
  assert.ok(Array.isArray(manifest?.hooks?.PostToolUseFailure), "PostToolUseFailure must be declared in hooks.json");
  assert.equal(
    manifest.hooks.PostToolUse[0].matcher,
    "^(?:Bash|run_terminal_command|Edit|Write|MultiEdit|NotebookEdit|edit_file|write_file|apply_patch)$",
  );
  const failureMatcher = manifest.hooks.PostToolUseFailure[0].matcher;
  assert.ok(
    failureMatcher === "" ||
      failureMatcher === "^(?:Bash|run_terminal_command|Agent|Task|update_plan|mcp__.*|.*)$",
    `expected widened PostToolUseFailure matcher, got: ${JSON.stringify(failureMatcher)}`,
  );
});

test("PostToolUse skips clean-exit commands without stdout scan and fires on failed commands (exit_code !== 0 or is_error: true)", async (t) => {
  const fixture = await makeFixture(t, "posttooluse-masked");
  await startFakeDaemon(fixture);

  // 1. Clean-exit Bash (exit_code: 0, is_error: false) MUST return { continue: true } immediately
  // even when stdout contains scar keywords ("Expectation failed", "verify_source_baseline.sh: FAIL").
  const cleanExitZeroWithNoise = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-clean-0",
    tool_name: "Bash",
    tool_input: { command: "grep 'Expectation failed' gate.log" },
    is_error: false,
    tool_response: {
      stdout: '✘ Test "Inspector layout bounds" failed after 0.042 seconds.\nExpectation failed: (frame.width → 0.0) == 320.0\nverify_source_baseline.sh: FAIL',
      stderr: "",
      exit_code: 0,
    },
  });
  assert.deepEqual(cleanExitZeroWithNoise.output, { continue: true });

  // 2. Clean-exit Bash with omitted exit_code (exitCode === null && !isError) MUST ALSO return { continue: true } immediately.
  const cleanExitNullWithNoise = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-clean-null",
    tool_name: "Bash",
    tool_input: { command: "cat gate.log" },
    is_error: false,
    tool_response: '✘ Test "Inspector layout bounds" failed after 0.042 seconds.\nExpectation failed: (frame.width → 0.0) == 320.0',
  });
  assert.deepEqual(cleanExitNullWithNoise.output, { continue: true });

  // 3. Failed Bash via non-zero exit_code (exit_code: 1, even with is_error: false) surfaces lesson.
  const nonZeroExitSwift = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-1",
    tool_name: "Bash",
    tool_input: { command: "swift test" },
    is_error: false,
    tool_response: {
      stdout: '✘ Test "Inspector layout bounds" failed after 0.042 seconds.\nExpectation failed: (frame.width → 0.0) == 320.0',
      stderr: "",
      exit_code: 1,
    },
  });
  assert.equal(nonZeroExitSwift.code, 0);
  assert.equal(nonZeroExitSwift.output.continue, true);
  assert.equal(nonZeroExitSwift.output.hookSpecificOutput?.hookEventName, "PostToolUse");
  const ctx1 = additionalContext(nonZeroExitSwift.output);
  assert.match(ctx1, /📓 Minni failure lesson/);
  assert.match(ctx1, /Expectation failed/i);
  assert.match(ctx1, /<EVIDENCE\b[\s\S]*<\/EVIDENCE>/);

  // 4. Failed Bash via is_error: true surfaces verify_source_baseline.sh lesson.
  const failedBaseline = await runHook(fixture, "PostToolUse", {
    session_id: "sess-post-1",
    tool_name: "Bash",
    tool_input: { command: "./scripts/verify_source_baseline.sh" },
    is_error: true,
    tool_response: "verify_source_baseline.sh: FAIL: unregistered test file NewFeatureTests.swift\nexit=1",
  });
  assert.equal(failedBaseline.code, 0);
  assert.equal(failedBaseline.output.continue, true);
  const ctx2 = additionalContext(failedBaseline.output);
  assert.match(ctx2, /📓 Minni failure lesson/);
  assert.match(ctx2, /project\.pbxproj|membershipExceptions/);

  // 5. PostToolUseFailure with real non-zero exit / error (is_error: true)
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
});

test("B4 & Wave 1: file-edit tools (Edit, Write, edit_file, write_file, apply_patch) fire Swift antipattern rules while clean-exit Bash and non-Swift repos stay quiet", async (t) => {
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

  // 2. Non-Swift repo (`cwd: /workspace/Projects/PythonApp`) running failed pytest MUST NOT trigger Swift-scoped rule
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

  // 3. Doc-comment-only match (`/// Do not use @unchecked Sendable`) on file-edit Write MUST NOT fire
  const commentOnlyEdit = await runHook(fixture, "PostToolUse", {
    session_id: "sess-b4-3",
    cwd: "/workspace/Projects/Praxis",
    tool_name: "Write",
    tool_input: {
      file_path: "/workspace/Projects/Praxis/Sources/DocNote.swift",
      content: "/// Warning: avoid @unchecked Sendable in actor code\nstruct SafeActor {}\n",
    },
    is_error: false,
    tool_response: { ok: true },
  });
  assert.deepEqual(
    commentOnlyEdit.output,
    { continue: true },
    "comment-only mention of @unchecked Sendable in file edit must not fire",
  );

  // 4a. File-edit `Edit` introducing `@unchecked Sendable` in Swift fires m:7a11e510 (even with is_error: false)
  const editUnchecked = await runHook(fixture, "PostToolUse", {
    session_id: "sess-b4-edit-1",
    cwd: "/workspace/Projects/Praxis",
    tool_name: "Edit",
    tool_input: {
      file_path: "/workspace/Projects/Praxis/Sources/SyncEngine.swift",
      new_string: "final class SyncEngine: @unchecked Sendable {}",
    },
    is_error: false,
    tool_response: "File updated",
  });
  assert.match(additionalContext(editUnchecked.output), /m:7a11e510/);

  // 4b. File-edit `edit_file` / `write_file` / `apply_patch` introducing silent `try?` in Swift fires m:7a11e511
  for (const [idx, editPayload] of [
    {
      tool_name: "edit_file",
      tool_input: {
        Path: "/workspace/Projects/Praxis/Sources/Worker.swift",
        Replacement: "    let parsed = try? JSONDecoder().decode(Item.self, from: raw)",
      },
    },
    {
      tool_name: "write_file",
      tool_input: {
        Path: "/workspace/Projects/Praxis/Sources/Loader.swift",
        Content: "import Foundation\nfunc load() { _ = try? Data(contentsOf: url) }\n",
      },
    },
    {
      tool_name: "apply_patch",
      tool_input: {
        patch: "*** Update File: Sources/PatchTarget.swift\n@@\n+    _ = try? session.send(msg)\n",
      },
    },
  ].entries()) {
    const res = await runHook(fixture, "PostToolUse", {
      session_id: `sess-b4-file-edit-${idx}`,
      cwd: "/workspace/Projects/Praxis",
      ...editPayload,
      is_error: false,
      tool_response: "Applied",
    });
    assert.match(
      additionalContext(res.output),
      /m:7a11e511/,
      `${editPayload.tool_name} must fire swift_silent_try_question on PostToolUse`,
    );
  }

  // 4c. Real Swift/Praxis violation (`@unchecked Sendable` on code line during failed build) fires m:7a11e510
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

  const { isSwiftOrPraxisContext, isFileEditTool } = await import("../dist/failure-lessons.js");
  assert.equal(typeof isSwiftOrPraxisContext, "function", "isSwiftOrPraxisContext must be exported");
  assert.equal(typeof isFileEditTool, "function", "isFileEditTool must be exported");
});

test("Wave 1 & 2: expandTilde on MINNI_SOCKET_PATH, clean-exit Bash fast return, and scheduled prompt skip", async (t) => {
  const os = await import("node:os");
  const { jsonRpcSocketCandidates } = await import("../dist/daemon_transport.js");
  const prevSock = process.env.MINNI_SOCKET_PATH;
  try {
    process.env.MINNI_SOCKET_PATH = "~/.minni/run/minnid.sock";
    const cands = jsonRpcSocketCandidates();
    assert.equal(
      cands[0],
      path.join(os.homedir(), ".minni", "run", "minnid.sock"),
      "MINNI_SOCKET_PATH with leading ~/ must expand to os.homedir()",
    );
  } finally {
    if (prevSock === undefined) delete process.env.MINNI_SOCKET_PATH;
    else process.env.MINNI_SOCKET_PATH = prevSock;
  }

  const fixture = await makeFixture(t, "posttooluse-readonly-wave2");
  await startFakeDaemon(fixture);

  // 1. Clean-exit Bash grep/cat commands return { continue: true } immediately without scanning stdout
  for (const [idx, cmd] of [
    'grep -rn "func withDeadline\\|func joinWithin" Services/ | cut -c1-170 | head -40',
    'L=/tmp/gate.log; grep "Expectation failed:" "$L" | sort | uniq -c | head -20',
    "cat /tmp/f230b-red.log",
  ].entries()) {
    const res = await runHook(fixture, "PostToolUse", {
      session_id: `sess-w2-${idx}`,
      cwd: "/workspace/Projects/Praxis",
      tool_name: "Bash",
      tool_input: { command: cmd },
      is_error: false,
      tool_response: {
        stdout:
          "Services/TaskTimeout.swift:28:    _ = try? await group.next()\nExpectation failed: (count → 0) == 1\nverify_source_baseline.sh: FAIL: unregistered file",
        stderr: "",
        exit_code: 0,
      },
    });
    assert.deepEqual(res.output, { continue: true });
  }

  // 2. Scheduled / Self-monitor tick prompt skips UserPromptSubmit vault/daemon recall (no recall_pointer / memory_matches)
  const { writeVaultPage } = await import("../dist/vault.js");
  await writeVaultPage({
    vaultPath: fixture.vault,
    title: "Self monitor tick background job check",
    content: "Decision: check background job logs and worktree gates on every self-monitor tick.",
    section: "decisions",
    source: "test",
    frontmatter: { privacy: "safe", status: "accepted" },
  });
  const scheduledTick = await runHook(fixture, "UserPromptSubmit", {
    session_id: "sess-w2-5",
    prompt:
      "Self-monitor tick (every ~15 min while work is active):\n1. Check every background job and worktree gate.",
  });
  const schedCtx = additionalContext(scheduledTick.output);
  assert.doesNotMatch(
    schedCtx,
    /recall_pointer|memory_matches/,
    "Self-monitor tick prompt must skip UserPromptSubmit vault/daemon recall",
  );
});
