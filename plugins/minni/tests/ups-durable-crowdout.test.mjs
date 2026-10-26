import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { extractStrongRecall } from "../dist/recall-state.js";
import { ensureVault, searchVaultNotes } from "../dist/vault.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-s3c-crowdout-"));
  try {
    await ensureVault(root);
    return await fn(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("20 high-term-count noise notes cannot crowd out a durable Route (b) note under ups_gate", async () => {
  await withTempVault(async (vault) => {
    const sessionsDir = path.join(vault, "wiki", "sessions");
    const decisionsDir = path.join(vault, "wiki", "decisions");
    await mkdir(sessionsDir, { recursive: true });
    await mkdir(decisionsDir, { recursive: true });

    const query = "heliosgrine federation quorum handoff";
    // Write 20 noise notes (compaction dumps and [Subagent Context] notes) that
    // repeat the query terms many times so their raw term score is huge (> 80).
    for (let i = 0; i < 20; i += 1) {
      const isSubagent = i % 2 === 0;
      const relName = isSubagent
        ? `20260618-subagent-${i}.md`
        : `20260618-compact-${i}.md`;
      const title = isSubagent
        ? `[Subagent Context] heliosgrine federation quorum handoff ${i}`
        : `Session compaction heliosgrine federation quorum handoff ${i}`;
      const repeated = Array.from({ length: 12 }, () => query).join(" ");
      await writeFile(
        path.join(sessionsDir, relName),
        [
          "---",
          `title: "${title}"`,
          "source: compact_distillation:subagent",
          "privacy: safe",
          "status: accepted",
          "---",
          "",
          `# ${title}`,
          "",
          repeated,
          "",
        ].join("\n"),
        "utf8",
      );
    }

    // Write 1 durable decision note that matches via Route (b) (3 of 4 terms in title).
    await writeFile(
      path.join(decisionsDir, "heliosgrine-quorum-handoff.md"),
      [
        "---",
        "title: Heliosgrine federation quorum rule",
        "type: decision",
        "category: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Heliosgrine federation quorum rule",
        "",
        "Decision: The heliosgrine federation quorum requires two-thirds agreement before handoff.",
        "",
      ].join("\n"),
      "utf8",
    );

    const results = await searchVaultNotes(vault, query, 5, undefined, { rank: "ups_gate" });
    assert.ok(results.length >= 1);
    assert.equal(
      results[0].relativePath,
      path.join("wiki", "decisions", "heliosgrine-quorum-handoff.md"),
      "durable decision note must rank #1 ahead of all 20 noise notes",
    );
    assert.equal(results[0].clearsGate, true);
    assert.equal(results[0].durability, "durable");

    const strong = extractStrongRecall(undefined, results, 0.55, 5);
    assert.ok(strong, "durable note must produce a StrongRecall");
    assert.equal(strong.topHits.length, 1, "only the durable note may enter topHits");
    assert.equal(strong.topHits[0].title, "Heliosgrine federation quorum rule");

    // Also verify end-to-end through handleUserPromptSubmit.
    const prevSocket = process.env.MINNI_SOCKET_PATH;
    process.env.MINNI_SOCKET_PATH = path.join(vault, ".runtime", "missing.sock");
    try {
      const handlers = createHookHandlers({
        agentId: "claude-code",
        vaultPath: vault,
        defaultWorkspaceId: "test-ws",
        contextWindow: 200_000,
        hooksEnabled: true,
        auditPrefix: "hook_test",
      });
      const out = await handlers.handleUserPromptSubmit({
        session_id: "crowdout-sess-1",
        prompt: `Where is the ${query} documented?`,
      });
      const ctx = out.hookSpecificOutput?.additionalContext ?? "";
      assert.match(ctx, /Heliosgrine federation quorum rule/);
      assert.doesNotMatch(ctx, /Subagent Context|Session compaction/);
    } finally {
      if (prevSocket === undefined) delete process.env.MINNI_SOCKET_PATH;
      else process.env.MINNI_SOCKET_PATH = prevSocket;
    }
  });
});
