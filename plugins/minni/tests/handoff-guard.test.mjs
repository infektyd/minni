import assert from "node:assert/strict";
import test from "node:test";

const {
  planHandoffDelivery,
} = await import("../dist/handoff_guard.js");

test("direct handoff cannot impersonate another agent", () => {
  assert.throws(
    () =>
      planHandoffDelivery({
        runtimeAgent: "codex",
        fromAgent: "claude-code",
        toAgent: "codex",
        task: "Continue the implementation from Claude's current notes.",
      }),
    /cannot impersonate another agent/,
  );
});

test("information requests route to the ping contract instead of direct handoff", () => {
  const plan = planHandoffDelivery({
    runtimeAgent: "codex",
    fromAgent: "codex",
    toAgent: "claude-code",
    task: "Ask Claude what its vault remembers about the launchd hardening pass.",
    openQuestions: ["What does your private memory say about launchd socket hardening?"],
  });

  assert.equal(plan.kind, "ping_required");
  assert.equal(plan.toAgent, "claude-code");
  assert.match(plan.question, /vault remembers/i);
  assert.match(plan.purpose, /direct handoff/i);
});

test("work-transfer handoffs remain eligible for direct delivery", () => {
  const plan = planHandoffDelivery({
    runtimeAgent: "codex",
    fromAgent: "codex",
    toAgent: "claude-code",
    task: "Continue the frontend console implementation using the included packet.",
    openQuestions: ["Decide whether the next step is CSS cleanup or API smoke testing."],
  });

  assert.deepEqual(plan, { kind: "direct", fromAgent: "codex", toAgent: "claude-code" });
});
