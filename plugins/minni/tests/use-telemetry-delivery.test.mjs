// Recall-to-use S1a (§4.3, §4.6): deliver first, record second, in-process.
//
// `recordSurfaced` builds `surfaced` rows in memory and registers them with
// `deferUntilDelivered`. The rows land only when the output was delivered, and
// as `delivered: false, visible: false` when it was abandoned. It runs inside
// hooks, so no argument a caller can hand it may throw, and no caller text
// beyond the sanitized title may reach a row.
//
// Driven in-process through the deferral registry (`runDeliveryCommits` is the
// delivered path, `discardDeliveryCommits` the abandoned one); the stdout half
// of emitAndCommit is covered by hook-delivery-onabandon.test.mjs.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { FLAGS_RELPATH, resetFlagsCacheForTests } from "../dist/flags.js";
import {
  discardDeliveryCommits,
  pendingDeliveryCommitCount,
  runDeliveryCommits,
} from "../dist/hook-delivery.js";
import { LEDGER_RELDIR, ledgerFilePath, readSessionLedger, recordSurfaced } from "../dist/use-ledger.js";

// The kill switch is honoured from env too; keep a shell that has it off from
// turning these into false failures. Each test file runs in its own process.
delete process.env.MINNI_USE_LEDGER;

const TS = "2026-10-23T11:00:00.000Z";
const CTX = {
  session_key: "fedcba9876543210",
  agent_key: "main",
  wire: "claude-code",
  principal: "claude-code",
  features: ["MINNI_USE_LEDGER"],
};

async function makeVault(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-telemetry-delivery-"));
  // Settle anything a failed assertion left registered before the vault goes.
  t.after(() => discardDeliveryCommits());
  t.after(() => rm(root, { recursive: true, force: true }));
  t.after(() => resetFlagsCacheForTests());
  discardDeliveryCommits();
  resetFlagsCacheForTests();
  const vault = path.join(root, "vault");
  await mkdir(vault, { recursive: true });
  return { root, vault };
}

function pointerArgs(vault, overrides = {}) {
  return {
    vaultPath: vault,
    context: CTX,
    channel: "prompt_pointer",
    items: [
      { mkey: "vault:wiki/one.md", handles: ["m:0123abcd"], title: "ITEM-ONE", rank: 1, score: 0.8 },
      { mkey: "doc:p:7", handles: ["d:p:7"], title: "ITEM-ABSENT", rank: 2 },
    ],
    emitted: "Relevant memory: ITEM-ONE (m:0123abcd).",
    capChars: 10_000,
    turn_ix: 4,
    ts: TS,
    ...overrides,
  };
}

test("delivered: the rows land with delivered: true and their visibility", async (t) => {
  const { vault } = await makeVault(t);
  const before = pendingDeliveryCommitCount();
  const result = recordSurfaced(pointerArgs(vault));
  assert.equal(result.registered, true);
  assert.equal(result.rows.length, 2);
  assert.equal(pendingDeliveryCommitCount(), before + 1, "one deferral per call");
  assert.equal(readSessionLedger(vault, CTX.session_key).rows.length, 0, "nothing is written before delivery");

  await runDeliveryCommits();
  const { rows } = readSessionLedger(vault, CTX.session_key);
  assert.equal(rows.length, 2);
  for (const row of rows) {
    assert.equal(row.kind, "surfaced");
    assert.equal(row.delivered, true);
    assert.equal(row.channel, "prompt_pointer");
    assert.equal(row.turn_ix, 4);
    assert.match(row.sid, /^[0-9a-f]{16}$/);
    assert.equal("cap_unknown" in row, false);
  }
  assert.deepEqual(rows.map((row) => row.visible), [true, false], "only the item in the emitted text was visible");
  assert.notEqual(rows[0].sid, rows[1].sid);

  // Delivered rows are never also written as abandoned.
  discardDeliveryCommits();
  assert.equal(readSessionLedger(vault, CTX.session_key).rows.length, 2);
});

test("discarded: the rows land with delivered: false, visible: false", async (t) => {
  const { vault } = await makeVault(t);
  // No known cap: delivered rows would say cap_unknown; abandoned rows are
  // definitively invisible and say nothing about the cap.
  const result = recordSurfaced(pointerArgs(vault, { capChars: undefined }));
  assert.equal(result.registered, true);
  assert.equal(result.rows[0].cap_unknown, true);

  discardDeliveryCommits();
  const { rows } = readSessionLedger(vault, CTX.session_key);
  assert.equal(rows.length, 2);
  for (const row of rows) {
    assert.equal(row.delivered, false);
    assert.equal(row.visible, false);
    assert.equal("cap_unknown" in row, false);
  }
  assert.deepEqual(
    rows.map((row) => row.sid),
    result.rows.map((row) => row.sid),
    "the abandoned rows keep the sids they were built with",
  );

  // The commit was dropped with the discard: a later delivery pass adds nothing.
  await runDeliveryCommits();
  assert.equal(readSessionLedger(vault, CTX.session_key).rows.length, 2);
});

