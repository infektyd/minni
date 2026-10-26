// Recall-to-use S1a-2 (§4.3): a PreToolUse guard deny records one `guard`
// row per hit the deny reason lists, keyed with the same mkey UPS gave that
// hit, and only when the deny is actually returned.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  daemonResult,
  makeFixture,
  readLedger,
  runHook,
  sessionKeyOf,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const SESSION = "guard-keys-session";

test("armed state, then a Read: one guard row per listed hit, with its mkey", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "guard-keys");
  const note = path.join(fixture.vault, "wiki", "ops", "rotation.md");
  await mkdir(path.dirname(note), { recursive: true });
  await writeFile(note, "# Rotation\n\nRotate quarterly.\n", "utf8");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [
        daemonResult({ source: note, docId: 3, wikilink: "[[wiki/ops/rotation]]", title: "Rotation", confidence: 0.97 }),
        daemonResult({
          source: "/elsewhere/runbook.md",
          docId: 42,
          src: "shared",
          wikilink: "[[wiki/runbook]]",
          title: "Runbook",
          confidence: 0.91,
        }),
      ],
    }),
  });

  await runHook(fixture, "UserPromptSubmit", {
    session_id: SESSION,
    prompt_id: "arm",
    prompt: "how do we rotate the signing key",
  });
  const { output, code } = await runHook(fixture, "PreToolUse", {
    session_id: SESSION,
    prompt_id: "arm",
    tool_name: "Read",
    tool_input: { file_path: "/tmp/anything.txt" },
  });
  assert.equal(code, 0);
  const reason = output.hookSpecificOutput?.additionalContext ?? output.hookSpecificOutput?.permissionDecisionReason;
  assert.equal(typeof reason, "string", "the armed guard surfaces context");

  const guard = (await readLedger(fixture.vault)).filter((row) => row.kind === "surfaced" && row.channel === "guard");
  assert.deepEqual(
    guard.map((row) => row.mkey),
    ["vault:wiki/ops/rotation.md", "doc:shared:42"],
    "one row per listed hit, in the order the deny lists them",
  );
  assert.deepEqual(
    guard.map((row) => row.rank),
    [1, 2],
  );
  for (const row of guard) {
    assert.equal(row.delivered, true);
    assert.equal(row.visible, true, "the deny reason is short: within the cap");
    assert.equal(row.session_key, sessionKeyOf(SESSION));
    assert.ok(reason.includes(row.title), `the row's title is the listed title: ${row.title}`);
  }

  // The re-issued call is allowed: no deny, so no new rows.
  const again = await runHook(fixture, "PreToolUse", {
    session_id: SESSION,
    tool_name: "Read",
    tool_input: { file_path: "/tmp/anything.txt" },
  });
  assert.equal(again.output.hookSpecificOutput, undefined);
  const after = (await readLedger(fixture.vault)).filter((row) => row.kind === "surfaced" && row.channel === "guard");
  assert.equal(after.length, 2, "an allowed call records nothing");
});

test("an out-of-scope tool is allowed and records no guard row", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "guard-keys-scope");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [daemonResult({ source: "/elsewhere/a.md", docId: 9, wikilink: "[[a]]", title: "Alpha" })],
    }),
  });
  await runHook(fixture, "UserPromptSubmit", { session_id: SESSION, prompt: "alpha question please" });
  const { output } = await runHook(fixture, "PreToolUse", {
    session_id: SESSION,
    tool_name: "Edit",
    tool_input: { file_path: "/tmp/x" },
  });
  assert.equal(output.hookSpecificOutput, undefined);
  const guard = (await readLedger(fixture.vault)).filter((row) => row.channel === "guard");
  assert.equal(guard.length, 0);
});
