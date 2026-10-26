import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  additionalContext,
  daemonResult,
  makeFixture,
  readLedger,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

function parseEnvelopeBody(output) {
  const ctx = additionalContext(output);
  if (!ctx) return undefined;
  const match = ctx.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/);
  return match ? JSON.parse(match[1]) : undefined;
}

test("cross-turn suppression: named on turn 1, suppressed on turns 2..11 at flat score, re-named on +0.12 score jump, reset, or at turn 12", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "suppression-on");
  let currentConfidence = 0.72;

  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [
        daemonResult({
          source: "/elsewhere/beryl-cadence.md",
          docId: 404,
          wikilink: "[[wiki/decisions/beryl-cadence]]",
          title: "Beryl harbor checkpoint cadence",
          confidence: currentConfidence,
        }),
      ],
    }),
  });

  const sessionId = "sess-suppress-10turn";

  // Turn 1 (score 0.72): named on pointer
  const t1 = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "p1",
    prompt: "check the beryl harbor checkpoint cadence configuration",
  });
  const body1 = parseEnvelopeBody(t1.output);
  assert.ok(body1?.recall_pointer, "turn 1 must emit recall_pointer");
  assert.match(body1.recall_pointer, /Beryl harbor checkpoint cadence/);

  // Turns 2 and 3 at flat score (0.72): suppressed! No pointer, no recall-state armed, turn exit = "weak"
  for (const turn of [2, 3]) {
    const res = await runHook(fixture, "UserPromptSubmit", {
      session_id: sessionId,
      prompt_id: `p${turn}`,
      prompt: `follow up ${turn} on beryl harbor checkpoint cadence`,
    });
    const body = parseEnvelopeBody(res.output);
    assert.equal(body?.recall_pointer, undefined, `turn ${turn} must suppress flat-score memory`);
    assert.equal(
      existsSync(path.join(fixture.vault, ".runtime", "recall-state.json")),
      false,
      `turn ${turn} must not leave recall-state.json armed when all hits are suppressed`,
    );
  }

  const rows = await readLedger(fixture.vault);
  const turn2Row = rows.find((r) => r.kind === "turn" && r.prompt_id === "p2");
  const turn3Row = rows.find((r) => r.kind === "turn" && r.prompt_id === "p3");
  assert.equal(turn2Row?.exit, "nothing_salient", "fully suppressed turn 2 must record weak exit (nothing_salient)");
  assert.equal(turn3Row?.exit, "nothing_salient", "fully suppressed turn 3 must record weak exit (nothing_salient)");
  assert.equal(
    rows.filter((r) => r.kind === "surfaced" && r.channel === "prompt_pointer").length,
    1,
    "only turn 1 recorded a prompt_pointer surfaced row",
  );

  // Turn 4 with score +0.12 (0.72 -> 0.84 >= +0.10 jump): re-named!
  currentConfidence = 0.84;
  const t4 = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "p4",
    prompt: "urgent beryl harbor checkpoint cadence failure",
  });
  const body4 = parseEnvelopeBody(t4.output);
  assert.ok(body4?.recall_pointer, "score jump of +0.12 (>= 0.10) must re-surface the memory on turn 4");

  // After a reset (SessionStart) at flat score 0.84: re-named on turn 5!
  await runHook(fixture, "SessionStart", {
    session_id: sessionId,
  });
  const t5 = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "p5",
    prompt: "post-reset check on beryl harbor checkpoint cadence",
  });
  const body5 = parseEnvelopeBody(t5.output);
  assert.ok(body5?.recall_pointer, "reset epoch change must re-surface the memory on turn 5");

  // Now test the 10-turn window in a fresh session:
  // Named on turn 1 -> suppressed on turns 2..11 (turnDiff 1..10) -> re-named on turn 12 (turnDiff = 11 > 10)!
  const session12 = "sess-window-12";
  currentConfidence = 0.75;
  const w1 = await runHook(fixture, "UserPromptSubmit", {
    session_id: session12,
    prompt_id: "w1",
    prompt: "window turn 1 beryl harbor checkpoint cadence",
  });
  assert.ok(parseEnvelopeBody(w1.output)?.recall_pointer, "window turn 1 must surface");

  for (let ix = 2; ix <= 11; ix += 1) {
    const w = await runHook(fixture, "UserPromptSubmit", {
      session_id: session12,
      prompt_id: `w${ix}`,
      prompt: `window turn ${ix} beryl harbor checkpoint cadence`,
    });
    assert.equal(
      parseEnvelopeBody(w.output)?.recall_pointer,
      undefined,
      `window turn ${ix} (turnDiff=${ix - 1} <= 10) must remain suppressed`,
    );
  }

  const w12 = await runHook(fixture, "UserPromptSubmit", {
    session_id: session12,
    prompt_id: "w12",
    prompt: "window turn 12 beryl harbor checkpoint cadence",
  });
  assert.ok(
    parseEnvelopeBody(w12.output)?.recall_pointer,
    "window turn 12 (turnDiff=11 > 10) must re-surface the memory",
  );
});

test("cross-turn suppression works identically with MINNI_USE_LEDGER=off without creating recall-session.d", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "suppression-off");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [
        daemonResult({
          source: "/elsewhere/cobalt-wal.md",
          docId: 505,
          wikilink: "[[wiki/decisions/cobalt-wal]]",
          title: "Cobalt WAL durability policy",
          confidence: 0.80,
        }),
      ],
    }),
  });

  const sessionId = "sess-ledger-off";
  const offEnv = { MINNI_USE_LEDGER: "off" };

  const r1 = await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: sessionId, prompt_id: "off-1", prompt: "check cobalt wal durability policy" },
    { env: offEnv },
  );
  assert.ok(parseEnvelopeBody(r1.output)?.recall_pointer, "turn 1 with ledger off must surface");

  const r2 = await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: sessionId, prompt_id: "off-2", prompt: "check cobalt wal durability policy again" },
    { env: offEnv },
  );
  assert.equal(
    parseEnvelopeBody(r2.output)?.recall_pointer,
    undefined,
    "turn 2 with ledger off must suppress the already-surfaced memory",
  );
  assert.equal(
    existsSync(path.join(fixture.vault, ".runtime", "recall-session.d")),
    false,
    "MINNI_USE_LEDGER=off must never create .runtime/recall-session.d",
  );
});
