// S3a (§5.2.1, §5.3): UserPromptSubmit wrapped relay body recall.
// A wrapped relay prompt recalls on the unwrapped `body` only, so a vault note
// or daemon result matching only header words (e.g. inside
// `<cross-session-message from="...">` or `[Workflow harness ...]`) is never
// matched, while a decision note matching the relay body IS recalled.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { readRecallState } from "../dist/recall-state.js";
import { writeVaultPage } from "../dist/vault.js";
import {
  additionalContext,
  daemonResult,
  makeFixture,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

function parseEnvelopeBody(output) {
  const ctx = additionalContext(output);
  const match = /<minni:context\b[^>]*>\s*([\s\S]*?)\s*<\/minni:context>/.exec(ctx);
  assert.ok(match, `expected <minni:context> in additionalContext, got: ${ctx}`);
  return JSON.parse(match[1]);
}

test("UPS wrapped relay recalls on the unwrapped body only, ignoring wrapper header tokens", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "ups-relay-body-recall");
  const observedQueries = [];

  await startFakeDaemon(fixture, {
    search: (params) => {
      const q = String(params.query ?? "");
      observedQueries.push(q);
      const results = [];
      if (q.toLowerCase().includes("pelicanheaderjournal")) {
        results.push(
          daemonResult({
            source: "/elsewhere/pelican-plan-journal.md",
            docId: 101,
            wikilink: "[[wiki/artifacts/pelican-plan-journal]]",
            title: "Pelicanheaderjournal plan journal",
            confidence: 0.99,
          }),
        );
      }
      if (q.toLowerCase().includes("heronbodydecision")) {
        results.push(
          daemonResult({
            source: "/elsewhere/heron-decision.md",
            docId: 202,
            wikilink: "[[wiki/decisions/heron-decision]]",
            title: "Heronbodydecision architecture note",
            confidence: 0.92,
          }),
        );
      }
      return { agent_id: "claude-code", results };
    },
  });

  // Also write two vault notes:
  // 1) A plan journal note matching ONLY header words ("pelicanheaderjournal" / "cross-session-message")
  await writeVaultPage({
    vaultPath: fixture.vault,
    title: "Pelicanheaderjournal relay header journal",
    content:
      "pelicanheaderjournal pelicanheaderjournal cross-session-message workflow harness header token.",
    section: "artifacts",
    type: "artifact",
    status: "accepted",
  });

  // 2) A decision note matching ONLY the unwrapped relay body ("heronbodydecision")
  await writeVaultPage({
    vaultPath: fixture.vault,
    title: "Heronbodydecision retry policy",
    content:
      "heronbodydecision heronbodydecision retry backoff policy for idempotent socket calls.",
    section: "decisions",
    type: "decision",
    status: "accepted",
  });

  const wrappedPrompt =
    "Another Claude session sent a message:\n" +
    '<cross-session-message from="pelicanheaderjournal-orchestrator" thread="pelicanheaderjournal">\n' +
    "Where is the heronbodydecision retry backoff policy documented?\n" +
    "</cross-session-message>";

  const sessionId = "relay-body-session";
  const res = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "pid-relay-1",
    prompt: wrappedPrompt,
  });
  assert.equal(res.code, 0);

  // Daemon search query must be the unwrapped body ONLY.
  assert.equal(observedQueries.length, 1);
  assert.equal(
    observedQueries[0],
    "Where is the heronbodydecision retry backoff policy documented?",
    "daemon recall query must be the unwrapped relay body only",
  );
  assert.ok(
    !observedQueries[0].includes("pelicanheaderjournal"),
    "daemon query must not contain wrapper header tokens",
  );

  const body = parseEnvelopeBody(res.output);
  assert.equal(typeof body.recall_pointer, "string", "strong body match must emit recall_pointer");
  assert.match(body.recall_pointer, /heron-decision|Heronbodydecision/i);
  assert.doesNotMatch(
    body.recall_pointer,
    /pelican/i,
    "recall_pointer must not include the header-only plan journal note",
  );

  const state = await readRecallState(fixture.vault, { sessionId, guardV2: true });
  assert.ok(state, "recall-state must be written for strong body match");
  assert.ok(
    state.top_hits.some((h) => /heron/i.test(h.title) || /heron/i.test(h.wikilink)),
    "top_hits must include the body-matched decision note",
  );
  assert.ok(
    !state.top_hits.some((h) => /pelican/i.test(h.title) || /pelican/i.test(h.wikilink)),
    "top_hits must NOT include the header-matched journal note",
  );

  // Audit check: envelope_kind: "wrapped", subkind: "cross_session_lead"
  const logRaw = await readFile(`${fixture.vault}/log.md`, "utf8");
  assert.match(logRaw, /"envelope_kind":\s*"wrapped"/);
  assert.match(logRaw, /"subkind":\s*"cross_session_lead"/);

  // Also verify with MINNI_USE_LEDGER=off: still unwraps and recalls on body only.
  const resOff = await runHook(
    fixture,
    "UserPromptSubmit",
    {
      session_id: sessionId,
      prompt_id: "pid-relay-2",
      prompt: "[Workflow harness pelicanheaderjournal] Check heronbodydecision retry policy",
    },
    { env: { MINNI_USE_LEDGER: "off" } },
  );
  assert.equal(resOff.code, 0);
  assert.equal(observedQueries.at(-1), "Check heronbodydecision retry policy");
  const bodyOff = parseEnvelopeBody(resOff.output);
  assert.doesNotMatch(bodyOff.recall_pointer ?? "", /pelican/i);
  assert.match(bodyOff.recall_pointer ?? "", /heron/i);

  // 4-layer nested wrapper (maxWrapperDepth = 4) combined with <system-reminder>:
  const fourLayerPrompt =
    "Another Claude session sent a message:\n" +
    '<cross-session-message from="pelicanheaderjournal">\n' +
    "[Workflow harness pelicanheaderjournal] " +
    "[Artifact comment pelicanheaderjournal] " +
    "<system-reminder>pelicanheaderjournal internal reminder</system-reminder>\n" +
    "Verify heronbodydecision retry backoff policy\n" +
    "</cross-session-message>";
  const resFourLayer = await runHook(fixture, "UserPromptSubmit", {
    session_id: sessionId,
    prompt_id: "pid-relay-4layer",
    prompt: fourLayerPrompt,
  });
  assert.equal(resFourLayer.code, 0);
  assert.equal(observedQueries.at(-1), "Verify heronbodydecision retry backoff policy");
});
