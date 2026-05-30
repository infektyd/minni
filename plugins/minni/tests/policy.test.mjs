import assert from "node:assert/strict";
import test from "node:test";

import { assessLearningQuality, routeMemoryIntent } from "../dist/policy.js";

test("routeMemoryIntent allows automatic recall but not learning", () => {
  const recall = routeMemoryIntent("continue testing the Sovereign Memory plugin from prior context");
  assert.equal(recall.action, "recall");
  assert.equal(recall.automaticAllowed, true);
  assert.equal(recall.suggestedTool, "sovereign_recall");

  const learn = routeMemoryIntent("remember this decision in Sovereign Memory");
  assert.equal(learn.action, "learn");
  assert.equal(learn.automaticAllowed, false);
  assert.equal(learn.suggestedTool, "sovereign_learn");
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
