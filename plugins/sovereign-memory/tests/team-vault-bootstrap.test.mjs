import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtemp, readFile, readdir, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { bootstrapApprenticeVault } from "../dist/team-vault-bootstrap.js";

const tmpRoots = [];

async function makeTmpRoot() {
  const root = await mkdtemp(path.join(os.tmpdir(), "sm-apprentice-"));
  tmpRoots.push(root);
  return root;
}

after(async () => {
  for (const root of tmpRoots) {
    await rm(root, { recursive: true, force: true });
  }
});

function makePermanentProfile(overrides = {}) {
  return {
    agentId: "agent-swift-strict-concurrency-reviewer",
    role: "reviewer",
    focus: "Audit Swift strict concurrency for cross-actor sends.",
    ownership: ["Praxis/Services/"],
    permissions: ["read", "test", "memory-recall"],
    lifetime: "permanent",
    memoryPolicy: { recall: "allowed", learn: "manual-only", vaultWrites: "manual-only" },
    sourceTemporaryAgentId: "team-reviewer-1",
    promotionEvidence: {
      score: 5,
      reasons: ["completed assigned task", "submitted evidence plus verification"],
    },
    ...overrides,
  };
}

async function pathExists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

test("bootstrapApprenticeVault creates a fresh vault tree with seeded files", async () => {
  const root = await makeTmpRoot();
  const profile = makePermanentProfile();
  const result = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "swift-strict-concurrency-reviewer",
    profile,
  });

  assert.equal(result.bootstrapped, true);
  assert.equal(result.vaultPath, path.join(root, "agents", "swift-strict-concurrency-reviewer-vault"));

  for (const sub of ["schema", "inbox", "wiki", "raw"]) {
    assert.equal(await pathExists(path.join(result.vaultPath, sub)), true, `expected ${sub}/ to exist`);
  }
  assert.equal(await pathExists(path.join(result.vaultPath, "schema", "AGENTS.md")), true);
  assert.equal(await pathExists(path.join(result.vaultPath, "index.md")), true);
  assert.equal(await pathExists(path.join(result.vaultPath, "log.md")), true);

  assert.deepEqual(
    result.filesCreated,
    ["index.md", "log.md", path.join("schema", "AGENTS.md")].sort(),
  );

  const agentsMd = await readFile(path.join(result.vaultPath, "schema", "AGENTS.md"), "utf8");
  assert.match(agentsMd, /Role:\*\* reviewer/);
  assert.match(agentsMd, /Audit Swift strict concurrency/);
  assert.match(agentsMd, /read, test, memory-recall/);
  assert.match(agentsMd, /Promoted from:\*\* team-reviewer-1/);
  assert.match(agentsMd, /Promotion score:\*\* 5/);
  assert.match(agentsMd, /completed assigned task; submitted evidence plus verification/);
});

test("bootstrapApprenticeVault is idempotent on re-call", async () => {
  const root = await makeTmpRoot();
  const profile = makePermanentProfile();
  const first = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "reviewer",
    profile,
  });
  const firstSchema = await readFile(path.join(first.vaultPath, "schema", "AGENTS.md"), "utf8");

  const second = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "reviewer",
    profile,
  });
  assert.equal(second.bootstrapped, false);
  assert.deepEqual(second.filesCreated, []);
  assert.equal(second.vaultPath, first.vaultPath);

  const secondSchema = await readFile(path.join(first.vaultPath, "schema", "AGENTS.md"), "utf8");
  assert.equal(firstSchema, secondSchema, "schema must be untouched on idempotent re-call");
});

test("bootstrapApprenticeVault slugifies dirty agent ids", async () => {
  const root = await makeTmpRoot();
  const result = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "Swift Strict Concurrency Reviewer (v2)",
    profile: makePermanentProfile(),
  });
  assert.equal(result.bootstrapped, true);
  const dirName = path.basename(result.vaultPath);
  assert.match(dirName, /^[a-z0-9-]+-vault$/);
  assert.equal(dirName, "swift-strict-concurrency-reviewer-v2-vault");
});

test("bootstrapApprenticeVault throws when agentId reduces to empty after slugify", async () => {
  const root = await makeTmpRoot();
  await assert.rejects(
    bootstrapApprenticeVault({
      sovereignRoot: root,
      permanentAgentId: "@@@",
      profile: makePermanentProfile(),
    }),
    /alphanumeric/i,
  );
});

test("bootstrapApprenticeVault seeds inbox entries when seedInbox provided", async () => {
  const root = await makeTmpRoot();
  const result = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "reviewer",
    profile: makePermanentProfile(),
    seedInbox: [
      { slug: "harvest-foo", payload: { kind: "team-harvest", text: "small backend changes win" } },
      { slug: "harvest-bar", payload: { kind: "team-harvest", text: "guard against expired runtimes" } },
    ],
  });

  assert.equal(result.bootstrapped, true);
  const inboxFiles = (await readdir(path.join(result.vaultPath, "inbox"))).filter((n) => n.endsWith(".json"));
  assert.equal(inboxFiles.length, 2);

  const parsed = await Promise.all(
    inboxFiles.map(async (n) => JSON.parse(await readFile(path.join(result.vaultPath, "inbox", n), "utf8"))),
  );
  const slugs = parsed.map((p) => p.slug).sort();
  assert.deepEqual(slugs, ["harvest-bar", "harvest-foo"]);
  for (const entry of parsed) {
    assert.equal(typeof entry.createdAt, "string");
    assert.equal(entry.kind, "team-harvest");
    assert.equal(typeof entry.text, "string");
  }

  // filesCreated includes the inbox files with relative paths
  const inboxRel = result.filesCreated.filter((p) => p.startsWith("inbox" + path.sep) || p.startsWith("inbox/"));
  assert.equal(inboxRel.length, 2);
});

test("bootstrapApprenticeVault works against the real default fs in a tmp dir", async () => {
  const root = await makeTmpRoot();
  const result = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "real-fs-test",
    profile: makePermanentProfile(),
  });
  assert.equal(result.bootstrapped, true);
  // Re-stat through real fs to confirm directory structure landed.
  const st = await stat(result.vaultPath);
  assert.equal(st.isDirectory(), true);
});
