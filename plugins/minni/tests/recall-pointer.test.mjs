// Slice s5: per-turn UserPromptSubmit recall POINTER + portable recall-state
// file. Proves:
//   (a) a STRONG-hit turn writes <vault>/.runtime/recall-state.json with the
//       canonical shape AND the injected envelope is the LIGHT POINTER (not the
//       full recall pack);
//   (b) a WEAK/empty turn writes NO state file and injects nothing;
//   (c) `consumed` resets to false on each new task_signature (new turn);
//   plus the daemon-confidence strength gate (pure) and a rough before/after
//   token-size comparison of the injected envelope.
//
// Isolation: the factory's UserPromptSubmit recall surface is (1) the daemon
// (socket pointed at a missing path => recall.ok=false, fast structured fail)
// and (2) the vault filesystem. The integration tests drive STRONG hits purely
// through vault notes (a direct substring match scores >= VAULT_DIRECT_MATCH_SCORE),
// so no live daemon is needed. The confidence gate is unit-tested directly.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import {
  buildRecallPointer,
  DEFAULT_RECALL_POINTER_THRESHOLD,
  extractStrongRecall,
  RECALL_STATE_RELPATH,
  recallPointerThreshold,
} from "../dist/recall-state.js";

const MISSING_SOCKET = (home) => path.join(home, "missing.sock");

function turnConfig(vaultPath) {
  return {
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    alwaysWriteStopInbox: false,
  };
}

async function withFixture(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-recall-pointer-"));
  const vault = path.join(root, "vault");
  const home = path.join(root, "home");
  const saved = {
    home: process.env.MINNI_HOME,
    socket: process.env.MINNI_SOCKET_PATH,
    afm: process.env.MINNI_AFM_HEALTH_URL,
    bypass: process.env.MINNI_BYPASS_AUDIT_LIMIT,
    threshold: process.env.MINNI_RECALL_POINTER_THRESHOLD,
  };
  process.env.MINNI_HOME = home;
  process.env.MINNI_SOCKET_PATH = MISSING_SOCKET(home);
  process.env.MINNI_AFM_HEALTH_URL = "http://127.0.0.1:1/health";
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  delete process.env.MINNI_RECALL_POINTER_THRESHOLD;
  await mkdir(home, { recursive: true });
  try {
    await run({ vault, home });
  } finally {
    for (const [key, value] of [
      ["MINNI_HOME", saved.home],
      ["MINNI_SOCKET_PATH", saved.socket],
      ["MINNI_AFM_HEALTH_URL", saved.afm],
      ["MINNI_BYPASS_AUDIT_LIMIT", saved.bypass],
      ["MINNI_RECALL_POINTER_THRESHOLD", saved.threshold],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(root, { recursive: true, force: true });
  }
}

// A direct full-query substring match in the body scores +50 (VAULT_DIRECT_MATCH_SCORE).
async function writeStrongVaultNote(vault, prompt) {
  const wiki = path.join(vault, "wiki", "sessions");
  await mkdir(wiki, { recursive: true });
  await writeFile(
    path.join(wiki, "20260617-strong-hit.md"),
    `# Strong hit note\n\nThe exact phrase: ${prompt}\nThis note documents the decision so the agent need not derive it from scratch.\n`,
    "utf8",
  );
}

function parseEnvelope(output) {
  const ctx = output.hookSpecificOutput?.additionalContext ?? "";
  const body = ctx.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/)?.[1];
  return { ctx, body: body ? JSON.parse(body) : undefined };
}

async function fileExists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

test("(a) strong-hit turn: writes recall-state.json with the canonical shape and injects only the LIGHT POINTER", async () => {
  await withFixture(async ({ vault }) => {
    const prompt = "resume the aetherkernel xhci mmio window debugging from prior context";
    await writeStrongVaultNote(vault, prompt);

    const handlers = createHookHandlers(turnConfig(vault));
    const output = await handlers.handleUserPromptSubmit({ prompt, workspace_id: "workspace-fixture" });

    // State file written under <vault>/.runtime/recall-state.json (portable).
    const statePath = path.join(vault, RECALL_STATE_RELPATH);
    assert.equal(RECALL_STATE_RELPATH, path.join(".runtime", "recall-state.json"));
    assert.ok(await fileExists(statePath), "recall-state.json must be written on a strong turn");
    const state = JSON.parse(await readFile(statePath, "utf8"));

    // Canonical shape.
    assert.equal(typeof state.task_signature, "string");
    assert.equal(state.intent, "recall");
    assert.ok(Array.isArray(state.top_hits) && state.top_hits.length > 0);
    for (const hit of state.top_hits) {
      assert.equal(typeof hit.title, "string");
      assert.equal(typeof hit.wikilink, "string");
      assert.equal(typeof hit.score, "number");
    }
    assert.equal(typeof state.top_score, "number");
    assert.equal(state.consumed, false, "consumed resets false each turn (s6 guard flips it)");
    assert.equal(typeof state.ts, "string");
    assert.ok(state.top_score >= DEFAULT_RECALL_POINTER_THRESHOLD);

    // Injected envelope is the LIGHT POINTER, not the full pack.
    const { ctx, body } = parseEnvelope(output);
    assert.ok(body, "strong turn must inject a minni:context envelope");
    assert.equal(typeof body.recall_pointer, "string");
    assert.match(body.recall_pointer, /Consult minni_recall/);
    assert.match(body.recall_pointer, /relevant memor/);
    // The light pointer must NOT carry the full recall pack fields.
    assert.equal(body.recall, undefined, "no full recall pack in the prompt");
    assert.equal(body.vault, undefined, "no full vault dump in the prompt");
    assert.ok(!ctx.includes("## Daemon Results"), "full daemon-results blob must not be injected");
    assert.ok(!ctx.includes("AI Context Pack"), "full context pack must not be injected");
    // It points at the state file rather than inlining its contents.
    assert.equal(body.recall_state, statePath);
  });
});

test("(b) weak/empty turn: no state file and no injection", async () => {
  await withFixture(async ({ vault }) => {
    // No vault notes at all + missing daemon socket => no strong hits.
    const handlers = createHookHandlers(turnConfig(vault));
    const output = await handlers.handleUserPromptSubmit({
      prompt: "an utterly novel question with zero prior memory zzzqqx",
      workspace_id: "workspace-fixture",
    });

    const statePath = path.join(vault, RECALL_STATE_RELPATH);
    assert.equal(await fileExists(statePath), false, "weak turn must NOT write recall-state.json");

    assert.equal(output.continue, true);
    assert.equal(output.hookSpecificOutput, undefined, "weak turn injects nothing");
  });
});

test("(b2) weak turn clears a stale strong-turn state file", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(turnConfig(vault));
    const statePath = path.join(vault, RECALL_STATE_RELPATH);

    // Strong turn first => state written.
    const strongPrompt = "resume the cobalt indexer migration decision from prior context";
    await writeStrongVaultNote(vault, strongPrompt);
    await handlers.handleUserPromptSubmit({ prompt: strongPrompt, workspace_id: "workspace-fixture" });
    assert.ok(await fileExists(statePath), "precondition: strong turn writes state");

    // Remove the note so the next turn is weak; the stale state must be cleared.
    await rm(path.join(vault, "wiki", "sessions", "20260617-strong-hit.md"), { force: true });
    const out = await handlers.handleUserPromptSubmit({
      prompt: "totally unrelated novel prompt wibblewobble",
      workspace_id: "workspace-fixture",
    });
    assert.equal(out.hookSpecificOutput, undefined);
    assert.equal(await fileExists(statePath), false, "stale strong-turn state must be cleared on a weak turn");
  });
});

