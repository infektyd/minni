import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, rm, writeFile, readFile } from "node:fs/promises";
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
  compactPlanView,
  shelfDrift,
  appendJournal,
  parseJournal,
  appendHistorySnapshot,
  historyPathFor,
  readHistory
} from "../dist/plan.js";
import { ensureVault, writeVaultPage } from "../dist/vault.js";

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
    const { findPlanNote } = await import("../dist/plan.js");
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
