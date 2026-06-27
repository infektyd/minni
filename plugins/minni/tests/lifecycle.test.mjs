// Slice c1: the passive-hook Minni lifecycle REPRESENTATION (claude-code only).
// Proves the pure representation content is correct before it is wired into the
// hook envelope (c2/c3):
//   (a) MINNI_LIFECYCLE_LINE is ONE compact line naming all 4 surfaces, with
//       `plan` annotated by its two plan-adjacent options (minni_plan, handoff)
//       and NOT a dump of Minni's ~47 affordances;
//   (b) lifecycleSurfaceForIntent maps classifyIntent labels to the surface to
//       emphasize (or null), and the generic `work`/`none` fallback gets nothing;
//   (c) buildLifecycleEmphasis returns a soft one-line signpost per surface, and
//       the `plan` emphasis names <=2 plan-adjacent options.
import assert from "node:assert/strict";
import test from "node:test";

import {
  MINNI_LIFECYCLE_LINE,
  lifecycleSurfaceForIntent,
  buildLifecycleEmphasis,
} from "../dist/agent_envelope.js";
import { classifyIntent } from "../dist/task.js";

test("c1: the lifecycle line is one compact line naming all four surfaces", () => {
  // one line — persistent visibility must not be a wall of text
  assert.ok(!MINNI_LIFECYCLE_LINE.includes("\n"), "must be a single line");
  assert.ok(MINNI_LIFECYCLE_LINE.length < 280, "must stay compact");
  for (const surface of ["prepare_task", "prepare_outcome", "plan", "learn"]) {
    assert.ok(MINNI_LIFECYCLE_LINE.includes(surface), `names ${surface}`);
  }
});

test("c1: plan names only its two plan-adjacent options, not all of Minni", () => {
  assert.ok(MINNI_LIFECYCLE_LINE.includes("minni_plan"), "names minni_plan");
  assert.ok(MINNI_LIFECYCLE_LINE.includes("handoff"), "names handoff");
  // representation-only: must NOT enumerate the long tail
  for (const noise of ["team", "ping", "drill", "vault_write", "negotiate", "compile"]) {
    assert.ok(!MINNI_LIFECYCLE_LINE.includes(noise), `does not enumerate ${noise}`);
  }
});

test("c1: intent -> surface mapping matches the classifyIntent labels", () => {
  assert.equal(lifecycleSurfaceForIntent("plan"), "plan");
  for (const ambitious of ["implement", "debug", "review", "verify"]) {
    assert.equal(lifecycleSurfaceForIntent(ambitious), "prepare_task", ambitious);
  }
  // the generic fallback and chatter get no emphasis (would fire every turn)
  assert.equal(lifecycleSurfaceForIntent("work"), null);
  assert.equal(lifecycleSurfaceForIntent("none"), null);
  assert.equal(lifecycleSurfaceForIntent(""), null);
});

test("c1: classifyIntent labels are exactly the ones the mapping handles", () => {
  // guards against task.ts drifting out from under the mapping
  assert.equal(classifyIntent("plan the architecture"), "plan");
  assert.equal(classifyIntent("implement the feature"), "implement");
  assert.equal(classifyIntent("debug the failing test"), "debug");
  assert.equal(classifyIntent("just chatting"), "work");
  // every non-work label the classifier can emit must resolve in the mapping
  for (const label of ["review", "debug", "verify", "plan", "implement"]) {
    const surface = lifecycleSurfaceForIntent(label);
    assert.ok(surface === "plan" || surface === "prepare_task", `${label} maps`);
  }
});

test("c1: emphasis is a soft one-line signpost per surface", () => {
  for (const surface of ["prepare_task", "prepare_outcome", "plan", "learn"]) {
    const line = buildLifecycleEmphasis(surface);
    assert.ok(line.length > 0 && !line.includes("\n"), `${surface} one line`);
  }
  // the plan emphasis surfaces its plan-adjacent options, not the long tail
  const planEmphasis = buildLifecycleEmphasis("plan");
  assert.ok(planEmphasis.includes("minni_plan"));
  assert.ok(planEmphasis.includes("handoff"));
  // each leaf emphasis names its own verb
  assert.ok(buildLifecycleEmphasis("prepare_task").includes("minni_prepare_task"));
  assert.ok(buildLifecycleEmphasis("prepare_outcome").includes("minni_prepare_outcome"));
  assert.ok(buildLifecycleEmphasis("learn").includes("minni_learn"));
});
