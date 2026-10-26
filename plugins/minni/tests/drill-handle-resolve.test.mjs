import assert from "node:assert/strict";
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
