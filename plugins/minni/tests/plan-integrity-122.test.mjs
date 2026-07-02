// Issue #122 plan-tooling integrity regressions:
//   1. F-PLAN-RESTORE-SELFBLOCK   — restore must heal a note whose strict rehydrate throws
//   2. F-PLAN-CREATE-OVERWRITES-ACTIVE — create must surface the displaced in-flight plan
//   3. F-PLAN-ACTIVATE-NO-TERMINAL-GUARD — activate must reject terminal plans
//   4. F-PLAN-DIGEST-CROSSPROC    — version-tagged digest with a read-time registry
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  activatePlanChecked,
  clearActivePlan,
  computePlanDigest,
  createPlan,
  PlanDigestVersionError,
  getActivePlan,
  getRevision,
  persistPlan,
  rehydratePlan,
  rehydratePlanScalars,
  restorePlan,
  updateSlice,
  TERMINAL_PLAN_STATUSES,
} from "../dist/plan.js";
import { ensureVault } from "../dist/vault.js";

const serverSource = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");

function handlerBlock(toolName) {
  const start = serverSource.indexOf(`"${toolName}"`);
  assert.ok(start >= 0, `${toolName} must be registered`);
  const nextTool = serverSource.indexOf("server.registerTool(", start + 1);
  return serverSource.slice(start, nextTool < 0 ? undefined : nextTool);
}

// ---- 1. F-PLAN-RESTORE-SELFBLOCK ---------------------------------------------

test("#122/1: restore path heals a digest-bricked note via bare-scalar rehydrate", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-restore-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      {
        goal: "ship the recovery test",
        slices: [{ id: "s1", title: "t1" }, { id: "s2", title: "t2" }],
        vaultPath: root,
      },
      { vaultPath: root },
    );
    // Brick the note exactly like the issue repro: stored digest matches nothing.
    const raw = await readFile(write.notePath, "utf8");
    await writeFile(
      write.notePath,
      raw.replace(/^plan_digest:.*$/m, 'plan_digest: "deadbeefdeadbeef"'),
      "utf8",
    );
    await assert.rejects(() => rehydratePlan(write.notePath), /plan_digest mismatch/);

    // The fixed handler sequence: bare scalars (no digest check) -> restore -> persist.
    const current = await rehydratePlanScalars(write.notePath);
    assert.equal(current.plan_id, plan.plan_id);
    const snapshot = await getRevision(write.notePath, 1);
    assert.ok(snapshot, "rev 1 must exist in history");
    const next = restorePlan(current, snapshot);
    await persistPlan(next, { vaultPath: root, notePath: write.notePath });

    // Healed: strict rehydrate succeeds again with full content intact.
    const healed = await rehydratePlan(write.notePath);
    assert.equal(healed.goal, "ship the recovery test");
    assert.equal(healed.slices.length, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/1: minni_plan_restore handler falls back to rehydratePlanScalars (source pin)", () => {
  const block = handlerBlock("minni_plan_restore");
  assert.match(
    block,
    /rehydratePlanScalars/,
    "restore handler must not be gated on strict rehydrate of the corrupt current note",
  );
});

// ---- 2. F-PLAN-CREATE-OVERWRITES-ACTIVE --------------------------------------

