import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, rm, utimes, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  updateSlice,
  unmetDependencies,
  diffDependsOn,
  diffSupersededDependencies,
  landedReplanTopology,
  applySliceDelta,
  structuralProposalDelta,
  computePlanDigest,
  computePlanDigestV1,
  computePlanDigestHexV2,
  PlanDigestVersionError,
  PlanHistoryAppendError,
  rehydratePlan,
  createPlan,
  replan,
  persistPlan,
  findPlanNote,
  journalPathFor,
  setActivePlan,
  clearActivePlan,
  getActivePlan,
  resolveActivePlanView,
  addScar,
  compactPlanView,
  shelfDrift,
  appendJournal,
  parseJournal,
  appendHistorySnapshot,
  historyPathFor,
  readHistory
} from "../dist/plan.js";
import { appendFileWithFsync as realAppendFileWithFsync, ensureVault, writeFileAtomic, writeVaultPage } from "../dist/vault.js";

test("createPlan rejects duplicate explicit slice ids before writing", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-duplicate-create-"));
  try {
    await assert.rejects(
      createPlan({
        goal: "Reject duplicate explicit slice ids",
        slices: [
          { id: "same", title: "First" },
          { id: "same", title: "Second" },
        ],
        vaultPath: root,
      }, { vaultPath: root }),
      /duplicate explicit slice id "same"/,
    );
    await assert.rejects(
      createPlan({
        goal: "Reject generated and explicit slice id collision",
        slices: [
          { title: "Generated Same" },
          { id: "generated-same", title: "Explicit Same" },
        ],
        vaultPath: root,
      }, { vaultPath: root }),
      /duplicate slice id "generated-same"/,
    );
    await assert.rejects(
      readdir(path.join(root, "wiki", "artifacts")),
      /ENOENT/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("replan rejects duplicate explicit slice ids before graph mutation", () => {
  const plan = {
    plan_id: "duplicate-replan",
    goal: "Reject duplicate replan ids",
    status: "draft",
    constraints: [],
    slices: [
      { id: "a", title: "Original A", status: "pending" },
      { id: "b", title: "Original B", status: "pending" },
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "a",
    plan_digest: "",
    created: "2026-08-18T12:00:00.000Z",
    updated: "2026-08-18T12:00:00.000Z",
    rev: 1,
  };
  plan.plan_digest = computePlanDigest(plan);
  assert.throws(
    () => replan(plan, [
      { id: "a", title: "First A" },
      { id: "a", title: "Second A" },
    ]),
    /duplicate explicit slice id "a"/,
  );
  assert.deepEqual(plan.slices.map((slice) => slice.title), [
    "Original A",
    "Original B",
  ]);
});

test("strict rehydrate rejects a digest-valid note with duplicate slice ids", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-duplicate-note-"));
  try {
    const { plan, write } = await createPlan({
      goal: "Reject persisted duplicate ids",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B" },
      ],
      vaultPath: root,
    }, { vaultPath: root });
    const duplicate = {
      ...plan,
      slices: [
        { ...plan.slices[0] },
        { ...plan.slices[1], id: "a" },
      ],
    };
    duplicate.plan_digest = computePlanDigest(duplicate);
    const raw = await readFile(write.notePath, "utf8");
    const tampered = raw
      .replace(
        /^plan_slices:.*$/m,
        `plan_slices: ${JSON.stringify(duplicate.slices)}`,
      )
      .replace(/^plan_digest:.*$/m, `plan_digest: ${duplicate.plan_digest}`);
    await writeFile(write.notePath, tampered, "utf8");

    await assert.rejects(
      rehydratePlan(write.notePath),
      /duplicate slice id "a"/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

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

test("getActivePlan discards a tampered pointer whose notePath escapes the vault", async () => {
  const { activePointerPath } = await import("../dist/plan.js");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-tamper-"));
  try {
    await ensureVault(root);
    await mkdir(path.dirname(activePointerPath(root)), { recursive: true });
    for (const evil of ["../../outside.md", "/etc/passwd", `${root}/../outside.md`]) {
      await writeFile(
        activePointerPath(root),
        JSON.stringify({ plan_id: "plan-x", notePath: evil, set_at: new Date().toISOString() }),
        "utf8",
      );
      assert.equal(await getActivePlan(root), undefined, `tampered notePath must be discarded: ${evil}`);
      assert.equal(await resolveActivePlanView(root), undefined, evil);
    }
    // A legitimate in-vault pointer still resolves (the guard is not over-broad).
    const { plan, write } = await createPlan(
      { goal: "Containment guard sanity", vaultPath: root },
      { vaultPath: root },
    );
    await setActivePlan(root, plan.plan_id, write.notePath);
    const active = await getActivePlan(root);
    assert.ok(active);
    assert.equal(active.plan_id, plan.plan_id);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("getActivePlan audits a rejected traversal instead of discarding it silently (PLUMB-T4 / #231)", async () => {
  const { activePointerPath } = await import("../dist/plan.js");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-tamper-audit-"));
  const originalConsoleError = console.error;
  const calls = [];
  console.error = (...args) => {
    calls.push(args.join(" "));
  };
  try {
    await ensureVault(root);
    await mkdir(path.dirname(activePointerPath(root)), { recursive: true });
    const evil = "../../outside.md";
    await writeFile(
      activePointerPath(root),
      JSON.stringify({ plan_id: "plan-x", notePath: evil, set_at: new Date().toISOString() }),
      "utf8",
    );

    const result = await getActivePlan(root);

    assert.equal(result, undefined, "the escaped pointer must still be discarded");
    assert.equal(
      calls.length,
      1,
      `traversal rejection must be audited exactly once, got: ${JSON.stringify(calls)}`,
    );
    assert.match(calls[0], /getActivePlan rejected/, "audit line must name the rejection");
    assert.ok(
      calls[0].includes(evil),
      `audit line must include the offending notePath: ${calls[0]}`,
    );
  } finally {
    console.error = originalConsoleError;
    await rm(root, { recursive: true, force: true });
  }
});

test("getActivePlan does not audit the routine absent-pointer case (no active plan yet)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-no-pointer-"));
  const originalConsoleError = console.error;
  const calls = [];
  console.error = (...args) => {
    calls.push(args.join(" "));
  };
  try {
    await ensureVault(root);
    const result = await getActivePlan(root);
    assert.equal(result, undefined);
    assert.equal(
      calls.length,
      0,
      `routine "no pointer file yet" must not be logged as a security event: ${JSON.stringify(calls)}`,
    );
  } finally {
    console.error = originalConsoleError;
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

test("P10: completing the last slice moves the plan to a terminal status (complete) and back if reopened", () => {
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

  // H6: closing the last open slice -> all resolved -> plan auto-transitions
  // draft -> "complete" (a terminal, NON-recallable status; model-driven
  // completion must never self-promote into the recallable "accepted").
  const done = updateSlice(base, "a", "done", "A verified by running the suite, 12/12 passed");
  assert.equal(done.status, "complete", "plan should become terminal (complete) when all slices resolve");
  assert.notEqual(done.status, "accepted", "model completion must not reach the recallable accepted status");

  // reopening a slice un-finishes the plan -> complete reverts to draft
  const reopened = updateSlice(done, "a", "in_progress");
  assert.equal(reopened.status, "draft", "reopening a slice should revert the terminal status");
});

// ── C5 / plan-N3: id-less active-plan addressing ─────────────────────────────

test("resolvePlanIdOrActive prefers an explicit plan_id and trims it", async () => {
  const { resolvePlanIdOrActive } = await import("../dist/plan.js");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-resolve-"));
  try {
    await ensureVault(root);
    assert.deepEqual(await resolvePlanIdOrActive(root, "  plan-abc123  "), {
      plan_id: "plan-abc123",
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolvePlanIdOrActive falls back to the active plan when plan_id is omitted", async () => {
  const { resolvePlanIdOrActive } = await import("../dist/plan.js");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-resolve-active-"));
  try {
    await ensureVault(root);
    const { plan } = await createPlan(
      { goal: "id-less addressing", slices: [{ title: "only slice" }], vaultPath: root },
      { vaultPath: root },
    );
    // createPlan set the active pointer; omitted/blank ids resolve to it.
    assert.deepEqual(await resolvePlanIdOrActive(root, undefined), { plan_id: plan.plan_id });
    assert.deepEqual(await resolvePlanIdOrActive(root, "   "), { plan_id: plan.plan_id });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolvePlanIdOrActive returns a clear error when nothing is active", async () => {
  const { resolvePlanIdOrActive } = await import("../dist/plan.js");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-resolve-none-"));
  try {
    await ensureVault(root);
    const result = await resolvePlanIdOrActive(root, undefined);
    assert.ok("error" in result);
    assert.match(result.error, /no active plan/);
    assert.match(result.error, /minni_thread_activate/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("minni_thread_status/_update/_history accept an OPTIONAL plan_id (C5 schema pin)", async () => {
  // The acceptance spec requires the id-less form VERBATIM on these three
  // tools; pin the schemas so a refactor cannot quietly re-require plan_id.
  const source = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  // minni_thread_replan and minni_thread_scar are beyond the verbatim spec but
  // pinned too (review panel): a hookless agent must be able to replan or
  // record a dead-end against "the active plan" without round-tripping the id
  // through minni_thread_status.
  for (const tool of ["minni_thread_status", "minni_thread_update", "minni_thread_history", "minni_thread_replan", "minni_thread_scar"]) {
    const start = source.indexOf(`"${tool}"`);
    assert.ok(start >= 0, `${tool} must be registered`);
    const block = source.slice(start, source.indexOf("server.registerTool", start + 1));
    assert.match(
      block,
      /plan_id:\s*z\.string\(\)\.min\(1\)\.optional\(\)/,
      `${tool} must accept an optional plan_id (default = active plan)`,
    );
    assert.match(
      block,
      /resolvePlanTarget\(/,
      `${tool} must resolve the active plan when plan_id is omitted`,
    );
  }
  // ...and the shared helper itself must defer to resolvePlanIdOrActive so
  // the five handlers keep the active-plan default through one code path.
  const helperStart = source.indexOf("async function resolvePlanTarget(");
  assert.ok(helperStart >= 0, "shared resolvePlanTarget helper must exist");
  const helper = source.slice(helperStart, helperStart + 1800);
  assert.match(
    helper,
    /resolvePlanIdOrActive\(/,
    "resolvePlanTarget must default to the active plan via resolvePlanIdOrActive",
  );
  assert.match(
    helper,
    /try \{/,
    "resolvePlanTarget must catch remaining discovery I/O instead of leaking a raw JSON-RPC error",
  );
  assert.match(
    helper,
    /threadWorkerErrorText\(error\)/,
    "resolvePlanTarget must sanitize discovery throws through threadWorkerErrorText",
  );
});

test("every id-less plan tool returns the no-active-plan error end-to-end through the MCP server", async (t) => {
  const { spawn } = await import("node:child_process");
  const net = await import("node:net");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-mcp-"));
  const home = path.join(root, "home");
  const socketPath = path.join(home, "minnid.sock");
  await mkdir(home, { recursive: true });
  const fakeDaemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.split("\n")[0]);
      const respond = (result) => {
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      };
      if (request.method === "gate.shared") {
        respond({ ok: true, status: "allowed" });
        return;
      }
      respond({ ok: true });
    });
  });
  await new Promise((resolve) => fakeDaemon.listen(socketPath, resolve));
  t.after(() => fakeDaemon.close());
  const serverPath = new URL("../dist/server.js", import.meta.url).pathname;
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: root,
      MINNI_CLAUDECODE_VAULT_PATH: root,
      MINNI_KILOCODE_VAULT_PATH: root,
      MINNI_GROK_VAULT_PATH: root,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  try {
    const responses = new Map();
    let buffered = "";
    const waiters = new Map();
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      let nl;
      while ((nl = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined) {
            responses.set(msg.id, msg);
            waiters.get(msg.id)?.(msg);
          }
        } catch {
          // non-JSON noise on stdout would be a protocol bug; surface via timeout
        }
      }
    });
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 15000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout waiting for response ${id}`)), ms);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });

    send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "plan-e2e-test", version: "0.0.0" },
      },
    });
    const init = await awaitResponse(1);
    assert.ok(init.result, JSON.stringify(init));
    send({ jsonrpc: "2.0", method: "notifications/initialized" });

    // Each tool called WITHOUT plan_id (other required args supplied so zod
    // validation passes and the handler's resolvePlanIdOrActive path runs).
    const calls = [
      ["minni_thread_status", {}],
      ["minni_thread_update", { slice_id: "slice-1", status: "in_progress" }],
      ["minni_thread_history", {}],
      ["minni_thread_replan", { new_slices: [] }],
      ["minni_thread_scar", { kind: "dead_end", signal: "tried the obvious thing" }],
    ];
    let id = 2;
    for (const [name, args] of calls) {
      send({
        jsonrpc: "2.0",
        id,
        method: "tools/call",
        params: { name, arguments: args },
      });
      const reply = await awaitResponse(id);
      assert.ok(reply.result, `${name}: ${JSON.stringify(reply)}`);
      const body = JSON.parse(reply.result.content[0].text);
      assert.ok(body.error, `${name}: ${JSON.stringify(body)}`);
      assert.match(body.error, /no plan_id provided and no active plan/, name);
      assert.match(body.error, /minni_thread_activate/, name);
      id += 1;
    }
  } finally {
    child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
});

// H7: a plan persisted before the digest was widened carries the legacy (v1)
// digest. rehydratePlan must NOT hard-fail it as "tampered" — it must recognize
// the v1 digest, load the plan, and UPGRADE the stored digest to v2 in place.
// (A digest matching NEITHER algorithm is still a genuine tamper and throws,
// which the "rejects note with mismatched/tampered digest" test above covers.)
test("H7: rehydratePlan upgrades a pre-H7 (v1-digest) plan instead of hard-failing it", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-h7-migrate-"));
  try {
    await ensureVault(root);

    // Build and persist a real plan (persistPlan writes the current v2 digest).
    const { plan, write } = await createPlan(
      { goal: "Digest migration coverage", vaultPath: root },
      { vaultPath: root },
    );
    const notePath = write.notePath;

    // Canonicalize by rehydrating once, then compute what the OLD (v1) digest
    // would have been for this exact plan, and rewrite the note to carry it —
    // simulating a plan persisted before the H7 widening.
    const canonical = await rehydratePlan(notePath);
    const v1 = computePlanDigestV1(canonical);
    const v2 = computePlanDigest(canonical);
    assert.notEqual(v1, v2, "v1 and v2 digests must differ for this to be a real migration");

    const before = await readFile(notePath, "utf8");
    const withLegacy = before
      .replace(/^plan_digest: .*$/m, `plan_digest: ${v1}`)
      // #122: a real pre-H7 note carries no plan_digest_v field either; with it
      // present the declared-version check would (correctly) flag the v1 hex.
      .replace(/^plan_digest_v:.*\n/m, "");
    assert.notEqual(withLegacy, before, "expected to rewrite the plan_digest line");
    await writeFile(notePath, withLegacy, "utf8");

    // Rehydrate the pre-H7 note: must succeed (no throw) and report the v2 digest.
    const upgraded = await rehydratePlan(notePath);
    assert.equal(upgraded.plan_id, plan.plan_id);
    assert.equal(upgraded.plan_digest, v2, "loaded plan must carry the upgraded v2 digest");

    // And the upgrade must be persisted back to the note (v2 on disk now).
    const after = await readFile(notePath, "utf8");
    assert.match(after, new RegExp(`^plan_digest: ${v2}$`, "m"), "note must be re-persisted with the v2 digest");
    assert.doesNotMatch(after, new RegExp(`^plan_digest: ${v1}$`, "m"), "legacy digest must be replaced");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Task 2 (Thread Phase 1): worker slice metadata + digest v3 compatibility.
// ---------------------------------------------------------------------------

/** Minimal two-slice plan for pure digest-coverage assertions (no I/O). */
function makePlan() {
  const plan = {
    plan_id: "digest-v3-plan",
    goal: "digest v3 coverage",
    status: "draft",
    constraints: [],
    slices: [
      { id: "a", title: "Slice A", status: "pending" },
      { id: "b", title: "Slice B", status: "pending" },
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "test",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1,
  };
  plan.plan_digest = computePlanDigest(plan);
  return plan;
}

/**
 * Applies `extra` to slice index 0 ONLY, leaving slice count, slice "b", and
 * every other plan-level field byte-for-byte identical to `base`. This is
 * the isolation review finding #1 fixed: the original version of this
 * helper replaced the whole `slices` array with a ONE-element array
 * (dropping slice "b" entirely), so every assertion below would have passed
 * even if computePlanDigestHexV3 ignored every new field completely — the
 * digest would still have changed purely because the slice COUNT changed.
 * Preserving slice "b" and everything else means the *only* possible cause
 * of a digest change is the specific field(s) in `extra`.
 */
function withSliceZeroField(base, extra) {
  return {
    ...base,
    slices: base.slices.map((slice, i) => (i === 0 ? { ...slice, ...extra } : slice)),
  };
}

test("digest v3 changes for assignment, generation, claim metadata, and proposals", () => {
  const base = makePlan();
  const variants = [
    { assigned_to: "worker-a" },
    { generation: 2 },
    { attempt: 1 },
    { claim: {
      claim_id: "claim-a",
      worker_agent_id: "worker-a",
      claimed_at: "2026-08-18T00:00:00.000Z",
      expires_at: "2026-08-18T00:10:00.000Z",
    }},
    { proposals: [{ kind: "contract", reason: "enough evidence", slice_ids: ["b"] }] },
  ];
  for (const extra of variants) {
    const changed = withSliceZeroField(base, extra);
    // Sanity guard for the isolation itself: slice count and slice "b" must
    // be untouched, so a failure below can only be explained by the
    // specific field in `extra`, never a structural side effect.
    assert.equal(changed.slices.length, base.slices.length, JSON.stringify(extra));
    assert.deepEqual(changed.slices[1], base.slices[1], JSON.stringify(extra));
    assert.notEqual(computePlanDigest(base), computePlanDigest(changed), JSON.stringify(extra));
  }
});

// Gate T2 requires EVERY new durable slice field to affect v3, not just the
// five exercised above — `requirements` and `assignment_profile` are the
// remaining two named in the interface (plan.ts PlanSlice).
test("digest v3 also changes for requirements and assignment_profile", () => {
  const base = makePlan();
  const variants = [
    { requirements: ["needs-shell-access"] },
    { assignment_profile: "profile-research" },
  ];
  for (const extra of variants) {
    const changed = withSliceZeroField(base, extra);
    assert.equal(changed.slices.length, base.slices.length, JSON.stringify(extra));
    assert.deepEqual(changed.slices[1], base.slices[1], JSON.stringify(extra));
    assert.notEqual(computePlanDigest(base), computePlanDigest(changed), JSON.stringify(extra));
  }
});

/**
 * Writes a real vault note that DECLARES digest v2 (plan_digest_v: 2, bare
 * v2-computed hex) — simulating a note still owned by an older-plugin host
 * mid rolling-upgrade. Caller is responsible for removing the returned root.
 */
async function writeDeclaredV2Plan() {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-declared-v2-"));
  await ensureVault(root);
  const { plan, write } = await createPlan(
    { goal: "declared v2 compatibility", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
    { vaultPath: root },
  );
  const raw = await readFile(write.notePath, "utf8");
  const v2Hex = computePlanDigestHexV2(plan);
  const rewritten = raw
    .replace(/^plan_digest:.*$/m, `plan_digest: ${v2Hex}`)
    .replace(/^plan_digest_v:.*$/m, "plan_digest_v: 2");
  assert.notEqual(rewritten, raw, "expected to actually stamp a declared v2 note for this fixture");
  await writeFile(write.notePath, rewritten, "utf8");
  return { notePath: write.notePath, plan, root };
}

test("rehydratePlan reads declared v2 without write-on-read upgrade", async () => {
  const fixture = await writeDeclaredV2Plan();
  try {
    const before = await readFile(fixture.notePath, "utf8");
    const plan = await rehydratePlan(fixture.notePath);
    const after = await readFile(fixture.notePath, "utf8");
    assert.equal(plan.plan_id, fixture.plan.plan_id);
    assert.equal(after, before);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("rehydratePlan reads declared v1 without write-on-read upgrade", async () => {
  // Same contract as declared v2 above, but for a note that declares the
  // OLDEST known algorithm via plan_digest_v: 1 (v1 predates the plan_digest_v
  // tagging field, but the declared-version gate treats it uniformly through
  // PLAN_DIGEST_ALGORITHMS — this must not be special-cased into a write).
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-declared-v1-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "declared v1 compatibility", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    const v1Hex = computePlanDigestV1(plan);
    const rewritten = raw
      .replace(/^plan_digest:.*$/m, `plan_digest: ${v1Hex}`)
      .replace(/^plan_digest_v:.*$/m, "plan_digest_v: 1");
    assert.notEqual(rewritten, raw, "expected to actually stamp a declared v1 note for this fixture");
    await writeFile(write.notePath, rewritten, "utf8");

    const before = await readFile(write.notePath, "utf8");
    const rehydrated = await rehydratePlan(write.notePath);
    const after = await readFile(write.notePath, "utf8");
    assert.equal(rehydrated.plan_id, plan.plan_id);
    assert.equal(after, before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

const V3_ONLY_FIELD_SAMPLES = {
  requirements: ["needs-shell-access"],
  assigned_to: "worker-a",
  assignment_profile: "profile-research",
  generation: 2,
  attempt: 1,
  claim: {
    claim_id: "claim-a",
    worker_agent_id: "worker-a",
    claimed_at: "2026-08-18T00:00:00.000Z",
    expires_at: "2026-08-18T00:10:00.000Z",
  },
  proposals: [{ kind: "contract", reason: "enough evidence", slice_ids: ["s1"] }],
};

/**
 * Writes a note that DECLARES an older digest version (1 or 2) whose
 * declared-algorithm digest genuinely validates — computed over a slice that
 * ALSO carries one v3-only field the older algorithm never looks at. This is
 * exactly the review's tamper scenario: a real v1/v2 algorithm run (proven
 * below) picks specific known keys and ignores anything else, so the
 * declared hash matching says nothing about whether the field was
 * injected/edited outside a genuine older writer's reach.
 */
async function writeDeclaredOlderPlanWithV3Field(declaredVersion, field) {
  const root = await mkdtemp(path.join(tmpdir(), `sm-plan-declared-v${declaredVersion}-v3field-`));
  await ensureVault(root);
  const { plan, write } = await createPlan(
    { goal: `declared v${declaredVersion} tamper probe`, slices: [{ id: "s1", title: "t1" }], vaultPath: root },
    { vaultPath: root },
  );
  const mutatedSlices = [{ ...plan.slices[0], [field]: V3_ONLY_FIELD_SAMPLES[field] }];
  const mutatedPlan = { ...plan, slices: mutatedSlices };
  const olderAlgo = declaredVersion === 1 ? computePlanDigestV1 : computePlanDigestHexV2;
  const olderHex = olderAlgo(mutatedPlan);
  // Prove the older algorithm truly ignores this field — otherwise this
  // fixture would not be testing what it claims to.
  assert.equal(olderHex, olderAlgo(plan), `expected declared-v${declaredVersion} digest to be blind to "${field}"`);

  const raw = await readFile(write.notePath, "utf8");
  const rewritten = raw
    .replace(/^plan_slices:.*$/m, `plan_slices: ${JSON.stringify(JSON.stringify(mutatedSlices))}`)
    .replace(/^plan_digest:.*$/m, `plan_digest: ${olderHex}`)
    .replace(/^plan_digest_v:.*$/m, `plan_digest_v: ${declaredVersion}`);
  assert.notEqual(rewritten, raw, "expected to actually inject a v3-only field into this fixture");
  await writeFile(write.notePath, rewritten, "utf8");
  return { notePath: write.notePath, root };
}

test("rehydratePlan rejects a declared v2 note whose slice carries a v3-only field outside v2's digest coverage", async (t) => {
  for (const field of Object.keys(V3_ONLY_FIELD_SAMPLES)) {
    await t.test(field, async () => {
      const fixture = await writeDeclaredOlderPlanWithV3Field(2, field);
      try {
        const before = await readFile(fixture.notePath, "utf8");
        await assert.rejects(
          () => rehydratePlan(fixture.notePath),
          (err) => {
            assert.match(err.message, /v3-only field/);
            assert.match(err.message, new RegExp(field));
            assert.match(err.message, /tampered/);
            return true;
          },
        );
        const after = await readFile(fixture.notePath, "utf8");
        assert.equal(after, before, "a rejected read must not rewrite the note either");
      } finally {
        await rm(fixture.root, { recursive: true, force: true });
      }
    });
  }
});

test("rehydratePlan rejects a declared v1 note whose slice carries a v3-only field outside v1's digest coverage", async () => {
  const fixture = await writeDeclaredOlderPlanWithV3Field(1, "assigned_to");
  try {
    const before = await readFile(fixture.notePath, "utf8");
    await assert.rejects(
      () => rehydratePlan(fixture.notePath),
      (err) => {
        assert.match(err.message, /v3-only field/);
        assert.match(err.message, /assigned_to/);
        return true;
      },
    );
    const after = await readFile(fixture.notePath, "utf8");
    assert.equal(after, before);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

/**
 * Writes a note with NO plan_digest_v field at all (the pre-H7 legacy
 * shape) whose plan_digest genuinely equals computePlanDigestV1 — but one
 * slice ALSO carries a v3-only field the v1 algorithm never looks at. This
 * is the re-review's scenario: an UNDECLARED note is just as blind to the
 * seven v3-only keys as a declared-v1 note is, and — unlike the declared
 * case — it was about to be silently upgraded (persisted) as a genuine v3
 * note, which would bless the injected field permanently.
 */
async function writeUndeclaredV1PlanWithV3Field(field) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-undeclared-v1-v3field-"));
  await ensureVault(root);
  const { write } = await createPlan(
    { goal: "undeclared v1 tamper probe", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
    { vaultPath: root },
  );
  const canonical = await rehydratePlan(write.notePath);
  const mutatedSlices = [{ ...canonical.slices[0], [field]: V3_ONLY_FIELD_SAMPLES[field] }];
  const mutatedPlan = { ...canonical, slices: mutatedSlices };
  const v1Hex = computePlanDigestV1(mutatedPlan);
  // Prove v1 is genuinely blind to this field, exactly as for the declared
  // case above — otherwise this fixture would not test what it claims.
  assert.equal(v1Hex, computePlanDigestV1(canonical), `expected undeclared-v1 digest to be blind to "${field}"`);

  const raw = await readFile(write.notePath, "utf8");
  const rewritten = raw
    .replace(/^plan_slices:.*$/m, `plan_slices: ${JSON.stringify(JSON.stringify(mutatedSlices))}`)
    .replace(/^plan_digest:.*$/m, `plan_digest: ${v1Hex}`)
    // A real pre-H7 note carries no plan_digest_v field either (see the H7
    // test above) — with it present this would hit the DECLARED branch,
    // not the undeclared-legacy fallback this test targets.
    .replace(/^plan_digest_v:.*\n/m, "");
  assert.notEqual(rewritten, raw, "expected to actually inject a v3-only field into this fixture");
  await writeFile(write.notePath, rewritten, "utf8");
  return { notePath: write.notePath, root };
}

test("rehydratePlan rejects an undeclared legacy (no plan_digest_v) valid-v1 note whose slice carries a v3-only field", async (t) => {
  for (const field of ["assigned_to", "claim"]) {
    await t.test(field, async () => {
      const fixture = await writeUndeclaredV1PlanWithV3Field(field);
      try {
        const before = await readFile(fixture.notePath, "utf8");
        await assert.rejects(
          () => rehydratePlan(fixture.notePath),
          (err) => {
            assert.match(err.message, /v3-only field/);
            assert.match(err.message, new RegExp(field));
            assert.match(err.message, /tampered/);
            return true;
          },
        );
        const after = await readFile(fixture.notePath, "utf8");
        assert.equal(after, before, "a rejected read must not upgrade/rewrite the note either");
      } finally {
        await rm(fixture.root, { recursive: true, force: true });
      }
    });
  }
});

test("rehydratePlan still upgrades a CLEAN undeclared legacy (no plan_digest_v) valid-v1 note on read", async () => {
  // The new legacy-path guard above must not be a false positive: a genuine
  // pre-H7 note with none of the seven v3-only keys must keep upgrading in
  // place exactly as the pre-existing H7 test already proves — this test
  // re-confirms that specifically alongside the new tamper guard, using the
  // same note shape (single slice, no plan_digest_v) as the rejection test
  // above so the only variable between "clean" and "tampered" is the
  // injected field itself.
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-undeclared-v1-clean-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "undeclared v1 clean upgrade", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const canonical = await rehydratePlan(write.notePath);
    const v1Hex = computePlanDigestV1(canonical);
    const v3Hex = computePlanDigest(canonical);
    assert.notEqual(v1Hex, v3Hex, "v1 and v3 digests must differ for this to be a real migration");

    const raw = await readFile(write.notePath, "utf8");
    const withLegacy = raw
      .replace(/^plan_digest:.*$/m, `plan_digest: ${v1Hex}`)
      .replace(/^plan_digest_v:.*\n/m, "");
    assert.notEqual(withLegacy, raw, "expected to actually rewrite plan_digest to the legacy v1 hex");
    await writeFile(write.notePath, withLegacy, "utf8");

    const upgraded = await rehydratePlan(write.notePath);
    assert.equal(upgraded.plan_digest, v3Hex, "a clean undeclared v1 note must still upgrade to the current digest");

    const after = await readFile(write.notePath, "utf8");
    assert.match(after, new RegExp(`^plan_digest: ${v3Hex}$`, "m"), "note must be re-persisted with the current digest");
    assert.match(after, /^plan_digest_v: 3$/m, "upgrade must stamp the current plan_digest_v going forward");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

/**
 * Writes a note with NO plan_digest_v field at all whose plan_digest
 * genuinely equals computePlanDigestHexV2 — simulating a genuine v2-era
 * writer that predated the plan_digest_v tagging field. One slice ALSO
 * carries a v3-only field the v2 algorithm never looks at, mirroring
 * writeUndeclaredV1PlanWithV3Field above but for the "bare v2-or-v1"
 * fallback's other half. Task 2 final review: this half of the undeclared
 * fallback regressed to "v1 only" and stopped recognizing v2 at all, which
 * this test's sibling (the clean bare-v2 upgrade test) targets; this test
 * targets the OTHER failure mode — a bare-v2 note tampered with a v3-only
 * field must still be rejected, not silently upgraded/blessed.
 */
async function writeUndeclaredV2PlanWithV3Field(field) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-undeclared-v2-v3field-"));
  await ensureVault(root);
  const { write } = await createPlan(
    { goal: "undeclared v2 tamper probe", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
    { vaultPath: root },
  );
  const canonical = await rehydratePlan(write.notePath);
  const mutatedSlices = [{ ...canonical.slices[0], [field]: V3_ONLY_FIELD_SAMPLES[field] }];
  const mutatedPlan = { ...canonical, slices: mutatedSlices };
  const v2Hex = computePlanDigestHexV2(mutatedPlan);
  assert.equal(v2Hex, computePlanDigestHexV2(canonical), `expected undeclared-v2 digest to be blind to "${field}"`);

  const raw = await readFile(write.notePath, "utf8");
  const rewritten = raw
    .replace(/^plan_slices:.*$/m, `plan_slices: ${JSON.stringify(JSON.stringify(mutatedSlices))}`)
    .replace(/^plan_digest:.*$/m, `plan_digest: ${v2Hex}`)
    .replace(/^plan_digest_v:.*\n/m, "");
  assert.notEqual(rewritten, raw, "expected to actually inject a v3-only field into this fixture");
  await writeFile(write.notePath, rewritten, "utf8");
  return { notePath: write.notePath, root };
}

test("rehydratePlan rejects an undeclared legacy (no plan_digest_v) valid-v2 note whose slice carries a v3-only field", async (t) => {
  for (const field of ["assigned_to", "claim"]) {
    await t.test(field, async () => {
      const fixture = await writeUndeclaredV2PlanWithV3Field(field);
      try {
        const before = await readFile(fixture.notePath, "utf8");
        await assert.rejects(
          () => rehydratePlan(fixture.notePath),
          (err) => {
            assert.match(err.message, /v3-only field/);
            assert.match(err.message, new RegExp(field));
            assert.match(err.message, /tampered/);
            return true;
          },
        );
        const after = await readFile(fixture.notePath, "utf8");
        assert.equal(after, before, "a rejected read must not upgrade/rewrite the note either");
      } finally {
        await rm(fixture.root, { recursive: true, force: true });
      }
    });
  }
});

test("rehydratePlan still upgrades a CLEAN undeclared legacy (no plan_digest_v) valid-v2 note on read", async () => {
  // The "bare v2-or-v1" contract (see PLAN_DIGEST_VERSION's doc comment)
  // means an undeclared note validating against v2 — not just v1 — must
  // keep upgrading in place. Same note shape (single slice, no
  // plan_digest_v) as the rejection test above, no injected field, so the
  // only variable is the field itself.
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-undeclared-v2-clean-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "undeclared v2 clean upgrade", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const canonical = await rehydratePlan(write.notePath);
    const v2Hex = computePlanDigestHexV2(canonical);
    const v3Hex = computePlanDigest(canonical);
    assert.notEqual(v2Hex, v3Hex, "v2 and v3 digests must differ for this to be a real migration");

    const raw = await readFile(write.notePath, "utf8");
    const withLegacy = raw
      .replace(/^plan_digest:.*$/m, `plan_digest: ${v2Hex}`)
      .replace(/^plan_digest_v:.*\n/m, "");
    assert.notEqual(withLegacy, raw, "expected to actually rewrite plan_digest to the legacy v2 hex");
    await writeFile(write.notePath, withLegacy, "utf8");

    const upgraded = await rehydratePlan(write.notePath);
    assert.equal(upgraded.plan_digest, v3Hex, "a clean undeclared v2 note must still upgrade to the current digest");

    const after = await readFile(write.notePath, "utf8");
    assert.match(after, new RegExp(`^plan_digest: ${v3Hex}$`, "m"), "note must be re-persisted with the current digest");
    assert.match(after, /^plan_digest_v: 3$/m, "upgrade must stamp the current plan_digest_v going forward");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("rehydratePlan normalizes a declared v1 note's interim 'v1:<hex>' tag to bare hex in memory without writing the note", async () => {
  // Focused assertion for the review's second ask: the no-write-on-read
  // contract only says the FILE is left alone for a declared-older version.
  // The returned in-memory plan.plan_digest must still be the bare hex, not
  // leak the "v1:" on-disk tag encoding to callers.
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-declared-v1-tag-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "declared v1 interim tag", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const v1Hex = computePlanDigestV1(plan);
    const raw = await readFile(write.notePath, "utf8");
    const rewritten = raw
      .replace(/^plan_digest:.*$/m, `plan_digest: v1:${v1Hex}`)
      .replace(/^plan_digest_v:.*$/m, "plan_digest_v: 1");
    assert.notEqual(rewritten, raw);
    await writeFile(write.notePath, rewritten, "utf8");

    const before = await readFile(write.notePath, "utf8");
    const rehydrated = await rehydratePlan(write.notePath);
    const after = await readFile(write.notePath, "utf8");
    assert.equal(rehydrated.plan_digest, v1Hex, "in-memory digest must be normalized to bare hex");
    assert.equal(after, before, "the note itself must not be rewritten for a declared-older version");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("rehydratePlan still normalizes an interim 'v3:<hex>' tag on the CURRENT declared version", async () => {
  // The no-write-on-read rule above applies only to a declared OLDER
  // algorithm. A note whose EFFECTIVE declared version is already current
  // (v3) but still carries an interim "v3:<hex>" prefix (as an in-flight
  // build of this task might emit transiently) keeps normalizing to bare
  // hex on read, exactly like the pre-existing v1->v2 interim-tag behavior.
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-declared-v3-tag-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "declared v3 interim tag", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    assert.match(raw, /^plan_digest_v: 3$/m, "new plans must declare the current v3 algorithm");
    const bare = raw.match(/^plan_digest: "?([0-9a-f]{16})"?$/m)?.[1];
    assert.ok(bare, "expected a bare-hex digest to prefix");
    await writeFile(
      write.notePath,
      raw.replace(/^plan_digest:.*$/m, `plan_digest: v3:${bare}`),
      "utf8",
    );

    const rehydrated = await rehydratePlan(write.notePath);
    assert.equal(rehydrated.plan_digest, bare, "in-memory digest must be normalized bare hex");
    const rewritten = await readFile(write.notePath, "utf8");
    assert.match(rewritten, /^plan_digest: "?[0-9a-f]{16}"?$/m, "note must be re-stamped bare hex");
    assert.match(rewritten, /^plan_digest_v: 3$/m);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("rehydratePlan still rejects a newer-than-current declared digest version", async () => {
  // Preserve the existing newer-version fail-closed contract across the v3
  // bump: a plugin build that has never heard of v4 must refuse to guess at
  // it, not treat it as tampered and not silently downgrade-write it.
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-declared-newer-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "newer than v3", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    await writeFile(write.notePath, raw.replace(/^plan_digest_v:.*$/m, "plan_digest_v: 4"), "utf8");
    await assert.rejects(
      () => rehydratePlan(write.notePath),
      (err) => {
        assert.ok(err instanceof PlanDigestVersionError, "must be the typed newer-version error");
        assert.equal(err.code, "PLAN_DIGEST_NEWER");
        assert.match(err.message, /newer than this plugin/);
        assert.equal(err.notePath, write.notePath, "notePath stays a typed internal field");
        assert.equal(
          err.message.includes(write.notePath),
          false,
          "PlanDigestVersionError.message must not interpolate notePath",
        );
        assert.equal(err.message.includes("wiki/artifacts"), false);
        return true;
      },
    );
    const untouched = await readFile(write.notePath, "utf8");
    assert.match(untouched, /^plan_digest_v: 4$/m, "newer-version note must not be rewritten");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("createPlan with shelf_ref configures shelfDrift (punch-list §4b: MCP layer must actually thread shelf_ref through)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-shelf-"));
  try {
    await ensureVault(root);
    const shelfContent = "# Identity shelf\nAgent: codex\nRole: worker";
    const { plan } = await createPlan(
      {
        goal: "Wire shelf_ref through minni_thread_create",
        vaultPath: root,
        shelf_ref: {
          agent: "codex",
          wikilink: "[[wiki/identity/codex]]",
          pull_hint: "pull before each session",
          shelf_content: shelfContent,
        },
      },
      { vaultPath: root },
    );

    assert.ok(plan.shelf_ref, "plan.shelf_ref must be set when shelf_ref is passed to createPlan");
    assert.equal(plan.shelf_ref.agent, "codex");
    assert.equal(plan.shelf_ref.wikilink, "[[wiki/identity/codex]]");
    assert.ok(plan.shelf_ref.shelf_hash, "shelf_hash must be derived from shelf_content");

    // Matching live content: drift check reports configured + not drifted.
    const matched = shelfDrift(plan, shelfContent);
    assert.equal(matched.configured, true);
    assert.equal(matched.drifted, false);

    // Divergent live content: still configured, now flags drift.
    const drifted = shelfDrift(plan, `${shelfContent}\nRole: reviewer`);
    assert.equal(drifted.configured, true);
    assert.equal(drifted.drifted, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("createPlan without shelf_ref leaves shelfDrift unconfigured (regression guard for the punch-list symptom)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-noshelf-"));
  try {
    await ensureVault(root);
    const { plan } = await createPlan({ goal: "No shelf attached", vaultPath: root }, { vaultPath: root });
    assert.equal(plan.shelf_ref, undefined);
    const drift = shelfDrift(plan, "anything");
    assert.equal(drift.configured, false);
    assert.equal(drift.note, "no shelf attached");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── #295 (June audit N8): shelf drift auto-checked at resolveActivePlanView ─
//
// shelfDrift() used to be reachable only by explicitly passing
// live_shelf_content to minni_thread_status — drift was found only when
// someone thought to check. resolveActivePlanView now optionally accepts the
// live shelf content (the SessionStart handler's own already-read
// layer1/core.md body) and attaches a shelf_drift result automatically.

test("resolveActivePlanView attaches shelf_drift when live content is supplied and the plan has a shelf_ref", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-autodrift-"));
  try {
    await ensureVault(root);
    const shelfContent = "# Identity shelf\nAgent: codex\nRole: worker";
    await createPlan(
      {
        goal: "auto drift check",
        vaultPath: root,
        shelf_ref: {
          agent: "codex",
          wikilink: "[[wiki/identity/codex]]",
          pull_hint: "pull before each session",
          shelf_content: shelfContent,
        },
      },
      { vaultPath: root },
    );

    const matched = await resolveActivePlanView(root, shelfContent);
    assert.ok(matched);
    assert.ok(matched.shelf_drift, "shelf_drift must be attached when live content is supplied");
    assert.equal(matched.shelf_drift.configured, true);
    assert.equal(matched.shelf_drift.drifted, false);

    const drifted = await resolveActivePlanView(root, `${shelfContent}\nRole: reviewer`);
    assert.ok(drifted);
    assert.equal(drifted.shelf_drift.configured, true);
    assert.equal(drifted.shelf_drift.drifted, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveActivePlanView omits shelf_drift entirely when no live content is supplied (not a misleading 'unconfigured')", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-nodrift-arg-"));
  try {
    await ensureVault(root);
    await createPlan(
      {
        goal: "no live content passed",
        vaultPath: root,
        shelf_ref: {
          agent: "codex",
          wikilink: "[[wiki/identity/codex]]",
          pull_hint: "pull before each session",
          shelf_content: "# shelf",
        },
      },
      { vaultPath: root },
    );

    const view = await resolveActivePlanView(root);
    assert.ok(view);
    assert.equal(
      Object.hasOwn(view, "shelf_drift"),
      false,
      "shelf_drift must be OMITTED, not present as configured:false, when the caller passed no live content — " +
        "a degraded/absent shelf read upstream must read as 'not checked', not 'checked and fine'",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveActivePlanView never crashes on a plan with no shelf_ref even when live content is supplied", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-noshelfref-drift-"));
  try {
    await ensureVault(root);
    await createPlan({ goal: "no shelf attached at all", vaultPath: root }, { vaultPath: root });

    const view = await resolveActivePlanView(root, "some live content nobody asked to compare against");
    assert.ok(view);
    assert.equal(Object.hasOwn(view, "shelf_drift"), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Wave 2 residual: self-heal wrote legacy appendJournal({ kind: "status_reconciled" })
// which the ordered parser ignores. Heal must land on the ordered cursor.
test("resolveActivePlanView self-heal writes status_reconciled onto the ordered cursor", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-heal-ordered-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      {
        goal: "Finish the stuck plan",
        slices: [
          { id: "s1", title: "Slice one" },
          { id: "s2", title: "Slice two" },
        ],
        vaultPath: root,
      },
      { vaultPath: root },
    );
    plan.slices[0] = {
      ...plan.slices[0],
      status: "done",
      evidence: "tests/plan.test.mjs ordered self-heal pin",
    };
    plan.slices[1] = {
      ...plan.slices[1],
      status: "superseded",
      superseded_by: "replan-test",
    };
    assert.equal(plan.status, "draft");
    await persistPlan(plan, { vaultPath: root, notePath: write.notePath });

    const view = await resolveActivePlanView(root);
    assert.equal(view, undefined);

    const healed = await rehydratePlan(write.notePath);
    assert.equal(healed.status, "complete");

    const { readThreadEvents } = await import("../dist/thread-events.js");
    const journalPath = journalPathFor(write.notePath, plan.plan_id);
    const { events } = await readThreadEvents(journalPath, 0, 100);
    const reconciled = events.find((event) => event.kind === "status_reconciled");
    assert.ok(
      reconciled,
      `expected ordered status_reconciled, got kinds: ${events.map((e) => e.kind)}`,
    );
    assert.equal(typeof reconciled.seq, "number");
    assert.deepEqual(reconciled.payload, { from: "draft", to: "complete" });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Wave 2 residual: FS/lock failures from getActivePlan / resolveActivePlanView
// used to return undefined and look like "no active plan."
test("resolveActivePlanView distinguishes FS errors from an empty active pointer", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-active-fs-"));
  try {
    await ensureVault(root);
    const pointerPath = path.join(root, "wiki", "artifacts", "_active_plan.json");
    await mkdir(path.dirname(pointerPath), { recursive: true });
    await rm(pointerPath, { force: true });
    await mkdir(pointerPath); // EISDIR on readFile

    await assert.rejects(
      resolveActivePlanView(root),
      (error) =>
        error?.code === "ACTIVE_PLAN_READ_FAILED" &&
        typeof error.message === "string" &&
        !error.message.includes(pointerPath),
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("getActivePlan still returns undefined for a missing pointer (empty, not error)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-active-empty-"));
  try {
    await ensureVault(root);
    assert.equal(await getActivePlan(root), undefined);
    assert.equal(await resolveActivePlanView(root), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Wave 2 residual: a live held lock used to be indistinguishable from "no
// active plan" if the view swallowed THREAD_BUSY. Domain rethrows; this pin
// is the missing active-plan-view proof.
test("resolveActivePlanView rethrows THREAD_BUSY when the plan lock is held", { timeout: 20_000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-active-busy-"));
  try {
    await ensureVault(root);
    const { plan } = await createPlan(
      { goal: "held lock is busy, not empty", vaultPath: root },
      { vaultPath: root },
    );
    const key = createHash("sha256").update(plan.plan_id).digest("hex").slice(0, 32);
    const lockDir = path.join(root, ".runtime", "thread-locks", `${key}.lock`);
    await mkdir(lockDir, { recursive: true });
    await writeFile(
      path.join(lockDir, "owner.json"),
      `${JSON.stringify({
        pid: process.pid,
        operationId: "live-holder",
        acquiredAt: "2026-01-01T00:00:00.000Z",
      })}\n`,
      { mode: 0o600 },
    );
    const old = new Date("2026-01-01T00:00:00.000Z");
    await utimes(lockDir, old, old);

    await assert.rejects(
      resolveActivePlanView(root),
      (error) => error?.code === "THREAD_BUSY",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── PLUMB-T4 / #231: active pointer is written atomically ───────────────────
//
// A crash mid-write used to leave a truncated `_active_plan.json` because
// setActivePlan called plain writeFile. It must go through writeFileAtomic
// (temp + fsync + rename) which already exists in vault.ts.
test("setActivePlan writes _active_plan.json via writeFileAtomic (PLUMB-T4 / #231)", async () => {
  const src = await readFile(new URL("../src/plan.ts", import.meta.url), "utf8");
  assert.match(
    src,
    /writeFileAtomic/,
    "plan.ts must import/use writeFileAtomic for the active pointer",
  );
  // The pointer write itself must call writeFileAtomic(pointerPath, ...), not
  // a bare writeFile on the same path.
  assert.match(
    src,
    /await writeFileAtomic\(\s*pointerPath/,
    "setActivePlan must write the pointer through writeFileAtomic",
  );
  assert.doesNotMatch(
    src,
    /await writeFile\(\s*pointerPath/,
    "setActivePlan must not use non-atomic writeFile for the pointer",
  );

  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-atomic-pointer-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "atomic active pointer", vaultPath: root },
      { vaultPath: root },
    );
    await setActivePlan(root, plan.plan_id, write.notePath);
    const pointerPath = path.join(root, "wiki", "artifacts", "_active_plan.json");
    const pointer = JSON.parse(await readFile(pointerPath, "utf8"));
    assert.equal(pointer.plan_id, plan.plan_id);
    assert.equal(pointer.notePath, write.notePath);
    // No leftover temp siblings from a half-finished atomic write.
    const artifacts = await readdir(path.dirname(pointerPath));
    assert.equal(
      artifacts.filter((name) => name.startsWith("_active_plan.json.") && name.endsWith(".tmp")).length,
      0,
      "atomic write must not leave .tmp siblings",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── #293 (June audit N6, sibling of PLUMB-T4/#231): journal + plan note ────
//
// history.jsonl was fsync'd (appendFileWithFsync) but the journal
// (appendJournal) and the plan note (writeVaultPage) were plain
// appendFile/writeFile — a crash could leave history durable and the
// journal/note behind it or truncated. Same durability guarantee as the
// pointer write #231 already fixed.

// Bugbot on #309 (campaign scar #3 — source-grep tests are false confidence):
// the original version of this test read plan.ts's source text and regex-
// matched the helper names. A mutant that RENAMES the call trips a regex but
// proves nothing about behavior, and a mutant that keeps the name/signature
// but swaps the durable helper's internals for a plain write sails straight
// past a text assertion. Spy on the actual injected dependency instead: prove
// the caller invokes appendFileWithFsync/writeFileAtomic — with the right
// path and content — as an observed call, not grepped text.
test("appendJournal invokes appendFileWithFsync/writeFileAtomic with the right path and content (#293)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-journal-spy-"));
  try {
    const journalPath = path.join(root, "spy.log.md");
    const appendCalls = [];
    const atomicCalls = [];
    const deps = {
      appendFileWithFsync: async (p, c) => {
        appendCalls.push([p, c]);
      },
      writeFileAtomic: async (p, c) => {
        atomicCalls.push([p, c]);
      },
    };

    // Init path: file does not exist yet -> writeFileAtomic, not appendFileWithFsync.
    await appendJournal(journalPath, { kind: "rehydrated", at: "2026-01-01T00:00:00.000Z" }, deps);
    assert.equal(appendCalls.length, 0, "init path must not call appendFileWithFsync");
    assert.equal(atomicCalls.length, 1, "init path must call writeFileAtomic exactly once");
    assert.equal(atomicCalls[0][0], journalPath, "writeFileAtomic must be called with the journal path");
    assert.match(atomicCalls[0][1], /# Minni Plan Journal/, "init write must include the header");
    assert.match(atomicCalls[0][1], /"kind":"rehydrated"/, "init write must include the first event");

    // Real write so the file genuinely exists for the append branch below —
    // the spy above recorded the call but never touched the filesystem.
    await writeFile(journalPath, atomicCalls[0][1], "utf8");

    // Append path: file now exists -> appendFileWithFsync, not writeFileAtomic again.
    await appendJournal(journalPath, { kind: "rehydrated", at: "2026-01-01T00:01:00.000Z" }, deps);
    assert.equal(atomicCalls.length, 1, "append path must not call writeFileAtomic again");
    assert.equal(appendCalls.length, 1, "append path must call appendFileWithFsync exactly once");
    assert.equal(appendCalls[0][0], journalPath, "appendFileWithFsync must be called with the journal path");
    assert.match(appendCalls[0][1], /"kind":"rehydrated".*"at":"2026-01-01T00:01/, "append write must be the second event's line");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("appendJournal round-trips real init + append with no leftover .tmp siblings (#293)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-journal-durable-"));
  try {
    const journalPath = path.join(root, "test.log.md");

    // Init path: first event creates the file atomically, no leftover .tmp.
    await appendJournal(journalPath, { kind: "rehydrated", at: "2026-01-01T00:00:00.000Z" });
    let siblings = await readdir(root);
    assert.equal(
      siblings.filter((name) => name.startsWith("test.log.md.") && name.endsWith(".tmp")).length,
      0,
      "journal init must not leave .tmp siblings",
    );

    // Append path: second event appends without disturbing the first.
    await appendJournal(journalPath, { kind: "rehydrated", at: "2026-01-01T00:01:00.000Z" });
    siblings = await readdir(root);
    assert.equal(
      siblings.filter((name) => name.startsWith("test.log.md.") && name.endsWith(".tmp")).length,
      0,
      "journal append must not leave .tmp siblings",
    );

    const text = await readFile(journalPath, "utf8");
    const events = parseJournal(text);
    assert.deepEqual(
      events.map((e) => e.at),
      ["2026-01-01T00:00:00.000Z", "2026-01-01T00:01:00.000Z"],
      "both events must be present, in order, after init + append",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Cassandra PR #371 G1: appendJournal's catch-all used to treat ANY
// append/fsync failure as "journal missing" and overwrite it via
// writeFileAtomic. This file is also the ordered Thread event journal —
// rewriting it after a failed fsync is real lost-events, not recovery.
test("appendJournal does not rewrite an existing journal when append/fsync fails", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-journal-fsync-"));
  try {
    const journalPath = path.join(root, "test.log.md");
    await appendJournal(journalPath, { kind: "rehydrated", at: "2026-01-01T00:00:00.000Z" });
    await appendJournal(journalPath, { kind: "status_changed", at: "2026-01-01T00:01:00.000Z" });
    const before = parseJournal(await readFile(journalPath, "utf8"));
    assert.deepEqual(
      before.map((e) => e.at),
      ["2026-01-01T00:00:00.000Z", "2026-01-01T00:01:00.000Z"],
    );

    const atomicCalls = [];
    const landedThenThrows = async (filePath, content) => {
      await realAppendFileWithFsync(filePath, content);
      throw Object.assign(new Error("simulated fsync failure after write landed"), {
        code: "EIO",
      });
    };

    await assert.rejects(
      () =>
        appendJournal(
          journalPath,
          { kind: "gate_passed", at: "2026-01-01T00:02:00.000Z" },
          {
            appendFileWithFsync: landedThenThrows,
            writeFileAtomic: async (p, c) => {
              atomicCalls.push([p, c]);
              await writeFileAtomic(p, c);
            },
          },
        ),
      /simulated fsync failure/,
    );

    assert.equal(
      atomicCalls.length,
      0,
      "fsync-fail on an existing journal must not rewrite via writeFileAtomic",
    );
    const after = parseJournal(await readFile(journalPath, "utf8"));
    assert.deepEqual(
      after.map((e) => e.at),
      ["2026-01-01T00:00:00.000Z", "2026-01-01T00:01:00.000Z", "2026-01-01T00:02:00.000Z"],
      "prior events must survive; the line that landed before the throw must remain",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Bugbot on #309 (campaign scar #3 — source-grep tests are false confidence):
// same lesson as appendJournal's test above. Spy on the injected dependency
// to prove writeVaultPage actually invokes writeFileAtomic with the note's
// real path and rendered body, rather than grepping vault.ts's source text.
test("writeVaultPage invokes writeFileAtomic with the note's path and rendered body (#293)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-note-spy-"));
  try {
    await ensureVault(root);
    const calls = [];
    const result = await writeVaultPage(
      {
        vaultPath: root,
        title: "Spy Note",
        content: "durability spy body",
        section: "concepts",
      },
      {
        writeFileAtomic: async (p, c) => {
          calls.push([p, c]);
        },
      },
    );
    assert.equal(calls.length, 1, "writeVaultPage must call writeFileAtomic exactly once for the note body");
    assert.equal(calls[0][0], result.notePath, "writeFileAtomic must be called with the note's own path");
    assert.match(calls[0][1], /durability spy body/, "writeFileAtomic must be called with the rendered note body");
    assert.match(calls[0][1], /^---\n/, "the rendered body must include frontmatter");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeVaultPage (plan note) round-trips real content with no leftover .tmp siblings (#293)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-note-durable-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "atomic plan note", vaultPath: root },
      { vaultPath: root },
    );
    assert.ok(plan.plan_id);
    const noteDir = path.dirname(write.notePath);
    const siblings = await readdir(noteDir);
    assert.equal(
      siblings.filter((name) => name.startsWith(path.basename(write.notePath) + ".") && name.endsWith(".tmp")).length,
      0,
      "plan note write must not leave .tmp siblings",
    );
    const body = await readFile(write.notePath, "utf8");
    assert.match(body, /atomic plan note/, "the note's full content must be readable immediately");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── minni:threads rename — artifact-format freeze guard ────────────────────
//
// The minni:plan → minni:threads rename is a TOOL/COMMAND layer rename only.
// The `plan-` id prefix is DELIBERATELY frozen and must not be "finished" into
// `thread-` later: the prefix is baked into durable, recall-visible artifacts a
// code change cannot rewrite — the note filename wiki/artifacts/plan-<hex>.md,
// the `[[plan-<hex>]]` wikilink indexed by the vault and cited from hand-written
// notes, the `plan-<hex>.log.md` journal, its history sibling, and every
// historical audit line. Changing it would split the vault into two id
// conventions and orphan every inbound wikilink.
//
// Frozen alongside it, for the same reason: the `plan_id` parameter name, the
// `plan.*` shared-gate operation strings, the `minni_plan: true` / `plan_*`
// frontmatter keys, and the `_active_plan.json` pointer filename.
//
// If a `thread-` prefix is ever genuinely wanted it needs a real migration
// (notes + journals + history + pointer + every inbound wikilink), not an edit
// to plan.ts. THIS TEST FAILING MEANS SOMEONE SKIPPED THAT MIGRATION.
test("freeze guard: createPlan still mints plan- prefixed ids after the threads rename", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-idfreeze-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "id prefix must survive the minni:threads rename", vaultPath: root },
      { vaultPath: root },
    );
    assert.match(plan.plan_id, /^plan-[0-9a-f]{16}$/, "id prefix is frozen at 'plan-'");
    // the prefix must reach the durable surfaces, not just the in-memory object
    assert.match(path.basename(write.notePath), /^plan-[0-9a-f]{16}\.md$/, "note filename is frozen");
    assert.match(write.wikilink, /\[\[.*plan-[0-9a-f]{16}\]\]/, "wikilink is frozen");

    // and lookup still resolves by the frozen frontmatter key, not the filename
    assert.equal(await findPlanNote(root, plan.plan_id), write.notePath);
    const note = await readFile(write.notePath, "utf8");
    assert.match(note, /minni_plan:\s*true/, "legacy frontmatter marker is frozen");
    assert.ok(note.includes(`plan_id: ${plan.plan_id}`) || note.includes(`plan_id: "${plan.plan_id}"`),
      "plan_id frontmatter key is frozen");

    // the active pointer file keeps its pre-rename name so an in-flight active
    // plan is not orphaned on upgrade
    await setActivePlan(root, plan.plan_id, write.notePath);
    const pointer = await readFile(path.join(root, "wiki", "artifacts", "_active_plan.json"), "utf8");
    assert.equal(JSON.parse(pointer).plan_id, plan.plan_id);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Cassandra PR #371 round 3: findPlanNote used to readFile every wiki/artifacts
// *.md, including plan-*.log.md journals, with no per-file catch. An EISDIR
// journal (or any sibling *.md directory) threw a path-bearing Node error
// before MCP handlers reached threadWorkerErrorResult.
test("findPlanNote skips an EISDIR journal and still locates the plan note", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-find-eisdir-journal-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "findPlanNote must survive a directory-at-journal-path", vaultPath: root },
      { vaultPath: root },
    );
    const journalPath = journalPathFor(write.notePath, plan.plan_id);
    await rm(journalPath, { force: true });
    await mkdir(journalPath);

    const found = await findPlanNote(root, plan.plan_id);
    assert.equal(found, write.notePath);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("findPlanNote skips an unreadable sibling *.md and still locates the plan note", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-find-eisdir-sibling-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "findPlanNote must catch per-file read failures", vaultPath: root },
      { vaultPath: root },
    );
    const poison = path.join(root, "wiki", "artifacts", "poison.md");
    await mkdir(poison);

    const found = await findPlanNote(root, plan.plan_id);
    assert.equal(found, write.notePath);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});


// ── #294 (June audit N7): history.jsonl is capped, not unbounded ───────────
//
// history.jsonl stored one full plan snapshot per revision with no bound —
// a long-running/frequently-updated plan grew this file forever (H4). Cap it
// to the most recent MINNI_PLAN_HISTORY_CAP (default 200) lines on write,
// with hysteresis (rotate only once cap+50 over, trim back to cap) so a
// long-running plan doesn't pay a full-file rewrite on every single edit.
// Behavioral, not source-grep (campaign scar #3, per #309's Bugbot round):
// these tests spy on the injected dependency to prove real invocation, and
// separately prove the real bound holds by writing real files and reading
// them back — including a real concurrent-write test proving the per-file
// lock (added after a cassandra round REPRODUCED data loss without it)
// actually prevents an already-durable revision from being erased by a
// racing rotation.
//
// HISTORY_ROTATION_HYSTERESIS (50) is an internal, unexported constant —
// these tests reference the literal 50 and must be updated together if that
// constant ever changes.

function historyLine(rev) {
  return JSON.stringify({ rev, at: `t${rev}`, digest: `d${rev}`, plan: { plan_id: "p" } });
}

async function seedHistoryFile(historyFile, count, startRev = 1) {
  const lines = [];
  for (let i = 0; i < count; i++) lines.push(historyLine(startRev + i));
  await writeFile(historyFile, lines.join("\n") + "\n", "utf8");
}

test("appendHistorySnapshot: hysteresis — no rotation at cap+50, rotation fires at cap+51", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-hyst-"));
  try {
    process.env.MINNI_PLAN_HISTORY_CAP = "2";
    try {
      // Just at the threshold: 2 (cap) + 50 (hysteresis) = 52 lines total
      // after this append must NOT trigger rotation.
      const belowFile = path.join(root, "below.history.jsonl");
      await seedHistoryFile(belowFile, 51, 1); // 51 existing + 1 new = 52
      const belowCalls = [];
      await appendHistorySnapshot(belowFile, { rev: 52, at: "t52", digest: "d", plan: { plan_id: "p" } }, {
        writeFileAtomic: async (p, c) => belowCalls.push([p, c]),
      });
      assert.equal(belowCalls.length, 0, "52 lines (== cap+hysteresis) must not trigger rotation yet");

      // One more line past the threshold: 52 existing + 1 new = 53 must rotate.
      const overFile = path.join(root, "over.history.jsonl");
      await seedHistoryFile(overFile, 52, 1); // 52 existing + 1 new = 53
      const overCalls = [];
      await appendHistorySnapshot(overFile, { rev: 53, at: "t53", digest: "d", plan: { plan_id: "p" } }, {
        writeFileAtomic: async (p, c) => overCalls.push([p, c]),
      });
      assert.equal(overCalls.length, 1, "53 lines (cap+hysteresis+1) must trigger rotation");
      const keptRevs = overCalls[0][1].trim().split("\n").map((l) => JSON.parse(l).rev);
      assert.equal(keptRevs.length, 2, "rotation must trim back down to the cap (2), not to cap+hysteresis");
      assert.deepEqual(keptRevs, [52, 53], "must keep the 2 MOST RECENT revisions");
    } finally {
      delete process.env.MINNI_PLAN_HISTORY_CAP;
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("appendHistorySnapshot: real repeated appends stay bounded at cap+hysteresis, settle at cap after rotation", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-real-"));
  try {
    const historyFile = path.join(root, "real.history.jsonl");
    process.env.MINNI_PLAN_HISTORY_CAP = "5";
    try {
      for (let rev = 1; rev <= 60; rev++) {
        await appendHistorySnapshot(historyFile, { rev, at: `t${rev}`, digest: "d", plan: { plan_id: "p" } });
      }
    } finally {
      delete process.env.MINNI_PLAN_HISTORY_CAP;
    }

    const raw = await readFile(historyFile, "utf8");
    const lines = raw.trim().split("\n");
    // After 60 appends with cap=5, hysteresis=50: rotation first fires once
    // the file exceeds 55 lines (at the 56th append), trimming to 5, then
    // grows again to 60-56+5=9 by the last append (no second rotation since
    // 9 < 55). Assert the STRUCTURAL invariant (bounded, newest present,
    // monotonically increasing) rather than a fragile exact count, since the
    // exact number depends on the hysteresis arithmetic.
    assert.ok(lines.length <= 55, `history must never exceed cap+hysteresis (55), got ${lines.length}`);
    const revs = lines.map((l) => JSON.parse(l).rev);
    assert.equal(revs[revs.length - 1], 60, "the newest revision must always be present");
    assert.deepEqual(revs, [...revs].sort((a, b) => a - b), "revisions must stay in order after rotation");

    const history = await readHistory(path.join(root, "real.md"));
    assert.deepEqual(history.map((h) => h.rev), revs, "readHistory must agree with the raw file exactly");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("appendHistorySnapshot: malformed MINNI_PLAN_HISTORY_CAP truly falls back to the DEFAULT cap (200), not a smaller value", async () => {
  // Cassandra review round: a prior version of this test only asserted
  // doesNotReject — a mutant that made planHistoryCap() return 1 instead of
  // the real default (200) passed every existing test while destroying
  // nearly all history on the next write. Assert the EFFECTIVE cap value by
  // observing real rotation behavior, not just "no exception was thrown".
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-malformed-"));
  try {
    for (const badValue of ["not-a-number", "0", "-5", "", "1e9", "1e3", "2.9", "0x10"]) {
      const historyFile = path.join(root, `bad-${Buffer.from(badValue || "empty").toString("hex")}.history.jsonl`);
      // Seed comfortably past DEFAULT_PLAN_HISTORY_CAP(200) + hysteresis(50).
      await seedHistoryFile(historyFile, 251, 1);
      process.env.MINNI_PLAN_HISTORY_CAP = badValue;
      try {
        await appendHistorySnapshot(historyFile, { rev: 252, at: "t252" });
      } finally {
        delete process.env.MINNI_PLAN_HISTORY_CAP;
      }
      const raw = await readFile(historyFile, "utf8");
      const lines = raw.trim().split("\n");
      assert.equal(
        lines.length,
        200,
        `MINNI_PLAN_HISTORY_CAP=${JSON.stringify(badValue)} must fall back to the real default (200), got ${lines.length} lines`,
      );
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("appendHistorySnapshot: an extremely large MINNI_PLAN_HISTORY_CAP is clamped to MAX_PLAN_HISTORY_CAP, not left unbounded", async () => {
  // team-lead verification found this exact escaped mutant: changing
  // MAX_PLAN_HISTORY_CAP from 100_000 to Number.MAX_SAFE_INTEGER left every
  // existing test green, because nothing observed the clamp's effect — the
  // fix that exists specifically to stop a huge/typo'd env value from
  // silently disabling the whole #294 bound had no test pinning it. Force a
  // real rotation past the clamped ceiling: seed just past
  // MAX_PLAN_HISTORY_CAP + HISTORY_ROTATION_HYSTERESIS lines, set the cap to
  // an astronomically large numeric string, and assert the file actually
  // rotates down to the clamp (100000). If the clamp were removed (or
  // widened to MAX_SAFE_INTEGER), the huge requested cap would never be
  // crossed by this seed and rotation would never fire — the surviving line
  // count would stay above 100000, catching the mutant directly.
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-cap-ceiling-"));
  try {
    const historyFile = path.join(root, "ceiling.history.jsonl");
    const MAX_CAP = 100_000;
    const HYSTERESIS = 50; // must match HISTORY_ROTATION_HYSTERESIS in plan.ts
    const SEED = MAX_CAP + HYSTERESIS; // one short of forcing rotation on its own
    await seedHistoryFile(historyFile, SEED, 1); // revs 1..100050
    process.env.MINNI_PLAN_HISTORY_CAP = "99999999999"; // huge, digit-only, would-be-valid cap
    try {
      // This append pushes the file to SEED + 1 lines, crossing the real
      // (clamped) rotation threshold of MAX_CAP + HYSTERESIS.
      await appendHistorySnapshot(historyFile, {
        rev: SEED + 1,
        at: `t${SEED + 1}`,
        digest: "d",
        plan: { plan_id: "p" },
      });
    } finally {
      delete process.env.MINNI_PLAN_HISTORY_CAP;
    }
    const raw = await readFile(historyFile, "utf8");
    const lines = raw.trim().split("\n").filter((l) => l.trim());
    assert.equal(
      lines.length,
      MAX_CAP,
      `MINNI_PLAN_HISTORY_CAP=99999999999 must clamp to MAX_PLAN_HISTORY_CAP (${MAX_CAP}) and actually rotate, got ${lines.length} lines`,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("appendHistorySnapshot: rotation counting agrees with readHistory — garbage lines neither occupy cap slots nor survive a rotation", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-garbage-"));
  try {
    const historyFile = path.join(root, "garbage.history.jsonl");
    process.env.MINNI_PLAN_HISTORY_CAP = "3";
    try {
      // 60 garbage lines (malformed JSON + schema-invalid JSON) interleaved
      // with cap(3) + hysteresis(50) = 53 VALID lines — enough valid lines
      // to force rotation, proving garbage does not inflate the trigger
      // count (if it did, 60 garbage + a handful of valid lines would also
      // trigger rotation, which this setup cannot distinguish) NOR survive
      // once rotation actually runs.
      const lines = [];
      for (let i = 0; i < 60; i++) lines.push(`not even json ${i}`);
      lines.push(JSON.stringify({ rev: 9999 })); // valid JSON, missing required fields
      for (let rev = 1; rev <= 53; rev++) lines.push(historyLine(rev));
      await writeFile(historyFile, lines.join("\n") + "\n", "utf8");

      // 53 valid + 1 new = 54 > cap(3)+hysteresis(50)=53 -> rotation fires.
      await appendHistorySnapshot(historyFile, { rev: 54, at: "t54", digest: "d", plan: { plan_id: "p" } });
    } finally {
      delete process.env.MINNI_PLAN_HISTORY_CAP;
    }

    const raw = await readFile(historyFile, "utf8");
    const survivingLines = raw.trim().split("\n").filter((l) => l.trim());
    for (const line of survivingLines) {
      assert.ok(isValidJsonHistoryLineForTest(line), `every surviving line must be a valid history entry, garbage must not survive rotation: ${line}`);
    }
    const revs = survivingLines.map((l) => JSON.parse(l).rev);
    assert.equal(revs.length, 3, "rotation must trim to exactly the cap (3), counting only valid lines");
    assert.deepEqual(revs, [52, 53, 54], "must keep the 3 most recent VALID revisions — garbage never occupied a slot");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

function isValidJsonHistoryLineForTest(line) {
  try {
    const p = JSON.parse(line);
    return typeof p.rev === "number" && typeof p.at === "string" && typeof p.digest === "string" && p.plan && typeof p.plan.plan_id === "string";
  } catch {
    return false;
  }
}

test("appendHistorySnapshot: concurrent writers to the same history file do not lose an already-durable revision", async () => {
  // Cassandra review round REPRODUCED this exact loss without the per-file
  // lock: caller A's read-then-rewrite rotation clobbered caller B's
  // already-fsync'd append that landed in the window between A's read and
  // A's write. That race only opens during an actual rotation — a round-2
  // review caught that the original version of this test never crossed the
  // rotation threshold (cap 3 + hysteresis 50 = 53 lines needed, only 40
  // appends fired) and so never exercised the race it claimed to prove: a
  // lock-removed mutant still passed every assertion here except ordering,
  // which isn't a real invariant across unserialized concurrent callers in
  // the first place. Fixed by pre-seeding the file to just under the
  // rotation threshold so the concurrent appends are guaranteed to force at
  // least one real rotation, and asserting on the one invariant that
  // actually matters: the newest durable revision must survive.
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-race-"));
  const CAP = 3;
  const HYSTERESIS = 50; // must match HISTORY_ROTATION_HYSTERESIS in plan.ts
  const SEED = CAP + HYSTERESIS - 1; // 52: one short of the rotation trigger
  const N = 40;
  try {
    const historyFile = path.join(root, "race.history.jsonl");
    await seedHistoryFile(historyFile, SEED, 1); // revs 1..52
    process.env.MINNI_PLAN_HISTORY_CAP = String(CAP);
    try {
      await Promise.all(
        Array.from({ length: N }, (_, i) =>
          appendHistorySnapshot(historyFile, {
            rev: SEED + i + 1, // revs 53..92 — crosses the 53-line rotation threshold
            at: `t${SEED + i + 1}`,
            digest: "d",
            plan: { plan_id: "p" },
          }),
        ),
      );
    } finally {
      delete process.env.MINNI_PLAN_HISTORY_CAP;
    }

    const raw = await readFile(historyFile, "utf8");
    const lines = raw.trim().split("\n").filter((l) => l.trim());
    // Every surviving line must be well-formed (a torn/corrupted rewrite
    // from an unsynchronized race would produce a line that fails to parse).
    for (const line of lines) {
      assert.doesNotThrow(() => JSON.parse(line), `every line must be valid JSON, got: ${line}`);
    }
    const revs = lines.map((l) => JSON.parse(l).rev);
    const expectedMax = SEED + N; // 92 — the newest revision that was appended
    // The concrete, checkable invariant a lock-less rotation race breaks:
    // the newest already-fsync'd revision goes missing when a concurrent
    // rotation's read-then-rewrite clobbers it. Reproduced without the lock
    // (5/5 runs clean with it, data loss in 3/5 without, per round-2
    // review's independent reproduction).
    assert.ok(revs.includes(expectedMax), `the newest revision (${expectedMax}) must survive — a lock-less rotation race can erase it`);
    assert.ok(new Set(revs).size === revs.length, "no duplicate revisions from a torn concurrent write");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("persistPlan wires real plan writes through the capped history append (#294 end-to-end)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-cap-e2e-"));
  try {
    await ensureVault(root);
    process.env.MINNI_PLAN_HISTORY_CAP = "1";
    let plan, write;
    try {
      const created = await createPlan({ goal: "#294 e2e history cap", vaultPath: root }, { vaultPath: root });
      plan = created.plan;
      write = created.write;
      // cap(1) + hysteresis(50) = 51 — need to cross that via real
      // persistPlan calls (the actual production call site) to observe a
      // real rotation end-to-end, not just through the lower-level function.
      for (let i = 0; i < 55; i++) {
        write = await persistPlan(plan, { vaultPath: root, notePath: write.notePath });
      }
    } finally {
      delete process.env.MINNI_PLAN_HISTORY_CAP;
    }

    const history = await readHistory(write.notePath);
    assert.ok(history.length <= 51, `history must stay bounded at cap+hysteresis, got ${history.length}`);
    assert.equal(history[history.length - 1].rev, plan.rev, "the newest entry must be the plan's current revision");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("PlanHistoryAppendError keeps notePath typed and out of .message", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const error = new PlanHistoryAppendError(
    notePath,
    4,
    new Error("EISDIR: illegal operation on a directory"),
  );
  assert.equal(error.notePath, notePath);
  assert.equal(error.rev, 4);
  assert.equal(error.code, "PLAN_HISTORY_APPEND_FAILED");
  assert.equal(
    error.message,
    "persistPlan: note committed at rev 4, but appending the history snapshot failed: history append failed",
  );
  assert.equal(error.message.includes(notePath), false);
  assert.equal(error.message.includes("wiki/artifacts"), false);
});

test("PlanHistoryAppendError drops a Node-style cause path (history file, not notePath)", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const historyPath = historyPathFor(notePath);
  const cause = Object.assign(
    new Error(`EISDIR: illegal operation on a directory, open '${historyPath}'`),
    { code: "EISDIR", path: historyPath },
  );
  const error = new PlanHistoryAppendError(notePath, 4, cause);
  assert.equal(error.notePath, notePath);
  assert.equal(
    error.message,
    "persistPlan: note committed at rev 4, but appending the history snapshot failed: EISDIR",
  );
  assert.equal(error.message.includes(historyPath), false);
  assert.equal(error.message.includes(notePath), false);
  assert.equal(error.message.includes("wiki/artifacts"), false);
});

test("PlanHistoryAppendError does not interpolate a path-bearing cause that has no syscall code", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const historyPath = historyPathFor(notePath);
  const error = new PlanHistoryAppendError(
    notePath,
    4,
    new Error(`append failed at ${historyPath}`),
  );
  assert.equal(
    error.message,
    "persistPlan: note committed at rev 4, but appending the history snapshot failed: history append failed",
  );
  assert.equal(error.message.includes(historyPath), false);
  assert.equal(error.message.includes("wiki/artifacts"), false);
});

// persistPlan performs two durable steps: it writes the canonical vault note,
// then appends a history snapshot line. A real EISDIR on the history file
// proves the note write itself already committed while the second step
// throws — this must surface as the typed PlanHistoryAppendError (never a
// bare Error a caller could mistake for "nothing was written"), so a caller
// holding a freshly staged secret (thread-worker's claimSlice) can tell this
// apart from a genuine pre-commit failure without guessing from message text.
test("persistPlan throws the typed PlanHistoryAppendError when the note commits but history append fails", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-append-error-"));
  try {
    await ensureVault(root);
    const created = await createPlan(
      { goal: "Reproduce a real post-commit history failure", vaultPath: root },
      { vaultPath: root },
    );
    const { plan, write } = created;
    const historyFile = historyPathFor(write.notePath);
    await rm(historyFile, { force: true });
    await mkdir(historyFile);

    const before = await rehydratePlan(write.notePath);
    await assert.rejects(
      persistPlan(plan, { vaultPath: root, notePath: write.notePath }),
      (error) => {
        assert.ok(
          error instanceof PlanHistoryAppendError,
          `expected PlanHistoryAppendError, got ${error?.constructor?.name}`,
        );
        assert.equal(error.code, "PLAN_HISTORY_APPEND_FAILED");
        assert.equal(error.notePath, write.notePath);
        assert.match(error.message, /persistPlan: note committed at rev/);
        assert.match(error.message, /history snapshot failed/);
        assert.match(error.message, /EISDIR/);
        assert.equal(
          error.message.includes(write.notePath),
          false,
          "PlanHistoryAppendError.message must keep notePath as a typed field only",
        );
        assert.equal(
          error.message.includes(historyFile),
          false,
          "PlanHistoryAppendError.message must not embed the history file path from cause.message",
        );
        assert.equal(
          error.message.includes("wiki/artifacts"),
          false,
          "PlanHistoryAppendError.message must not leak a vault artifacts path",
        );
        return true;
      },
    );

    // The note write already landed durably despite the thrown error.
    // persistPlan mutates `plan` in place before the history append is
    // attempted, so the in-memory rev/digest already match what is on disk.
    const after = await rehydratePlan(write.notePath);
    assert.equal(after.rev, before.rev + 1);
    assert.equal(after.rev, plan.rev);
    assert.equal(after.plan_digest, plan.plan_digest);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("persistPlan notePath mismatch after durable write does not embed vault paths", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-history-notepath-mismatch-"));
  try {
    await ensureVault(root);
    const created = await createPlan(
      { goal: "Reproduce a post-commit notePath mismatch", vaultPath: root },
      { vaultPath: root },
    );
    const { plan, write } = created;
    const otherPath = path.join(root, "wiki", "artifacts", "plan-other.md");
    await assert.rejects(
      persistPlan(plan, {
        vaultPath: root,
        notePath: write.notePath,
        writeVaultPage: async () => ({
          notePath: otherPath,
          relativePath: "wiki/artifacts/plan-other.md",
          wikilink: "[[plan-other]]",
        }),
      }),
      (error) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /different notePath than the caller expected/);
        assert.equal(error.message.includes(write.notePath), false);
        assert.equal(error.message.includes(otherPath), false);
        assert.equal(error.message.includes("wiki/artifacts"), false);
        assert.equal(error.message.includes(root), false);
        return true;
      },
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// #291 (audit N4): depends_on was advisory only — a slice whose dependency
// was still open could be silently marked done. Pure-function coverage of
// unmetDependencies()/updateSlice()'s hard block; a real-vault, real-server
// E2E test for the journaled force override follows below (the journal
// write itself is I/O performed in server.ts, not testable at the pure
// plan.ts layer).
// ---------------------------------------------------------------------------

function dependsOnPlan() {
  const plan = {
    plan_id: "test-plan",
    goal: "Test goal",
    status: "draft",
    constraints: [],
    slices: [
      { id: "a", title: "Slice A", status: "pending" },
      { id: "b", title: "Slice B", status: "pending", depends_on: ["a"] },
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "test",
    plan_digest: "",
    created: new Date().toISOString(),
    updated: new Date().toISOString(),
    rev: 1,
  };
  plan.plan_digest = computePlanDigest(plan);
  return plan;
}

test("unmetDependencies: reports an unresolved dependency, and none once it resolves", () => {
  const plan = dependsOnPlan();
  assert.deepEqual(unmetDependencies(plan, "b"), ["a"]);
  assert.deepEqual(unmetDependencies(plan, "a"), [], "a has no depends_on of its own");

  const aDone = updateSlice(plan, "a", "done", "verified via test output, exit 0");
  assert.deepEqual(unmetDependencies(aDone, "b"), [], "a is now done — no longer unmet");
});

test("unmetDependencies: a depends_on id with no matching slice is unmet, not silently ignored (typo/removed-by-replan)", () => {
  const plan = dependsOnPlan();
  const withGhostDep = {
    ...plan,
    slices: plan.slices.map((s) => (s.id === "b" ? { ...s, depends_on: ["ghost-slice"] } : s)),
  };
  assert.deepEqual(unmetDependencies(withGhostDep, "b"), ["ghost-slice"]);
});

test("updateSlice: hard-blocks a transition to done when depends_on is unmet (#291)", () => {
  const plan = dependsOnPlan();
  assert.throws(
    () => updateSlice(plan, "b", "done", "B is finished, verified via logs/b.log"),
    /depends_on unmet: a/,
    "marking b done while a is still pending must not be silently accepted",
  );
  // The framework must not have mutated anything on the way to throwing.
  assert.equal(plan.slices.find((s) => s.id === "b").status, "pending");
});

test("updateSlice: superseded (not just done) also satisfies a dependency", () => {
  const plan = dependsOnPlan();
  const aSuperseded = updateSlice(plan, "a", "superseded", "replaced by a different approach, see replan");
  const bDone = updateSlice(aSuperseded, "b", "done", "B is finished, verified via logs/b.log");
  assert.equal(bDone.slices.find((s) => s.id === "b").status, "done");
});

test("updateSlice: succeeds once the dependency actually resolves, no force needed", () => {
  const plan = dependsOnPlan();
  const aDone = updateSlice(plan, "a", "done", "verified via test output, exit 0");
  const bDone = updateSlice(aDone, "b", "done", "B is finished, verified via logs/b.log");
  assert.equal(bDone.slices.find((s) => s.id === "b").status, "done");
});

test("updateSlice: force alone, without forceReason, still throws — cannot silently bypass", () => {
  const plan = dependsOnPlan();
  assert.throws(
    () => updateSlice(plan, "b", "done", "B is finished, verified via logs/b.log", { force: true }),
    /force override of depends_on requires a non-empty force reason/,
  );
  assert.throws(
    () => updateSlice(plan, "b", "done", "B is finished, verified via logs/b.log", { force: true, forceReason: "   " }),
    /force override of depends_on requires a non-empty force reason/,
    "whitespace-only forceReason is not a real reason",
  );
});

// #291 round-1 cassandra finding 6 (MEDIUM, confirmed with a real live-vault
// example): every test above only used a "pending" dep status. A mutant
// that widened the resolved-status predicate to also accept "blocked" or
// "in_progress" survived the full suite untouched. Parameterize over every
// non-resolved status so that predicate is actually pinned.
test("updateSlice: every non-resolved dependency status (pending, in_progress, blocked) is unmet, not just pending", () => {
  for (const depStatus of ["pending", "in_progress", "blocked"]) {
    const plan = dependsOnPlan();
    const withStatus = {
      ...plan,
      slices: plan.slices.map((s) => (s.id === "a" ? { ...s, status: depStatus } : s)),
    };
    assert.deepEqual(unmetDependencies(withStatus, "b"), ["a"], `dep status "${depStatus}" must count as unmet`);
    assert.throws(
      () => updateSlice(withStatus, "b", "done", "B is finished, verified via logs/b.log"),
      /depends_on unmet: a/,
      `dep status "${depStatus}" must still hard-block b`,
    );
  }
});

test("updateSlice: force + a real forceReason bypasses the hard block", () => {
  const plan = dependsOnPlan();
  const bDone = updateSlice(plan, "b", "done", "B is finished, verified via logs/b.log", {
    force: true,
    forceReason: "operator approved fast-tracking B ahead of A for the hotfix",
  });
  assert.equal(bDone.slices.find((s) => s.id === "b").status, "done");
});

// #291 round-1 cassandra finding 1 (HIGH): replan()'s `??` on depends_on
// only guards null/undefined, not `[]` — a plan author can silently wipe an
// existing slice's dependency instead of ever touching force/force_reason,
// defeating the hard block above with no trace. diffDependsOn is the pure
// building block the minni_thread_replan handler uses to make that edit
// visible; the full silent-bypass-then-non-silent-fix path is covered by
// the E2E test below (the journal write is server.ts's job).
test("diffDependsOn: reports a depends_on change on an existing slice, ignores unrelated fields and new slices", () => {
  const before = dependsOnPlan();
  const after = {
    ...before,
    slices: [
      before.slices[0],
      { ...before.slices[1], depends_on: [] }, // b's dependency silently wiped
      { id: "c", title: "Slice C", status: "pending" }, // brand new slice — not a "change"
    ],
  };
  const changes = diffDependsOn(before, after);
  assert.deepEqual(changes, [{ slice_id: "b", from: ["a"], to: [] }]);
});

test("diffDependsOn: reordering depends_on is not a change", () => {
  const before = { ...dependsOnPlan(), slices: [{ id: "a", title: "A", status: "pending" }, { id: "b", title: "B", status: "pending", depends_on: ["a", "c"] }, { id: "c", title: "C", status: "pending" }] };
  const after = { ...before, slices: before.slices.map((s) => (s.id === "b" ? { ...s, depends_on: ["c", "a"] } : s)) };
  assert.deepEqual(diffDependsOn(before, after), []);
});

// #291 round-2 cassandra finding MEDIUM-3 (surviving mutant, confirmed): a
// mutant that made unmetDependencies only ever check depends_on[0] passed
// the entire suite untouched, because every existing test used a
// single-dependency slice. A real live plan has a two-dependency slice
// (interaction-e2e-verify). Pin the multi-dependency case directly.
test("unmetDependencies: reports EVERY unmet dependency when a slice has more than one, not just the first", () => {
  const plan = {
    ...dependsOnPlan(),
    slices: [
      { id: "a", title: "A", status: "done", evidence: "verified" },
      { id: "c", title: "C", status: "pending" },
      { id: "b", title: "B", status: "pending", depends_on: ["a", "c"] },
    ],
  };
  // a is done, c is pending — only c should be unmet, and it must not be
  // masked by a's resolved status coming first in the array.
  assert.deepEqual(unmetDependencies(plan, "b"), ["c"]);

  const bothOpen = {
    ...plan,
    slices: plan.slices.map((s) => (s.id === "a" ? { ...s, status: "pending", evidence: undefined } : s)),
  };
  assert.deepEqual(unmetDependencies(bothOpen, "b"), ["a", "c"], "both unmet deps must be reported, not just the first");
  assert.throws(
    () => updateSlice(bothOpen, "b", "done", "B is finished, verified via logs/b.log"),
    /depends_on unmet: a, c/,
  );
});

// #291 round-2 cassandra finding HIGH-1 (confirmed by independent
// reproduction against dist/plan.js before trusting the review):
// diffDependsOn alone cannot see a dependency satisfied "for free" by
// superseding the dependency slice itself (the ordinary way to replan —
// omit a slice, or drop_slice_ids). diffSupersededDependencies closes that
// visibility gap.
test("diffSupersededDependencies: reports a dependency slice that became superseded while something still depends on it", () => {
  const before = dependsOnPlan(); // a: pending, b: pending depends_on [a]
  const after = { ...before, slices: [{ ...before.slices[0], status: "superseded", superseded_by: "replan-x" }, before.slices[1]] };
  assert.deepEqual(diffSupersededDependencies(before, after), [{ slice_id: "a", depended_on_by: ["b"] }]);
});

test("diffSupersededDependencies: a slice that was ALREADY superseded before this operation is not re-reported", () => {
  const before = {
    ...dependsOnPlan(),
    slices: [
      { id: "a", title: "A", status: "superseded", superseded_by: "earlier" },
      { id: "b", title: "B", status: "pending", depends_on: ["a"] },
    ],
  };
  const after = before; // nothing changed this operation
  assert.deepEqual(diffSupersededDependencies(before, after), []);
});

test("diffSupersededDependencies: a superseded dependency with no surviving dependent produces no entry", () => {
  const before = dependsOnPlan();
  const after = {
    ...before,
    slices: [
      { ...before.slices[0], status: "superseded", superseded_by: "replan-x" },
      { ...before.slices[1], status: "done", evidence: "irrelevant, already resolved before a was touched" },
    ],
  };
  assert.deepEqual(diffSupersededDependencies(before, after), [], "b is done — no longer a live dependent to report");
});

test("landedReplanTopology: new_slices-shaped apply reports landed add ids and superseded ids", () => {
  const before = {
    ...dependsOnPlan(),
    slices: [
      { id: "keep", title: "Keep", status: "pending" },
      { id: "drop-me", title: "Drop", status: "pending" },
    ],
  };
  const after = replan(before, [
    { id: "keep", title: "Keep" },
    { id: "child-a", title: "Child A", depends_on: ["keep"] },
  ]);
  const landed = landedReplanTopology(before, after);
  assert.deepEqual(landed.drop_slice_ids, ["drop-me"]);
  assert.deepEqual(landed.add_slices, [
    { id: "child-a", title: "Child A", depends_on: ["keep"] },
  ]);
  assert.equal(
    JSON.stringify(landed).includes("claim"),
    false,
    "claim tokens stay off the landed topology payload",
  );
});

test("landedReplanTopology: depends_on-only remount (no add/supersede) omits add/drop", () => {
  const before = {
    ...dependsOnPlan(),
    slices: [
      { id: "b", title: "B", status: "pending", depends_on: ["s0"] },
      { id: "child-a", title: "Child A", status: "pending" },
    ],
  };
  const after = replan(before, [
    { id: "b", title: "B", depends_on: ["child-a"] },
    { id: "child-a", title: "Child A" },
  ]);
  assert.deepEqual(landedReplanTopology(before, after), {});
  assert.deepEqual(diffDependsOn(before, after), [
    { slice_id: "b", from: ["s0"], to: ["child-a"] },
  ]);
});

// #291 round-2 cassandra finding HIGH-2 (confirmed by independent
// reproduction via applySliceDelta below, and matching a real duplicate-id
// shape found in two live vaults). NOTE: replan()'s own new_slices path
// does NOT have a reachable version of this bug — the "stillProposed"
// check keys off `ns.id === slice.id` across every newSlices entry, so any
// entry that names id "a" unconditionally keeps the original "a" alive
// (never superseded), which means there is no newSlices shape that both
// supersedes an id and re-adds a fresh entry under that same id in one
// call. The guard added in replan() (see its comment) is defense-in-depth,
// not a live exploit fix, so there is no test asserting it throws here —
// that would assert an unreachable path.

test("applySliceDelta: rejects an explicit slice id that collides with a slice just dropped in the same call", () => {
  const plan = dependsOnPlan();
  assert.throws(
    () => applySliceDelta(plan, { drop_slice_ids: ["a"], add_slices: [{ id: "a", title: "A retry" }] }),
    /applySliceDelta: cannot add slice with id "a"/,
  );
});

test("depends_on hard block end-to-end: minni_thread_update refuses without force and journals a force override (#291)", async (t) => {
  const { spawn } = await import("node:child_process");
  const net = await import("node:net");
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-depends-on-mcp-"));
  const home = path.join(root, "home");
  const socketPath = path.join(home, "minnid.sock");
  await mkdir(home, { recursive: true });
  // #291 round-2 cassandra finding MEDIUM-4 (surviving mutant, confirmed):
  // round-1 finding 4 added force/force_reason to the plan.update gate
  // call's details, but nothing observed the gate call's PAYLOAD — deleting
  // that fix entirely left the whole suite green. Record every gate.shared
  // call the real server makes so the test can assert on what actually
  // reached the gate, not just that a gate call happened.
  const gateCalls = [];
  const fakeDaemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.split("\n")[0]);
      const respond = (result) => {
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      };
      if (request.method === "gate.shared") {
        gateCalls.push(request.params);
        respond({ ok: true, status: "allowed" });
        return;
      }
      respond({ ok: true });
    });
  });
  await new Promise((resolve) => fakeDaemon.listen(socketPath, resolve));
  t.after(() => fakeDaemon.close());
  const serverPath = new URL("../dist/server.js", import.meta.url).pathname;
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: root,
      MINNI_CLAUDECODE_VAULT_PATH: root,
      MINNI_KILOCODE_VAULT_PATH: root,
      MINNI_GROK_VAULT_PATH: root,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  try {
    const responses = new Map();
    let buffered = "";
    const waiters = new Map();
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      let nl;
      while ((nl = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined) {
            responses.set(msg.id, msg);
            waiters.get(msg.id)?.(msg);
          }
        } catch {
          // non-JSON noise on stdout would be a protocol bug; surface via timeout
        }
      }
    });
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 15000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout waiting for response ${id}`)), ms);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });

    send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "plan-291-e2e-test", version: "0.0.0" },
      },
    });
    const init = await awaitResponse(1);
    assert.ok(init.result, JSON.stringify(init));
    send({ jsonrpc: "2.0", method: "notifications/initialized" });

    let id = 2;
    const call = async (name, args) => {
      const thisId = id++;
      send({ jsonrpc: "2.0", id: thisId, method: "tools/call", params: { name, arguments: args } });
      return awaitResponse(thisId);
    };

    const created = await call("minni_thread_create", {
      goal: "#291 depends_on hard block e2e",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: ["a"] },
      ],
    });
    assert.ok(created.result && !created.result.isError, JSON.stringify(created));
    const createdBody = JSON.parse(created.result.content[0].text);
    const notePath = createdBody.notePath;

    // 1. No force: the tool must refuse, not silently accept.
    const blocked = await call("minni_thread_update", {
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
    });
    // Same contract as claim/assign: domain refusals are typed JSON via
    // threadWorkerErrorResult, not MCP isError (which would trip thread-server call()).
    assert.ok(blocked.result && !blocked.result.isError, `expected typed JSON error, got: ${JSON.stringify(blocked)}`);
    const blockedBody = JSON.parse(blocked.result.content[0].text);
    assert.equal(blockedBody.status, "error");
    assert.equal(blockedBody.operation, "plan.update");
    assert.match(blockedBody.error, /depends_on unmet: a/);

    // 2. force without a reason: still refused — force alone cannot bypass silently.
    const forceNoReason = await call("minni_thread_update", {
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
      force: true,
    });
    assert.ok(forceNoReason.result && !forceNoReason.result.isError, `expected typed JSON error, got: ${JSON.stringify(forceNoReason)}`);
    const forceNoReasonBody = JSON.parse(forceNoReason.result.content[0].text);
    assert.equal(forceNoReasonBody.status, "error");
    assert.equal(forceNoReasonBody.operation, "plan.update");
    // #291 round-1 cassandra finding 8: the refusal message must name the
    // MCP-facing field (force_reason) a retrying model can actually pass,
    // not the internal TS option name (forceReason).
    // #291 round-2 cassandra finding LOW-6: the no-force error (scenario 1,
    // above) ALSO contains the substring "force reason", so a loose /force
    // reason/ regex here would pass even if this scenario's specific
    // "force without a reason" refusal were silently replaced by the wrong
    // error. Match the discriminating phrase instead.
    assert.match(forceNoReasonBody.error, /requires a non-empty force reason/);

    // 2b. round-1 finding 7: a non-"done" transition with an unmet dep must
    // NOT emit an override record even if force is set — there's nothing
    // to override yet, and a bogus record here would pollute the audit
    // trail. (in_progress doesn't go through the depends_on gate at all.)
    const forceInProgress = await call("minni_thread_update", {
      slice_id: "b",
      status: "in_progress",
      force: true,
      force_reason: "should not matter — status isn't done",
    });
    assert.ok(forceInProgress.result && !forceInProgress.result.isError, JSON.stringify(forceInProgress));

    // 3. force + force_reason: succeeds, AND must journal the override —
    // folded atomically into the same status_changed event (round-1
    // finding 5: two separate journal writes left a crash window where
    // "done" could land durably with no override record at all).
    const forced = await call("minni_thread_update", {
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
      force: true,
      force_reason: "operator approved fast-tracking B ahead of A for the hotfix",
    });
    assert.ok(forced.result && !forced.result.isError, JSON.stringify(forced));
    const forcedBody = JSON.parse(forced.result.content[0].text);
    assert.equal(forcedBody.plan.slices.find((s) => s.id === "b").status, "done");

    const journalPath = path.join(path.dirname(notePath), `${createdBody.plan_id}.log.md`);
    const journalText = await readFile(journalPath, "utf8");
    const events = parseJournal(journalText);
    const overrideEvent = events.find((e) => e.kind === "status_changed" && e.slice_id === "b" && e.to === "done");
    assert.ok(overrideEvent, `expected the done status_changed event for b, got kinds: ${events.map((e) => e.kind).join(", ")}`);
    assert.ok(overrideEvent.depends_on_override, `expected depends_on_override on the status_changed event, got: ${JSON.stringify(overrideEvent)}`);
    assert.deepEqual(overrideEvent.depends_on_override.unmet, ["a"]);
    assert.equal(overrideEvent.depends_on_override.reason, "operator approved fast-tracking B ahead of A for the hotfix");
    assert.ok(overrideEvent.depends_on_override.forced_by, "override must record who forced it");

    // 3a. round-1 finding 4 (must actually be pinned — round-2 finding
    // MEDIUM-4 found the fix itself had no test): the shared-gate call for
    // this update must have carried force/force_reason so an approval
    // policy can see the override BEFORE it's applied, not just read it
    // from the journal after the fact.
    const updateGateCalls = gateCalls.filter((c) => c.operation === "plan.update" && c.details?.slice_id === "b");
    // Scenarios 2 and 2b earlier ALSO had force:true (without this specific
    // reason, or on a non-"done" status) — match on the exact force_reason
    // this scenario sent, not just "some force:true call", or this
    // assertion can silently check the wrong request.
    const forcedGateCall = updateGateCalls.find(
      (c) => c.details?.force === true && c.details?.force_reason === "operator approved fast-tracking B ahead of A for the hotfix",
    );
    assert.ok(forcedGateCall, `expected a plan.update gate call with force:true, got: ${JSON.stringify(updateGateCalls)}`);
    assert.equal(forcedGateCall.details.force_reason, "operator approved fast-tracking B ahead of A for the hotfix");

    // 2b's forced-but-not-done in_progress transition must not have carried
    // an override record either — belt-and-suspenders on finding 7.
    const inProgressEvent = events.find((e) => e.kind === "status_changed" && e.slice_id === "b" && e.to === "in_progress");
    assert.ok(inProgressEvent, "expected the in_progress status_changed event for b");
    assert.equal(inProgressEvent.depends_on_override, undefined, "force on a non-done transition must not fabricate an override record");

    // 3b. round-1 finding 7 (other direction): force+force_reason when the
    // dependency is ALREADY resolved must not emit an override record —
    // there was nothing to override, so recording one would be a false
    // "this was forced" claim in the audit trail.
    const created1b = await call("minni_thread_create", {
      goal: "#291 depends_on force-when-already-met e2e",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: ["a"] },
      ],
    });
    const created1bBody = JSON.parse(created1b.result.content[0].text);
    await call("minni_thread_update", {
      plan_id: created1bBody.plan_id,
      slice_id: "a",
      status: "done",
      evidence: "A is finished, verified via logs/a.log",
    });
    const forcedButMet = await call("minni_thread_update", {
      plan_id: created1bBody.plan_id,
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
      force: true,
      force_reason: "force set defensively even though a is already done",
    });
    assert.ok(forcedButMet.result && !forcedButMet.result.isError, JSON.stringify(forcedButMet));
    const journalPath1b = path.join(path.dirname(created1bBody.notePath), `${created1bBody.plan_id}.log.md`);
    const events1b = parseJournal(await readFile(journalPath1b, "utf8"));
    const bDoneEvent1b = events1b.find((e) => e.kind === "status_changed" && e.slice_id === "b" && e.to === "done");
    assert.ok(bDoneEvent1b, "expected b's done status_changed event");
    assert.equal(bDoneEvent1b.depends_on_override, undefined, "force with an already-met dependency must not fabricate an override record");

    // 4. Happy path (no override): a second plan where the dependency is
    // resolved first must NOT emit a depends_on_override event at all.
    const created2 = await call("minni_thread_create", {
      goal: "#291 depends_on happy path e2e",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: ["a"] },
      ],
    });
    const created2Body = JSON.parse(created2.result.content[0].text);
    const doneA = await call("minni_thread_update", {
      plan_id: created2Body.plan_id,
      slice_id: "a",
      status: "done",
      evidence: "A is finished, verified via logs/a.log",
    });
    assert.ok(doneA.result && !doneA.result.isError, JSON.stringify(doneA));
    const doneB = await call("minni_thread_update", {
      plan_id: created2Body.plan_id,
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
    });
    assert.ok(doneB.result && !doneB.result.isError, JSON.stringify(doneB));

    const journalPath2 = path.join(path.dirname(created2Body.notePath), `${created2Body.plan_id}.log.md`);
    const journalText2 = await readFile(journalPath2, "utf8");
    const events2 = parseJournal(journalText2);
    assert.ok(
      !events2.some((e) => e.kind === "status_changed" && e.depends_on_override),
      "resolving the dependency first must not produce a depends_on_override record",
    );

    // 5. round-1 cassandra finding 1 (HIGH): replan() can silently rewrite
    // an existing slice's depends_on to [] — I independently reproduced
    // this against the real compiled server before trusting the review.
    // Without this fix, the exploit is: replan away the dependency, then
    // mark done with no force, no reason, no journal trail at all. The fix
    // does not re-gate the edit; it makes it visible via a depends_on_changed
    // entry on the replan event, so it can never be silent.
    const created3 = await call("minni_thread_create", {
      goal: "#291 replan depends_on erasure e2e",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: ["a"] },
      ],
    });
    const created3Body = JSON.parse(created3.result.content[0].text);
    const replanned = await call("minni_thread_replan", {
      plan_id: created3Body.plan_id,
      new_slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: [] }, // erases the dependency
      ],
    });
    assert.ok(replanned.result && !replanned.result.isError, JSON.stringify(replanned));
    const replannedBody = JSON.parse(replanned.result.content[0].text);
    assert.deepEqual(replannedBody.slices.find((s) => s.id === "b").depends_on, [], "sanity: the dependency really was erased");
    // The erasure itself no longer requires force — that's an intentional
    // scope boundary (see the comment on minni_thread_replan in server.ts)
    // — but it must be journaled, unlike before this fix.
    const doneBAfterReplan = await call("minni_thread_update", {
      plan_id: created3Body.plan_id,
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
    });
    assert.ok(doneBAfterReplan.result && !doneBAfterReplan.result.isError, JSON.stringify(doneBAfterReplan));
    const journalPath3 = path.join(path.dirname(created3Body.notePath), `${created3Body.plan_id}.log.md`);
    const events3 = parseJournal(await readFile(journalPath3, "utf8"));
    const replanEvent = events3.find((e) => e.kind === "replan");
    assert.ok(replanEvent, "expected a replan journal event");
    assert.ok(replanEvent.depends_on_changed, `expected depends_on_changed on the replan event, got: ${JSON.stringify(replanEvent)}`);
    assert.deepEqual(replanEvent.depends_on_changed, [{ slice_id: "b", from: ["a"], to: [] }]);

    // 6. round-2 cassandra finding HIGH-1 (confirmed by independent
    // reproduction against dist/plan.js before trusting the review): the
    // depends_on ARRAY isn't the only way to satisfy a dependency for
    // free. Omitting the dependency slice from new_slices (the ordinary,
    // more common way to replan than editing depends_on directly)
    // supersedes it, which unmetDependencies already treats as resolved —
    // and before this round's fix, that produced ZERO journal trail at
    // all (b's depends_on array is untouched, so diffDependsOn alone sees
    // nothing). Must now show up as depends_on_superseded.
    const created4 = await call("minni_thread_create", {
      goal: "#291 replan supersedes a live dependency e2e",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: ["a"] },
      ],
    });
    const created4Body = JSON.parse(created4.result.content[0].text);
    const replannedAway = await call("minni_thread_replan", {
      plan_id: created4Body.plan_id,
      new_slices: [{ id: "b", title: "Slice B", depends_on: ["a"] }], // a omitted -> superseded
    });
    assert.ok(replannedAway.result && !replannedAway.result.isError, JSON.stringify(replannedAway));
    const replannedAwayBody = JSON.parse(replannedAway.result.content[0].text);
    assert.equal(replannedAwayBody.slices.find((s) => s.id === "a").status, "superseded", "sanity: a really was superseded");
    const doneBAfterSupersede = await call("minni_thread_update", {
      plan_id: created4Body.plan_id,
      slice_id: "b",
      status: "done",
      evidence: "B is finished, verified via logs/b.log",
    });
    assert.ok(doneBAfterSupersede.result && !doneBAfterSupersede.result.isError, JSON.stringify(doneBAfterSupersede));
    const journalPath4 = path.join(path.dirname(created4Body.notePath), `${created4Body.plan_id}.log.md`);
    const events4 = parseJournal(await readFile(journalPath4, "utf8"));
    const replanEvent4 = events4.find((e) => e.kind === "replan");
    assert.ok(replanEvent4, "expected a replan journal event");
    assert.ok(replanEvent4.depends_on_superseded, `expected depends_on_superseded on the replan event, got: ${JSON.stringify(replanEvent4)}`);
    assert.deepEqual(replanEvent4.depends_on_superseded, [{ slice_id: "a", depended_on_by: ["b"] }]);

    // 7. round-2 cassandra finding HIGH-2 (confirmed by independent
    // reproduction, and matching a real shape already present in two live
    // vaults): dropping a slice and re-adding an explicit slice with the
    // SAME id in the same replan call used to create a duplicate id, and
    // enforcement resolved against whichever instance was found first —
    // silently satisfying a dependency against the SUPERSEDED instance
    // while the live one became unreachable. Must now be refused outright.
    const created5 = await call("minni_thread_create", {
      goal: "#291 duplicate slice id rejection e2e",
      slices: [
        { id: "a", title: "Slice A" },
        { id: "b", title: "Slice B", depends_on: ["a"] },
      ],
    });
    const created5Body = JSON.parse(created5.result.content[0].text);
    const dupIdAttempt = await call("minni_thread_replan", {
      plan_id: created5Body.plan_id,
      drop_slice_ids: ["a"],
      add_slices: [{ id: "a", title: "A retry" }],
    });
    assert.ok(dupIdAttempt.result && !dupIdAttempt.result.isError, `expected typed JSON error, got: ${JSON.stringify(dupIdAttempt)}`);
    const dupIdBody = JSON.parse(dupIdAttempt.result.content[0].text);
    assert.equal(dupIdBody.status, "error");
    assert.equal(dupIdBody.operation, "plan.replan");
    assert.match(dupIdBody.error, /cannot add slice with id "a"/);
    // And the plan itself must be unchanged on disk — the rejected replan
    // (thrown before persistPlan is ever called) must not have partially
    // applied (a is still live, pending, exactly one slice with that id).
    const planAfterRejected = await rehydratePlan(created5Body.notePath);
    assert.equal(planAfterRejected.slices.filter((s) => s.id === "a").length, 1, "no duplicate id must have been written");
    assert.equal(planAfterRejected.slices.find((s) => s.id === "a").status, "pending", "the rejected replan must not have partially applied");
  } finally {
    child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
});


