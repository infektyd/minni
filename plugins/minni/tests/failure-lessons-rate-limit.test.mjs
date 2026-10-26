// Phase 1b (S4d, §7): per-session per-lesson rate limiting, reset epoch
// clearing, use-ledger `failure_lesson` channel capture (when MINNI_USE_LEDGER=on
// vs off), and maskTelemetry self-echo stripping.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { markFailureLessonFired } from "../dist/failure-lessons.js";
import { maskTelemetry } from "../dist/jev-harvest.js";
import { additionalContext, exists, makeFixture, readLedger, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("failure lessons fire at most once per (session, lesson) until a session reset, and record failure_lesson ledger rows when MINNI_USE_LEDGER=on", async (t) => {
  const fixture = await makeFixture(t, "failure-lessons-ratelimit");
  await startFakeDaemon(fixture);

  const payload = {
    session_id: "sess-rl-1",
    tool_name: "Bash",
    tool_input: { command: "swift test | tail -n 10" },
    is_error: false,
    tool_response: '✘ Test "alpha" failed\nExpectation failed: x == y',
  };

  // 1st firing in sess-rl-1 surfaces the lesson and records a failure_lesson row.
  const first = await runHook(fixture, "PostToolUse", payload, { env: { MINNI_USE_LEDGER: "on" } });
  assert.match(additionalContext(first.output), /📓 Minni failure lesson/);

  const rowsAfterFirst = (await readLedger(fixture.vault)).filter(
    (r) => r.kind === "surfaced" && r.channel === "failure_lesson",
  );
  assert.equal(rowsAfterFirst.length, 1, "delivered failure lesson writes 1 surfaced row on failure_lesson channel");
  assert.equal(rowsAfterFirst[0].visible, true);
  assert.equal(rowsAfterFirst[0].delivered, true);
  assert.ok(Array.isArray(rowsAfterFirst[0].shingles) && rowsAfterFirst[0].shingles.length > 0);

  // 2nd firing of the SAME lesson in the SAME session is rate-limited.
  const second = await runHook(fixture, "PostToolUse", payload, { env: { MINNI_USE_LEDGER: "on" } });
  assert.deepEqual(second.output, { continue: true }, "repeat firing in same session is suppressed");

  // A DIFFERENT session_id is not suppressed by sess-rl-1's rate limit.
  const otherSession = await runHook(
    fixture,
    "PostToolUse",
    { ...payload, session_id: "sess-rl-2" },
    { env: { MINNI_USE_LEDGER: "off" } },
  );
  assert.match(additionalContext(otherSession.output), /📓 Minni failure lesson/);
  const rowsAfterOff = (await readLedger(fixture.vault)).filter(
    (r) => r.kind === "surfaced" && r.channel === "failure_lesson",
  );
  assert.equal(rowsAfterOff.length, 1, "MINNI_USE_LEDGER=off writes no additional ledger rows");

  // SessionStart reset on sess-rl-1 clears its rate limit so the lesson can fire again in the new epoch.
  await runHook(fixture, "SessionStart", { session_id: "sess-rl-1", source: "startup" });
  const afterReset = await runHook(fixture, "PostToolUse", payload, { env: { MINNI_USE_LEDGER: "on" } });
  assert.match(
    additionalContext(afterReset.output),
    /📓 Minni failure lesson/,
    "after SessionStart reset, the lesson may fire again in the new epoch",
  );
});

test("maskTelemetry strips 📓 Minni failure lesson lines and PostToolUse hook additional context headers", () => {
  const raw = [
    "PostToolUse:Bash hook additional context:",
    '📓 Minni failure lesson (m:a1b2c3d4): "Swift Testing expectation failure" — Never mask exit codes with | tail; inspect the failing assertion.',
    "Normal human sentence that should survive.",
  ].join("\n");
  const masked = maskTelemetry(raw);
  assert.ok(!masked.includes("Minni failure lesson"), `failure lesson leaked: ${masked}`);
  assert.ok(!masked.includes("PostToolUse:Bash hook additional context"), `header leaked: ${masked}`);
  assert.ok(masked.includes("Normal human sentence that should survive."));
});

test("markFailureLessonFired refuses a symlinked .runtime escaping the vault before mkdirSync", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-fl-escape-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  const outside = path.join(root, "outside");
  await mkdir(vault, { recursive: true });
  await mkdir(outside, { recursive: true });
  await symlink(outside, path.join(vault, ".runtime"));

  const ok = markFailureLessonFired(vault, "sess-escape", "scar-git-cat-file-instead-of-show");
  assert.equal(ok, false, "symlink escape must be refused");
  assert.equal(
    await exists(path.join(outside, "recall-state.d")),
    false,
    "must not create recall-state.d in the outside target directory",
  );
});
