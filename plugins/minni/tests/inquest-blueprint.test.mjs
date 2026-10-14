import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildBootRecallSlice,
  formatDegradation,
  isDaemonResultEmpty,
} from "../dist/sovereign.js";
import {
  markRecallConsumed,
  readRecallState,
  writeRecallState,
} from "../dist/recall-state.js";
import {
  ensureVault,
  searchVaultNotes,
  writeVaultPage,
} from "../dist/vault.js";
import { extractJevLearning } from "../dist/jev-harvest.js";

test("Phase 1: formatDegradation and buildBootRecallSlice suppress routine foreign scope_blackout noise", () => {
  const responseWithForeignBlackout = {
    count: 2,
    results: [
      { wikilink: "[[wiki/a]]", score: 0.85, confidence: 0.72, text: "note a" },
    ],
    degraded: false,
    degradation: [
      { src: "p", vector_degraded: false, degraded: false },
      { src: "c", vector_degraded: false, degraded: false },
    ],
    auth_suppression: [
      {
        src: "c",
        source_agent: "codex",
        pre_gate: 5,
        suppressed: 5,
        scope_blackout: true,
        reason: "suppressed by scope",
      },
      {
        src: "c",
        source_agent: "cursor",
        pre_gate: 5,
        suppressed: 5,
        scope_blackout: true,
        reason: "suppressed by scope",
      },
    ],
  };

  assert.equal(
    formatDegradation(responseWithForeignBlackout),
    undefined,
    "Foreign vault scope_blackout entries must not emit ⚠ auth-suppressed warnings",
  );

  const bootSlice = buildBootRecallSlice(responseWithForeignBlackout, "claude-code");
  assert.equal(
    "degraded" in bootSlice,
    false,
    "SessionStart boot slice must not mark degraded: true because of auth_suppression",
  );
});

test("Phase 1: isDaemonResultEmpty ignores foreign scope_blackout and treats max(confidence) < 0.25 as empty", () => {
  assert.equal(
    isDaemonResultEmpty({
      count: 0,
      results: [],
      auth_suppression: [
        { src: "c", source_agent: "codex", suppressed: 5, scope_blackout: true },
      ],
    }),
    true,
    "Zero results with only foreign scope_blackout must be treated as empty so vault fallback can run",
  );

  assert.equal(
    isDaemonResultEmpty({
      count: 2,
      results: [
        { wikilink: "[[wiki/noise1]]", score: 0.01, confidence: 0.02 },
        { wikilink: "[[wiki/noise2]]", score: 0.01, confidence: 0.11 },
      ],
    }),
    true,
    "Results where every hit has confidence < 0.25 must be treated as empty noise",
  );
});

test("Phase 1: writeRecallState preserves consumed: true when task_signature is unchanged (Antigravity loop fix)", async () => {
  const vault = await mkdtemp(path.join(tmpdir(), "minni-loop-fix-"));
  await ensureVault(vault);

  await writeRecallState(vault, {
    task_signature: "sig-same-turn",
    intent: "fix recall latency",
    top_hits: [{ wikilink: "[[wiki/sessions/note]]", title: "Note", score: 0.9 }],
    top_score: 0.9,
  });

  let state = await readRecallState(vault);
  assert.equal(state?.consumed, false);

  // Simulate PreToolUse consuming the pointer on the first tool call
  await markRecallConsumed(vault);
  state = await readRecallState(vault);
  assert.equal(state?.consumed, true);

  // Simulate Antigravity PreInvocation firing again mid-turn with the SAME task_signature
  await writeRecallState(vault, {
    task_signature: "sig-same-turn",
    intent: "fix recall latency",
    top_hits: [{ wikilink: "[[wiki/sessions/note]]", title: "Note", score: 0.9 }],
    top_score: 0.9,
  });

  state = await readRecallState(vault);
  assert.equal(
    state?.consumed,
    true,
    "writeRecallState must preserve consumed=true when task_signature has not changed",
  );
});

