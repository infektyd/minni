import assert from "node:assert/strict";
import test from "node:test";

import { assessLearningQuality, routeMemoryIntent } from "../dist/policy.js";
import { lifecycleNudgeMode } from "../dist/agent_envelope.js";

test("routeMemoryIntent allows automatic recall but not learning", () => {
  const recall = routeMemoryIntent("continue testing the Sovereign Memory plugin from prior context");
  assert.equal(recall.action, "recall");
  assert.equal(recall.automaticAllowed, true);
  assert.equal(recall.suggestedTool, "minni_recall");

  const learn = routeMemoryIntent("remember this decision in Sovereign Memory");
  assert.equal(learn.action, "learn");
  assert.equal(learn.automaticAllowed, false);
  assert.equal(learn.suggestedTool, "minni_learn");
});

test("question-form 'what did we learn about X?' routes to recall, not learn (S15)", () => {
  for (const q of [
    "what did we learn about the FAISS sync bug?",
    "What have we learned about retries here",
    "did we learn anything about the parser?",
  ]) {
    const out = routeMemoryIntent(q);
    assert.equal(out.action, "recall", `expected recall for: ${q}`);
    assert.equal(out.suggestedTool, "minni_recall");
    assert.equal(out.automaticAllowed, true);
  }
  // An imperative learn request (not a question) still routes to learn.
  const imperative = routeMemoryIntent("learn this: retries must be idempotent");
  assert.equal(imperative.action, "learn");
});

test("lifecycleNudgeMode trims/lowercases the disable value (PR90-7)", () => {
  assert.equal(lifecycleNudgeMode({}), "soft");
  assert.equal(lifecycleNudgeMode({ MINNI_LIFECYCLE_NUDGE_MODE: "off" }), "off");
  assert.equal(lifecycleNudgeMode({ MINNI_LIFECYCLE_NUDGE_MODE: "OFF" }), "off");
  assert.equal(lifecycleNudgeMode({ MINNI_LIFECYCLE_NUDGE_MODE: "  Off  " }), "off");
  assert.equal(lifecycleNudgeMode({ MINNI_LIFECYCLE_NUDGE_MODE: "soft" }), "soft");
});

test("assessLearningQuality rewards sourced durable notes and warns on weak notes", () => {
  const good = assessLearningQuality({
    title: "Codex recall ranking decision",
    content: "Codex recall should show vault context packs before broad daemon semantic results.",
    category: "decision",
    source: "unit-test",
  });
  assert.equal(good.ok, true);
  assert.equal(good.warnings.length, 0);

  const weak = assessLearningQuality({
    title: "todo",
    content: "maybe store the thing later",
  });
  assert.equal(weak.ok, false);
  assert.match(weak.summary, /Title is very short/);
  assert.match(weak.summary, /vague wording/);
});
