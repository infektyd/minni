import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { extractStrongRecall } from "../dist/recall-state.js";
import { ensureVault, searchVaultNotes } from "../dist/vault.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-s3c-vault-cal-"));
  try {
    await ensureVault(root);
    return await fn(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("Route (a): exact phrase match (>= 24 chars, >= 2 terms) clears gate at 1.0 with gateRoute 'a'", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "decisions");
    await mkdir(dir, { recursive: true });
    const phrase = "quasar_flux_capacitor_stabilizer_protocol";
    await writeFile(
      path.join(dir, "quasar-protocol.md"),
      [
        "---",
        "title: System architecture notes",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# System architecture notes",
        "",
        `Always initialize the ${phrase} before opening the main valve.`,
        "",
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(vault, phrase, 5, undefined, { rank: "ups_gate" });
    assert.equal(results.length, 1);
    const hit = results[0];
    assert.equal(hit.clearsGate, true);
    assert.equal(hit.gateRoute, "a");
    assert.equal(hit.gateScore, 1.0);
    assert.equal(hit.weightedScore, 1.0);
    assert.equal(hit.durability, "durable");
  });
});

test("Route (b): title + coverage (>= 2 title hits and (>= 3 title hits or titleCov >= 0.60)) clears gate in [0.55, 0.85]", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "decisions");
    await mkdir(dir, { recursive: true });
    await writeFile(
      path.join(dir, "zircon-token-rotation.md"),
      [
        "---",
        "title: Zircon token rotation schedule",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Zircon token rotation schedule",
        "",
        "Rotate zircon credentials every 30 days using the automated key escrow.",
        "",
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(
      vault,
      "zircon token rotation policy",
      5,
      undefined,
      { rank: "ups_gate" },
    );
    assert.equal(results.length, 1);
    const hit = results[0];
    assert.equal(hit.clearsGate, true);
    assert.equal(hit.gateRoute, "b");
    assert.ok(
      typeof hit.gateScore === "number" && hit.gateScore >= 0.55 && hit.gateScore <= 0.85,
      `expected Route (b) gateScore in [0.55, 0.85], got ${hit.gateScore}`,
    );
    assert.equal(hit.weightedScore, hit.gateScore);
  });
});

test("Route (c): dense body coverage (>= 4 query terms in body and bodyCov >= 0.75) clears gate in [0.55, 0.70]", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "concepts");
    await mkdir(dir, { recursive: true });
    await writeFile(
      path.join(dir, "general-ops-runbook.md"),
      [
        "---",
        "title: General operational runbook",
        "type: concept",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# General operational runbook",
        "",
        "During failover, check beryllium, germanium, strontium, and palladium sensors before restarting.",
        "",
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(
      vault,
      "beryllium germanium strontium palladium tungsten",
      5,
      undefined,
      { rank: "ups_gate" },
    );
    assert.equal(results.length, 1);
    const hit = results[0];
    assert.equal(hit.clearsGate, true);
    assert.equal(hit.gateRoute, "c");
    assert.ok(
      typeof hit.gateScore === "number" && hit.gateScore >= 0.55 && hit.gateScore <= 0.70,
      `expected Route (c) gateScore in [0.55, 0.70], got ${hit.gateScore}`,
    );
    assert.equal(hit.weightedScore, hit.gateScore);
  });
});

test("Weak matches (1/4 title terms or 2/5 body terms) fail the UPS gate and do not clear", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "concepts");
    await mkdir(dir, { recursive: true });
    await writeFile(
      path.join(dir, "weak-title.md"),
      [
        "---",
        "title: Cobalt overview",
        "type: concept",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Cobalt overview",
        "",
        "Cobalt is a transition metal.",
        "",
      ].join("\n"),
      "utf8",
    );
    await writeFile(
      path.join(dir, "weak-body.md"),
      [
        "---",
        "title: Miscellaneous notes",
        "type: concept",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Miscellaneous notes",
        "",
        "Mentions rhodium and iridium in passing.",
        "",
      ].join("\n"),
      "utf8",
    );

    const titleResults = await searchVaultNotes(
      vault,
      "cobalt lanthanum cerium praseodymium",
      5,
      undefined,
      { rank: "ups_gate" },
    );
    assert.equal(titleResults.length, 1);
    assert.equal(titleResults[0].clearsGate, false);
    assert.equal(titleResults[0].gateRoute, null);
    assert.equal(titleResults[0].gateScore, 0);
    assert.equal(titleResults[0].weightedScore, 0);
    assert.equal(extractStrongRecall(undefined, titleResults), null);

    const bodyResults = await searchVaultNotes(
      vault,
      "rhodium iridium osmium ruthenium platinum",
      5,
      undefined,
      { rank: "ups_gate" },
    );
    assert.equal(bodyResults.length, 1);
    assert.equal(bodyResults[0].clearsGate, false);
    assert.equal(bodyResults[0].gateRoute, null);
    assert.equal(bodyResults[0].gateScore, 0);
    assert.equal(bodyResults[0].weightedScore, 0);
    assert.equal(extractStrongRecall(undefined, bodyResults), null);
  });
});

test("Noise notes get weightedScore = gateScore * rho (default rho = 0 -> clearsGate = false)", async () => {
  await withTempVault(async (vault) => {
    const sessionsDir = path.join(vault, "wiki", "sessions");
    const conceptsDir = path.join(vault, "wiki", "concepts");
    await mkdir(sessionsDir, { recursive: true });
    await mkdir(conceptsDir, { recursive: true });

    const phrase = "neodymium_magnet_quench_recovery_procedure";
    await writeFile(
      path.join(sessionsDir, "20260617-compact-abc.md"),
      `---\ntitle: Session compaction\nprivacy: safe\n---\n\n# Session compaction\n\n${phrase}\n`,
      "utf8",
    );
    await writeFile(
      path.join(conceptsDir, "subagent-ctx.md"),
      `---\ntitle: "[Subagent Context] Worker run"\nsource: compact_distillation:subagent\nprivacy: safe\n---\n\n# [Subagent Context] Worker run\n\n${phrase}\n`,
      "utf8",
    );

    const defaultResults = await searchVaultNotes(vault, phrase, 5, undefined, { rank: "ups_gate" });
    assert.equal(defaultResults.length, 2);
    for (const hit of defaultResults) {
      assert.equal(hit.durability, "noise");
      assert.equal(hit.gateRoute, "a");
      assert.equal(hit.gateScore, 1.0);
      assert.equal(hit.weightedScore, 0);
      assert.equal(hit.clearsGate, false);
    }
    assert.equal(extractStrongRecall(undefined, defaultResults), null);

    const rhoResults = await searchVaultNotes(vault, phrase, 5, undefined, {
      rank: "ups_gate",
      rho: 0.6,
    });
    for (const hit of rhoResults) {
      assert.equal(hit.weightedScore, 0.6);
      assert.equal(hit.clearsGate, true);
    }
  });
});
