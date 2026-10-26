import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { drillMemory } from "../dist/daemon_transport.js";
import { recordSessionHandles, vaultHandleForPath } from "../dist/recall-state.js";
import { ensureVault } from "../dist/vault.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-s3c-drill-handle-"));
  try {
    await ensureVault(root);
    return await fn(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function makeUnreachableConfig(vaultPath) {
  return {
    agentId: "claude-code",
    vaultPath,
    workspaceId: "test-ws",
    daemonSocketPath: path.join(vaultPath, ".runtime", "nonexistent-daemon.sock"),
    daemonTimeoutMs: 200,
    recallTimeoutMs: 200,
  };
}

test("minni_drill resolves m:<8hex> handle locally against the vault even when the daemon is unreachable", async () => {
  await withTempVault(async (vault) => {
    const relPath = "wiki/decisions/socket-migration.md";
    const fullPath = path.join(vault, relPath);
    await mkdir(path.dirname(fullPath), { recursive: true });
    await writeFile(
      fullPath,
      [
        "---",
        "title: Socket migration decision",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Socket migration decision",
        "",
        "## Rationale",
        "",
        "Moved the daemon transport from TCP to a local Unix domain socket.",
        "",
      ].join("\n"),
      "utf8",
    );

    const handle = vaultHandleForPath(relPath);
    assert.match(handle, /^m:[0-9a-f]{8}$/);

    const config = makeUnreachableConfig(vault);
    const res = await drillMemory(config, {
      wikilink: handle,
      vaultPath: vault,
    });
    assert.equal(res.ok, true);
    assert.equal(res.source, "vault-fallback");
    assert.ok(res.data);
    assert.match(String(res.data.content ?? ""), /Moved the daemon transport from TCP/);
  });
});

test("minni_drill resolves registered d:<src>:<id> handle from session handle registry and tags unregistered handles source_unverified: true", async () => {
  await withTempVault(async (vault) => {
    const relPath = "wiki/decisions/registered-doc.md";
    const fullPath = path.join(vault, relPath);
    await mkdir(path.dirname(fullPath), { recursive: true });
    await writeFile(
      fullPath,
      "# Registered doc\n\nRegistered daemon document content body.\n",
      "utf8",
    );

    await recordSessionHandles(vault, "sess-drill-1", [
      {
        title: "Registered doc",
        wikilink: "[[wiki/decisions/registered-doc]]",
        score: 0.91,
        handle: "d:personal:42",
        mkey: "doc:personal:42",
        relativePath: relPath,
        scope: "personal",
        docId: 42,
      },
    ]);

    const config = makeUnreachableConfig(vault);
    const verified = await drillMemory(config, {
      wikilink: "d:personal:42",
      vaultPath: vault,
      sessionId: "sess-drill-1",
    });
    assert.equal(verified.ok, true);
    assert.equal(verified.data?.source_unverified, false);
    assert.match(String(verified.data?.content ?? ""), /Registered daemon document content body/);

    // Fabricated / unregistered d:<src>:<id> handle is tagged source_unverified: true.
    const unverified = await drillMemory(config, {
      wikilink: "d:personal:99999",
      vaultPath: vault,
      sessionId: "sess-drill-1",
    });
    assert.equal(unverified.data?.source_unverified, true);
  });
});

test("minni_drill returns a clean not-found response for nonexistent or malformed m:<8hex> handles without throwing", async () => {
  await withTempVault(async (vault) => {
    const config = makeUnreachableConfig(vault);
    const missing = await drillMemory(config, {
      wikilink: "m:deadbeef",
      vaultPath: vault,
    });
    assert.equal(missing.ok, false);
    assert.equal(missing.data?.found, false);

    const traversal = await drillMemory(config, {
      wikilink: "m:../../etc/passwd",
      vaultPath: vault,
    });
    assert.equal(traversal.ok, false);
    assert.equal(traversal.data?.found, false);
  });
});
