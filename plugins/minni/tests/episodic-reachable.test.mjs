// Audit #225-R1: the episodic layer was advertised by the `layer` enum and by
// BOOT_RECALL_LAYERS, but nothing on either side of the wire could carry an
// episodic hit — the daemon had no episodic call site, and the plugin had no
// place to put one. These pin the plugin half.

import assert from "node:assert/strict";
import test from "node:test";

import {
  BOOT_RECALL_LAYERS,
  formatEpisodic,
  formatRecall,
  formatRecallLean,
  isDaemonResultEmpty,
} from "../dist/sovereign.js";

const EVENT = {
  event_id: 7,
  agent_id: "codex",
  event_type: "observation",
  thread_id: "t-1",
  content: "deployment rollback completed on the edge fleet",
};

test("boot recall still asks for the episodic layer", () => {
  assert.ok(
    BOOT_RECALL_LAYERS.includes("episodic"),
    "if episodic is dropped from the boot layers, the wire below is dead weight",
  );
});

test("formatEpisodic renders episodic hits", () => {
  const out = formatEpisodic({ episodic: [EVENT] });
  assert.ok(out.includes("Episodic Events"));
  assert.ok(out.includes("deployment rollback"));
  assert.ok(out.includes("observation"));
});

test("formatEpisodic is absent, not empty, when there are no events", () => {
  assert.equal(formatEpisodic({ episodic: [] }), undefined);
  assert.equal(formatEpisodic({}), undefined);
});

test("formatEpisodic caps the rendered events and says how many it dropped", () => {
  const many = Array.from({ length: 9 }, (_, i) => ({ ...EVENT, event_id: i }));
  const out = formatEpisodic({ episodic: many }, 5);
  assert.ok(out.includes("4 further episodic hit(s) omitted"));
});

test("formatRecall surfaces the episodic section", () => {
  const out = formatRecall("rollback", { results: [], count: 0, episodic: [EVENT] });
  assert.ok(
    out.includes("deployment rollback"),
    "an episodic-only answer must not render as an empty recall",
  );
});

test("formatRecallLean surfaces the episodic section (the boot path)", () => {
  const out = formatRecallLean("rollback", { results: [], count: 0, episodic: [EVENT] });
  assert.ok(out.includes("deployment rollback"));
});

test("an episodic-only answer is NOT treated as an empty daemon result", () => {
  assert.equal(
    isDaemonResultEmpty({ results: [], count: 0, episodic: [EVENT] }),
    false,
    "reporting empty would trigger the workspace-unscoped vault pre-scan and bury the hit",
  );
});

test("a genuinely empty answer is still empty", () => {
  assert.equal(isDaemonResultEmpty({ results: [], count: 0, episodic: [] }), true);
});