test("(c) consumed resets per task_signature (new turn)", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(turnConfig(vault));
    const statePath = path.join(vault, RECALL_STATE_RELPATH);

    const prompt1 = "resume the falcon scheduler latency investigation from prior context";
    await writeStrongVaultNote(vault, prompt1);
    await handlers.handleUserPromptSubmit({ prompt: prompt1, workspace_id: "workspace-fixture" });
    const state1 = JSON.parse(await readFile(statePath, "utf8"));
    assert.equal(state1.consumed, false);

    // Simulate the s6 guard consuming the pointer.
    await writeFile(statePath, JSON.stringify({ ...state1, consumed: true }, null, 2), "utf8");

    // New turn (different prompt => different task_signature) must reset consumed.
    const prompt2 = "resume the falcon scheduler retry backoff investigation from prior context";
    await writeStrongVaultNote(vault, prompt2);
    await handlers.handleUserPromptSubmit({ prompt: prompt2, workspace_id: "workspace-fixture" });
    const state2 = JSON.parse(await readFile(statePath, "utf8"));

    assert.notEqual(state2.task_signature, state1.task_signature, "new prompt => new task_signature");
    assert.equal(state2.consumed, false, "a new turn resets consumed to false");
  });
});

test("explicit WRITE intents (learn/vault_write) are still suppressed: no pointer, no state", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(turnConfig(vault));
    const statePath = path.join(vault, RECALL_STATE_RELPATH);
    // "learn" is a write intent (automaticAllowed:false). Even with a strong
    // vault note present, the write-intent suppression must short-circuit.
    const prompt = "learn this: the cobalt indexer uses a write-ahead log for crash safety";
    await writeStrongVaultNote(vault, prompt);
    const out = await handlers.handleUserPromptSubmit({ prompt, workspace_id: "workspace-fixture" });
    assert.equal(out.hookSpecificOutput, undefined, "write intent injects nothing");
    assert.equal(await fileExists(statePath), false, "write intent writes no recall state");
  });
});

// ── Pure strength-gate unit tests (daemon confidence) ───────────────────────

