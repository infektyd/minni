// Audit cluster C2 (inbox lifecycle) + C4 remainder (plan terminal-state
// reconciliation): honest inbox reads, archive-on-resolution, file-handoff TTL.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  archiveInboxEntry,
  buildPendingLearningsSection,
  ensureVault,
  expireStaleInboxHandoffs,
  parseInboxTimestamp,
  readInboxStatus,
  readPendingInbox,
} from "../dist/vault.js";
import {
  createPlan,
  persistPlan,
  rehydratePlan,
  resolveActivePlanView,
} from "../dist/plan.js";

const DAY = 86_400_000;

function dashedName(epochMs, slug) {
  const day = new Date(epochMs).toISOString().slice(0, 10);
  return `${day}-${epochMs.toString(36)}-${slug}.json`;
}

function compactName(epochMs, slug) {
  const iso = new Date(epochMs).toISOString(); // 2026-04-26T18:42:33.000Z
  const stamp = iso.slice(0, 19).replace(/[-:]/g, "") + "Z";
  return `${stamp}-${slug}.json`;
}

async function writeInboxFixture(root, name, payload) {
  const inbox = path.join(root, "inbox");
  await mkdir(inbox, { recursive: true });
  const filePath = path.join(inbox, name);
  await writeFile(filePath, JSON.stringify(payload, null, 2), "utf8");
  return filePath;
}

test("parseInboxTimestamp handles both inbox filename formats", () => {
  const compact = parseInboxTimestamp("20260426T184233Z-review-auth-migration-trace-pr.json");
  assert.equal(compact, Date.parse("2026-04-26T18:42:33Z"));

  const epoch = Date.parse("2026-06-08T09:30:00Z");
  const dashed = parseInboxTimestamp(dashedName(epoch, "session"));
  assert.equal(dashed, epoch);

  // Dashed date with a non-millis slug segment still parses to the day.
  assert.equal(
    parseInboxTimestamp("2026-06-08-somsession.json"),
    Date.parse("2026-06-08T00:00:00Z"),
  );

  assert.equal(parseInboxTimestamp("not-a-timestamp.json"), undefined);
});

