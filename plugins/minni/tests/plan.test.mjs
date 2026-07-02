import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  updateSlice,
  computePlanDigest,
  computePlanDigestV1,
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
    assert.match(result.error, /minni_plan_activate/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("minni_plan_status/_update/_history accept an OPTIONAL plan_id (C5 schema pin)", async () => {
  // The acceptance spec requires the id-less form VERBATIM on these three
  // tools; pin the schemas so a refactor cannot quietly re-require plan_id.
  const source = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  // minni_plan_replan and minni_plan_scar are beyond the verbatim spec but
  // pinned too (review panel): a hookless agent must be able to replan or
  // record a dead-end against "the active plan" without round-tripping the id
  // through minni_plan_status.
  for (const tool of ["minni_plan_status", "minni_plan_update", "minni_plan_history", "minni_plan_replan", "minni_plan_scar"]) {
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
  assert.match(
    source.slice(helperStart, helperStart + 1200),
    /resolvePlanIdOrActive\(/,
    "resolvePlanTarget must default to the active plan via resolvePlanIdOrActive",
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
      ["minni_plan_status", {}],
      ["minni_plan_update", { slice_id: "slice-1", status: "in_progress" }],
      ["minni_plan_history", {}],
      ["minni_plan_replan", { new_slices: [] }],
      ["minni_plan_scar", { kind: "dead_end", signal: "tried the obvious thing" }],
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
      assert.match(body.error, /minni_plan_activate/, name);
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
