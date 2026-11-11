// Wave 2 regressions: recallMemory workspace_id serialization + zero blank <EVIDENCE> envelopes
// across drillMemory local fallbacks, formatRecall cards, and prepareTask context packs.
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  drillMemory,
  formatRecall,
  recallMemory,
} from "../dist/daemon_transport.js";
import { vaultHandleForPath } from "../dist/recall-state.js";
import { prepareTask } from "../dist/task.js";

const BLANK_EVIDENCE_RE = /<EVIDENCE\b[^>]*>\s*<\/EVIDENCE>/i;

test("recallMemory serializes non-empty workspaceId onto search RPC params and omits blank values", async () => {
  const seen = [];
  const requester = async (_sock, method, params) => {
    seen.push({ method, params: structuredClone(params) });
    return {
      ok: true,
      data: {
        results: [],
        count: 0,
        workspace_id: params.workspace_id,
      },
    };
  };

  await recallMemory({ query: "alpha check", agentId: "gemini" }, requester);
  await recallMemory({ query: "alpha check", agentId: "gemini", workspaceId: "   " }, requester);
  const withWs = await recallMemory(
    { query: "alpha check", agentId: "gemini", workspaceId: "workspace-alpha" },
    requester,
  );

  assert.equal(seen.length, 3);
  assert.equal(Object.hasOwn(seen[0].params, "workspace_id"), false);
  assert.equal(Object.hasOwn(seen[1].params, "workspace_id"), false);
  assert.equal(seen[2].params.workspace_id, "workspace-alpha");
  assert.equal(withWs.ok, true);
  assert.equal(withWs.data?.workspace_id, "workspace-alpha");
});

test("drillMemory vault fallback strips frontmatter and never emits blank <EVIDENCE> blocks", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-w2-drill-"));
  try {
    const vaultDir = path.join(root, "gemini-vault");
    const decisionsDir = path.join(vaultDir, "wiki", "decisions");
    await mkdir(decisionsDir, { recursive: true });

    await writeFile(
      path.join(decisionsDir, "empty-frontmatter.md"),
      "---\ntitle: Empty Frontmatter Only\nworkspace_id: workspace-alpha\n---\n\n   \n",
      "utf8",
    );
    await writeFile(
      path.join(decisionsDir, "real-note.md"),
      "---\ntitle: Real Note\nworkspace_id: workspace-alpha\n---\n\nConcrete architecture rule for alpha.\n",
      "utf8",
    );

    const offlineRequester = async () => ({
      ok: false,
      error: "socket unavailable",
    });

    const emptyHandle = vaultHandleForPath("wiki/decisions/empty-frontmatter.md");
    const emptyHandleRes = await drillMemory(
      {
        vaultPath: vaultDir,
        handle: emptyHandle,
        depth: "full",
      },
      offlineRequester,
    );
    assert.equal(emptyHandleRes.ok, true);
    const emptyItems = Array.isArray(emptyHandleRes.data?.results) ? emptyHandleRes.data.results : [];
    assert.equal(emptyItems.length, 1);
    assert.equal(emptyItems[0].content, "");
    assert.equal(BLANK_EVIDENCE_RE.test(JSON.stringify(emptyHandleRes.data)), false);

    const mixedWikilinkRes = await drillMemory(
      {
        vaultPath: vaultDir,
        references: [
          "[[wiki/decisions/empty-frontmatter]]",
          "[[wiki/decisions/real-note]]",
        ],
        depth: "full",
      },
      offlineRequester,
    );
    assert.equal(mixedWikilinkRes.ok, true);
    const realItems = Array.isArray(mixedWikilinkRes.data?.results) ? mixedWikilinkRes.data.results : [];
    assert.equal(realItems.length, 1);
    assert.equal(realItems[0].relativePath, "wiki/decisions/real-note.md");
    assert.match(String(realItems[0].content), /<EVIDENCE\b[^>]*>/);
    assert.match(String(realItems[0].content), /Concrete architecture rule for alpha\./);
    assert.doesNotMatch(String(realItems[0].content), /workspace_id:\s*workspace-alpha/);
    assert.equal(BLANK_EVIDENCE_RE.test(JSON.stringify(mixedWikilinkRes.data)), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("formatRecall strips empty <EVIDENCE> envelopes in daemon cards while preserving real content", () => {
  const rendered = formatRecall("alpha architecture", {
    count: 2,
    agent_id: "gemini",
    workspace_id: "workspace-alpha",
    results: [
      {
        wikilink: "[[wiki/decisions/blank-hit]]",
        title: "Blank Hit",
        score: 0.42,
        text: '<EVIDENCE source="wiki/decisions/blank-hit.md" status="accepted">   \n  </EVIDENCE>',
      },
      {
        wikilink: "[[wiki/decisions/real-hit]]",
        title: "Real Hit",
        score: 0.91,
        text: '<EVIDENCE source="wiki/decisions/real-hit.md" status="accepted">Keep migrations idempotent.</EVIDENCE>',
      },
    ],
  });

  assert.equal(BLANK_EVIDENCE_RE.test(rendered), false);
  assert.match(rendered, /workspace=workspace-alpha/);
  assert.match(rendered, /Keep migrations idempotent\./);
});

test("prepareTask drops frontmatter-only vault snippets so zero blank <EVIDENCE> envelopes enter contextMarkdown", async () => {
  const packet = await prepareTask(
    {
      task: "verify workspace migration rules",
      agentId: "gemini",
      workspaceId: "workspace-alpha",
      useAfm: false,
    },
    {
      audit: async () => undefined,
      recall: async () => ({
        ok: true,
        data: { results: [], count: 0, workspace_id: "workspace-alpha" },
      }),
      searchVault: async () => [
        {
          title: "Empty Frontmatter Vault Note",
          wikilink: "[[wiki/decisions/empty-frontmatter]]",
          relativePath: "wiki/decisions/empty-frontmatter.md",
          snippet: "---\ntitle: Empty Frontmatter Vault Note\nworkspace_id: workspace-alpha\n---\n   \n",
          score: 12,
        },
        {
          title: "Valid Vault Note",
          wikilink: "[[wiki/decisions/valid-note]]",
          relativePath: "wiki/decisions/valid-note.md",
          snippet: "---\ntitle: Valid Vault Note\nworkspace_id: workspace-alpha\n---\nUse workspace-scoped storage on learnings.",
          score: 10,
        },
      ],
    },
  );

  assert.equal(BLANK_EVIDENCE_RE.test(packet.contextMarkdown), false);
  assert.equal(packet.relevantSources.length, 1);
  assert.equal(packet.relevantSources[0].relativePath, "wiki/decisions/valid-note.md");
  assert.match(packet.contextMarkdown, /Use workspace-scoped storage on learnings\./);
});
