// Slice c1: the passive-hook Minni lifecycle REPRESENTATION (claude-code only).
// Proves the pure representation content is correct before it is wired into the
// hook envelope (c2/c3):
//   (a) MINNI_LIFECYCLE_LINE is ONE compact line naming all 4 surfaces, with
//       `thread` annotated by its two thread-adjacent options (minni_thread_*, handoff)
//       and NOT a dump of Minni's ~47 affordances;
//   (b) lifecycleSurfaceForIntent maps classifyIntent labels to the surface to
//       emphasize (or null), and the generic `work`/`none` fallback gets nothing;
//   (c) buildLifecycleEmphasis returns a soft one-line signpost per surface, and
//       the `thread` emphasis names <=2 thread-adjacent options.
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
  for (const surface of ["prepare_task", "prepare_outcome", "thread", "learn"]) {
    assert.ok(MINNI_LIFECYCLE_LINE.includes(surface), `names ${surface}`);
  }
});

test("c1: thread names only its two thread-adjacent options, not all of Minni", () => {
  assert.ok(MINNI_LIFECYCLE_LINE.includes("minni_thread_"), "names minni_thread_*");
  assert.ok(MINNI_LIFECYCLE_LINE.includes("handoff"), "names handoff");
  // representation-only: must NOT enumerate the long tail
  for (const noise of ["team", "ping", "drill", "vault_write", "negotiate", "compile"]) {
    assert.ok(!MINNI_LIFECYCLE_LINE.includes(noise), `does not enumerate ${noise}`);
  }
});

test("c1: intent -> surface mapping matches the classifyIntent labels", () => {
  // classifyIntent still emits the "plan" intent -- the intent vocabulary is
  // frozen; the minni:threads rename moved only the lifecycle SURFACE name.
  assert.equal(lifecycleSurfaceForIntent("plan"), "thread");
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
    assert.ok(surface === "thread" || surface === "prepare_task", `${label} maps`);
  }
});

// Regression guard for the minni:plan -> minni:threads rename. Adding a bare
// `thread` alternative to classifyIntent's plan regex looks harmless but is not:
// "thread" is an overloaded English word that appears far more often in
// CONCURRENCY prompts than in planning ones, so it silently relabels
// implement/debug/work turns as "plan" and fires the wrong lifecycle emphasis.
// The rename needs no such alternative — the "plan" intent already reaches the
// renamed "thread" SURFACE through lifecycleSurfaceForIntent.
test("classifyIntent does not treat concurrency work as planning (threads-rename guard)", () => {
  for (const concurrency of [
    "make the cache thread-safe",
    "the thread pool is sized wrong",
    "convert this to a threaded worker",
    "multithreaded access to the queue",
    "summarize this email thread",
  ]) {
    assert.notEqual(
      classifyIntent(concurrency),
      "plan",
      `"${concurrency}" is concurrency/prose work, not planning`,
    );
  }
  // ...while genuine planning prompts still classify as planning
  assert.equal(classifyIntent("plan the architecture"), "plan");
  assert.equal(classifyIntent("design the migration"), "plan");
});

test("c1: emphasis is a soft one-line signpost per surface", () => {
  for (const surface of ["prepare_task", "prepare_outcome", "thread", "learn"]) {
    const line = buildLifecycleEmphasis(surface);
    assert.ok(line.length > 0 && !line.includes("\n"), `${surface} one line`);
  }
  // the thread emphasis surfaces its thread-adjacent options, not the long tail
  const threadEmphasis = buildLifecycleEmphasis("thread");
  assert.ok(threadEmphasis.includes("minni_thread_"));
  assert.ok(threadEmphasis.includes("handoff"));
  // each leaf emphasis names its own verb
  assert.ok(buildLifecycleEmphasis("prepare_task").includes("minni_prepare_task"));
  assert.ok(buildLifecycleEmphasis("prepare_outcome").includes("minni_prepare_outcome"));
  assert.ok(buildLifecycleEmphasis("learn").includes("minni_learn"));
});
