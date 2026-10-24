// Recall-to-use S1a (§4.2): memory keys and handles.
//
// The identity bug these guard against is silent: `_path_to_wikilink` emits the
// same `[[wiki/…]]` for notes from different vaults, and a daemon doc id is
// unique only within one index. Keying on either alone merges distinct memories
// in the ledger, and the before/after rates then count the wrong thing. So a
// daemon result is a vault memory only when its `source` is a file inside THIS
// vault, and every daemon handle carries its index tag.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  docHandle,
  handlesForKey,
  memoryKeyFor,
  vaultHandle,
} from "../dist/use-ledger.js";

const sha1 = (text) => createHash("sha1").update(text, "utf8").digest("hex");

async function makeVaults(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-mkey-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "this-vault");
  const other = path.join(root, "other-vault");
  for (const dir of [vault, other]) {
    await mkdir(path.join(dir, "wiki"), { recursive: true });
    await writeFile(path.join(dir, "wiki", "shared-note.md"), "# Shared note\n", "utf8");
  }
  return { root, vault, other };
}

test("same wikilink, different sources -> different mkeys", async (t) => {
  const { vault, other } = await makeVaults(t);
  const wikilink = "[[wiki/shared-note]]";

  const mine = memoryKeyFor(
    { kind: "daemon", src: "p", doc_id: 7, wikilink, source: path.join(vault, "wiki", "shared-note.md") },
    vault,
  );
  const theirs = memoryKeyFor(
    { kind: "daemon", src: "c", doc_id: 7, wikilink, source: path.join(other, "wiki", "shared-note.md") },
    vault,
  );

  assert.equal(mine, "vault:wiki/shared-note.md", "a source inside this vault is a vault memory");
  assert.equal(theirs, "doc:c:7", "a source in another vault is a daemon document");
  assert.notEqual(mine, theirs);
});

test("the wikilink text never makes a result a vault memory", async (t) => {
  const { vault, other } = await makeVaults(t);
  // The wikilink names a note that DOES exist in this vault; the source is elsewhere.
  const key = memoryKeyFor(
    {
      kind: "daemon",
      src: "p",
      doc_id: "41",
      wikilink: "[[wiki/shared-note]]",
      source: path.join(other, "wiki", "shared-note.md"),
    },
    vault,
  );
  assert.equal(key, "doc:p:41");

  // No source at all: still a doc key, never a guess from the wikilink.
  assert.equal(
    memoryKeyFor({ kind: "daemon", src: "p", doc_id: 41, wikilink: "[[wiki/shared-note]]" }, vault),
    "doc:p:41",
  );
});

test("one doc id in two indexes gives two mkeys", async (t) => {
  const { vault } = await makeVaults(t);
  const p = memoryKeyFor({ kind: "daemon", src: "p", doc_id: 12, source: "/elsewhere/a.md" }, vault);
  const c = memoryKeyFor({ kind: "daemon", src: "c", doc_id: 12, source: "/elsewhere/b.md" }, vault);
  assert.equal(p, "doc:p:12");
  assert.equal(c, "doc:c:12");
});

test("a symlink inside the vault that points outside is not a vault memory", async (t) => {
  const { vault, other } = await makeVaults(t);
  const link = path.join(vault, "wiki", "escape.md");
  await symlink(path.join(other, "wiki", "shared-note.md"), link);
  const key = memoryKeyFor({ kind: "daemon", src: "p", doc_id: 3, source: link }, vault);
  assert.equal(key, "doc:p:3", "containment is judged on the real path, not the spelling");
});

test("a relative or root-equal source cannot prove it is inside this vault", async (t) => {
  const { vault } = await makeVaults(t);
  assert.equal(
    memoryKeyFor({ kind: "daemon", src: "p", doc_id: 5, source: "wiki/shared-note.md" }, vault),
    "doc:p:5",
  );
  assert.equal(memoryKeyFor({ kind: "daemon", src: "p", doc_id: 5, source: vault }, vault), "doc:p:5");
});

test("vault hits key on their vault-relative path, absolute or relative", async (t) => {
  const { vault } = await makeVaults(t);
  assert.equal(memoryKeyFor({ kind: "vault", path: "wiki/shared-note.md" }, vault), "vault:wiki/shared-note.md");
  assert.equal(
    memoryKeyFor({ kind: "vault", path: path.join(vault, "wiki", "shared-note.md") }, vault),
    "vault:wiki/shared-note.md",
  );
  assert.equal(memoryKeyFor({ kind: "vault", path: "../outside.md" }, vault), null, "an escaping path is unkeyable");
});

test("learnings and boot lines get their own key families", async (t) => {
  const { vault } = await makeVaults(t);
  assert.equal(memoryKeyFor({ kind: "learning", learning_id: 812 }, vault), "learning:812");
  assert.equal(memoryKeyFor({ kind: "learning", learning_id: "812" }, vault), "learning:812");
  const line = "- [fix] Always pass uri=True for read-only sqlite opens";
  assert.equal(memoryKeyFor({ kind: "boot_line", line }, vault), `lhash:${sha1(line).slice(0, 12)}`);
  // Surrounding whitespace from line splitting does not change the key.
  assert.equal(memoryKeyFor({ kind: "boot_line", line: `  ${line}\n` }, vault), `lhash:${sha1(line).slice(0, 12)}`);
});

test("unkeyable inputs return null instead of a colliding key", async (t) => {
  const { vault } = await makeVaults(t);
  assert.equal(memoryKeyFor({ kind: "daemon", doc_id: 9, source: "/elsewhere/x.md" }, vault), null, "no src tag");
  assert.equal(memoryKeyFor({ kind: "daemon", src: "p", source: "/elsewhere/x.md" }, vault), null, "no doc id");
  assert.equal(memoryKeyFor({ kind: "daemon", src: "p:q", doc_id: 9 }, vault), null, "a separator in src");
  assert.equal(memoryKeyFor({ kind: "learning", learning_id: "" }, vault), null);
  assert.equal(memoryKeyFor({ kind: "boot_line", line: "   " }, vault), null);
});

test("d: handles carry the index tag; m: handles hash the vault path", () => {
  assert.equal(docHandle("p", 42), "d:p:42");
  assert.equal(docHandle("c", "42"), "d:c:42");
  assert.notEqual(docHandle("p", 42), docHandle("c", 42));
  assert.equal(docHandle("", 42), null);

  const rel = "wiki/shared-note.md";
  assert.equal(vaultHandle(rel), `m:${sha1(rel).slice(0, 8)}`);

  assert.deepEqual(handlesForKey("doc:p:42"), ["d:p:42"]);
  assert.deepEqual(handlesForKey(`vault:${rel}`), [vaultHandle(rel)]);
  assert.deepEqual(handlesForKey("learning:812"), []);
});
