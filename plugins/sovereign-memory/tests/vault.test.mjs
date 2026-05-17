import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditTail,
  auditReport,
  ensureVault,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  vaultFirstLearn,
  writeVaultPage,
} from "../dist/vault.js";

test("ensureVault creates the Codex LLM wiki structure and schema", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-"));
  try {
    const result = await ensureVault(root);

    assert.equal(result.vaultPath, root);
    assert.ok(result.created.includes(path.join(root, "raw")));
    assert.ok(result.created.includes(path.join(root, "wiki", "entities")));
    assert.ok(result.created.includes(path.join(root, "outbox")));

    const schema = await readFile(path.join(root, "schema", "AGENTS.md"), "utf8");
    assert.match(schema, /Codex Sovereign Memory Vault/);
    assert.match(schema, /raw sources/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveInboxHandoffContext resolves wikilink refs for boot priming", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-handoff-prime-"));
  try {
    await ensureVault(root);
    const decisionDir = path.join(root, "wiki", "decisions");
    await mkdir(decisionDir, { recursive: true });
    await writeFile(
      path.join(decisionDir, "auth-migration.md"),
      "---\ntitle: Auth Migration\n---\n\nUse the short-lived token exchange for auth migration.",
      "utf8",
    );

    const snippets = await resolveInboxHandoffContext(root, [
      {
        slug: "auth-handoff",
        filePath: path.join(root, "inbox", "auth.json"),
        createdAt: "2026-04-26T00:00:00.000Z",
        payload: {
          kind: "handoff",
          wikilink_refs: ["wiki/decisions/auth-migration"],
        },
      },
    ]);

    assert.equal(snippets.length, 1);
    assert.equal(snippets[0].ref, "wiki/decisions/auth-migration");
    assert.match(snippets[0].snippet, /short-lived token exchange/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("vaultFirstLearn writes a note, updates index, and appends audit logs", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-learn-"));
  try {
    const result = await vaultFirstLearn({
      vaultPath: root,
      title: "Socket daemon health check",
      content: "Sovereign Memory daemon health is checked through /tmp/sovereign.sock.",
      category: "fact",
      source: "unit-test",
      agentId: "codex",
      storeResult: { ok: true, detail: "learned" },
    });

    assert.match(result.notePath, /wiki\/sessions\/\d{8}-socket-daemon-health-check\.md$/);

    const note = await readFile(result.notePath, "utf8");
    assert.match(note, /agent: codex/);
    assert.match(note, /category: fact/);
    assert.match(note, /Sovereign Memory daemon health/);

    const index = await readFile(path.join(root, "index.md"), "utf8");
    assert.match(index, /\[\[wiki\/sessions\/\d{8}-socket-daemon-health-check\]\]/);

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /sovereign_learn/);
    assert.match(log, /Socket daemon health check/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeVaultPage supports raw and wiki pages without treating them as learnings", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-page-"));
  try {
    const raw = await writeVaultPage({
      vaultPath: root,
      title: "Session Excerpt",
      content: "Immutable session source.",
      section: "raw",
      source: "unit-test",
    });
    const concept = await writeVaultPage({
      vaultPath: root,
      title: "Recall Transparency",
      content: "Memory tools should show what they read and write.",
      section: "concepts",
      source: "unit-test",
    });

    assert.match(raw.notePath, /raw\/\d{8}-session-excerpt\.md$/);
    assert.match(concept.notePath, /wiki\/concepts\/recall-transparency\.md$/);

    const rawNote = await readFile(raw.notePath, "utf8");
    assert.match(rawNote, /immutable: true/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("auditTail returns recent audit entries from daily logs", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "sovereign_status",
      summary: "status checked",
      details: { socket: "ok" },
    });

    const tail = await auditTail(root, 5);

    assert.equal(tail.entries.length, 1);
    assert.match(tail.text, /sovereign_status/);
    assert.match(tail.text, /status checked/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("auditReport summarizes recent tool activity", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-report-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "sovereign_recall",
      summary: "recall checked",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "sovereign_learning_quality",
      summary: "quality checked",
      details: { ok: true },
    });

    const report = await auditReport(root, 10);

    assert.equal(report.entries, 2);
    assert.equal(report.tools.sovereign_recall, 1);
    assert.equal(report.tools.sovereign_learning_quality, 1);
    assert.deepEqual(report.recentSummaries, [
      "sovereign_recall: recall checked",
      "sovereign_learning_quality: quality checked",
    ]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("searchVaultNotes ranks Codex wiki learnings for recall context", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-search-"));
  try {
    await vaultFirstLearn({
      vaultPath: root,
      title: "Codex plugin full suite marker",
      content: "SM_SEARCH_MARKER confirms vault-first learning is visible to AI recall context packs.",
      category: "fact",
      source: "unit-test",
      agentId: "codex",
      storeResult: { ok: true },
    });
    await writeVaultPage({
      vaultPath: root,
      title: "Unrelated concept",
      content: "This note discusses a different subject.",
      section: "concepts",
      source: "unit-test",
    });

    const results = await searchVaultNotes(root, "SM_SEARCH_MARKER AI recall context", 3);

    assert.equal(results.length, 1);
    assert.match(results[0].relativePath, /wiki\/sessions\/\d{8}-codex-plugin-full-suite-marker\.md$/);
    assert.match(results[0].wikilink, /\[\[wiki\/sessions\/\d{8}-codex-plugin-full-suite-marker\]\]/);
    assert.match(results[0].snippet, /SM_SEARCH_MARKER/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
