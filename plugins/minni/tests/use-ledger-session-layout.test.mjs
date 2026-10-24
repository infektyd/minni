// Recall-to-use S1a (§4.2, §4.3): ledger layout and session keys.
//
// The ledger is laid out per session, then per day:
// <vault>/.runtime/use-ledger/<session_key>/<yyyy-mm-dd>.jsonl. A session that
// runs past midnight must stay ONE session (one directory), or the detector
// would split its windows in two. Session ids are hashed before they become a
// path component.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  appendLedgerRows,
  buildTurnRow,
  LEDGER_RELDIR,
  readSessionLedger,
  sessionKeyFrom,
} from "../dist/use-ledger.js";

// The ledger honours the MINNI_USE_LEDGER kill switch from env; a shell that
// has it off must not turn these into false failures. Each test file runs in
// its own process, so this stays local.
delete process.env.MINNI_USE_LEDGER;

const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

async function makeVault(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-ledger-layout-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(vault, { recursive: true });
  return { root, vault };
}

test("a session spanning two days writes into one session directory", async (t) => {
  const { vault } = await makeVault(t);
  const ctx = {
    session_key: sessionKeyFrom({ session_id: "synthetic-overnight-session" }),
    agent_key: "main",
    wire: "claude-code",
    principal: "claude-code",
    features: [],
  };
  const late = buildTurnRow(ctx, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "weak" }, "2026-10-22T23:59:30.000Z");
  const early = buildTurnRow(ctx, { turn_ix: 2, envelope_kind: "human", subkind: null, exit: "weak" }, "2026-10-23T00:00:30.000Z");

  assert.equal(appendLedgerRows(vault, [late]).written, 1);
  assert.equal(appendLedgerRows(vault, [early]).written, 1);

  const ledgerRoot = path.join(vault, LEDGER_RELDIR);
  assert.deepEqual(await readdir(ledgerRoot), [ctx.session_key], "one directory for the session");
  assert.deepEqual(
    (await readdir(path.join(ledgerRoot, ctx.session_key))).sort(),
    ["2026-10-22.jsonl", "2026-10-23.jsonl"],
    "one file per UTC day inside it",
  );

  const { rows } = readSessionLedger(vault, ctx.session_key);
  assert.deepEqual(rows.map((row) => row.turn_ix), [1, 2], "the session reads back in day order");
});

test("two sessions never share a directory", async (t) => {
  const { vault } = await makeVault(t);
  const base = { agent_key: "main", wire: "claude-code", principal: "claude-code", features: [] };
  const ts = "2026-10-23T08:00:00.000Z";
  const a = buildTurnRow({ ...base, session_key: sessionKeyFrom({ session_id: "synthetic-a" }) }, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "weak" }, ts);
  const b = buildTurnRow({ ...base, session_key: sessionKeyFrom({ session_id: "synthetic-b" }) }, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "weak" }, ts);
  assert.equal(appendLedgerRows(vault, [a, b]).written, 2);
  assert.equal((await readdir(path.join(vault, LEDGER_RELDIR))).length, 2);
});

test("session keys: hashed session_id, else tx- transcript realpath, else unlabeled", async (t) => {
  const { root } = await makeVault(t);

  const key = sessionKeyFrom({ session_id: "synthetic-session-0001", transcript_path: "/ignored.jsonl" });
  assert.equal(key, sha256("synthetic-session-0001").slice(0, 16));
  assert.match(key, /^[0-9a-f]{16}$/);

  // Transcript fallback hashes the REAL path, so two spellings of one file agree.
  const realDir = path.join(root, "transcripts");
  await mkdir(realDir, { recursive: true });
  const transcript = path.join(realDir, "synthetic.jsonl");
  await writeFile(transcript, "", "utf8");
  const alias = path.join(root, "alias");
  await symlink(realDir, alias);
  const viaReal = sessionKeyFrom({ transcript_path: transcript });
  const viaAlias = sessionKeyFrom({ transcript_path: path.join(alias, "synthetic.jsonl") });
  assert.match(viaReal, /^tx-[0-9a-f]{16}$/);
  assert.equal(viaAlias, viaReal);
  assert.equal(viaReal, `tx-${sha256(await realpath(transcript)).slice(0, 16)}`);

  // A transcript that does not exist yet still keys (on its resolved spelling).
  assert.match(sessionKeyFrom({ transcript_path: path.join(root, "missing.jsonl") }), /^tx-[0-9a-f]{16}$/);

  assert.equal(sessionKeyFrom({}), "unlabeled");
  assert.equal(sessionKeyFrom({ session_id: "" }), "unlabeled");
  assert.equal(sessionKeyFrom({ session_id: 42, transcript_path: 7 }), "unlabeled");
});
