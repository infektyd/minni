import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { drillMemory } from "../dist/daemon_transport.js";
import {
  docHandleFor,
  recordSessionHandles,
  vaultHandleForPath,
} from "../dist/recall-state.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s3c-drill-"));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });
  try {
    await fn(vault);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("drillMemory resolves m:<8hex> locally when daemon is down, wraps safe notes in <EVIDENCE>, and returns unresolved for unsafe or missing notes", async () => {
  await withTempVault(async (vault) => {
    const safeRel = "wiki/decisions/safe-note.md";
    const privateRel = "wiki/decisions/private-note.md";

    await writeFile(
      path.join(vault, safeRel),
      [
        "---",
        "title: Safe Note",
        "type: decision",
        "privacy: safe",
        "status: active",
        "---",
        "# Safe Note",
        "",
        "Safe decision body content.",
      ].join("\n"),
      "utf8",
    );

    await writeFile(
      path.join(vault, privateRel),
      [
        "---",
        "title: Private Secret Note",
        "type: decision",
        "privacy: private",
        "status: active",
        "---",
        "# Private Secret Note",
        "",
        "Never expose this private note.",
      ].join("\n"),
      "utf8",
    );

    const safeHandle = vaultHandleForPath(safeRel);
    const privateHandle = vaultHandleForPath(privateRel);
    const missingHandle = "m:deadbeef";

    // Daemon requester that always fails (simulating daemon down)
    const deadDaemon = async () => ({
      ok: false,
      error: "connect ENOENT /tmp/missing-minnid.sock",
    });

    // 1. Safe note resolves locally with <EVIDENCE> wrapper
    const safeRes = await drillMemory(
      { references: [safeHandle], vaultPath: vault, sessionId: "sess-drill-1" },
      deadDaemon,
    );
    assert.equal(safeRes.ok, true);
    assert.equal(safeRes.data.status, "resolved");
    assert.equal(safeRes.data.handle, safeHandle);
    assert.equal(safeRes.data.source, safeRel);
    assert.match(safeRes.data.content, /^<EVIDENCE source="wiki\/decisions\/safe-note\.md">/);
    assert.match(safeRes.data.content, /Safe decision body content\./);
    assert.match(safeRes.data.content, /<\/EVIDENCE>$/);

    // 2. Unsafe (privacy: private) note returns status: "unresolved" and never leaks body
    const unsafeRes = await drillMemory(
      { references: [privateHandle], vaultPath: vault, sessionId: "sess-drill-1" },
      deadDaemon,
    );
    assert.equal(unsafeRes.ok, false);
    assert.equal(unsafeRes.data.status, "unresolved");
    assert.equal(unsafeRes.data.reason, "unsafe");
    assert.equal(unsafeRes.data.content, undefined);

    // 3. Missing m:<8hex> handle returns status: "unresolved"
    const missingRes = await drillMemory(
      { references: [missingHandle], vaultPath: vault, sessionId: "sess-drill-1" },
      deadDaemon,
    );
    assert.equal(missingRes.ok, false);
    assert.equal(missingRes.data.status, "unresolved");
    assert.equal(missingRes.data.reason, "missing");
  });
});

test("drillMemory forwards registered d:<src>:<id> as {doc_id, src, source} and unregistered d:<src>:<id> as {doc_id, src} with source_unverified: true", async () => {
  await withTempVault(async (vault) => {
    const sessionId = "sess-drill-d";
    const registeredHandle = docHandleFor("episodic", 4412);
    const unregisteredHandle = docHandleFor("episodic", 9999);

    await recordSessionHandles(vault, sessionId, [
      {
        title: "Registered Episodic Hit",
        wikilink: "[[episodic/4412]]",
        scope: "episodic",
        docId: 4412,
        source: "session:2026-10-20",
        score: 0.88,
      },
    ]);

    const observedRpcCalls = [];
    const fakeDaemon = async (_sock, method, params) => {
      observedRpcCalls.push({ method, params });
      return {
        ok: true,
        data: {
          results: (params.references ?? []).map((ref) => ({
            doc_id: ref.doc_id,
            src: ref.src,
            text: `Drilled content for ${ref.doc_id}`,
          })),
        },
      };
    };

    // 1. Registered d: handle -> reaches daemon as { doc_id: 4412, src: "episodic", source: "session:2026-10-20" }
    const regRes = await drillMemory(
      { references: [registeredHandle], vaultPath: vault, sessionId },
      fakeDaemon,
    );
    assert.equal(regRes.ok, true);
    assert.equal(observedRpcCalls.length, 1);
    assert.equal(observedRpcCalls[0].method, "sm_drill");
    assert.deepEqual(observedRpcCalls[0].params.references, [
      { doc_id: 4412, src: "episodic", source: "session:2026-10-20" },
    ]);
    assert.equal(regRes.data.source_unverified, false);
    assert.equal(regRes.data.results[0].source_unverified, false);

    // 2. Unregistered d: handle -> reaches daemon as { doc_id: 9999, src: "episodic" } and returns source_unverified: true
    const unregRes = await drillMemory(
      { references: [unregisteredHandle], vaultPath: vault, sessionId },
      fakeDaemon,
    );
    assert.equal(unregRes.ok, true);
    assert.equal(observedRpcCalls.length, 2);
    assert.deepEqual(observedRpcCalls[1].params.references, [
      { doc_id: 9999, src: "episodic" },
    ]);
    assert.equal(unregRes.data.source_unverified, true);
    assert.equal(unregRes.data.results[0].source_unverified, true);

    // 3. Registered d: handle drilled from a synthetic MCP process session id -> still resolves via fallback scan
    const mcpRes = await drillMemory(
      { references: [registeredHandle], vaultPath: vault, sessionId: "mcp-m9xyz-12345678" },
      fakeDaemon,
    );
    assert.equal(mcpRes.ok, true);
    assert.equal(observedRpcCalls.length, 3);
    assert.deepEqual(observedRpcCalls[2].params.references, [
      { doc_id: 4412, src: "episodic", source: "session:2026-10-20" },
    ]);
    assert.equal(mcpRes.data.source_unverified, false);
  });
});

test("drillMemory supports multi-element references array mixing m:<8hex> and d:<src>:<id>", async () => {
  await withTempVault(async (vault) => {
    const safeRel = "wiki/decisions/multi-ref.md";
    await writeFile(
      path.join(vault, safeRel),
      "# Multi Ref Decision\n\nLocal vault body for multi-ref test.\n",
      "utf8",
    );
    const mHandle = vaultHandleForPath(safeRel);
    const dHandle = "d:personal:77";

    const observedDaemonRefs = [];
    const fakeDaemon = async (_sock, _method, params) => {
      observedDaemonRefs.push(...(params.references ?? []));
      return {
        ok: true,
        data: {
          results: [{ doc_id: 77, src: "personal", text: "Daemon body 77" }],
        },
      };
    };

    const res = await drillMemory(
      { references: [mHandle, dHandle], vaultPath: vault, sessionId: "sess-multi" },
      fakeDaemon,
    );
    assert.equal(res.ok, true);
    assert.deepEqual(observedDaemonRefs, [{ doc_id: 77, src: "personal" }]);
    assert.equal(res.data.results.length, 2);
    assert.equal(res.data.results[0].handle, mHandle);
    assert.match(res.data.results[0].content, /<EVIDENCE source="wiki\/decisions\/multi-ref\.md">/);
    assert.equal(res.data.results[1].handle, dHandle);
    assert.equal(res.data.results[1].source_unverified, true);
  });
});

test("B2: drillMemory resolves m:<8hex> via .handles.json, raw/ notes, wikilink/sha256/title hashes, failure-lesson handles m:7a11e501..m:7a11e513, sibling vaults, and SQLite minni.db", async () => {
  await withTempVault(async (vault) => {
    const root = path.dirname(vault);
    const deadDaemon = async () => ({ ok: false, error: "daemon unreachable" });

    // 1. Raw note under raw/sessions/raw-session.md
    const rawRel = "raw/sessions/raw-session.md";
    await mkdir(path.dirname(path.join(vault, rawRel)), { recursive: true });
    await writeFile(
      path.join(vault, rawRel),
      "---\ntitle: Captured Turn Note\nprivacy: safe\n---\n# Captured Turn Note\n\nBody of captured turn note.\n",
      "utf8",
    );
    const rawHandle = vaultHandleForPath(rawRel);
    const rawRes = await drillMemory(
      { references: [rawHandle], vaultPath: vault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(rawRes.ok, true, "raw/**/*.md note must resolve via m:<8hex>");
    assert.equal(rawRes.data.source, rawRel);
    assert.match(rawRes.data.content, /Body of captured turn note\./);

    // 2. Wikilink without wiki/ prefix or .md suffix (`[[decisions/wikilink-note]]`)
    const wikiRel = "wiki/decisions/wikilink-note.md";
    await writeFile(
      path.join(vault, wikiRel),
      "---\ntitle: Wikilink Derived Title\nprivacy: safe\n---\n# Wikilink Derived Title\n\nWikilink resolved body.\n",
      "utf8",
    );
    const wikilinkHandle = vaultHandleForPath("[[decisions/wikilink-note]]");
    const wikiRes = await drillMemory(
      { references: [wikilinkHandle], vaultPath: vault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(wikiRes.ok, true, "wikilink-derived m:<8hex> without wiki/ prefix must resolve");
    assert.equal(wikiRes.data.source, wikiRel);

    // 3. Explicit <vaultPath>/.handles.json entry
    const handlesIndex = {
      "m:abcd1234": {
        source: wikiRel,
        title: "Indexed Handle Note",
      },
    };
    await writeFile(path.join(vault, ".handles.json"), JSON.stringify(handlesIndex, null, 2), "utf8");
    const idxRes = await drillMemory(
      { references: ["m:abcd1234"], vaultPath: vault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(idxRes.ok, true, "<vaultPath>/.handles.json entry must resolve");
    assert.equal(idxRes.data.source, wikiRel);

    // 4. All 13 approved failure-lesson handles (m:7a11e501 .. m:7a11e513) resolve
    const lessonHandles = [
      "m:7a11e501",
      "m:7a11e502",
      "m:7a11e503",
      "m:7a11e504",
      "m:7a11e505",
      "m:7a11e506",
      "m:7a11e507",
      "m:7a11e508",
      "m:7a11e509",
      "m:7a11e510",
      "m:7a11e511",
      "m:7a11e512",
      "m:7a11e513",
    ];
    for (const h of lessonHandles) {
      const lessonRes = await drillMemory(
        { references: [h], vaultPath: vault, sessionId: "sess-b2" },
        deadDaemon,
      );
      assert.equal(lessonRes.ok, true, `failure lesson handle ${h} must resolve with ok: true`);
      assert.match(lessonRes.data.content, /<EVIDENCE\b[\s\S]*<\/EVIDENCE>/);
    }

    // 5. Sibling vault resolution (`claudecode-vault` next to `vault`)
    const siblingVault = path.join(root, "claudecode-vault");
    const siblingRel = "wiki/decisions/sibling-only.md";
    await mkdir(path.dirname(path.join(siblingVault, siblingRel)), { recursive: true });
    await writeFile(
      path.join(siblingVault, siblingRel),
      "---\ntitle: Sibling Vault Note\nprivacy: safe\n---\n# Sibling Vault Note\n\nResolved from sibling claudecode-vault.\n",
      "utf8",
    );
    const siblingHandle = vaultHandleForPath(siblingRel);
    const sibRes = await drillMemory(
      { references: [siblingHandle], vaultPath: vault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(sibRes.ok, true, "handle in sibling claudecode-vault must resolve when drilling from default vault");
    assert.match(sibRes.data.content, /Resolved from sibling claudecode-vault\./);

    // 6. SQLite minni.db fallback in temp root (including sha256("learning:" + id) and dynamic *-vault sibling)
    const customSiblingVault = path.join(root, "grok-vault");
    const customSiblingRel = "wiki/decisions/grok-sibling.md";
    await mkdir(path.dirname(path.join(customSiblingVault, customSiblingRel)), { recursive: true });
    await writeFile(
      path.join(customSiblingVault, customSiblingRel),
      "---\ntitle: Grok Sibling Vault Note\nprivacy: safe\n---\n# Grok Sibling Vault Note\n\nResolved from dynamic grok-vault sibling.\n",
      "utf8",
    );
    const customSiblingHandle = vaultHandleForPath(customSiblingRel);
    const customSibRes = await drillMemory(
      { references: [customSiblingHandle], vaultPath: vault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(customSibRes.ok, true, "handle in dynamically named *-vault sibling (grok-vault) must resolve");
    assert.match(customSibRes.data.content, /Resolved from dynamic grok-vault sibling\./);

    const { DatabaseSync } = await import("node:sqlite");
    const dbPath = path.join(root, "minni.db");
    const db = new DatabaseSync(dbPath);
    db.exec("CREATE TABLE learnings (id INTEGER PRIMARY KEY, category TEXT, content TEXT, source TEXT)");
    const sqlSource = "learnings/sqlite-only-entry";
    db.prepare("INSERT INTO learnings (id, category, content, source) VALUES (?, ?, ?, ?)").run(
      4242,
      "decision",
      "SQLite fallback resolved learning content by learning:id.",
      sqlSource,
    );
    db.close();
    const learningIdSha256Handle = `m:${createHash("sha256").update("learning:4242", "utf8").digest("hex").slice(0, 8)}`;
    const sqlRes = await drillMemory(
      { references: [learningIdSha256Handle], vaultPath: vault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(sqlRes.ok, true, "sha256('learning:' + id) handle in minni.db learnings table must resolve");
    assert.match(sqlRes.data.content, /SQLite fallback resolved learning content by learning:id\./);
    assert.deepEqual(sqlRes.data.missing, []);

    // Also test unconfigured unknown-vault path under root resolving to sibling claudecode-vault
    const unconfiguredVault = path.join(root, "unknown-vault");
    const unconfRes = await drillMemory(
      { references: [siblingHandle], vaultPath: unconfiguredVault, sessionId: "sess-b2" },
      deadDaemon,
    );
    assert.equal(unconfRes.ok, true, "passing unconfigured unknown-vault path must resolve via sibling claudecode-vault");
  });
});

test("Class C: drillMemory forwards input.wikilink and input.handle into sm_drill references and falls back to local vault when daemon is down", async () => {
  await withTempVault(async (vault) => {
    const observed = [];
    const fakeDaemon = async (_sock, method, params) => {
      observed.push({ method, params });
      if (!Array.isArray(params.references) || params.references.length === 0) {
        return {
          ok: false,
          error: "chunk_ids, result_ids, references, or refs is required",
        };
      }
      return {
        ok: true,
        data: {
          depth: params.depth,
          count: 1,
          missing: [],
          results: [
            {
              doc_id: 101,
              wikilink: "[[claude-code__21e233a9a944a28a]]",
              source: "/vault/_durable/claude-code__21e233a9a944a28a.md",
              text: "Drilled wikilink body",
            },
          ],
        },
      };
    };

    const res = await drillMemory(
      {
        wikilink: "claude-code__21e233a9a944a28a",
        depth: "snippet",
        vaultPath: vault,
        sessionId: "sess-class-c",
      },
      fakeDaemon,
    );
    assert.equal(res.ok, true, `drillMemory with wikilink failed: ${res.error}`);
    assert.equal(observed.length, 1);
    assert.deepEqual(observed[0].params.references, ["claude-code__21e233a9a944a28a"]);
  });
});

test("Class D: drillMemory local handle resolution does not duplicate note body 6x across text, raw_content, and items", async () => {
  await withTempVault(async (vault) => {
    const safeRel = "wiki/decisions/bloat-check.md";
    const distinctMarker = "UNIQUE_DRILL_BODY_MARKER_8812";
    await writeFile(
      path.join(vault, safeRel),
      [
        "---",
        "title: Bloat Check Note",
        "type: decision",
        "privacy: safe",
        "status: active",
        "---",
        "# Bloat Check Note",
        "",
        `${distinctMarker} `.repeat(30),
      ].join("\n"),
      "utf8",
    );
    const handle = vaultHandleForPath(safeRel);
    const deadDaemon = async () => ({ ok: false, error: "daemon down" });

    const res = await drillMemory(
      { references: [handle], depth: "snippet", vaultPath: vault, sessionId: "sess-class-d" },
      deadDaemon,
    );
    assert.equal(res.ok, true);
    assert.equal(res.data.raw_content, undefined, "local drill item must not duplicate un-fenced raw_content");
    assert.equal(res.data.text, undefined, "local drill item must not duplicate text alongside content");
    assert.equal(res.data.items, undefined, "local drill response must not duplicate items[] alongside results[]");
    assert.equal(res.data.results[0].raw_content, undefined);
    assert.equal(res.data.results[0].text, undefined);
  });
});