test("#122/2: createPlan surfaces displaced_active when it displaces an in-flight plan", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-create-"));
  try {
    await ensureVault(root);
    // First plan: no incumbent, so no displacement reported.
    const a = await createPlan(
      { goal: "plan A", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    assert.equal(a.displaced_active, undefined, "first plan must auto-activate silently");
    // Make A clearly in-flight.
    const planA = await rehydratePlan(a.write.notePath);
    const inFlight = updateSlice(planA, "s1", "in_progress");
    await persistPlan(inFlight, { vaultPath: root, notePath: a.write.notePath });

    // Plan B displaces the non-terminal A: still auto-activates, but names A.
    const b = await createPlan({ goal: "plan B", vaultPath: root }, { vaultPath: root });
    assert.equal(b.displaced_active, a.plan.plan_id, "displaced in-flight plan must be named");
    const active = await getActivePlan(root);
    assert.equal(active?.plan_id, b.plan.plan_id, "new plan still becomes active");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/2: createPlan stays silent when the incumbent active plan is terminal", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-create-term-"));
  try {
    await ensureVault(root);
    const a = await createPlan(
      { goal: "plan A", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    let planA = await rehydratePlan(a.write.notePath);
    planA = updateSlice(planA, "s1", "done", "verified via test output, exit 0");
    assert.equal(planA.status, "complete");
    await persistPlan(planA, { vaultPath: root, notePath: a.write.notePath });

    const b = await createPlan({ goal: "plan B", vaultPath: root }, { vaultPath: root });
    assert.equal(b.displaced_active, undefined, "terminal incumbent must not warn");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/2: createPlan stays silent when the incumbent is stale all-resolved (draft status, slices done)", async () => {
  // Codex round 4 (PR #130): the displacement check must use the same
  // effective-terminal predicate as resolveActivePlanView/activatePlanChecked —
  // a stale incumbent (status scalar stuck at draft/candidate but every slice
  // done/superseded) is finished, so displacing it must not warn the user
  // toward re-activating a finished plan.
  const root = await mkdtemp(path.join(tmpdir(), "i122-create-stale-"));
  try {
    await ensureVault(root);
    const a = await createPlan(
      { goal: "plan A", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    // Persist the stale shape directly (updateSlice would reconcile status).
    const planA = await rehydratePlan(a.write.notePath);
    planA.slices[0].status = "done";
    planA.slices[0].evidence = "verified via test output, exit 0";
    assert.equal(planA.status, "draft", "precondition: status scalar stays draft");
    await persistPlan(planA, { vaultPath: root, notePath: a.write.notePath });

    const b = await createPlan({ goal: "plan B", vaultPath: root }, { vaultPath: root });
    assert.equal(b.displaced_active, undefined, "stale all-resolved incumbent must displace silently");
    assert.equal((await getActivePlan(root))?.plan_id, b.plan.plan_id);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/2: minni_plan_create handler returns displaced_active + warning (source pin)", () => {
  const block = handlerBlock("minni_plan_create");
  assert.match(block, /displaced_active/, "create response must surface the displaced plan_id");
  assert.match(block, /warning/, "create response must carry a warning field when displacing");
});

// ---- 3. F-PLAN-ACTIVATE-NO-TERMINAL-GUARD ------------------------------------

test("#122/3: activatePlanChecked rejects terminal plans and activates non-terminal ones", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-activate-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "terminal guard", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    // Non-terminal (draft) re-activation still works.
    await clearActivePlan(root);
    const okRes = await activatePlanChecked(root, plan.plan_id, write.notePath);
    assert.equal(okRes.ok, true);
    assert.equal((await getActivePlan(root))?.plan_id, plan.plan_id);

    // Drive to terminal, clear pointer, attempt explicit re-activate.
    let p = await rehydratePlan(write.notePath);
    p = updateSlice(p, "s1", "done", "verified via test output, exit 0");
    assert.equal(p.status, "complete");
    await persistPlan(p, { vaultPath: root, notePath: write.notePath });
    await clearActivePlan(root);

    const res = await activatePlanChecked(root, plan.plan_id, write.notePath);
    assert.equal(res.ok, false, "terminal plan must not be re-activated");
    assert.match(res.error, /terminal status 'complete'/);
    assert.equal(await getActivePlan(root), undefined, "pointer must stay clear on rejection");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/3: activatePlanChecked rejects a stale all-resolved plan whose status scalar is still draft", async () => {
  // Codex review (PR #130): legacy/stale notes completed under an old plugin
  // deploy can have every slice done/superseded while the status scalar is
  // still 'draft'/'candidate'. resolveActivePlanView treats that all-resolved
  // shape as terminal (self-heals to 'complete'); the activate guard must
  // reject it too, or id-less plan tools get retargeted to a finished plan.
  const root = await mkdtemp(path.join(tmpdir(), "i122-activate-stale-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      { goal: "stale draft guard", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    // Persist the stale shape directly (updateSlice would reconcile status).
    const p = await rehydratePlan(write.notePath);
    p.slices[0].status = "done";
    p.slices[0].evidence = "verified via test output, exit 0";
    assert.equal(p.status, "draft", "precondition: status scalar stays draft");
    await persistPlan(p, { vaultPath: root, notePath: write.notePath });
    await clearActivePlan(root);

    const res = await activatePlanChecked(root, plan.plan_id, write.notePath);
    assert.equal(res.ok, false, "all-resolved stale plan must not be re-activated");
    assert.match(res.error, /every slice resolved/);
    assert.equal(await getActivePlan(root), undefined, "pointer must stay clear on rejection");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/3: terminal set mirrors resolveActivePlanView's suppression set", () => {
  assert.deepEqual(
    [...TERMINAL_PLAN_STATUSES].sort(),
    ["accepted", "complete", "rejected", "superseded"],
  );
});

test("#122/3: minni_plan_activate handler routes through the guard (source pin)", () => {
  const block = handlerBlock("minni_plan_activate");
  assert.match(block, /activatePlanChecked/, "activate handler must use the terminal-status guard");
});

// ---- 4. F-PLAN-DIGEST-CROSSPROC ----------------------------------------------
// Codex review (PR #130): the persisted plan_digest value stays a BARE hex so
// pre-tagging readers on other hosts keep validating it during a rolling
// update; the algorithm version travels in the separate plan_digest_v
// frontmatter field. "vN:<hex>"-prefixed stored digests (written by interim
// builds of this PR) are still accepted on read and normalized on write.

test("#122/4: new plans persist a bare-hex digest plus plan_digest_v and stay readable to pre-tagging readers", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-tag-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "digest tagging", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    assert.match(raw, /^plan_digest: "?[0-9a-f]{16}"?$/m, "stored digest must stay bare hex for old readers");
    assert.match(raw, /^plan_digest_v: 2$/m, "algorithm version must travel in plan_digest_v");
    const rehydrated = await rehydratePlan(write.notePath);
    // Simulated pre-tagging reader check: an old reader compares the stored
    // value byte-for-byte against its own bare v2 computation — which is
    // exactly computePlanDigest here, so equality proves old-reader compat.
    const storedHex = raw.match(/^plan_digest: "?([0-9a-f]{16})"?$/m)[1];
    assert.equal(computePlanDigest(rehydrated), storedHex, "old readers must match the stored digest");
    assert.equal(rehydrated.plan_digest, storedHex);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: note without plan_digest_v (pre-tagging writer) is recognized as bare v2, as before", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-untagged-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "untagged compat", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    // Simulate a note written by a pre-tagging build: drop the version field.
    const raw = await readFile(write.notePath, "utf8");
    assert.match(raw, /^plan_digest_v: 2$/m);
    await writeFile(write.notePath, raw.replace(/^plan_digest_v:.*\n/m, ""), "utf8");
    const rehydrated = await rehydratePlan(write.notePath);
    assert.equal(rehydrated.plan_digest, computePlanDigest(rehydrated));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: 'v2:<hex>'-prefixed stored digest (interim PR build) is accepted and normalized to bare hex", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-prefixed-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "prefixed compat", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    const bare = raw.match(/^plan_digest: "?([0-9a-f]{16})"?$/m)?.[1];
    assert.ok(bare, "expected a bare-hex digest to prefix");
    await writeFile(
      write.notePath,
      raw.replace(/^plan_digest:.*$/m, `plan_digest: v2:${bare}`),
      "utf8",
    );
    const rehydrated = await rehydratePlan(write.notePath);
    assert.match(rehydrated.plan_digest, /^[0-9a-f]{16}$/, "in-memory digest must be normalized bare hex");
    const rewritten = await readFile(write.notePath, "utf8");
    assert.match(rewritten, /^plan_digest: "?[0-9a-f]{16}"?$/m, "note must be re-stamped bare hex");
    assert.match(rewritten, /^plan_digest_v: 2$/m);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: unknown newer plan_digest_v degrades gracefully with a typed error, not as tampered", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-newer-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "future version", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    // Leave the (valid v2) digest untouched: the version field alone must gate.
    const raw = await readFile(write.notePath, "utf8");
    await writeFile(write.notePath, raw.replace(/^plan_digest_v:.*$/m, "plan_digest_v: 3"), "utf8");
    await assert.rejects(
      () => rehydratePlan(write.notePath),
      (err) => {
        assert.ok(err instanceof PlanDigestVersionError, "must be the typed newer-version error");
        assert.equal(err.code, "PLAN_DIGEST_NEWER");
        assert.match(err.message, /newer than this plugin/);
        assert.doesNotMatch(err.message, /tampered/);
        return true;
      },
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: unknown 'v99:' digest prefix also degrades gracefully, not as tampered", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-newer-prefix-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "future prefix", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    await writeFile(
      write.notePath,
      raw.replace(/^plan_digest:.*$/m, 'plan_digest: "v99:deadbeefdeadbeef"'),
      "utf8",
    );
    await assert.rejects(
      () => rehydratePlan(write.notePath),
      (err) => {
        assert.ok(err instanceof PlanDigestVersionError);
        assert.match(err.message, /newer than this plugin/);
        assert.doesNotMatch(err.message, /tampered/);
        return true;
      },
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: declared-version digest with wrong hex is still rejected as tampered", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-tamper-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "tagged tamper", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    // plan_digest_v: 2 stays; the hex itself is wrong -> genuine tamper.
    await writeFile(
      write.notePath,
      raw.replace(/^plan_digest:.*$/m, 'plan_digest: "deadbeefdeadbeef"'),
      "utf8",
    );
    await assert.rejects(() => rehydratePlan(write.notePath), (err) => {
      assert.match(err.message, /plan_digest mismatch/);
      assert.ok(!(err instanceof PlanDigestVersionError));
      return true;
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: restore refuses a newer-version note instead of downgrade-writing it", async () => {
  // The restore fallback (#122/1) must only swallow recoverable corruption; a
  // PlanDigestVersionError means a NEWER writer owns this note, and restoring
  // through an older plugin would silently downgrade newer fields.
  const root = await mkdtemp(path.join(tmpdir(), "i122-restore-newer-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "no downgrade restore", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(write.notePath, "utf8");
    await writeFile(write.notePath, raw.replace(/^plan_digest_v:.*$/m, "plan_digest_v: 3"), "utf8");
    // The typed error is what the restore handler re-throws instead of healing.
    await assert.rejects(
      () => rehydratePlan(write.notePath),
      (err) => err instanceof PlanDigestVersionError && err.code === "PLAN_DIGEST_NEWER",
    );
    const untouched = await readFile(write.notePath, "utf8");
    assert.match(untouched, /^plan_digest_v: 3$/m, "newer-version note must not be rewritten");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: newer-version gate fires BEFORE current-schema validations (evidence check)", async () => {
  // Codex re-review round 3: a plan_digest_v: 3 note whose slice shape is
  // invalid under the CURRENT schema (e.g. v3 moved evidence elsewhere) must
  // throw the typed PlanDigestVersionError, not the generic evidence error —
  // otherwise minni_plan_restore's downgrade guard (which keys on the typed
  // error) falls through to the bare-scalar heal path and can persist a v2
  // restore over a newer writer's note.
  const root = await mkdtemp(path.join(tmpdir(), "i122-digest-newer-schema-"));
  try {
    await ensureVault(root);
    const { write } = await createPlan(
      { goal: "newer schema shape", slices: [{ id: "s1", title: "t1" }], vaultPath: root },
      { vaultPath: root },
    );
    // Persist a shape that is invalid under the current schema: a 'done' slice
    // with empty evidence (persistPlan does not validate; rehydratePlan does).
    const p = await rehydratePlan(write.notePath);
    p.slices[0].status = "done";
    p.slices[0].evidence = "";
    await persistPlan(p, { vaultPath: root, notePath: write.notePath });
    // Sanity: under the declared CURRENT version this shape trips the evidence check.
    await assert.rejects(() => rehydratePlan(write.notePath), /without evidence/);
    // Declare a newer version: the version gate must now fire first.
    const raw = await readFile(write.notePath, "utf8");
    await writeFile(write.notePath, raw.replace(/^plan_digest_v:.*$/m, "plan_digest_v: 3"), "utf8");
    await assert.rejects(
      () => rehydratePlan(write.notePath),
      (err) => {
        assert.ok(err instanceof PlanDigestVersionError, "version gate must precede schema validation");
        assert.doesNotMatch(err.message, /without evidence/);
        return true;
      },
    );
    const untouched = await readFile(write.notePath, "utf8");
    assert.match(untouched, /^plan_digest_v: 3$/m, "newer-version note must not be rewritten");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#122/4: minni_plan_restore handler re-throws PlanDigestVersionError (source pin)", () => {
  const block = handlerBlock("minni_plan_restore");
  assert.match(
    block,
    /PlanDigestVersionError/,
    "restore fallback must not swallow the newer-version error and downgrade-write the note",
  );
});