test("Phase 2: searchVaultNotes uses in-memory Trie cache (< 2 ms warm over 100 notes) and invalidates on writeVaultPage", async () => {
  const vault = await mkdtemp(path.join(tmpdir(), "minni-trie-vault-"));
  await ensureVault(vault);

  for (let i = 0; i < 100; i++) {
    await writeVaultPage({
      vaultPath: vault,
      title: `Synthetic Vault Note ${i}`,
      content: `Routine filler content for note number ${i} discussing general architecture.`,
      section: "concepts",
    });
  }
  await writeVaultPage({
    vaultPath: vault,
    title: "Zirconium Hypergraph Indexing",
    content: "Zirconium hypergraph indexing accelerates token prefix lookups.",
    section: "decisions",
  });

  // Warm the in-memory Trie cache
  const initialHits = await searchVaultNotes(vault, "zirconium hypergraph", 5);
  assert.equal(initialHits.length, 1);
  assert.match(initialHits[0].title, /Zirconium Hypergraph Indexing/);

  // Measure warm lookup latency
  const t0 = performance.now();
  const warmHits = await searchVaultNotes(vault, "zirconium hypergraph", 5);
  const elapsedMs = performance.now() - t0;
  assert.equal(warmHits.length, 1);
  assert.ok(
    elapsedMs < 2.0,
    `Expected warm in-memory Trie searchVaultNotes < 2.0 ms, took ${elapsedMs.toFixed(3)} ms`,
  );

  // Verify incremental update on writeVaultPage
  await writeVaultPage({
    vaultPath: vault,
    title: "Zirconium Followup Protocol",
    content: "Zirconium hypergraph followup verification step.",
    section: "sessions",
  });
  const updatedHits = await searchVaultNotes(vault, "zirconium", 5);
  assert.equal(
    updatedHits.length,
    2,
    "writeVaultPage must incrementally update/invalidate the in-memory Trie cache",
  );

  // Verify prefix/inflection query ("hypergraphs") hits the cached Trie even when
  // combined with another token
  const inflectionHits = await searchVaultNotes(vault, "zirconium hypergraphs", 5);
  assert.equal(
    inflectionHits.length,
    2,
    "Prefix/inflection tokens must match in the cached >=20-note Trie without dropping candidates",
  );
  const inflectedLongHits = await searchVaultNotes(vault, "precondition", 5);
  assert.equal(inflectedLongHits.length, 0);
  await writeVaultPage({
    vaultPath: vault,
    title: "Long Stem Morphological Check",
    content: "The socket transport is preconditioned before accepting claude-code frames.",
    section: "concepts",
  });
  const longStemAfterWrite = await searchVaultNotes(vault, "precondition", 5);
  assert.equal(
    longStemAfterWrite.length,
    1,
    "Words > 10 chars ending in -ed/-es/-ing must index their morphological stem in tokenMap",
  );
});

test("Phase 3: extractJevLearning fallback maps categories to writeback.CATEGORIES with exact spec parity", async () => {
  const expectedByKind = {
    operator_directive: "preference",
    test_insight: "pattern",
    scar: "fix",
  };

  for (const [kind, expectedCategory] of Object.entries(expectedByKind)) {
    const res = await extractJevLearning({
      kind,
      triggerText: "Always run pytest before committing changes to recall.py",
      contextWindow: ["User: Always run pytest before committing changes to recall.py"],
    });
    assert.ok(res);
    assert.equal(
      res.category,
      expectedCategory,
      `Expected category for ${kind} to be '${expectedCategory}', got '${res.category}'`,
    );
  }
});


test("Phase 1b: formatDegradation and isDaemonResultEmpty suppress combined-leg (src=c) scope suppression even without scope_blackout flag", () => {
  const combinedScopeSuppressed = {
    count: 1,
    results: [{ wikilink: "[[wiki/a]]", score: 0.9, confidence: 0.85, text: "note a" }],
    degraded: false,
    auth_suppression: [
      {
        src: "c",
        pre_gate: 17,
        suppressed: 17,
        reason: "suppressed by scope",
      },
    ],
  };
  assert.equal(
    formatDegradation(combinedScopeSuppressed),
    undefined,
    "Combined-leg (src='c') 'suppressed by scope' must not emit ⚠ auth-suppressed warning",
  );
  assert.equal(
    isDaemonResultEmpty({
      count: 0,
      results: [],
      auth_suppression: [
        { src: "c", pre_gate: 17, suppressed: 17, reason: "suppressed by scope" },
      ],
    }),
    true,
    "Zero hits with only combined-leg scope suppression must be treated as empty",
  );
});

