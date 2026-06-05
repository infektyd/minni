import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  updateSlice,
  computePlanDigest,
  rehydratePlan,
  createPlan,
  persistPlan,
  setActivePlan,
  clearActivePlan,
  getActivePlan,
  resolveActivePlanView
} from "../dist/plan.js";
import { ensureVault } from "../dist/vault.js";

test("isTrivialEvidence check in updateSlice prevents trivial/empty evidence for done status", () => {
  const plan = {
    plan_id: "test-plan",
    goal: "Test goal",
    status: "draft",
    constraints: [],
    slices: [
      { id: "slice-1", title: "Slice 1", status: "pending" }
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "test",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1
  };
  plan.plan_digest = computePlanDigest(plan);

  // updateSlice to done with empty evidence -> should throw
  assert.throws(() => {
    updateSlice(plan, "slice-1", "done", "");
  }, /substantive evidence is required/);

  // updateSlice to done with trivial evidence -> should throw
  assert.throws(() => {
    updateSlice(plan, "slice-1", "done", "lgtm");
  }, /substantive evidence is required/);

  assert.throws(() => {
    updateSlice(plan, "slice-1", "done", "x");
  }, /substantive evidence is required/);

  // updateSlice to done with less than 8 characters -> should throw
  assert.throws(() => {
    updateSlice(plan, "slice-1", "done", "fixed");
  }, /substantive evidence is required/);

  // updateSlice to done with substantive evidence (>= 8 chars and non-trivial) -> should pass
  const updated = updateSlice(plan, "slice-1", "done", "Verification: verified in logs/test.log file");
  assert.equal(updated.slices[0].status, "done");
  assert.equal(updated.slices[0].evidence, "Verification: verified in logs/test.log file");
});

test("updateSlice requires reason for blocked status", () => {
  const plan = {
    plan_id: "test-plan",
    goal: "Test goal",
    status: "draft",
    constraints: [],
    slices: [
      { id: "slice-1", title: "Slice 1", status: "pending" }
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "test",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1
  };
  plan.plan_digest = computePlanDigest(plan);

  // updateSlice to blocked with empty/whitespace evidence -> should throw
  assert.throws(() => {
    updateSlice(plan, "slice-1", "blocked", "   ");
  }, /blocked requires a reason in `evidence`/);

  // updateSlice to blocked with any non-trivial/non-empty reason -> should pass
  const updated = updateSlice(plan, "slice-1", "blocked", "API is down");
  assert.equal(updated.slices[0].status, "blocked");
  assert.equal(updated.slices[0].evidence, "API is down");
});

test("changing evidence in a slice changes the plan digest", () => {
  const plan1 = {
    plan_id: "test-plan",
    goal: "Test goal",
    status: "draft",
    constraints: [],
    slices: [
      { id: "slice-1", title: "Slice 1", status: "done", evidence: "Verified with code build passing" }
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "test",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1
  };

  const plan2 = {
    ...plan1,
    slices: [
      { id: "slice-1", title: "Slice 1", status: "done", evidence: "Verified with code build failing" }
    ]
  };

  const digest1 = computePlanDigest(plan1);
  const digest2 = computePlanDigest(plan2);

  assert.notEqual(digest1, digest2, "digest should change when slice evidence changes");
});

test("rehydratePlan rejects tampered note with done slice having empty evidence", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-tamper-"));
  try {
    await ensureVault(root);
    
    // We will create a plan note manually where a slice is marked 'done' but has empty evidence.
    const notePath = path.join(root, "wiki", "artifacts", "plan-test.md");
    const rawContent = `---
plan_id: plan-test
status: active
plan_goal: Test tampering
plan_slices: [{"id":"slice-1","title":"Tampered Slice","status":"done","evidence":""}]
plan_digest: dummy
created: 2026-06-05T00:00:00.000Z
updated: 2026-06-05T00:00:00.000Z
plan_rev: 1
---

# Test
`;
    await mkdir(path.dirname(notePath), { recursive: true });
    await writeFile(notePath, rawContent, "utf8");

    await assert.rejects(
      async () => {
        await rehydratePlan(notePath);
      },
      /rehydratePlan: slice slice-1 is 'done' without evidence/
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("rehydratePlan rejects note with mismatched/tampered digest", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-digest-"));
  try {
    await ensureVault(root);
    
    // Create a plan note with wrong digest.
    const notePath = path.join(root, "wiki", "artifacts", "plan-test.md");
    const rawContent = `---
plan_id: plan-test
status: active
plan_goal: Test digest verification
plan_slices: [{"id":"slice-1","title":"Slice 1","status":"pending"}]
plan_digest: wrongdigest1234
created: 2026-06-05T00:00:00.000Z
updated: 2026-06-05T00:00:00.000Z
plan_rev: 1
---

# Test
`;
    await mkdir(path.dirname(notePath), { recursive: true });
    await writeFile(notePath, rawContent, "utf8");

    await assert.rejects(
      async () => {
        await rehydratePlan(notePath);
      },
      /rehydratePlan: plan_digest mismatch/
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("active plan pointer management and resolution", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-active-"));
  try {
    await ensureVault(root);

    // Initial state: no active plan
    const initial = await getActivePlan(root);
    assert.equal(initial, undefined);

    const initialView = await resolveActivePlanView(root);
    assert.equal(initialView, undefined);

    // Create a plan
    const { plan, write } = await createPlan(
      { goal: "Complete the active plan pointer", vaultPath: root },
      { vaultPath: root }
    );

    // Creating plan should automatically activate it
    const active = await getActivePlan(root);
    assert.ok(active);
    assert.equal(active.plan_id, plan.plan_id);
    assert.equal(active.notePath, write.notePath);

    // Resolve view
    const viewResult = await resolveActivePlanView(root);
    assert.ok(viewResult);
    assert.equal(viewResult.plan_id, plan.plan_id);
    assert.equal(viewResult.rev, plan.rev);
    assert.equal(viewResult.view.goal, "Complete the active plan pointer");

    // Deactivate it
    await clearActivePlan(root);
    const cleared = await getActivePlan(root);
    assert.equal(cleared, undefined);

    const clearedView = await resolveActivePlanView(root);
    assert.equal(clearedView, undefined);

    // Reactivate plan
    await setActivePlan(root, plan.plan_id, write.notePath);
    const reactivated = await getActivePlan(root);
    assert.ok(reactivated);
    assert.equal(reactivated.plan_id, plan.plan_id);

    // Status change to accepted makes it resolve to undefined
    plan.status = "accepted";
    // We need to re-persist with the status change so rehydratePlan sees it
    await persistPlan(plan, { vaultPath: root, notePath: write.notePath });

    const finishedView = await resolveActivePlanView(root);
    assert.equal(finishedView, undefined);

  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
