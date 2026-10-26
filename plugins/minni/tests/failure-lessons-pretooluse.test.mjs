// Phase 1b (S4b, §7): PreToolUse pre-emptive command signature matching
// (e.g., hook-blocked `git show` / `git checkout` / unbounded `git diff` ->
// `git cat-file -p <rev>:<path>` lesson) delivered via `additionalContext` with
// no `permissionDecision` so the lesson reaches the model even when another
// hook denies the call (P2/P3), plus git common-dir project key sharing across
// worktrees and the MINNI_FAILURE_LESSONS kill switch.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { resolveProjectKey } from "../dist/failure-lessons.js";
import { additionalContext, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("resolveProjectKey maps main checkout and linked git worktree to the same canonical project key", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-projkey-"));
  t.after(() => rm(root, { recursive: true, force: true }));

  const mainRepo = path.join(root, "repo-main");
  await mkdir(mainRepo, { recursive: true });
  execFileSync("git", ["init", mainRepo], { stdio: "ignore" });
  execFileSync("git", ["-C", mainRepo, "config", "user.email", "test@example.com"], { stdio: "ignore" });
  execFileSync("git", ["-C", mainRepo, "config", "user.name", "Test"], { stdio: "ignore" });
  await writeFile(path.join(mainRepo, "README.md"), "# test\n", "utf8");
  execFileSync("git", ["-C", mainRepo, "add", "README.md"], { stdio: "ignore" });
  execFileSync("git", ["-C", mainRepo, "commit", "-m", "init"], { stdio: "ignore" });

  const wtRepo = path.join(root, "repo-worktree-slice");
  execFileSync("git", ["-C", mainRepo, "worktree", "add", "-b", "slice-a", wtRepo], { stdio: "ignore" });
  const nestedInWt = path.join(wtRepo, "src", "sub");
  await mkdir(nestedInWt, { recursive: true });

  const otherRepo = path.join(root, "other-repo");
  await mkdir(otherRepo, { recursive: true });
  execFileSync("git", ["init", otherRepo], { stdio: "ignore" });

  const keyMain = resolveProjectKey(mainRepo);
  const keyWt = resolveProjectKey(wtRepo);
  const keyNested = resolveProjectKey(nestedInWt);
  const keyOther = resolveProjectKey(otherRepo);

  assert.equal(keyMain, keyWt, "main checkout and linked worktree share the same project key");
  assert.equal(keyMain, keyNested, "nested directory inside worktree resolves to the same project key");
  assert.notEqual(keyMain, keyOther, "distinct git repository resolves to a distinct project key");
});

test("PreToolUse on Bash matching pre-emptive git show / git checkout signature injects git cat-file lesson via additionalContext with no permissionDecision", async (t) => {
  const fixture = await makeFixture(t, "pretooluse-lesson");
  await startFakeDaemon(fixture);

  const res = await runHook(fixture, "PreToolUse", {
    session_id: "sess-pre-1",
    tool_name: "Bash",
    tool_input: { command: "git show HEAD~1:src/minni/server.py" },
  });
  assert.equal(res.code, 0);
  assert.equal(res.output.continue, true);
  assert.equal(
    res.output.hookSpecificOutput?.permissionDecision,
    undefined,
    "pre-emptive failure lesson must not set permissionDecision (P2/P3)",
  );
  assert.equal(res.output.hookSpecificOutput?.hookEventName, "PreToolUse");
  const ctx = additionalContext(res.output);
  assert.match(ctx, /📓 Minni failure lesson/);
  assert.match(ctx, /git cat-file -p/);
  assert.match(ctx, /<EVIDENCE\b[\s\S]*<\/EVIDENCE>/);

  // Safe bounded git commands do not trigger a failure lesson.
  const safeRes = await runHook(fixture, "PreToolUse", {
    session_id: "sess-pre-safe",
    tool_name: "Bash",
    tool_input: { command: "git cat-file -p HEAD:src/minni/server.py" },
  });
  assert.deepEqual(safeRes.output, { continue: true });
});

test("MINNI_FAILURE_LESSONS=off disables PreToolUse failure-lesson injection", async (t) => {
  const fixture = await makeFixture(t, "pretooluse-lesson-off");
  await startFakeDaemon(fixture);

  const envOff = await runHook(
    fixture,
    "PreToolUse",
    {
      session_id: "sess-pre-off-1",
      tool_name: "Bash",
      tool_input: { command: "git show HEAD:src/minni/server.py" },
    },
    { env: { MINNI_FAILURE_LESSONS: "off" } },
  );
  assert.deepEqual(envOff.output, { continue: true });
});