test("flag off: nothing is registered and nothing is written", async (t) => {
  const { vault } = await makeVault(t);
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  await writeFile(path.join(vault, FLAGS_RELPATH), JSON.stringify({ MINNI_USE_LEDGER: "off" }), "utf8");

  const before = pendingDeliveryCommitCount();
  const result = recordSurfaced(pointerArgs(vault));
  assert.deepEqual(result, { rows: [], registered: false });
  assert.equal(pendingDeliveryCommitCount(), before);

  await runDeliveryCommits();
  discardDeliveryCommits();
  assert.ok(!(await readdir(path.join(vault, ".runtime"))).includes(path.basename(LEDGER_RELDIR)));
});

test("hostile arguments never throw and never register", async (t) => {
  const { vault } = await makeVault(t);
  const throwing = {
    get mkey() {
      throw new Error("hostile getter");
    },
    rank: 1,
  };
  // Labelled, not stringified: one case is a getter that throws when read.
  const hostile = [
    ["undefined", undefined],
    ["null", null],
    ["number", 42],
    ["string", "prompt_pointer"],
    ["empty object", {}],
    ["vault only", { vaultPath: vault }],
    ["items not an array", pointerArgs(vault, { items: "not an array" })],
    ["no items", pointerArgs(vault, { items: [] })],
    ["only malformed items", pointerArgs(vault, { items: [null, 7, "x", {}] })],
    ["throwing getter", pointerArgs(vault, { items: [throwing] })],
    ["unknown channel", pointerArgs(vault, { channel: "not-a-channel" })],
    ["missing channel", pointerArgs(vault, { channel: undefined })],
    ["null context", pointerArgs(vault, { context: null })],
    ["mistyped context", pointerArgs(vault, { context: { ...CTX, session_key: 5 } })],
    ["context with object prompt_id", pointerArgs(vault, { context: { ...CTX, prompt_id: { secret: 1 } } })],
    ["missing vault", pointerArgs(vault, { vaultPath: undefined })],
    ["object vault", pointerArgs(vault, { vaultPath: { toString: () => vault } })],
  ];
  const before = pendingDeliveryCommitCount();
  for (const [label, args] of hostile) {
    let result;
    assert.doesNotThrow(() => {
      result = recordSurfaced(args);
    }, label);
    assert.equal(result.registered, false, label);
    assert.deepEqual(result.rows, [], label);
  }
  assert.equal(pendingDeliveryCommitCount(), before, "no hostile call left a deferral behind");
  await runDeliveryCommits();
  discardDeliveryCommits();
});

test("hostile fields inside an item are dropped or rejected, never written", async (t) => {
  const { vault } = await makeVault(t);
  const result = recordSurfaced(
    pointerArgs(vault, {
      emitted: { not: "a string" },
      capChars: "10000",
      turn_ix: "four",
      items: [
        { mkey: { secret: "SECRET-1" }, rank: 1, title: "bad mkey" },
        { mkey: "vault:wiki/rank.md", rank: { secret: "SECRET-2" }, title: "bad rank" },
        { mkey: "vault:wiki/neg.md", rank: -1, title: "negative rank" },
        { mkey: "", rank: 2, title: "empty mkey" },
        {
          mkey: "vault:wiki/ok.md",
          rank: 3,
          title: "ok `title` with ## markup\nand a newline",
          score: "SECRET-3",
          route: { secret: "SECRET-4" },
          handles: ["SECRET-5 is not a handle", "m:89abcdef"],
          shingles: ["SECRET-6 body text", "0123456789abcdef"],
          echo: ["SECRET-7", "fedcba9876543210"],
          tx_off: "SECRET-8",
        },
      ],
    }),
  );
  assert.equal(result.registered, true);
  assert.equal(result.rows.length, 1, "only the well-formed item becomes a row");

  await runDeliveryCommits();
  const raw = await readFile(ledgerFilePath(vault, CTX.session_key, TS), "utf8");
  assert.ok(!raw.includes("SECRET"), `caller text reached the ledger: ${raw}`);
  const [row] = readSessionLedger(vault, CTX.session_key).rows;
  assert.equal(row.mkey, "vault:wiki/ok.md");
  assert.equal(row.rank, 3);
  assert.equal(row.title, "ok title with markup and a newline");
  assert.deepEqual(row.handles, ["m:89abcdef"]);
  assert.deepEqual(row.shingles, ["0123456789abcdef"]);
  assert.deepEqual(row.echo, ["fedcba9876543210"]);
  assert.equal("score" in row, false);
  assert.equal("route" in row, false);
  assert.equal("tx_off" in row, false);
  assert.equal("turn_ix" in row, false);
  assert.equal(row.visible, true, "a non-numeric cap is an unknown cap");
  assert.equal(row.cap_unknown, true);
});
