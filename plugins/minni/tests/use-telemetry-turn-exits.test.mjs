// Recall-to-use S1a-2 (§4.3 "Every UPS exit is counted"): UserPromptSubmit is
// wrapped at its dispatch arm by `withTurnRecord`, so every prompt writes
// exactly one `turn` row, whichever of its exits it leaves by, and each exit
// names itself. An exit that names nothing would be recorded as `error`, so a
// non-`error` exit on every row is the pin that each exit sets its reason.
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

const SESSION = "turn-exits-session";
const STRONG = "STRONGQ where does the deploy procedure live";

test("every UserPromptSubmit exit writes exactly one turn row with its own exit reason", { timeout: 180_000 }, async (t) => {
  const fixture = await makeFixture(t, "turn-exits");
  let slowSearch = false;
  await startFakeDaemon(fixture, {
    search: async (params) => {
      if (slowSearch) await new Promise((resolve) => setTimeout(resolve, 3_000));
      const strong = String(params.query ?? "").includes("STRONGQ");
      return {
        agent_id: "claude-code",
        results: strong
          ? [
              daemonResult({
                source: "/elsewhere/deploy.md",
                docId: 41,
                wikilink: "[[wiki/deploy]]",
                title: "Deploy procedure",
              }),
            ]
          : [],
      };
    },
  });

  // [prompt, extra env, expected exit]; one prompt_id per prompt.
  const steps = [
    ["   ", {}, "empty_prompt"],
    ["please remember this: the build uses make", {}, "write_intent"],
    ["please remember this: the linter is eslint", { MINNI_LIFECYCLE_NUDGE_MODE: "off" }, "write_intent"],
    ["what is the zebra pancake status", {}, "nothing_salient"],
    ["what is the quokka waffle status", { MINNI_LIFECYCLE_NUDGE_MODE: "off" }, "nothing_salient"],
    [STRONG, {}, "pointer"],
  ];
  const expected = [];
  let n = 0;
  for (const [prompt, env, exit] of steps) {
    n += 1;
    const { output, code } = await runHook(
      fixture,
      "UserPromptSubmit",
      { session_id: SESSION, prompt_id: `pid-${n}`, prompt },
      { env },
    );
    assert.equal(code, 0);
    assert.equal(output.continue, true);
    expected.push({ prompt_id: `pid-${n}`, exit });
  }

  // Degraded daemon after a strong turn: the previous turn's pointer, marked stale.
  slowSearch = true;
  n += 1;
  await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: SESSION, prompt_id: `pid-${n}`, prompt: "what about the quarterly rollout checklist" },
    { env: { MINNI_HOOK_BUDGET_MS: "800" } },
  );
  expected.push({ prompt_id: `pid-${n}`, exit: "stale_pointer" });
  slowSearch = false;

  // A failed active-plan read is salient: the final exit, for the plan.
  await mkdir(path.join(fixture.vault, "wiki", "artifacts", "_active_plan.json"), { recursive: true });
  n += 1;
  await runHook(fixture, "UserPromptSubmit", {
    session_id: SESSION,
    prompt_id: `pid-${n}`,
    prompt: "an utterly novel question about the heron lattice",
  });
  expected.push({ prompt_id: `pid-${n}`, exit: "plan" });

  const turns = (await readLedger(fixture.vault)).filter((row) => row.kind === "turn");
  assert.equal(turns.length, expected.length, `one turn row per prompt: ${JSON.stringify(turns)}`);
  for (const [index, want] of expected.entries()) {
    const row = turns[index];
    assert.equal(row.prompt_id, want.prompt_id);
    assert.equal(row.exit, want.exit, `prompt ${want.prompt_id} left by ${row.exit}`);
    assert.notEqual(row.exit, "error");
    assert.equal(row.session_key, sessionKeyOf(SESSION));
    assert.equal(row.agent_key, "main");
    assert.equal(row.wire, "claude-code");
    assert.equal(row.v, 5);
    assert.ok(Array.isArray(row.features) && row.features.includes("MINNI_USE_LEDGER"));
  }
  const indexes = turns.map((row) => row.turn_ix);
  assert.deepEqual(
    indexes,
    expected.map((_, index) => index + 1),
    `turn_ix counts the session's prompts from 1: ${indexes}`,
  );
});

test("turn_ix is per session: a second session counts from 1", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "turn-exits-sessions");
  await startFakeDaemon(fixture);
  for (const [session, prompt] of [
    ["session-a", "first question about otters"],
    ["session-a", "second question about otters"],
    ["session-b", "first question about badgers"],
  ]) {
    await runHook(fixture, "UserPromptSubmit", { session_id: session, prompt });
  }
  const turns = (await readLedger(fixture.vault)).filter((row) => row.kind === "turn");
  const bySession = (id) => turns.filter((row) => row.session_key === sessionKeyOf(id)).map((row) => row.turn_ix);
  assert.deepEqual(bySession("session-a"), [1, 2]);
  assert.deepEqual(bySession("session-b"), [1]);
});

test("a UserPromptSubmit that throws still writes its turn row, as error, and rethrows", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "turn-exits-throw");
  // `wiki` as a plain file makes ensureVault throw after the empty-prompt gate.
  await writeFile(path.join(fixture.vault, "wiki"), "not a directory\n", "utf8");
  await startFakeDaemon(fixture);
  const { output, code } = await runHook(fixture, "UserPromptSubmit", {
    session_id: SESSION,
    prompt_id: "pid-throw",
    prompt: "a question that will never reach recall",
  });
  assert.equal(code, 0);
  assert.match(output.systemMessage ?? "", /Minni hook degraded \(UserPromptSubmit\)/, "today's failure handling");
  const turns = (await readLedger(fixture.vault)).filter((row) => row.kind === "turn");
  assert.equal(turns.length, 1);
  assert.equal(turns[0].exit, "error");
  assert.equal(turns[0].prompt_id, "pid-throw");
});
