import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { searchVaultNotes } from "../dist/vault.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s3c-vault-cal-"));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "artifacts"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "sessions"), { recursive: true });
  try {
    await fn(vault);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("ups_gate Route A: exact phrase hit >= 24 chars with >= 2 tokens clears gate with gateScore=1.0", async () => {
  await withTempVault(async (vault) => {
    const decisionPath = path.join(vault, "wiki", "decisions", "zephyr-quorum-lease.md");
    await writeFile(
      decisionPath,
      [
        "---",
        "title: Core Consensus Notes",
        "type: decision",
        "status: active",
        "---",
        "# Core Consensus Notes",
        "",
        "Always enforce zephyr quorum lease fencing protocol before promoting a standby node.",
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(
      vault,
      "zephyr quorum lease fencing protocol",
      5,
      undefined,
      { rank: "ups_gate", candidates: "scan" },
    );

    assert.equal(results.length, 1);
    assert.equal(results[0].gateRoute, "a");
    assert.equal(results[0].gateScore, 1.0);
    assert.equal(results[0].weightedScore, 1.0);
    assert.equal(results[0].clearsGate, true);
    assert.equal(results[0].durability, "durable");
  });
});

test("ups_gate 1-word query (>= 24 chars) never opens Route A", async () => {
  await withTempVault(async (vault) => {
    const longSingleToken = "quasar_flux_capacitor_stabilizer_protocol";
    assert.ok(longSingleToken.length >= 24);
    await writeFile(
      path.join(vault, "wiki", "decisions", "single-token.md"),
      [
        "---",
        "title: Single Token Identifier Note",
        "type: decision",
        "status: active",
        "---",
        "# Single Token Identifier Note",
        "",
        `The identifier ${longSingleToken} appears verbatim in the body.`,
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(vault, longSingleToken, 5, undefined, {
      rank: "ups_gate",
      candidates: "scan",
    });

    assert.equal(results.length, 1);
    assert.notEqual(results[0].gateRoute, "a", "1-word query must never open Route A");
    assert.equal(results[0].gateRoute, null);
    assert.equal(results[0].clearsGate, false);
    assert.ok(results[0].gateScore <= 0.5);
  });
});

test("ups_gate Route B vs body-only: >= k_q distinctive terms with >= 1 in title/heading clears; body-only stays <= 0.50", async () => {
  await withTempVault(async (vault) => {
    // Seed 20 unrelated non-noise notes so N=22 and ceil(0.05 * 22) = 2, allowing
    // both test notes below to share the same 3 distinctive terms (df=2 <= 2).
    for (let i = 0; i < 20; i += 1) {
      await writeFile(
        path.join(vault, "wiki", "decisions", `bg-${i}.md`),
        `# Background Decision ${i}\n\nUnrelated baseline note ${i}.\n`,
        "utf8",
      );
    }

    // Route B match: 3 distinctive terms ("helios", "backpressure", "watermark"), with "helios" in title
    // and "backpressure" in a markdown heading, and "watermark" in body.
    await writeFile(
      path.join(vault, "wiki", "decisions", "helios-heading.md"),
      [
        "---",
        "title: Helios stream controller",
        "type: decision",
        "status: active",
        "---",
        "# Stream controller",
        "",
        "## Backpressure handling",
        "",
        "Raise the watermark threshold when downstream queues saturate.",
      ].join("\n"),
      "utf8",
    );

    // Body-only match: has all 4 distinctive terms in body, but 0 in title or headings, and no phrase match.
    await writeFile(
      path.join(vault, "wiki", "decisions", "body-only.md"),
      [
        "---",
        "title: General operational notes",
        "type: decision",
        "status: active",
        "---",
        "# General operational notes",
        "",
        "## Overview",
        "",
        "First we check helios. Later we inspect backpressure. Finally watermark and jitter are logged.",
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(
      vault,
      "configure helios backpressure watermark jitter",
      10,
      undefined,
      { rank: "ups_gate", candidates: "scan" },
    );

    const routeBHit = results.find((r) => r.relativePath.endsWith("helios-heading.md"));
    const bodyOnlyHit = results.find((r) => r.relativePath.endsWith("body-only.md"));
    assert.ok(routeBHit, "Route B note must be returned");
    assert.ok(bodyOnlyHit, "Body-only note must be returned");

    assert.equal(routeBHit.gateRoute, "b");
    assert.equal(routeBHit.clearsGate, true);
    assert.ok(routeBHit.gateScore >= 0.55);
    assert.equal(routeBHit.coveredDistinctive, 3);
    assert.deepEqual(routeBHit.contributingTerms, {
      title: ["helios"],
      heading: ["backpressure"],
      body: ["watermark"],
    });

    assert.equal(bodyOnlyHit.gateRoute, null, "body-only match must have gateRoute = null");
    assert.equal(bodyOnlyHit.clearsGate, false, "body-only match must not clear gate");
    assert.ok(bodyOnlyHit.gateScore <= 0.5, `expected gateScore <= 0.50, got ${bodyOnlyHit.gateScore}`);
    assert.equal(results[0].relativePath, routeBHit.relativePath, "clearing note must rank above non-clearing note");
  });
});

test("ups_gate DF threshold: ubiquitous terms (df > ceil(0.05 * N)) are not distinctive and cannot open Route B", async () => {
  await withTempVault(async (vault) => {
    // Create 25 non-noise notes so ceil(0.05 * 25) = 2.
    // Put "service", "config", "update" in 10 notes (df = 10 > 2 -> NOT distinctive).
    for (let i = 0; i < 10; i += 1) {
      await writeFile(
        path.join(vault, "wiki", "decisions", `common-${i}.md`),
        `# Routine Note ${i}\n\nContains service, config, and update tokens ${i}.\n`,
        "utf8",
      );
    }
    for (let i = 10; i < 25; i += 1) {
      await writeFile(
        path.join(vault, "wiki", "decisions", `filler-${i}.md`),
        `# Unrelated Note ${i}\n\nNothing matching here.\n`,
        "utf8",
      );
    }

    // Query has 3 ubiquitous terms ("service", "config", "update") and only 1 distinctive term ("kestrel").
    // Avoid contiguous query substring so Route A does not fire and Route B is tested in isolation.
    await writeFile(
      path.join(vault, "wiki", "decisions", "kestrel-target.md"),
      `# Kestrel — Service, Config, and Update\n\nAdjust the kestrel daemon's service settings and config during an update.\n`,
      "utf8",
    );

    const results = await searchVaultNotes(
      vault,
      "kestrel service config update",
      5,
      undefined,
      { rank: "ups_gate", candidates: "scan" },
    );

    const target = results.find((r) => r.relativePath.endsWith("kestrel-target.md"));
    assert.ok(target);
    assert.equal(target.coveredDistinctive, 1, "only kestrel is distinctive (df <= 2)");
    assert.equal(target.gateRoute, null, "1 distinctive term (< k_q=3) must not open Route B");
    assert.equal(target.clearsGate, false);
  });
});

test("ups_gate noise notes receive classWeight = 0.25 and never clear the gate regardless of rho", async () => {
  await withTempVault(async (vault) => {
    const phrase = "chronos distributed lock lease renewal invariant";
    await writeFile(
      path.join(vault, "wiki", "artifacts", "plan-chronos.log.md"),
      `# Chronos Distributed Lock Lease Renewal Invariant\n\n${phrase}\n`,
      "utf8",
    );

    const resultsDefault = await searchVaultNotes(vault, phrase, 5, undefined, {
      rank: "ups_gate",
      candidates: "scan",
    });
    assert.equal(resultsDefault.length, 1);
    assert.equal(resultsDefault[0].durability, "noise");
    assert.equal(resultsDefault[0].gateRoute, "a");
    assert.equal(resultsDefault[0].gateScore, 1.0);
    assert.equal(resultsDefault[0].weightedScore, 0.25);
    assert.equal(resultsDefault[0].clearsGate, false);

    // Passing rho = 0.8 (distinctive-term fraction parameter) must NOT alter noise classWeight or allow noise to clear.
    const resultsCustomRho = await searchVaultNotes(vault, phrase, 5, undefined, {
      rank: "ups_gate",
      candidates: "scan",
      rho: 0.8,
    });
    assert.equal(resultsCustomRho[0].weightedScore, 0.25);
    assert.equal(resultsCustomRho[0].clearsGate, false);
  });
});
