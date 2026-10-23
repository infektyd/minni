// Recall-to-use S1b (§4.5 "Stop receipt", §4.6): the receipt's use suffix.
//
// SessionReceipt gains use counts. With 0 surfaced the formatted line is
// byte-identical to today's (vault.test.mjs pins it); otherwise it appends
// ` Use: <n> surfaced, <c> cited, <a> applied.` to the same line, which
// maskTelemetry still drops so harvest never re-reads it.
import assert from "node:assert/strict";
import test from "node:test";

import { maskTelemetry } from "../dist/jev-harvest.js";
import {
  appendLedgerRows,
  buildJudgedRow,
  buildSurfacedRow,
  sessionKeyFrom,
} from "../dist/use-ledger.js";
import { ensureVault, formatSessionReceiptLine, sessionReceipt } from "../dist/vault.js";
import { makeVault } from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const BASE = {
  session_id: "synthetic-receipt",
  entries: 3,
  recalls_strong: 1,
  recalls_weak: 1,
  guard_denied: 0,
  guard_allowed: 1,
  learns: 0,
  vault_writes: 0,
  candidates_drafted: 0,
};
const TODAY = "Minni session receipt: 2 recalls (1 strong), 0 guard nudges, 0 learns committed, 0 candidates staged.";

const counts = (surfaced, cited, applied) => ({ surfaced, cited, applied, opened: 0, dismissed: 0, ignored: 0, unknown: 0, pending: 0 });

test("0 surfaced: the line is byte-identical to today's", () => {
  assert.equal(formatSessionReceiptLine(BASE), TODAY);
  assert.equal(formatSessionReceiptLine({ ...BASE, use: counts(0, 0, 0) }), TODAY);
});

test("2 surfaced: the suffix lands on the same line, and maskTelemetry drops the line", () => {
  const line = formatSessionReceiptLine({ ...BASE, use: counts(2, 1, 0) });
  assert.equal(line, `${TODAY} Use: 2 surfaced, 1 cited, 0 applied.`);
  assert.equal(line.includes("\n"), false);
  assert.equal(maskTelemetry(line), "");
  assert.equal(maskTelemetry(`keep this line\n${line}\nand this one`), "keep this line\nand this one");
});

test("sessionReceipt reads the session's use counts from the ledger", async (t) => {
  const { vault } = await makeVault(t, "minni-receipt-suffix-");
  await ensureVault(vault);
  const sessionId = "synthetic-receipt-ledger";
  const ctx = {
    session_key: sessionKeyFrom({ session_id: sessionId }),
    agent_key: "main",
    wire: "claude-code",
    principal: "claude-code",
    features: ["MINNI_USE_LEDGER"],
  };
  const ts = new Date().toISOString();
  const a = buildSurfacedRow(ctx, { channel: "prompt_pointer", mkey: "vault:wiki/a.md", title: "A", rank: 0, visible: true, delivered: true }, ts);
  const b = buildSurfacedRow(ctx, { channel: "prompt_pointer", mkey: "vault:wiki/b.md", title: "B", rank: 1, visible: true, delivered: true }, ts);
  const judged = buildJudgedRow(
    ctx,
    { sid: a.sid, outcome: "cited", via: "transcript", evidence: ["title"], title_assessable: true, final: true, rule_version: "s1b-1" },
    ts,
  );
  assert.equal(appendLedgerRows(vault, [a, b, judged]).written, 3);

  const receipt = await sessionReceipt(vault, sessionId);
  assert.equal(receipt.use?.surfaced, 2);
  assert.equal(receipt.use?.cited, 1);
  assert.equal(receipt.use?.pending, 1);
  assert.match(formatSessionReceiptLine(receipt), / Use: 2 surfaced, 1 cited, 0 applied\.$/);

  const quiet = await sessionReceipt(vault, "synthetic-no-ledger");
  assert.equal("use" in quiet, false, "no surfacings: the receipt keeps today's shape");
  assert.equal(formatSessionReceiptLine(quiet).includes("Use:"), false);
});

test("the receipt's cited count includes handle citations", async (t) => {
  const { vault } = await makeVault(t, "minni-receipt-handle-");
  await ensureVault(vault);
  const sessionId = "synthetic-receipt-handle";
  const ctx = {
    session_key: sessionKeyFrom({ session_id: sessionId }),
    agent_key: "main",
    wire: "claude-code",
    principal: "claude-code",
    features: ["MINNI_USE_LEDGER"],
  };
  const ts = new Date().toISOString();
  const a = buildSurfacedRow(ctx, { channel: "prompt_pointer", mkey: "vault:wiki/a.md", title: "A", rank: 0, visible: true, delivered: true }, ts);
  const judged = buildJudgedRow(
    ctx,
    { sid: a.sid, outcome: "cited_handle", via: "transcript", evidence: ["handle"], title_assessable: true, final: true, rule_version: "s1b-2" },
    ts,
  );
  assert.equal(appendLedgerRows(vault, [a, judged]).written, 2);
  const receipt = await sessionReceipt(vault, sessionId);
  assert.equal(receipt.use?.cited_handle, 1);
  assert.match(formatSessionReceiptLine(receipt), / Use: 1 surfaced, 1 cited, 0 applied\.$/);
});
