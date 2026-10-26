import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { buildRecallPointer, extractStrongRecall } from "../dist/recall-state.js";
import { searchVaultNotes } from "../dist/vault.js";

test("ups_gate prevents 22 plan journals echoing a 14-term prompt from crowding out 1 durable decision note with 3 distinctive title terms", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s3c-crowdout-"));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "artifacts"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "concepts"), { recursive: true });

  try {
    // 14-term prompt where 3 terms ("socket", "retry", "backoff") are distinctive in the decision note's title
    // and the other 11 terms appear across background notes or plan journals.
    const fourteenTermPrompt =
      "investigate socket retry backoff behavior during transient daemon connection failures across local worker processes";

    // Add 20 ordinary notes that do NOT contain "socket", "retry", or "backoff" so nonNoiseCount = 21
    // and dfThreshold = ceil(0.05 * 21) = 2, making "socket", "retry", "backoff" (df = 1) distinctive!
    for (let i = 1; i <= 20; i += 1) {
      await writeFile(
        path.join(vault, "wiki", "concepts", `background-note-${String(i).padStart(2, "0")}.md`),
        [
          "---",
          `title: Background Concept ${i}`,
          "type: concept",
          "status: active",
          "---",
          `# Background Concept ${i}`,
          "",
          "General architectural notes on module boundaries and state persistence.",
        ].join("\n"),
        "utf8",
      );
    }

    // 22 noise plan journals (`wiki/artifacts/plan-*.log.md`) that echo every single word of the 14-term prompt
    // multiple times in their title and body (so their raw lexical score is huge).
    for (let i = 1; i <= 22; i += 1) {
      const slug = `plan-20261025-slice-${String(i).padStart(2, "0")}.log.md`;
      await writeFile(
        path.join(vault, "wiki", "artifacts", slug),
        [
          "---",
          `title: Plan Journal ${i} — ${fourteenTermPrompt}`,
          "type: plan",
          "status: active",
          "---",
          `# Plan Journal ${i}`,
          "",
          fourteenTermPrompt,
          fourteenTermPrompt,
          fourteenTermPrompt,
        ].join("\n"),
        "utf8",
      );
    }

    // 1 durable decision note whose title has only the 3 distinctive terms ("Socket Retry Backoff")
    // and does NOT contain the full 14-term phrase.
    const decisionRelPath = path.join("wiki", "decisions", "socket-retry-backoff.md");
    await writeFile(
      path.join(vault, decisionRelPath),
      [
        "---",
        "title: Socket Retry Backoff Policy",
        "type: decision",
        "category: decision",
        "status: active",
        "---",
        "# Socket Retry Backoff Policy",
        "",
        "Decision: cap exponential retry backoff at 500ms on local Unix domain socket connections.",
      ].join("\n"),
      "utf8",
    );

    // Under legacy ranking with limit = 6, the 22 plan journals completely crowd out the decision note.
    const legacyResults = await searchVaultNotes(vault, fourteenTermPrompt, 6, undefined, {
      rank: "legacy",
      candidates: "scan",
    });
    assert.equal(
      legacyResults.some((r) => r.relativePath.includes("socket-retry-backoff")),
      false,
      "precondition: under legacy ranking, the 22 echoing plan journals crowd out the decision note from top 6",
    );

    // Under ups_gate ranking, the decision note clears Route B (3 distinctive terms in title) and ranks #1,
    // while all 22 plan journals are classified as noise (clearsGate: false, weightedScore <= 0.25).
    const gatedResults = await searchVaultNotes(vault, fourteenTermPrompt, 18, undefined, {
      rank: "ups_gate",
      candidates: "scan",
    });

    assert.ok(gatedResults.length > 0);
    assert.equal(gatedResults[0].relativePath, decisionRelPath);
    assert.equal(gatedResults[0].durability, "durable");
    assert.equal(gatedResults[0].gateRoute, "b");
    assert.equal(gatedResults[0].clearsGate, true);
    assert.ok(gatedResults[0].weightedScore >= 0.55);

    for (const r of gatedResults.slice(1)) {
      if (r.relativePath.startsWith("wiki/artifacts/plan-")) {
        assert.equal(r.durability, "noise");
        assert.equal(r.clearsGate, false);
        assert.ok(r.weightedScore <= 0.25);
      }
    }

    // Strong recall extraction + pointer names ONLY the durable decision note.
    const strong = extractStrongRecall(undefined, gatedResults.slice(0, 6));
    assert.ok(strong, "durable decision note must open strong recall gate");
    assert.equal(strong.topHits.length, 1);
    assert.equal(strong.topHits[0].title, "Socket Retry Backoff Policy");

    const pointer = buildRecallPointer(strong);
    assert.match(pointer, /Socket Retry Backoff Policy/);
    assert.doesNotMatch(pointer, /Plan Journal/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