test("readInboxStatus: true newest-first across both formats plus honest totals (B2 gate)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-honest-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    // 10 files across both formats. Lexicographic sort would pin every compact
    // (YYYYMMDD...) name AFTER every dashed name; real dates disagree.
    const expectedNewestFirst = [];
    for (let i = 0; i < 5; i++) {
      // dashed: 1,3,5,7,9 days old
      const ts = now - (2 * i + 1) * DAY;
      const name = dashedName(ts, `dashed-${i}`);
      await writeInboxFixture(root, name, { slug: `dashed-${i}`, createdAt: new Date(ts).toISOString() });
      expectedNewestFirst.push({ name, ts });
    }
    for (let i = 0; i < 5; i++) {
      // compact: 2,4,6,8,45 days old
      const ts = now - (i === 4 ? 45 : 2 * i + 2) * DAY;
      const name = compactName(ts, `compact-${i}`);
      await writeInboxFixture(root, name, { slug: `compact-${i}`, kind: "handoff" });
      expectedNewestFirst.push({ name, ts });
    }
    expectedNewestFirst.sort((a, b) => b.ts - a.ts);

    const status = await readInboxStatus(root, 3, now);
    assert.equal(status.totalPending, 10);
    assert.equal(status.oldestAgeDays, 45);
    assert.equal(status.entries.length, 3);
    assert.deepEqual(
      status.entries.map((e) => path.basename(e.filePath)),
      expectedNewestFirst.slice(0, 3).map((e) => e.name),
      "capped entries must be the TRUE newest 3, not the lexicographic tail",
    );
    // The 45-day-old compact-format file must NOT be in the top slice.
    assert.ok(
      !status.entries.some((e) => e.filePath.includes("compact-4")),
      "ancient compact-named file must not pin the newest slice",
    );

    // Back-compat wrapper returns the same (fixed) entries.
    const pending = await readPendingInbox(root, 3);
    assert.deepEqual(
      pending.map((e) => path.basename(e.filePath)),
      status.entries.map((e) => path.basename(e.filePath)),
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("archiveInboxEntry moves the file into inbox/.archive preserving the name (B1)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-archive-"));
  try {
    const filePath = await writeInboxFixture(root, "2026-06-09-abc-session.json", { slug: "session" });
    const archived = await archiveInboxEntry(filePath);
    assert.ok(archived);
    assert.equal(path.basename(archived), "2026-06-09-abc-session.json");
    assert.equal(path.basename(path.dirname(archived)), ".archive");
    const live = (await readdir(path.join(root, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(live.length, 0, "file must leave the live inbox");
    const raw = await readFile(archived, "utf8");
    assert.equal(JSON.parse(raw).slug, "session", "content preserved, never deleted");
    // Archived entries are invisible to the honest read.
    const status = await readInboxStatus(root, 3);
    assert.equal(status.totalPending, 0);
    // Already-gone file is a quiet no-op.
    assert.equal(await archiveInboxEntry(filePath), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("expireStaleInboxHandoffs: aged handoff expires, surfaces exactly once, stops re-surfacing (B3 gate)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-ttl-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    const agedTs = Date.parse("2026-04-26T18:42:33Z"); // the orphaned-handoff shape
    const agedName = compactName(agedTs, "review-auth-migration-trace-pr");
    await writeInboxFixture(root, agedName, {
      kind: "handoff",
      slug: "review-auth-migration-trace-pr",
      task: "review auth migration trace PR",
    });
    // Fresh handoff (1 day old) must survive.
    await writeInboxFixture(root, compactName(now - DAY, "fresh-handoff"), {
      kind: "handoff",
      slug: "fresh-handoff",
    });
    // Old NON-handoff (stop candidates) must survive: TTL is handoff-only.
    await writeInboxFixture(root, dashedName(now - 40 * DAY, "old-stop"), {
      slug: "old-stop",
      last_task: "t",
      candidates: ["a durable lesson"],
    });

    const expired = await expireStaleInboxHandoffs(root, 7, now);
    assert.equal(expired.length, 1, "exactly the aged handoff expires");
    assert.equal(expired[0].status, "expired", "explicit status, not silent drop");
    assert.equal(expired[0].slug, "review-auth-migration-trace-pr");
    assert.equal(expired[0].ageDays, 44);
    assert.ok(expired[0].archivedPath?.includes(`${path.sep}.archive${path.sep}`));

    // Surfaced once: a second pass finds nothing, and the honest read no
    // longer counts it.
    const again = await expireStaleInboxHandoffs(root, 7, now);
    assert.equal(again.length, 0, "expired handoff must not re-surface");
    const status = await readInboxStatus(root, 5, now);
    assert.equal(status.totalPending, 2);
    assert.ok(!status.entries.some((e) => e.filePath.endsWith(agedName)));

    // Never deleted: the file lives on under .archive with its name.
    const archivedRaw = await readFile(
      path.join(root, "inbox", ".archive", agedName),
      "utf8",
    );
    assert.equal(JSON.parse(archivedRaw).kind, "handoff");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("expireStaleInboxHandoffs honors live ack-channel leases and labels acked leftovers (lease semantics)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-lease-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    const agedTs = now - 30 * DAY;
    // (1) Aged file, but a LIVE pending lease: requires_ack + future
    // expires_at. The daemon ack channel owns it; the reaper must skip it.
    const liveName = compactName(agedTs, "live-lease");
    await writeInboxFixture(root, liveName, {
      kind: "handoff",
      slug: "live-lease",
      lease_id: "handoff-live",
      requires_ack: true,
      expires_at: "2026-07-01T00:00:00Z",
    });
    // (2) Aged lease whose OWN expiry has passed -> expired.
    const deadName = compactName(agedTs, "dead-lease");
    await writeInboxFixture(root, deadName, {
      kind: "handoff",
      slug: "dead-lease",
      lease_id: "handoff-dead",
      requires_ack: true,
      expires_at: "2026-05-15T00:00:00Z",
    });
    // (3) Aged pending lease with NO expires_at -> skip (ack channel drains it).
    const noExpName = compactName(agedTs, "noexp-lease");
    await writeInboxFixture(root, noExpName, {
      kind: "handoff",
      slug: "noexp-lease",
      lease_id: "handoff-noexp",
      requires_ack: true,
    });
    // (4) Already-acked leftover -> archived as "acked", never "expired".
    const ackedName = compactName(agedTs, "acked-lease");
    await writeInboxFixture(root, ackedName, {
      kind: "handoff",
      slug: "acked-lease",
      lease_id: "handoff-acked",
      requires_ack: true,
      ack_status: "accepted",
      expires_at: "2026-07-01T00:00:00Z",
    });

    const reaped = await expireStaleInboxHandoffs(root, 7, now);
    const bySlug = new Map(reaped.map((e) => [e.slug, e]));
    assert.deepEqual(
      [...bySlug.keys()].sort(),
      ["acked-lease", "dead-lease"],
      "live/no-expiry leases must be skipped; only own-expiry and acked drain",
    );
    assert.equal(bySlug.get("dead-lease").status, "expired");
    assert.equal(bySlug.get("acked-lease").status, "acked");
    for (const entry of reaped) {
      assert.ok(entry.archivedPath, "surfaced entries are always archived");
    }
    const live = (await readdir(path.join(root, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.deepEqual(live.sort(), [liveName, noExpName].sort());
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("expireStaleInboxHandoffs never reads dashed-name files (cheap pre-filter)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-prefilter-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    // A dashed-name file that CLAIMS kind handoff: the plugin channel never
    // writes plain handoffs, so the reaper must skip it without reading —
    // unparseable content proves no JSON.parse was attempted on it.
    const dashedHandoff = dashedName(now - 40 * DAY, "fake-handoff");
    const inbox = path.join(root, "inbox");
    await mkdir(inbox, { recursive: true });
    await writeFile(path.join(inbox, dashedHandoff), "{not json", "utf8");

    const reaped = await expireStaleInboxHandoffs(root, 7, now);
    assert.equal(reaped.length, 0);
    const live = (await readdir(inbox)).filter((n) => n.endsWith(".json"));
    assert.deepEqual(live, [dashedHandoff], "dashed-name files are untouched");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("expireStaleInboxHandoffs honors a lease's own expiry BEFORE the file-age TTL (gate ordering)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-lease-order-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    const freshTs = now - 2 * DAY; // well inside the 7d TTL
    // (1) Young file, but the lease's OWN expiry already passed (the daemon
    // default is created_at + 24h): must drain NOW, not in 5 more days.
    await writeInboxFixture(root, compactName(freshTs, "short-lease"), {
      kind: "handoff",
      slug: "short-lease",
      lease_id: "handoff-short",
      requires_ack: true,
      expires_at: new Date(now - DAY).toISOString(),
    });
    // (2) Young already-acked leftover: archived immediately as "acked".
    await writeInboxFixture(root, compactName(freshTs, "young-acked"), {
      kind: "handoff",
      slug: "young-acked",
      lease_id: "handoff-young-acked",
      requires_ack: true,
      ack_status: "accepted",
      expires_at: new Date(now + 10 * DAY).toISOString(),
    });
    // (3) Young requires_ack-falsy orphan: the TTL has not elapsed -> keep.
    await writeInboxFixture(root, compactName(freshTs, "young-orphan"), {
      kind: "handoff",
      slug: "young-orphan",
    });

    const reaped = await expireStaleInboxHandoffs(root, 7, now);
    const bySlug = new Map(reaped.map((e) => [e.slug, e]));
    assert.deepEqual(
      [...bySlug.keys()].sort(),
      ["short-lease", "young-acked"],
      "own-expiry and acked must drain inside the TTL window; orphans wait it out",
    );
    assert.equal(bySlug.get("short-lease").status, "expired");
    assert.equal(bySlug.get("young-acked").status, "acked");
    const live = (await readdir(path.join(root, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.deepEqual(live, [compactName(freshTs, "young-orphan")]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("archiveInboxEntry collision: both files survive under distinct names", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-collision-"));
  try {
    const name = "20260601T120000Z-twice.json";
    const archiveDir = path.join(root, "inbox", ".archive");
    await mkdir(archiveDir, { recursive: true });
    await writeFile(path.join(archiveDir, name), JSON.stringify({ old: true }), "utf8");
    const filePath = await writeInboxFixture(root, name, { fresh: true });

    const archived = await archiveInboxEntry(filePath);
    assert.ok(archived, "collision must not fail the archive");
    assert.notEqual(archived, path.join(archiveDir, name), "must not overwrite");
    assert.equal(path.basename(path.dirname(archived)), ".archive");
    assert.ok(path.basename(archived).endsWith(`-${name}`), "original name preserved in suffix");
    // Both survive with their own content: never a silent overwrite.
    assert.equal(JSON.parse(await readFile(path.join(archiveDir, name), "utf8")).old, true);
    assert.equal(JSON.parse(await readFile(archived, "utf8")).fresh, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("expireStaleInboxHandoffs: a failed archive surfaces nothing and retries next session", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-archivefail-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    const agedName = compactName(now - 30 * DAY, "blocked-handoff");
    await writeInboxFixture(root, agedName, { kind: "handoff", slug: "blocked-handoff" });
    // Block archiving: .archive exists as a regular FILE, so mkdir fails.
    const blocker = path.join(root, "inbox", ".archive");
    await writeFile(blocker, "not a directory", "utf8");

    const blockedRun = await expireStaleInboxHandoffs(root, 7, now);
    assert.equal(blockedRun.length, 0, "failed archive must not claim surfaced");
    const live = (await readdir(path.join(root, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.deepEqual(live, [agedName], "handoff stays live for the next session");

    // Next session (blocker gone): surfaces exactly once, then never again.
    await rm(blocker);
    const retried = await expireStaleInboxHandoffs(root, 7, now);
    assert.equal(retried.length, 1);
    assert.equal(retried[0].slug, "blocked-handoff");
    assert.equal(retried[0].status, "expired");
    assert.equal((await expireStaleInboxHandoffs(root, 7, now)).length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildPendingLearningsSection: shared envelope shape with honest totals (B2 envelope gate)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-inbox-envelope-"));
  try {
    const now = Date.parse("2026-06-10T12:00:00Z");
    for (let i = 0; i < 5; i++) {
      const ts = now - (i + 1) * DAY;
      await writeInboxFixture(root, dashedName(ts, `entry-${i}`), {
        slug: `entry-${i}`,
        createdAt: new Date(ts).toISOString(),
        candidates: [`lesson ${i}`],
        kind: "stop_candidates",
        task: `task ${i}`,
      });
    }
    const agedName = compactName(now - 20 * DAY, "stale-handoff");
    await writeInboxFixture(root, agedName, {
      kind: "handoff",
      slug: "stale-handoff",
      task: "stale",
    });

    // Mirror the hooks' SessionStart order: reap, then honest read, then build.
    const expired = await expireStaleInboxHandoffs(root, 7, now);
    const status = await readInboxStatus(root, 3, now);
    const section = buildPendingLearningsSection(status, expired);

    assert.equal(section.total_pending, 5, "total is the FULL backlog, not the cap");
    assert.equal(section.oldest_age_days, 5);
    assert.equal(section.showing, 3, "3-of-5 visible as such");
    assert.equal(section.entries.length, 3);
    for (const entry of section.entries) {
      assert.deepEqual(
        Object.keys(entry).sort(),
        ["candidates", "created", "kind", "path", "slug", "task"],
      );
    }
    assert.equal(section.entries[0].slug, "entry-0", "true newest first");
    assert.equal(section.expired_handoffs.length, 1);
    const [eh] = section.expired_handoffs;
    assert.equal(eh.slug, "stale-handoff");
    assert.equal(eh.status, "expired");
    assert.equal(eh.age_days, 20);
    assert.ok(eh.archived_to.includes(`${path.sep}.archive${path.sep}`));
    assert.deepEqual(
      Object.keys(section).sort(),
      ["entries", "expired_handoffs", "oldest_age_days", "showing", "total_pending"],
      "envelope section shape is pinned for all four hooks",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveActivePlanView self-heals a stuck-draft plan whose slices are all terminal (B6 gate)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-reconcile-"));
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
    // Simulate the stale-plugin shape: every slice terminal but status still
    // 'draft' (live evidence: plan-3da1b00ca39d2500 et al).
    plan.slices[0] = { ...plan.slices[0], status: "done", evidence: "tests/inbox-lifecycle.test.mjs passing" };
    plan.slices[1] = { ...plan.slices[1], status: "superseded", superseded_by: "replan-test" };
    assert.equal(plan.status, "draft");
    await persistPlan(plan, { vaultPath: root, notePath: write.notePath });

    const view = await resolveActivePlanView(root);
    assert.equal(view, undefined, "finished plan must stop being injected");

    const healed = await rehydratePlan(write.notePath);
    assert.equal(healed.status, "accepted", "terminal status re-derived and persisted");

    const journalRaw = await readFile(
      path.join(path.dirname(write.notePath), `${plan.plan_id}.log.md`),
      "utf8",
    );
    assert.match(journalRaw, /"kind":"status_reconciled"/);
    assert.match(journalRaw, /"from":"draft"/);
    assert.match(journalRaw, /"to":"accepted"/);

    // Idempotent: a second resolve still returns undefined and does not
    // re-journal another reconciliation.
    const view2 = await resolveActivePlanView(root);
    assert.equal(view2, undefined);
    const journalRaw2 = await readFile(
      path.join(path.dirname(write.notePath), `${plan.plan_id}.log.md`),
      "utf8",
    );
    assert.equal(
      journalRaw2.match(/"kind":"status_reconciled"/g)?.length,
      1,
      "reconciliation journals exactly once",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("all four hooks build their SessionStart pending_learnings through the shared helpers (B2 hook-drift pin)", async () => {
  // The envelope-shape test above pins buildPendingLearningsSection in
  // isolation; this pins that each hook actually CALLS the shared helpers in
  // its SessionStart path, so no hook can quietly revert to an inline
  // pending.map(...) or skip the pre-reap while every test stays green.
  const srcDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");
  for (const hook of ["hook.ts", "codex-hook.ts", "grok-hook.ts", "kilocode-hook.ts"]) {
    const source = await readFile(path.join(srcDir, hook), "utf8");
    for (const call of [
      "buildPendingLearningsSection(",
      "expireStaleInboxHandoffs(",
      "readInboxStatus(",
    ]) {
      assert.ok(source.includes(call), `${hook} must call ${call}...)`);
    }
    assert.ok(
      source.includes("pending_learnings: buildPendingLearningsSection("),
      `${hook} must assign the envelope's pending_learnings from the shared builder`,
    );
  }
});

test("resolveActivePlanView does NOT flip a plan with a non-terminal slice (negative reconciliation)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-negative-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      {
        goal: "Half-finished plan must stay injected",
        slices: [
          { id: "s1", title: "Slice one" },
          { id: "s2", title: "Slice two" },
        ],
        vaultPath: root,
      },
      { vaultPath: root },
    );
    // One slice done, one still pending: the riskiest regression is this plan
    // getting auto-accepted and silently dropped from injection.
    plan.slices[0] = { ...plan.slices[0], status: "done", evidence: "tests/inbox-lifecycle.test.mjs passing" };
    assert.equal(plan.slices[1].status, "pending");
    await persistPlan(plan, { vaultPath: root, notePath: write.notePath });

    const view = await resolveActivePlanView(root);
    assert.ok(view, "live plan must keep being injected");
    assert.equal(view.plan_id, plan.plan_id);

    const reloaded = await rehydratePlan(write.notePath);
    assert.equal(reloaded.status, "draft", "status must NOT be reconciled");
    const journalRaw = await readFile(
      path.join(path.dirname(write.notePath), `${plan.plan_id}.log.md`),
      "utf8",
    );
    assert.ok(
      !journalRaw.includes('"kind":"status_reconciled"'),
      "no reconciliation event may be journaled for a live plan",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveActivePlanView reconciles a stuck-candidate plan too (candidate -> accepted)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-plan-candidate-"));
  try {
    await ensureVault(root);
    const { plan, write } = await createPlan(
      {
        goal: "Finished plan stuck on candidate",
        slices: [{ id: "s1", title: "Slice one" }],
        vaultPath: root,
      },
      { vaultPath: root },
    );
    plan.slices[0] = { ...plan.slices[0], status: "done", evidence: "tests/inbox-lifecycle.test.mjs passing" };
    plan.status = "candidate";
    await persistPlan(plan, { vaultPath: root, notePath: write.notePath });

    const view = await resolveActivePlanView(root);
    assert.equal(view, undefined, "finished plan must stop being injected");
    const healed = await rehydratePlan(write.notePath);
    assert.equal(healed.status, "accepted");
    const journalRaw = await readFile(
      path.join(path.dirname(write.notePath), `${plan.plan_id}.log.md`),
      "utf8",
    );
    assert.match(journalRaw, /"kind":"status_reconciled"/);
    assert.match(journalRaw, /"from":"candidate"/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── C8: the handoff TTL honors its env override ──────────────────────────────

test("inboxHandoffTtlDays: default, env override, invalid-value fallback", async () => {
  const { inboxHandoffTtlDays } = await import("../dist/vault.js");
  const saved = process.env.MINNI_INBOX_HANDOFF_TTL_DAYS;
  try {
    delete process.env.MINNI_INBOX_HANDOFF_TTL_DAYS;
    assert.equal(inboxHandoffTtlDays(), 7, "default TTL is 7 days");

    process.env.MINNI_INBOX_HANDOFF_TTL_DAYS = "3";
    assert.equal(inboxHandoffTtlDays(), 3, "env override wins");

    process.env.MINNI_INBOX_HANDOFF_TTL_DAYS = "0.5";
    assert.equal(inboxHandoffTtlDays(), 0.5, "fractional override is honored");

    for (const invalid of ["banana", "", "0", "-2", "NaN"]) {
      process.env.MINNI_INBOX_HANDOFF_TTL_DAYS = invalid;
      assert.equal(inboxHandoffTtlDays(), 7, `invalid value ${JSON.stringify(invalid)} falls back to 7`);
    }
  } finally {
    if (saved === undefined) delete process.env.MINNI_INBOX_HANDOFF_TTL_DAYS;
    else process.env.MINNI_INBOX_HANDOFF_TTL_DAYS = saved;
  }
});

test("expireStaleInboxHandoffs reads the env TTL at call time (C8 behavioral)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-ttl-env-"));
  const saved = process.env.MINNI_INBOX_HANDOFF_TTL_DAYS;
  try {
    const now = Date.now();
    const fiveDaysOld = now - 5 * DAY;
    const name = compactName(fiveDaysOld, "env-ttl-orphan");
    await writeInboxFixture(root, name, {
      kind: "handoff",
      slug: "env-ttl-orphan",
      createdAt: new Date(fiveDaysOld).toISOString(),
      task: "aged orphan",
    });

    // Default TTL (7d): a 5-day-old orphan survives.
    delete process.env.MINNI_INBOX_HANDOFF_TTL_DAYS;
    assert.deepEqual(await expireStaleInboxHandoffs(root, undefined, now), []);

    // Operator shortens the TTL to 3d via env: the same file now expires.
    process.env.MINNI_INBOX_HANDOFF_TTL_DAYS = "3";
    const expired = await expireStaleInboxHandoffs(root, undefined, now);
    assert.equal(expired.length, 1);
    assert.equal(expired[0].slug, "env-ttl-orphan");
    assert.equal(expired[0].status, "expired");
  } finally {
    if (saved === undefined) delete process.env.MINNI_INBOX_HANDOFF_TTL_DAYS;
    else process.env.MINNI_INBOX_HANDOFF_TTL_DAYS = saved;
    await rm(root, { recursive: true, force: true });
  }
});

// ── C5 plan parity: active-plan injection must exist in ALL four hooks ───────

test("all four hooks inject the active plan through the shared plan helpers (C5 hook-drift pin)", async () => {
  const srcDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");
  for (const hook of ["hook.ts", "codex-hook.ts", "grok-hook.ts", "kilocode-hook.ts"]) {
    const source = await readFile(path.join(srcDir, hook), "utf8");
    assert.ok(
      source.includes("resolveActivePlanView("),
      `${hook} must resolve the active plan`,
    );
    assert.ok(
      /active_plan\s*=\s*activePlan/.test(source),
      `${hook} SessionStart must inject the full active_plan view`,
    );
    assert.ok(
      source.includes("active_plan_ref = compactPlanPointer("),
      `${hook} UserPromptSubmit must inject the compact plan pointer (budget discipline)`,
    );
  }
});
