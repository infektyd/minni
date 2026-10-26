import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { recallMemory } from "../dist/daemon_transport.js";
import { ensureVault, searchVaultNotes } from "../dist/vault.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-s3c-noise-last-"));
  try {
    await ensureVault(root);
    return await fn(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function makeUnreachableConfig(vaultPath) {
  return {
    agentId: "claude-code",
    vaultPath,
    workspaceId: "test-ws",
    daemonSocketPath: path.join(vaultPath, ".runtime", "nonexistent-daemon.sock"),
    daemonTimeoutMs: 200,
    recallTimeoutMs: 200,
  };
}

test("minni_recall / searchVaultNotes rank: 'noise_last' places durable notes before higher-raw-score noise notes", async () => {
  await withTempVault(async (vault) => {
    const sessionsDir = path.join(vault, "wiki", "sessions");
    const decisionsDir = path.join(vault, "wiki", "decisions");
    await mkdir(sessionsDir, { recursive: true });
    await mkdir(decisionsDir, { recursive: true });

    const query = "vanadium electrolyte rebalance";
    // Noise notes with high term repetition and exact phrase in title + body (raw score > 70)
    await writeFile(
      path.join(sessionsDir, "20260619-compact-1.md"),
      `---\ntitle: Session compaction ${query}\nprivacy: safe\n---\n\n# Session compaction ${query}\n\n${query} ${query} ${query} ${query}\n`,
      "utf8",
    );
    await writeFile(
      path.join(sessionsDir, "20260619-subagent-1.md"),
      `---\ntitle: "[Subagent Context] ${query}"\nsource: compact_distillation:subagent\nprivacy: safe\n---\n\n# [Subagent Context] ${query}\n\n${query} ${query} ${query} ${query}\n`,
      "utf8",
    );

    // Durable note with lower raw score (no exact 3-word phrase match -> raw score ~13.75)
    await writeFile(
      path.join(decisionsDir, "vanadium-rebalance.md"),
      `---\ntitle: Vanadium electrolyte decision\ntype: decision\ncategory: decision\nstatus: accepted\nprivacy: safe\n---\n\n# Vanadium electrolyte decision\n\nDecision: Rebalance the vanadium electrolyte tank weekly.\n`,
      "utf8",
    );

    const ranked = await searchVaultNotes(vault, query, 5, undefined, { rank: "noise_last" });
    assert.equal(ranked.length, 3);
    assert.equal(
      ranked[0].relativePath,
      path.join("wiki", "decisions", "vanadium-rebalance.md"),
      "durable note must appear before noise notes under noise_last",
    );
    assert.equal(ranked[0].durability, "durable");
    assert.equal(ranked[1].durability, "noise");
    assert.equal(ranked[2].durability, "noise");

    // Also verify through recallMemory (with MINNI_RECALL_NOISE_LAST=on default vs off)
    const config = makeUnreachableConfig(vault);
    const recallOn = await recallMemory(config, { query, limit: 5, vaultPath: vault });
    const resultsOn = recallOn.data?.results ?? [];
    assert.equal(resultsOn.length, 3);
    assert.match(String(resultsOn[0].title ?? ""), /Vanadium electrolyte decision/);

    // When MINNI_RECALL_NOISE_LAST=off, legacy raw-score order puts the higher-scoring noise note first
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    await writeFile(
      path.join(vault, ".runtime", "flags.json"),
      JSON.stringify({ MINNI_RECALL_NOISE_LAST: "off" }),
      "utf8",
    );
    const recallOff = await recallMemory(config, { query, limit: 5, vaultPath: vault });
    const resultsOff = recallOff.data?.results ?? [];
    assert.equal(resultsOff.length, 3);
    assert.match(String(resultsOff[0].title ?? ""), /Session compaction|\[Subagent Context\]/);
  });
});

test("minni_recall noise_last still returns noise notes when ONLY noise notes match", async () => {
  await withTempVault(async (vault) => {
    const sessionsDir = path.join(vault, "wiki", "sessions");
    await mkdir(sessionsDir, { recursive: true });
    const query = "ytterbium laser calibration";
    await writeFile(
      path.join(sessionsDir, "20260619-compact-only.md"),
      `---\ntitle: Session compaction ${query}\nprivacy: safe\n---\n\n# Session compaction\n\n${query}\n`,
      "utf8",
    );

    const config = makeUnreachableConfig(vault);
    const res = await recallMemory(config, { query, limit: 5, vaultPath: vault });
    const results = res.data?.results ?? [];
    assert.equal(results.length, 1);
    assert.match(String(results[0].title ?? ""), /Session compaction/);
  });
});