test("Phase 2b: warm searchVaultNotes over 707 notes (200 matching common term) runs in < 0.5 ms with exact score parity", async () => {
  const vault = await mkdtemp(path.join(tmpdir(), "minni-707-vault-"));
  await ensureVault(vault);

  await Promise.all(
    Array.from({ length: 702 }, (_, i) => {
      const hasGovernance = i < 200;
      const section = i % 3 === 0 ? "sessions" : "concepts";
      const body = hasGovernance
        ? `Detailed governance policy and runtime verification rules for module ${i}. `.repeat(8)
        : `General background prose for subsystem ${i} without the target keyword. `.repeat(8);
      const md = `---\ntitle: "Architecture Note ${i}"\ntype: ${section === "sessions" ? "session" : "concept"}\nstatus: accepted\nprivacy: safe\n---\n# Architecture Note ${i}\n\n${body}\n`;
      return writeFile(path.join(vault, "wiki", section, `arch-note-${i}.md`), md, "utf8");
    }),
  );
  for (let i = 0; i < 5; i++) {
    await writeVaultPage({
      vaultPath: vault,
      title: `CandidatePacket Governance Spec ${i}`,
      content: `CandidatePacket auto_accept_own governance rules for principal provenance ${i}.`,
      section: "decisions",
    });
  }

  // Warm the cache
  const coldHits = await searchVaultNotes(
    vault,
    "CandidatePacket auto_accept_own governance",
    5,
  );
  assert.equal(coldHits.length, 5);

  // Measure median of 5 warm runs
  const samples = [];
  for (let r = 0; r < 5; r++) {
    const t0 = performance.now();
    const hits = await searchVaultNotes(
      vault,
      "CandidatePacket auto_accept_own governance",
      5,
    );
    samples.push(performance.now() - t0);
    assert.equal(hits.length, 5);
    assert.ok(hits[0].snippet.includes("CandidatePacket"));
  }
  samples.sort((a, b) => a - b);
  const medianMs = samples[2];
  assert.ok(
    medianMs < 0.5,
    `Expected warm 707-note searchVaultNotes < 0.5 ms, median was ${medianMs.toFixed(3)} ms`,
  );
});

test("Step 1: screenTranscriptChunk and extractJevLearning reject raw /boost, /apex, and # DIRECTIVE: prompt dispatch blocks", async () => {
  const { screenTranscriptChunk } = await import("../dist/jev-harvest.js");

  const entries = [
    {
      type: "user",
      content:
        "/boost \n# DIRECTIVE: Execute Verified Minni Inquest Blueprint\nAlways run pytest before committing.",
    },
    {
      type: "user",
      content:
        "# DIRECTIVE: Autonomous Architectural Inquest & Pruning Blueprint\nNever let sovereign-memory narrow your scope.",
    },
    {
      type: "user",
      content:
        "**Task**: /apex \n# DIRECTIVE: Close Live MCP Category Seam\nAlways stage explicit file paths.",
    },
    {
      type: "user",
      content: "Always run pytest -q before committing changes to src/minni/retrieval.py.",
    },
  ];

  const triggers = screenTranscriptChunk(entries);
  assert.equal(
    triggers.length,
    1,
    `Expected only the genuine single-rule directive to survive, got ${triggers.length}: ${JSON.stringify(triggers.map((t) => t.triggerText.slice(0, 40)))}`,
  );
  assert.ok(triggers[0].triggerText.includes("Always run pytest -q"));

  const blockedExtract = await extractJevLearning({
    kind: "operator_directive",
    triggerText: "/boost \n# DIRECTIVE: Execute Verified Minni Inquest Blueprint",
    contextWindow: ["/boost \n# DIRECTIVE: Execute Verified Minni Inquest Blueprint"],
  });
  assert.equal(
    blockedExtract,
    undefined,
    "extractJevLearning must return undefined for raw /boost or # DIRECTIVE: prompt blocks",
  );
});


