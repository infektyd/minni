// Recall-to-use S1a-2 (§4.3): a strong UserPromptSubmit turn records a
// `prompt_pointer` row for the hit the pointer actually names (today, the top
// hit only). The other `top_hits` live only in the recall-state file, which
// the model never sees, so they are not surfaced and get no row.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  additionalContext,
  daemonResult,
  makeFixture,
  readLedger,
  runHook,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

test("a strong turn: one prompt_pointer row for the named hit, none for the unnamed top_hits", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "pointer");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [
        daemonResult({ source: "/elsewhere/second.md", docId: 2, wikilink: "[[second]]", title: "Second best", confidence: 0.81 }),
        daemonResult({ source: "/elsewhere/top.md", docId: 1, wikilink: "[[top]]", title: "Top hit", confidence: 0.96 }),
        daemonResult({ source: "/elsewhere/third.md", docId: 3, wikilink: "[[third]]", title: "Third", confidence: 0.77 }),
      ],
    }),
  });
  const { output } = await runHook(fixture, "UserPromptSubmit", {
    session_id: "pointer-session",
    prompt_id: "strong-1",
    prompt: "where is the release checklist kept",
  });
  assert.match(additionalContext(output), /top: \\"Top hit\\"/, "the pointer names the top hit");

  const state = JSON.parse(await readFile(path.join(fixture.vault, ".runtime", "recall-state.json"), "utf8"));
  assert.equal(state.top_hits.length, 3, "all three are top_hits in the state file");

  const rows = await readLedger(fixture.vault);
  const surfaced = rows.filter((row) => row.kind === "surfaced");
  assert.equal(surfaced.length, 1, `only the named hit is surfaced: ${JSON.stringify(surfaced.map((r) => r.mkey))}`);
  const [row] = surfaced;
  assert.equal(row.channel, "prompt_pointer");
  assert.equal(row.mkey, "doc:personal:1");
  assert.deepEqual(row.handles, ["d:personal:1"]);
  assert.equal(row.title, "Top hit");
  assert.equal(row.rank, 1);
  assert.equal(row.visible, true);
  assert.equal(row.delivered, true);
  assert.equal(row.prompt_id, "strong-1");
  const turn = rows.find((r) => r.kind === "turn");
  assert.equal(row.turn_ix, turn.turn_ix, "the pointer row carries its turn");
  assert.equal(turn.exit, "pointer");
});

test("a weak turn names nothing and records no surfaced row", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "pointer-weak");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [daemonResult({ source: "/elsewhere/low.md", docId: 5, wikilink: "[[low]]", title: "Low", confidence: 0.2 })],
    }),
  });
  await runHook(fixture, "UserPromptSubmit", { session_id: "pointer-weak", prompt: "something vague and unrelated" });
  const rows = await readLedger(fixture.vault);
  assert.equal(rows.filter((row) => row.kind === "surfaced").length, 0);
  assert.equal(rows.filter((row) => row.kind === "turn").length, 1);
});

test("Class G: prompt_pointer row records body shingles so applying a pointer-surfaced memory is detected", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "pointer-shingles");
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "claude-code",
      results: [
        daemonResult({
          source: "/elsewhere/claude-code__21e233a9a944a28a.md",
          docId: 42,
          wikilink: "[[claude-code__21e233a9a944a28a]]",
          title: "claude-code__21e233a9a944a28a",
          text: "Always drain the quartz queue before rotating the lattice keys at midnight.",
          confidence: 0.96,
        }),
      ],
    }),
  });
  await runHook(fixture, "UserPromptSubmit", {
    session_id: "pointer-shingles-session",
    prompt_id: "strong-shingles-1",
    prompt: "how do we rotate the lattice keys",
  });
  const rows = await readLedger(fixture.vault);
  const surfaced = rows.filter((row) => row.kind === "surfaced" && row.channel === "prompt_pointer");
  assert.equal(surfaced.length, 1);
  assert.ok(
    Array.isArray(surfaced[0].shingles) && surfaced[0].shingles.length > 0,
    "Class G: prompt_pointer surfaced row must carry body shingles for applied detection",
  );
});

