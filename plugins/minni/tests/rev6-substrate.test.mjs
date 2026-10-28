import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import * as hookHandlersMod from "../dist/hook-handlers.js";
import { executeLearnPipeline } from "../dist/jev-harvest.js";
import { ensureVault } from "../dist/vault.js";

test("Pillar 4: isSubstantiveRecallPrompt rejects conversational filler and short non-technical acknowledgements", () => {
  assert.equal(
    typeof hookHandlersMod.isSubstantiveRecallPrompt,
    "function",
    "isSubstantiveRecallPrompt must be exported from hook-handlers.ts",
  );
  const { isSubstantiveRecallPrompt } = hookHandlersMod;

  for (const filler of [
    "",
    "   ",
    "ok",
    "yes",
    "no",
    "thanks",
    "thank you!",
    "looks good",
    "go ahead",
    "sounds good to me",
    "Ok, what do you suggest?",
    "in plain english",
    "explain that in plain english please",
    "yes go ahead with that",
  ]) {
    assert.equal(
      isSubstantiveRecallPrompt(filler),
      false,
      `Expected filler prompt ${JSON.stringify(filler)} to be non-substantive`,
    );
  }

  for (const substantive of [
    "fix #412",
    "check src/minni/recall.py",
    "update handleUserPromptSubmit",
    "run --strict check",
    "why is minni daemon timing out",
    "inspect `chunk_embeddings` table",
    "what did we decide about the retrieval reranker",
    "an unrelated question entirely",
    "tell me about alpha",
  ]) {
    assert.equal(
      isSubstantiveRecallPrompt(substantive),
      true,
      `Expected technical/substantive prompt ${JSON.stringify(substantive)} to be substantive`,
    );
  }
});

test("Pillar 1 & Invariant 2: executeLearnPipeline gates non-durable session chatter to session_handoffs and never indexes wiki/sessions via vault_index_doc", async () => {
  const minniHome = await mkdtemp(path.join(tmpdir(), "rev6-home-"));
  const prevHome = process.env.MINNI_HOME;
  process.env.MINNI_HOME = minniHome;
  try {
    const vaultDir = path.join(minniHome, "codex-vault");
    await ensureVault(vaultDir);

    // 1. Non-durable session/sprint chatter ("ordinary" under durabilityOf) must route to session_handoffs
    const ordinaryRes = await executeLearnPipeline({
      vaultPath: vaultDir,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-rev6-ordinary",
      payload: {
        title: "Sprint 6 status note",
        content: "Completed slice 2 of the migration and verified the local branch state.",
        category: "general",
        source: "jev_harvest",
        triggerKind: "test_insight",
      },
    });

    assert.equal(
      ordinaryRes.status,
      "routed-to-session-handoff",
      "Non-durable jev_harvest payload must be routed to session_handoffs instead of learned",
    );
    assert.equal(ordinaryRes.ok, true);

    const handoffDir = path.join(minniHome, "session_handoffs");
    const handoffFiles = await readdir(handoffDir);
    assert.equal(handoffFiles.length, 1, "Must write daily handoffs-YYYY-MM-DD.jsonl file");
    const handoffRaw = await readFile(path.join(handoffDir, handoffFiles[0]), "utf8");
    const handoffRow = JSON.parse(handoffRaw.trim().split("\n")[0]);
    assert.equal(handoffRow.title, "Sprint 6 status note");
    assert.equal(handoffRow.session_id, "sess-rev6-ordinary");

    const sessionNotesAfterOrdinary = await readdir(path.join(vaultDir, "wiki", "sessions")).catch(
      () => [],
    );
    assert.equal(
      sessionNotesAfterOrdinary.length,
      0,
      "Routed handoff must not create wiki/sessions/*.md note",
    );

    // 2. Verify jev-harvest.ts source no longer calls vault_index_doc on wiki/sessions notes
    const jevHarvestSrc = await readFile(
      new URL("../src/jev-harvest.ts", import.meta.url),
      "utf8",
    );
    assert.ok(
      !jevHarvestSrc.includes('"vault_index_doc"'),
      "jev-harvest.ts must not call vault_index_doc on session notes",
    );

    // 3. Verify server.ts skips vault_index_doc for sessions/raw in minni_vault_write and minni_learn
    const serverSrc = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
    const matches = serverSrc.match(/"vault_index_doc"/g) ?? [];
    assert.equal(
      matches.length,
      1,
      "server.ts must only call vault_index_doc in minni_vault_write (for curated sections), not in minni_learn",
    );
  } finally {
    if (prevHome === undefined) {
      delete process.env.MINNI_HOME;
    } else {
      process.env.MINNI_HOME = prevHome;
    }
  }
});
