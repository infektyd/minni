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
  resolveActivePlanView,
  addScar,
  compactPlanView
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

test("addScar pure function and compactPlanView scars surfacing", () => {
  const plan = {
    plan_id: "test-plan",
    goal: "Test goal",
    status: "draft",
    constraints: [],
    slices: [],
    open_questions: [],
    scar_tissue: [
      { kind: "failed_command", signal: "run test", resolution: "fixed setup" }
    ],
    next_action: "test",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1
  };
  plan.plan_digest = computePlanDigest(plan);

  // 1. addScar - new entry
  const entry1 = { kind: "dead_end", signal: "tried approach X", resolution: "rejected approach" };
  const plan2 = addScar(plan, entry1);

  assert.notEqual(plan, plan2, "addScar should be pure (return a new object)");
  assert.equal(plan.scar_tissue.length, 1, "original plan scar_tissue should not be mutated");
  assert.equal(plan2.scar_tissue.length, 2, "new plan scar_tissue should have the added entry");
  assert.deepEqual(plan2.scar_tissue[1], entry1);

  // 2. addScar - duplicate entry kind+signal updates resolution instead of duplicating
  const entry2 = { kind: "failed_command", signal: "run test", resolution: "better fix" };
  const plan3 = addScar(plan2, entry2);
  assert.equal(plan3.scar_tissue.length, 2, "duplicate kind+signal should not append");
  assert.equal(plan3.scar_tissue[0].resolution, "better fix", "resolution should be updated");

  // 3. compactPlanView - scars array contains last 3 entries
  const entry3 = { kind: "rejected_hypothesis", signal: "hypothesis Y" };
  const entry4 = { kind: "dead_end", signal: "direction Z" };
  const plan4 = addScar(addScar(plan3, entry3), entry4); // now has 4 scars: 1 updated, 1 added, 2 more added

  const view = compactPlanView(plan4);
  assert.equal(view.scar_tissue, 4);
  assert.ok(Array.isArray(view.scars));
  assert.equal(view.scars.length, 3);
  assert.equal(view.scars[0], "dead_end: tried approach X");
  assert.equal(view.scars[1], "rejected_hypothesis: hypothesis Y");
  assert.equal(view.scars[2], "dead_end: direction Z");
});

test("rehydratePlan round-trips evidence containing backslashes (regex/path proofs) without false digest mismatch", async () => {
  // Regression for the live defect observed 2026-06-05 in codex's Runtime V4 plan:
  // a `done` slice whose evidence contained a `rg 'malloc\(|free\('` proof produced a
  // false-positive plan_digest mismatch on the next status/update, because the custom
  // frontmatter reader unescaped \" and \n but NOT \\, doubling every backslash on the
  // write->read round-trip. The writer (vault.ts yamlValue) uses JSON.stringify, so the
  // reader must use JSON.parse (its exact inverse).
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-backslash-"));
  try {
    await ensureVault(root);
    // NOTE: in this JS source, "\\(" is a single literal backslash + "(", matching the
    // real evidence string codex wrote.
    const evidence =
      "Build passed. `rg -n 'malloc\\(|free\\(|swift_' Sources/Support/uart_rx_irq.c` " +
      "returned no matches; pytest 6/6, full suite 35/35.";

    const { plan } = await createPlan(
      {
        goal: "Backslash evidence round-trip",
        slices: [{ title: "irq driver", gate: "no alloc in IRQ context" }],
        vaultPath: root,
      },
      { vaultPath: root },
    );

    const sliceId = plan.slices[0].id;
    const updated = updateSlice(plan, sliceId, "done", evidence);
    const writeRes = await persistPlan(updated, { vaultPath: root });

    // Must NOT throw a false digest mismatch, and evidence must survive byte-identical.
    const rehydrated = await rehydratePlan(writeRes.notePath);
    const got = rehydrated.slices.find((s) => s.id === sliceId);
    assert.ok(got, "slice should survive rehydrate");
    assert.equal(
      got.evidence,
      evidence,
      "evidence with backslashes must round-trip byte-identical",
    );
    assert.equal(
      rehydrated.plan_digest,
      computePlanDigest(rehydrated),
      "recomputed digest must match the stored digest after a write->read round-trip",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("P3: compactPlanView leads with progress headline so a closed slice is not read as a closed plan", () => {
  const mk = (statuses) => ({
    plan_id: "p3",
    goal: "g",
    status: "draft",
    constraints: [],
    slices: statuses.map((st, i) => ({ id: `s${i + 1}`, title: `S${i + 1}`, status: st })),
    open_questions: [],
    scar_tissue: [],
    next_action: "x",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1,
  });

  // slice 1 of 5 done -> headline must convey 1/5 + remaining + "not complete", NOT completion
  const v1 = compactPlanView(mk(["done", "pending", "pending", "pending", "pending"]));
  assert.equal(v1.progress.done, 1);
  assert.equal(v1.progress.total, 5);
  assert.equal(v1.progress.remaining, 4);
  assert.equal(v1.progress.complete, false);
  assert.match(v1.headline, /1\/5/);
  assert.match(v1.headline, /NOT complete/i);
  assert.doesNotMatch(v1.headline, /^PLAN COMPLETE/);

  // all 5 resolved (incl. one superseded) -> complete headline
  const v2 = compactPlanView(mk(["done", "done", "done", "superseded", "done"]));
  assert.equal(v2.progress.complete, true);
  assert.equal(v2.progress.remaining, 0);
  assert.match(v2.headline, /PLAN COMPLETE/);
});

test("P10: completing the last slice moves the plan to a terminal status (accepted) and back if reopened", () => {
  const base = {
    plan_id: "p10",
    goal: "g",
    status: "draft",
    constraints: [],
    slices: [
      { id: "a", title: "A", status: "pending" },
      { id: "b", title: "B", status: "done", evidence: "B verified in test log output" },
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "x",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1,
  };
  base.plan_digest = computePlanDigest(base);

  // close the last open slice -> all resolved -> plan auto-transitions draft -> accepted
  const done = updateSlice(base, "a", "done", "A verified by running the suite, 12/12 passed");
  assert.equal(done.status, "accepted", "plan should become terminal when all slices resolve");

  // reopening a slice un-finishes the plan -> accepted reverts to draft
  const reopened = updateSlice(done, "a", "in_progress");
  assert.equal(reopened.status, "draft", "reopening a slice should revert the terminal status");
});
