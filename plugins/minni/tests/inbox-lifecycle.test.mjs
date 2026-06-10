// Audit cluster C2 (inbox lifecycle) + C4 remainder (plan terminal-state
// reconciliation): honest inbox reads, archive-on-resolution, file-handoff TTL.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  archiveInboxEntry,
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
