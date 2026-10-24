// Recall-to-use S1b (§4.4, §4.6): `explicit_recall` rows are rebuilt from the
// transcript's minni_recall / minni_drill results.
//
// Result text carries wikilinks and sources, not ids, so each card is keyed
// through memoryKeyFor on its source. A card that cannot be keyed is counted
// `unnameable`, never silently dropped. The sid is sha256(tool_use_id, mkey),
// so re-reading the transcript cannot duplicate a row.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { rm } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { vaultHandle } from "../dist/use-ledger.js";
import { runDetector } from "../dist/use-detector.js";
import { buildUseReport } from "../dist/use-report.js";
import {
  Clock,
  detectorArgs,
  ledgerRows,
  makeVault,
  T0,
  Transcript,
  writeNote,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const sha16 = (parts) => createHash("sha256").update(JSON.stringify(parts), "utf8").digest("hex").slice(0, 16);

function recallText(cards) {
  return ["# Minni Recall", "", "Query: alpha rollout [faiss-disk]", "", "## AI Context Pack", "", ...cards].join("\n");
}

test("a minni_recall result with one vault card and one unkeyable card: one explicit_recall row, one unnameable", async (t) => {
  const { root, vault, transcriptPath } = await makeVault(t);
  await writeNote(vault, "wiki/sessions/alpha-note.md", "# Alpha note\n");
  const elsewhere = path.join(root, "other-vault", "wiki", "sessions", "ghost-note.md");
  const clock = new Clock();
  const tx = new Transcript(transcriptPath, clock);

  tx.prompt("What do we know about the alpha rollout?", "p1");
  const toolUseId = tx.tool("mcp__minni__minni_recall", { query: "alpha rollout" }, "toolu_recall_1");
  tx.result(
    toolUseId,
    recallText([
      "1. [[wiki/sessions/alpha-note]] — Alpha note (score=0.91)",
      '   <EVIDENCE source="wiki/sessions/alpha-note.md" agent="claude-code" status="accepted" privacy="safe" score="0.910">alpha rollout snippet</EVIDENCE>',
      "2. [[wiki/sessions/ghost-note]] — Ghost note (score=0.4)",
      `   <EVIDENCE source="${elsewhere}" agent="claude-code" status="candidate" privacy="safe" score="0.400">ghost snippet</EVIDENCE>`,
    ]),
  );
  tx.say("The alpha rollout is staged behind a flag.");
  await tx.flush();

  const result = runDetector(detectorArgs(vault, transcriptPath, clock, { lastAssistantMessage: "The alpha rollout is staged behind a flag." }));
  assert.equal(result.status, "ran");
  assert.equal(result.unnameable, 1);

  const explicit = ledgerRows(vault, "surfaced").filter((row) => row.channel === "explicit_recall");
  assert.equal(explicit.length, 1, "one row for the keyable card");
  const [row] = explicit;
  const mkey = "vault:wiki/sessions/alpha-note.md";
  assert.equal(row.mkey, mkey);
  assert.equal(row.sid, sha16([toolUseId, mkey]), "sid = sha256(tool_use_id, mkey)");
  assert.deepEqual(row.handles, [vaultHandle("wiki/sessions/alpha-note.md")]);
  assert.equal(row.visible, true);
  assert.equal(row.delivered, true);

  const unnameable = ledgerRows(vault, "judged").filter((j) => j.outcome === "unknown(unnameable)");
  assert.equal(unnameable.length, 1, "the unkeyable card is counted, not dropped");

  // Re-reading the same transcript from a fresh cursor (detector state lost)
  // duplicates nothing: the ledger's sids are the idempotency check.
  await rm(path.join(vault, ".runtime", "use-detector"), { recursive: true, force: true });
  runDetector(detectorArgs(vault, transcriptPath, clock, { lastAssistantMessage: "The alpha rollout is staged behind a flag." }));
  assert.equal(ledgerRows(vault, "surfaced").filter((r) => r.channel === "explicit_recall").length, 1);
  assert.equal(ledgerRows(vault, "judged").filter((j) => j.outcome === "unknown(unnameable)").length, 1);

  const report = buildUseReport(vault, { since: T0 - 1000, until: clock.now + 1000 });
  assert.equal(report.explicit_recall.cards, 2);
  assert.equal(report.explicit_recall.unnameable, 1);
  assert.equal(report.explicit_recall.unnameable_share, 0.5);
});

test("a card with no source and a wikilink that resolves nowhere is unnameable; a drill result keys on source", async (t) => {
  const { vault, transcriptPath } = await makeVault(t);
  const betaAbs = await writeNote(vault, "wiki/sessions/beta-note.md", "# Beta note\n");
  const clock = new Clock();
  const tx = new Transcript(transcriptPath, clock);

  tx.prompt("Recall the beta notes.", "p1");
  const recallId = tx.tool("mcp__plugin_minni_minni__minni_recall", { query: "beta" }, "toolu_recall_2");
  tx.result(recallId, [{ type: "text", text: recallText(["1. [[wiki/sessions/nowhere-note]] — Nowhere (vault score=3)"]) }]);
  const drillId = tx.tool("mcp__minni__minni_drill", { references: ["[[wiki/sessions/beta-note]]"] }, "toolu_drill_1");
  tx.result(drillId, JSON.stringify({ results: [{ doc_id: 7, src: "c", source: betaAbs, heading: "Beta note", text: "beta body" }] }));
  tx.say("Beta is covered.");
  await tx.flush();

  const result = runDetector(detectorArgs(vault, transcriptPath, clock, { lastAssistantMessage: "Beta is covered." }));
  assert.equal(result.unnameable, 1);
  const explicit = ledgerRows(vault, "surfaced").filter((row) => row.channel === "explicit_recall");
  assert.deepEqual(explicit.map((row) => row.mkey), ["vault:wiki/sessions/beta-note.md"]);
  assert.equal(explicit[0].sid, sha16([drillId, "vault:wiki/sessions/beta-note.md"]));
});
