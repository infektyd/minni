// Recall-to-use S1a-2 (§4.3, §5.2.1): the prompt classifier runs in
// record-only mode on every UserPromptSubmit. The `turn` row carries
// `envelope_kind`, `subkind`, `template_rule` and `rule_id`; UPS records each
// eligible prompt in the counts file so the template rule has its priors from
// the first day; and neither the prompt text nor its template hash ever
// reaches the ledger.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { templateHash } from "../dist/harness-envelope.js";
import {
  ledgerText,
  makeFixture,
  readLedger,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const COUNTS = path.join(".runtime", "prompt-templates.json");

async function turnsOf(vault) {
  return (await readLedger(vault)).filter((row) => row.kind === "turn");
}

test("a task notification is recorded as skip, a relay as wrapped, a typed prompt as human", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "turn-class-text");
  await startFakeDaemon(fixture);
  const prompts = [
    "<task-notification>\n<task-id>t1</task-id>\n<status>completed</status>\n</task-notification>",
    "<cross-session-message from=\"peer\">\nplease look at the failing build\n</cross-session-message>",
    "how do I rotate the staging certificate",
  ];
  for (const [index, prompt] of prompts.entries()) {
    await runHook(fixture, "UserPromptSubmit", { session_id: "class-text", prompt_id: `p${index}`, prompt });
  }
  const turns = await turnsOf(fixture.vault);
  assert.equal(turns.length, 3);
  assert.equal(turns[0].envelope_kind, "skip");
  assert.equal(turns[0].subkind, "task_notification");
  assert.equal(turns[0].rule_id, "skip.task_notification");
  assert.equal(turns[1].envelope_kind, "wrapped");
  assert.equal(turns[1].subkind, "cross_session_message");
  assert.equal(turns[1].rule_id, "wrap.cross_session_message");
  assert.equal(turns[2].envelope_kind, "human");
  assert.equal(turns[2].subkind, "typed");
  for (const row of turns) assert.equal(row.template_rule, "on", "the template rule runs from day one");
});

test("a long prompt repeated across 2 sessions with 3 priors is recorded as scheduled", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "turn-class-scheduled");
  await startFakeDaemon(fixture);
  const prompt =
    "Scheduled check-in 1042: review the open pull requests, summarise CI failures, and list anything blocked on review.";
  assert.ok(prompt.length >= 80);
  const hash = templateHash(prompt);
  const runs = [
    ["session-one", "sched-1"],
    ["session-one", "sched-2"],
    ["session-two", "sched-3"],
    ["session-two", "sched-4"],
  ];
  for (const [session, pid] of runs) {
    await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt_id: pid, prompt });
  }

  const turns = await turnsOf(fixture.vault);
  assert.equal(turns.length, 4);
  for (const row of turns.slice(0, 3)) {
    assert.equal(row.envelope_kind, "human", "fewer than 3 priors: still human");
    assert.equal(row.template_rule, "on");
  }
  const last = turns[3];
  assert.equal(last.envelope_kind, "scheduled");
  assert.equal(last.subkind, "template");
  assert.equal(last.template_rule, "on");
  assert.equal(last.rule_id, "scheduled.template");

  // The counts file gained every occurrence: one per prompt id, in 2 sessions.
  const counts = JSON.parse(await readFile(path.join(fixture.vault, COUNTS), "utf8"));
  const entry = counts.templates[hash];
  assert.ok(entry, "the template is counted under its hash");
  assert.deepEqual(
    entry.occ.map((o) => o.pid),
    runs.map(([, pid]) => pid),
  );
  assert.equal(Object.keys(entry.sessions).length, 2);
  assert.ok(!JSON.stringify(counts).includes("check-in"), "the counts file holds no text");

  // Neither the text nor its hash reaches the ledger.
  const text = await ledgerText(fixture.vault);
  assert.ok(text.length > 0);
  assert.ok(!text.includes(hash), "the template hash is never written to the ledger");
  for (const fragment of ["Scheduled check-in", "pull requests", "CI failures", "blocked on review"]) {
    assert.ok(!text.includes(fragment), `prompt text leaked into the ledger: ${fragment}`);
  }
});

test("a short prompt is not counted, and a skip prompt never touches the counts file", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "turn-class-ineligible");
  await startFakeDaemon(fixture);
  await runHook(fixture, "UserPromptSubmit", { session_id: "short", prompt_id: "s1", prompt: "short question" });
  await runHook(fixture, "UserPromptSubmit", {
    session_id: "short",
    prompt_id: "s2",
    prompt: `<task-notification>${"x".repeat(200)}</task-notification>`,
  });
  const turns = await turnsOf(fixture.vault);
  assert.equal(turns.length, 2, "both prompts are still counted as turns");
  let counts = null;
  try {
    counts = JSON.parse(await readFile(path.join(fixture.vault, COUNTS), "utf8"));
  } catch {
    // absent is the expected answer
  }
  assert.ok(counts === null || Object.keys(counts.templates).length === 0, "nothing eligible was counted");
});
