// S2b (§6.3.2, §6.3.3): atomic O_CREAT|O_EXCL claimArming ensures at most one
// sibling PreToolUse in a parallel batch surfaces; others return allow.
import assert from "node:assert/strict";
import { mkdir, readdir, utimes } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  armingKeyFor,
  claimArming,
  pruneOldClaimFiles,
  RECALL_STATE_DIR_RELPATH,
  writeRecallState,
} from "../dist/recall-state.js";
import {
  daemonResult,
  makeFixture,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

test("5 parallel PreToolUse processes against one armed session: exactly 1 surfaces and 4 allow", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "claim-once");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [
        daemonResult({
          source: "/elsewhere/architecture.md",
          docId: 101,
          wikilink: "[[wiki/architecture]]",
          title: "Architecture Invariants",
          confidence: 0.95,
        }),
      ],
    }),
  });

  const sessionId = "parallel-batch-session";
  await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "p-batch",
    prompt: "explain the architecture invariants before reading files",
  });

  const calls = Array.from({ length: 5 }, (_, i) =>
    runHook(fixture, "PreToolUse", {
      session_id: sessionId,
      prompt_id: "p-batch",
      tool_name: i % 2 === 0 ? "Read" : "Grep",
      tool_input: i % 2 === 0 ? { file_path: `/tmp/file-${i}.ts` } : { pattern: `needle_${i}` },
    }),
  );

  const results = await Promise.all(calls);
  for (const res of results) {
    assert.equal(res.code, 0);
  }

  const surfaced = results.filter((r) => typeof r.output.hookSpecificOutput?.additionalContext === "string");
  const allowed = results.filter((r) => r.output.hookSpecificOutput === undefined);

  assert.equal(surfaced.length, 1, `expected exactly 1 surfaced PreToolUse in parallel batch, got ${surfaced.length}`);
  assert.equal(allowed.length, 4, `expected 4 allowed siblings in parallel batch, got ${allowed.length}`);
  assert.match(surfaced[0].output.hookSpecificOutput.additionalContext, /Architecture Invariants/);
});

test("claimArming uses O_CREAT|O_EXCL and pruneOldClaimFiles removes claims older than 24h", async (t) => {
  const fixture = await makeFixture(t, "claim-prune");
  await writeRecallState(
    fixture.vault,
    {
      task_signature: "sig-claim",
      intent: "recall",
      top_hits: [{ title: "Claim Hit", wikilink: "[[claim-hit]]", score: 0.9 }],
      top_score: 0.9,
      session_id: "sess-claim",
      arming_id: "arm1234567890abc",
    },
    { sessionId: "sess-claim", guardV2: true },
  );

  const key = armingKeyFor({ task_signature: "sig-claim", arming_id: "arm1234567890abc" });
  assert.equal(key, "arm1234567890abc");

  const first = await claimArming(fixture.vault, "sess-claim", key);
  const second = await claimArming(fixture.vault, "sess-claim", key);
  assert.equal(first, true, "first claimArming must win");
  assert.equal(second, false, "second claimArming for same session+arming must lose");

  // Fallback arming key when arming_id is absent uses sha1(task_signature + ts)[:16].
  const fallbackKey = armingKeyFor({ task_signature: "sig-legacy", ts: "2026-10-25T00:00:00.000Z" });
  assert.match(fallbackKey, /^[0-9a-f]{16}$/);

  const stateDir = path.join(fixture.vault, RECALL_STATE_DIR_RELPATH);
  await mkdir(stateDir, { recursive: true });
  const claimFiles = (await readdir(stateDir)).filter((f) => f.endsWith(".claim"));
  assert.equal(claimFiles.length, 1);

  // Recent claim file is kept.
  assert.equal(pruneOldClaimFiles(fixture.vault), 0);

  // Age the claim file past 24h and verify pruneOldClaimFiles removes it.
  const oldDate = new Date(Date.now() - 25 * 60 * 60 * 1000);
  await utimes(path.join(stateDir, claimFiles[0]), oldDate, oldDate);
  assert.equal(pruneOldClaimFiles(fixture.vault), 1);
  const remaining = (await readdir(stateDir)).filter((f) => f.endsWith(".claim"));
  assert.equal(remaining.length, 0);
});
