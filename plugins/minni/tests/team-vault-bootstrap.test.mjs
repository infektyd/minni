import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { bootstrapApprenticeVault } from "../dist/team-vault-bootstrap.js";
import { agentIdToVaultSlug, resolveAgentVaultPath } from "../dist/agent_ping.js";
import { DEFAULT_AGENT_ID } from "../dist/config.js";

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
  // #298: flat layout, no "agents" segment — matches every real agent vault
  // on disk (~/.minni/codex-vault, not ~/.minni/agents/codex-vault). The
  // slug also strips hyphens now (agentIdToVaultSlug's fallback transform,
  // shared with the reader) rather than preserving them.
  assert.equal(result.vaultPath, path.join(root, "swiftstrictconcurrencyreviewer-vault"));

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
  // Pin the memory-policy line: recall, learn, vault-writes are each labeled correctly.
  assert.match(agentsMd, /Memory policy:\*\* recall=allowed, learn=manual-only, vault-writes=manual-only/);
});

test("bootstrapApprenticeVault throws clearly when vaultPath exists as a non-directory", async () => {
  const root = await makeTmpRoot();
  const collidingPath = path.join(root, "collide-vault");
  await writeFile(collidingPath, "not a directory", "utf8");

  await assert.rejects(
    bootstrapApprenticeVault({
      sovereignRoot: root,
      permanentAgentId: "collide",
      profile: makePermanentProfile(),
    }),
    /vaultPath exists but is not a directory/,
  );
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

test("bootstrapApprenticeVault slugifies dirty agent ids using the reader's own slug function", async () => {
  // #298: the slug transform is now agentIdToVaultSlug (shared with
  // agent_ping.ts's resolveAgentVaultPath), not a bespoke local one — it
  // strips punctuation entirely rather than collapsing it to hyphens. That
  // is a real, intentional behavior change from the pre-#298 writer: it
  // trades slug readability for the one property that actually matters,
  // guaranteed agreement with the reader.
  const agentId = "Swift Strict Concurrency Reviewer (v2)";
  const root = await makeTmpRoot();
  const result = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: agentId,
    profile: makePermanentProfile(),
  });
  assert.equal(result.bootstrapped, true);
  const dirName = path.basename(result.vaultPath);
  assert.match(dirName, /^[a-z0-9]+-vault$/);
  assert.equal(dirName, `${agentIdToVaultSlug(agentId)}-vault`);
});

test("#298: bootstrapApprenticeVault's vault directory NAME matches resolveAgentVaultPath's for the same agent id", async () => {
  // The regression this issue is about: writer and reader must agree on
  // the slug for a given promoted agent id. sovereignRoot (test-controlled,
  // for isolation) legitimately differs from resolveAgentVaultPath's real
  // <homedir>/.minni root, but the <slug>-vault BASENAME must be identical.
  const candidateIds = [
    "reviewer",
    "Swift Strict Concurrency Reviewer (v2)",
    "grok-build",
    "codex",
    "my_new-Apprentice.42",
  ];
  // resolveAgentVaultPath(DEFAULT_AGENT_ID) returns DEFAULT_VAULT_PATH, not a
  // <slug>-vault shape at all — exclude it so the test stays meaningful
  // regardless of what MINNI_AGENT_ID happens to be set to in this environment.
  // Also clear MINNI_AGENT_VAULTS / MINNI_*_VAULT_PATH: an operator's real
  // shell env would otherwise redirect resolveAgentVaultPath (and now the
  // writer too) somewhere this test never created, an environment-dependent
  // false failure a review round caught.
  const savedMapping = process.env.MINNI_AGENT_VAULTS;
  delete process.env.MINNI_AGENT_VAULTS;
  try {
    for (const agentId of candidateIds.filter((id) => id !== DEFAULT_AGENT_ID)) {
      const root = await makeTmpRoot();
      const result = await bootstrapApprenticeVault({
        sovereignRoot: root,
        permanentAgentId: agentId,
        profile: makePermanentProfile(),
      });
      const writerBasename = path.basename(result.vaultPath);
      const readerBasename = path.basename(resolveAgentVaultPath(agentId));
      assert.equal(
        writerBasename,
        readerBasename,
        `writer and reader disagree on the vault dir name for agentId=${JSON.stringify(agentId)}`,
      );
    }
  } finally {
    if (savedMapping === undefined) delete process.env.MINNI_AGENT_VAULTS;
    else process.env.MINNI_AGENT_VAULTS = savedMapping;
  }
});

test("#298 review: an operator MINNI_AGENT_VAULTS override is honored by the writer, not just the reader", async () => {
  const root = await makeTmpRoot();
  const overrideRoot = await makeTmpRoot();
  const overridePath = path.join(overrideRoot, "elsewhere-vault");
  const agentId = "override-test-agent";

  const savedMapping = process.env.MINNI_AGENT_VAULTS;
  process.env.MINNI_AGENT_VAULTS = JSON.stringify({ [agentId]: overridePath });
  try {
    const result = await bootstrapApprenticeVault({
      sovereignRoot: root,
      permanentAgentId: agentId,
      profile: makePermanentProfile(),
    });
    assert.equal(result.vaultPath, overridePath);
    assert.equal(result.vaultPath, resolveAgentVaultPath(agentId));
    assert.equal(await pathExists(path.join(root, "override-test-agent-vault")), false);
  } finally {
    if (savedMapping === undefined) delete process.env.MINNI_AGENT_VAULTS;
    else process.env.MINNI_AGENT_VAULTS = savedMapping;
  }
});

test("#298 review: a slug collision between two different agent ids is rejected loudly, not silently shared", async () => {
  const root = await makeTmpRoot();

  const first = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "my-agent",
    profile: makePermanentProfile(),
  });
  assert.equal(first.bootstrapped, true);

  // "my_agent" strips to the identical slug "myagent" as "my-agent" under
  // agentIdToVaultSlug — this must be a loud collision error, never a
  // silent bootstrapped:false handing the caller someone else's vault.
  await assert.rejects(
    bootstrapApprenticeVault({
      sovereignRoot: root,
      permanentAgentId: "my_agent",
      profile: makePermanentProfile(),
    }),
    /vaultPath collision/,
  );

  // The original agent re-bootstrapping its OWN vault is still a clean,
  // silent idempotent no-op — the collision check must not regress that.
  const again = await bootstrapApprenticeVault({
    sovereignRoot: root,
    permanentAgentId: "my-agent",
    profile: makePermanentProfile(),
  });
  assert.equal(again.bootstrapped, false);
  assert.equal(again.vaultPath, first.vaultPath);
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
