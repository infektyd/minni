// Recall-to-use S1a (§4.3): the use ledger's append discipline.
//
// The ledger is written from hooks that run concurrently (main thread and
// subagents share a session), so each row must land as one O_APPEND write no
// larger than 4 KB: two writers can interleave rows but never bytes within a
// row. A reader must survive the one failure that discipline still allows, a
// torn last line. And a hook must never die on a ledger problem: a refused or
// failed write is a count, not an exception.
import assert from "node:assert/strict";
import fs from "node:fs";
import { mkdir, mkdtemp, readdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import * as ledger from "../dist/use-ledger.js";
import {
  appendLedgerRows,
  buildJudgedRow,
  buildSurfacedRow,
  buildTurnRow,
  ledgerFilePath,
  MAX_ROW_BYTES,
  readSessionLedger,
} from "../dist/use-ledger.js";

// The ledger honours the MINNI_USE_LEDGER kill switch from env; a shell that
// has it off must not turn these into false failures. Each test file runs in
// its own process, so this stays local.
delete process.env.MINNI_USE_LEDGER;

const TS = "2026-10-23T09:30:00.000Z";
const CTX = {
  session_key: "0123456789abcdef",
  agent_key: "main",
  wire: "claude-code",
  principal: "claude-code",
  features: ["MINNI_USE_LEDGER"],
};

async function makeVault(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-append-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(vault, { recursive: true });
  return { root, vault };
}

function turn(ix, ctx = CTX, ts = TS) {
  return buildTurnRow(ctx, { turn_ix: ix, envelope_kind: "human", subkind: null, exit: "pointer" }, ts);
}

test("each row is one write() and one newline-terminated line", async (t) => {
  const { vault } = await makeVault(t);
  const original = fs.writeSync;
  const writes = [];
  fs.writeSync = function (...args) {
    writes.push(args[1]);
    return original.apply(this, args);
  };
  let result;
  try {
    result = appendLedgerRows(vault, [turn(1), turn(2), turn(3)]);
  } finally {
    fs.writeSync = original;
  }
  assert.deepEqual(result, { written: 3, failed: 0, clipped: 0, disabled: false });
  assert.equal(writes.length, 3, "one write() per row, never batched or split");
  for (const chunk of writes) {
    const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
    assert.ok(text.endsWith("\n"));
    assert.equal(text.indexOf("\n"), text.length - 1, "exactly one row per write");
  }

  const file = ledgerFilePath(vault, CTX.session_key, TS);
  const lines = (await readFile(file, "utf8")).split("\n");
  assert.equal(lines.pop(), "", "the file ends on a row boundary");
  assert.equal(lines.length, 3);
  assert.deepEqual(lines.map((line) => JSON.parse(line).turn_ix), [1, 2, 3]);
  const row = JSON.parse(lines[0]);
  assert.equal(row.v, 5);
  assert.equal(row.kind, "turn");
  assert.equal(row.session_key, CTX.session_key);
  assert.equal(row.agent_key, "main");
  assert.equal(row.wire, "claude-code");
  assert.equal(row.principal, "claude-code");
  assert.deepEqual(row.features, ["MINNI_USE_LEDGER"]);
  assert.ok("build" in row, "every row says which build wrote it (null when unknown)");
});

test("no row exceeds 4 KB: oversized hash lists are clipped, unclippable rows rejected", async (t) => {
  const { vault } = await makeVault(t);
  // Hash lists are capped at 64 each, so a row over 4 KB needs a long key as
  // well: with both lists full it overflows, and without them it fits.
  const many = Array.from({ length: 400 }, (_, i) => i.toString(16).padStart(16, "0"));
  const longKey = `vault:wiki/${"k".repeat(2_000)}.md`;
  const clippable = buildSurfacedRow(
    CTX,
    {
      channel: "prompt_pointer",
      mkey: longKey,
      handles: ["m:00000000"],
      title: "A",
      rank: 1,
      visible: true,
      delivered: true,
      shingles: many,
      echo: many,
    },
    TS,
  );
  assert.equal(clippable.shingles.length, 64, "a hash list is capped at 64 entries");
  assert.equal(clippable.echo.length, 64);
  assert.ok(Buffer.byteLength(`${JSON.stringify(clippable)}\n`, "utf8") > MAX_ROW_BYTES, "the fixture really overflows");
  const unclippable = buildSurfacedRow(
    CTX,
    {
      channel: "prompt_pointer",
      mkey: `vault:${"x".repeat(MAX_ROW_BYTES)}.md`,
      handles: [],
      title: "B",
      rank: 2,
      visible: true,
      delivered: true,
    },
    TS,
  );

  const result = appendLedgerRows(vault, [clippable, unclippable, turn(1)]);
  assert.equal(result.written, 2);
  assert.equal(result.failed, 1, "the unclippable row is rejected, not split");
  assert.equal(result.clipped, 1);

  const raw = await readFile(ledgerFilePath(vault, CTX.session_key, TS), "utf8");
  const lines = raw.split("\n").filter(Boolean);
  assert.equal(lines.length, 2);
  for (const line of lines) {
    assert.ok(Buffer.byteLength(`${line}\n`, "utf8") <= MAX_ROW_BYTES, "every line fits the bound");
  }
  const kept = JSON.parse(lines[0]);
  assert.equal(kept.mkey, longKey);
  assert.equal(kept.clipped, true);
  assert.equal(kept.shingles, undefined);
  assert.equal(kept.echo, undefined);
});

test("a torn last line and a malformed line are skipped by the reader", async (t) => {
  const { vault } = await makeVault(t);
  appendLedgerRows(vault, [turn(1), turn(2)]);
  const file = ledgerFilePath(vault, CTX.session_key, TS);
  // A malformed row in the middle and a torn (unterminated) tail.
  await writeFile(file, "not json at all\n", { flag: "a" });
  appendLedgerRows(vault, [turn(3)]);
  await writeFile(file, '{"v":5,"kind":"turn","session_key":"0123', { flag: "a" });

  const { rows, skipped } = readSessionLedger(vault, CTX.session_key);
  assert.deepEqual(rows.map((row) => row.turn_ix), [1, 2, 3]);
  assert.equal(skipped, 2);

  // An unterminated tail that happens to parse is still torn: a row counts only
  // once its newline landed.
  await writeFile(file, "\n", { flag: "a" });
  appendLedgerRows(vault, [turn(4)]);
  const full = JSON.stringify(turn(5));
  await writeFile(file, full, { flag: "a" });
  const again = readSessionLedger(vault, CTX.session_key);
  assert.deepEqual(again.rows.map((row) => row.turn_ix), [1, 2, 3, 4]);
});

test("a short write is terminated, so it costs one row, not two", async (t) => {
  const { vault } = await makeVault(t);
  // Row 2's write lands only half its bytes. Unterminated, those bytes would
  // fuse with row 3's O_APPEND write into one malformed line, losing both.
  const original = fs.writeSync;
  let calls = 0;
  fs.writeSync = function (...args) {
    calls += 1;
    if (calls === 2 && Buffer.isBuffer(args[1])) {
      const [fd, buffer] = args;
      return original.call(this, fd, buffer, 0, Math.floor(buffer.length / 2));
    }
    return original.apply(this, args);
  };
  let result;
  try {
    result = appendLedgerRows(vault, [turn(1), turn(2), turn(3)]);
  } finally {
    fs.writeSync = original;
  }
  assert.equal(result.written, 2);
  assert.equal(result.failed, 1);

  const { rows, skipped } = readSessionLedger(vault, CTX.session_key);
  assert.deepEqual(rows.map((row) => row.turn_ix), [1, 3], "the row after a short write survives");
  assert.equal(skipped, 1, "the short write costs exactly one line");
});

test("hash lists keep only 16-hex hashes; caller text never reaches a row", () => {
  const row = buildSurfacedRow(
    CTX,
    {
      channel: "prompt_pointer",
      mkey: "vault:wiki/a.md",
      handles: ["m:0123abcd", "d:p:42", "free text pretending to be a handle"],
      title: "A",
      rank: 1,
      visible: true,
      delivered: true,
      shingles: ["0123456789abcdef", "SECRET body text", "0123456789ABCDEF", "0123456789abcdef0", 7],
      echo: ["fedcba9876543210", "an injected SECRET sentence"],
    },
    TS,
  );
  assert.deepEqual(row.shingles, ["0123456789abcdef"]);
  assert.deepEqual(row.echo, ["fedcba9876543210"]);
  assert.deepEqual(row.handles, ["m:0123abcd", "d:p:42"]);
  assert.ok(!JSON.stringify(row).includes("SECRET"));
  assert.ok(!JSON.stringify(row).includes("pretending"));
});

test("judged evidence is a bounded enum, never free text", () => {
  const judged = buildJudgedRow(
    CTX,
    {
      sid: "0123456789abcdef",
      outcome: "cited",
      via: "title",
      evidence: ["title", "handle", "the reply said SECRET verbatim", "title", "lexicon", "shingle", "open", 3],
      title_assessable: true,
      final: true,
      rule_version: 1,
    },
    TS,
  );
  assert.deepEqual(judged.evidence, ["title", "handle", "lexicon", "shingle", "open"]);
  assert.ok(!JSON.stringify(judged).includes("SECRET"));

  for (const evidence of ["handle", { handle: true }, null, undefined]) {
    const row = buildJudgedRow(
      CTX,
      { sid: "0123456789abcdef", outcome: "ignored", via: "none", evidence, title_assessable: false, final: false, rule_version: 1 },
      TS,
    );
    assert.deepEqual(row.evidence, [], `non-array evidence ${JSON.stringify(evidence)}`);
  }
});

test("turn rows say whether the scheduled-template rule ran, and never carry its hash", () => {
  const on = buildTurnRow(
    CTX,
    { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "pointer", template_rule: "on", rule_id: "sched-template-v1" },
    TS,
  );
  assert.equal(on.template_rule, "on");
  assert.equal(on.rule_id, "sched-template-v1");

  const off = buildTurnRow(CTX, { turn_ix: 2, envelope_kind: "human", subkind: null, exit: "pointer", template_rule: "off" }, TS);
  assert.equal(off.template_rule, "off");
  assert.equal("rule_id" in off, false);

  // Absent or junk values are left out rather than guessed.
  const absent = buildTurnRow(CTX, { turn_ix: 3, envelope_kind: "human", subkind: null, exit: "pointer" }, TS);
  assert.equal("template_rule" in absent, false);
  assert.equal("rule_id" in absent, false);
  const junk = buildTurnRow(
    CTX,
    { turn_ix: 4, envelope_kind: "human", subkind: null, exit: "pointer", template_rule: "ON", rule_id: "has spaces and SECRET text" },
    TS,
  );
  assert.equal("template_rule" in junk, false);
  assert.equal("rule_id" in junk, false);

  // The turn row's keys are exactly the common fields plus these: no template hash.
  assert.deepEqual(Object.keys(on).sort(), [
    "agent_key", "build", "envelope_kind", "exit", "features", "kind", "principal", "rule_id",
    "session_key", "subkind", "template_rule", "ts", "turn_ix", "v", "wire",
  ]);
});

test("the per-file reader is not exported: reads go through the contained session reader", () => {
  assert.equal(ledger.readLedgerFile, undefined);
  assert.equal(typeof ledger.readSessionLedger, "function");
});

test("a symlinked .runtime that escapes the vault is refused, and nothing is created outside", async (t) => {
  const { root, vault } = await makeVault(t);
  const outside = path.join(root, "outside");
  await mkdir(outside, { recursive: true });
  await symlink(outside, path.join(vault, ".runtime"));

  let result;
  assert.doesNotThrow(() => {
    result = appendLedgerRows(vault, [turn(1)]);
  });
  assert.equal(result.written, 0);
  assert.equal(result.failed, 1);
  assert.deepEqual(await readdir(outside), [], "no directory or file was created through the link");

  const read = readSessionLedger(vault, CTX.session_key);
  assert.deepEqual(read.rows, []);
  assert.equal(read.refused, true);
});

test("a symlinked ledger file is refused", async (t) => {
  const { root, vault } = await makeVault(t);
  const target = path.join(root, "victim.jsonl");
  await writeFile(target, "", "utf8");
  const file = ledgerFilePath(vault, CTX.session_key, TS);
  await mkdir(path.dirname(file), { recursive: true });
  await symlink(target, file);

  const result = appendLedgerRows(vault, [turn(1)]);
  assert.equal(result.written, 0);
  assert.equal(result.failed, 1);
  assert.equal(await readFile(target, "utf8"), "", "the write did not follow the link");
});

test("a write failure returns a count and never throws", async (t) => {
  const { root, vault } = await makeVault(t);
  // .runtime is a regular file: the ledger directory cannot be created.
  await writeFile(path.join(vault, ".runtime"), "not a directory", "utf8");
  let result;
  assert.doesNotThrow(() => {
    result = appendLedgerRows(vault, [turn(1), turn(2)]);
  });
  assert.equal(result.written, 0);
  assert.equal(result.failed, 2);

  // A vault that does not exist at all.
  const missing = appendLedgerRows(path.join(root, "no-such-vault"), [turn(1)]);
  assert.equal(missing.written, 0);
  assert.equal(missing.failed, 1);

  // A session key that would leave the ledger directory.
  const { vault: fresh } = await makeVault(t);
  const traversal = appendLedgerRows(fresh, [turn(1, { ...CTX, session_key: "../escape" })]);
  assert.equal(traversal.written, 0);
  assert.equal(traversal.failed, 1);
  // An unparseable timestamp has no day file.
  const badTs = appendLedgerRows(fresh, [turn(1, CTX, "not-a-date")]);
  assert.equal(badTs.failed, 1);
});