test("structuralProposalDelta: expand is add-only; proposer stays", () => {
  const delta = structuralProposalDelta(
    {
      kind: "expand",
      reason: "Need a sibling branch",
      slices: [{ id: "extra", title: "Extra branch" }],
    },
    "parent",
  );
  assert.deepEqual(delta, {
    add_slices: [{ id: "extra", title: "Extra branch" }],
  });
  assert.equal("drop_slice_ids" in delta, false);
});

test("structuralProposalDelta: split supersedes parent and rejects parent-id reuse", () => {
  const delta = structuralProposalDelta(
    {
      kind: "split",
      reason: "Two independent outputs",
      slices: [
        { id: "child-a", title: "Child A" },
        { id: "child-b", title: "Child B" },
      ],
    },
    "parent",
  );
  assert.deepEqual(delta, {
    add_slices: [
      { id: "child-a", title: "Child A" },
      { id: "child-b", title: "Child B" },
    ],
    drop_slice_ids: ["parent"],
  });
  assert.throws(
    () => structuralProposalDelta(
      {
        kind: "split",
        reason: "Reuse is not a split",
        slices: [{ id: "parent", title: "Same id" }],
      },
      "parent",
    ),
    /split cannot reuse parent id "parent"/,
  );
});

test("structuralProposalDelta: contract is drop-only", () => {
  const delta = structuralProposalDelta(
    {
      kind: "contract",
      reason: "These slices are no longer needed",
      slice_ids: ["gone", "also-gone"],
    },
    "proposer",
  );
  assert.deepEqual(delta, { drop_slice_ids: ["gone", "also-gone"] });
  assert.equal("add_slices" in delta, false);
});