test("extractStrongRecall gates on calibrated confidence, excludes identity-shelf hits", () => {
  const threshold = DEFAULT_RECALL_POINTER_THRESHOLD; // 0.55
  const response = {
    results: [
      // identity-shelf hit with high confidence: MUST be excluded (boot context).
      { wikilink: "[[BOOT_ENVELOPE]]", layer: "identity", confidence: 0.99, score: 2.6 },
      // strong knowledge hit: clears the gate.
      { wikilink: "[[wiki/knowledge/cobalt-wal]]", layer: "knowledge", confidence: 0.81, score: 41 },
      // weak hit: below threshold, dropped.
      { wikilink: "[[wiki/sessions/old]]", layer: "episodic", confidence: 0.2, score: 33 },
    ],
  };
  const strong = extractStrongRecall(response, [], threshold);
  assert.ok(strong, "a confidence >= threshold non-identity hit must open the gate");
  assert.equal(strong.topHits.length, 1, "only the strong non-identity hit is kept");
  assert.equal(strong.topHits[0].wikilink, "[[wiki/knowledge/cobalt-wal]]");
  assert.equal(strong.topScore, 0.81);
});

test("extractStrongRecall returns null on weak/absent recall and ignores raw out-of-[0,1] score", () => {
  const threshold = DEFAULT_RECALL_POINTER_THRESHOLD;
  // No confidence field; raw score=96 is un-normalized and must NOT be treated
  // as strength (that is exactly the false-strong we are avoiding).
  const rawOnly = { results: [{ wikilink: "[[x]]", layer: "knowledge", score: 96 }] };
  assert.equal(extractStrongRecall(rawOnly, [], threshold), null);
  assert.equal(extractStrongRecall(undefined, [], threshold), null);
  assert.equal(extractStrongRecall({ results: [] }, [], threshold), null);
});

test("recallPointerThreshold honors MINNI_RECALL_POINTER_THRESHOLD override", () => {
  assert.equal(recallPointerThreshold({}), DEFAULT_RECALL_POINTER_THRESHOLD);
  assert.equal(recallPointerThreshold({ MINNI_RECALL_POINTER_THRESHOLD: "0.8" }), 0.8);
  // Invalid / non-positive values fall back to the default.
  assert.equal(recallPointerThreshold({ MINNI_RECALL_POINTER_THRESHOLD: "nope" }), DEFAULT_RECALL_POINTER_THRESHOLD);
  assert.equal(recallPointerThreshold({ MINNI_RECALL_POINTER_THRESHOLD: "0" }), DEFAULT_RECALL_POINTER_THRESHOLD);
});

// ── Rough before/after token-size comparison of the injected envelope ───────

test("light pointer is dramatically smaller than the old full recall pack (token estimate)", () => {
  const estTokens = (s) => Math.ceil(s.length / 4);

  // Representative "before": the old per-turn pack inlined formatRecallLean's
  // daemon-results JSON + vault dump (this is the lean variant; the full
  // formatRecall pack was larger still). ~6 hits x snippet text.
  const oldPackResults = Array.from({ length: 6 }, (_, i) => ({
    wikilink: `[[wiki/sessions/20260608-note-${i}]]`,
    src: "p",
    score: 30 + i,
    headline:
      "v63 xHCI MMIO 0xDEADDEAD is device-side not the window; the controller reports a stale completion and the retry path double-frees the TRB ring on the second pass",
  }));
  const oldPack = JSON.stringify(
    {
      recall:
        "# Recall (lean)\nQuery: resume debugging\n## AI Context Pack\n" +
        oldPackResults.map((r, i) => `${i + 1}. ${r.wikilink} (vault score=${30 + i})\n   ${r.headline}`).join("\n") +
        "\n## Daemon Results\n" +
        JSON.stringify(oldPackResults, null, 2),
      vault: oldPackResults.map((r) => ({ wikilink: r.wikilink, score: r.score, snippet: r.headline })),
    },
    null,
    2,
  );

  const strong = {
    topScore: 0.81,
    topHits: [
      { title: "20260608-note-0", wikilink: "[[wiki/sessions/20260608-note-0]]", score: 0.81 },
      { title: "20260608-note-1", wikilink: "[[wiki/sessions/20260608-note-1]]", score: 0.74 },
      { title: "20260608-note-2", wikilink: "[[wiki/sessions/20260608-note-2]]", score: 0.61 },
    ],
  };
  const pointer = buildRecallPointer(strong);

  const before = estTokens(oldPack);
  const after = estTokens(pointer);
  // Report the comparison for the deliverable.
  console.log(`[s5 token compare] before≈${before} tok, after≈${after} tok (pointer chars=${pointer.length})`);
  assert.ok(after <= 120, `light pointer must be <= ~120 tokens (got ${after})`);
  assert.ok(after * 4 < before, `pointer (${after} tok) must be far smaller than the old pack (${before} tok)`);
});