test("applySliceDelta via structuralProposalDelta: expand keeps proposer; split supersedes; contract never deletes", () => {
  const plan = {
    plan_id: "p",
    goal: "evolution helper",
    status: "active",
    constraints: [],
    slices: [
      { id: "parent", title: "Parent", status: "in_progress", assigned_to: "worker-a" },
      { id: "keep", title: "Keep", status: "pending" },
    ],
    open_questions: [],
    scar_tissue: [],
    next_action: "parent",
    plan_digest: "x",
    created: "2026-08-18T12:00:00.000Z",
    updated: "2026-08-18T12:00:00.000Z",
    rev: 1,
  };

  const expanded = applySliceDelta(
    plan,
    structuralProposalDelta(
      { kind: "expand", reason: "branch", slices: [{ id: "extra", title: "Extra" }] },
      "parent",
    ),
  );
  assert.deepEqual(expanded.slices.map((s) => s.id), ["parent", "keep", "extra"]);
  assert.equal(expanded.slices.find((s) => s.id === "parent").status, "in_progress");
  assert.equal(expanded.slices.find((s) => s.id === "parent").assigned_to, "worker-a");
  assert.equal(expanded.slices.find((s) => s.id === "extra").status, "pending");

  const split = applySliceDelta(
    plan,
    structuralProposalDelta(
      {
        kind: "split",
        reason: "split",
        slices: [{ id: "child-a", title: "Child A" }, { id: "child-b", title: "Child B" }],
      },
      "parent",
    ),
  );
  const splitParent = split.slices.find((s) => s.id === "parent");
  assert.equal(splitParent.status, "superseded");
  assert.ok(splitParent.superseded_by);
  assert.deepEqual(
    split.slices.filter((s) => s.status !== "superseded").map((s) => s.id).sort(),
    ["child-a", "child-b", "keep"],
  );
  assert.equal(split.slices.filter((s) => s.id === "parent").length, 1, "split never deletes the parent");

  const contracted = applySliceDelta(
    plan,
    structuralProposalDelta(
      { kind: "contract", reason: "drop keep", slice_ids: ["keep"] },
      "parent",
    ),
  );
  const dropped = contracted.slices.find((s) => s.id === "keep");
  assert.ok(dropped, "contract never deletes");
  assert.equal(dropped.status, "superseded");
  assert.equal(contracted.slices.find((s) => s.id === "parent").status, "in_progress");
});
