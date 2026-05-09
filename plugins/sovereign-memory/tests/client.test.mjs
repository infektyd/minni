import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { buildStatusReport, compileVault, formatRecall, jsonRpcSocketRequest, parseSovrdJson } from "../dist/sovereign.js";

test("parseSovrdJson accepts healthy JSON responses", () => {
  assert.deepEqual(parseSovrdJson('{"status":"ok","agent":"shared-daemon"}'), {
    ok: true,
    data: { status: "ok", agent: "shared-daemon" },
  });
});

test("formatRecall returns concise markdown with query and provenance", () => {
  const formatted = formatRecall("socket health", {
    results: "### daemon.md (score=1.000)\nUse /tmp/sovereign.sock for local health.",
    agent_id: "codex",
    layer: "knowledge",
  }, [
    {
      notePath: "/tmp/vault/wiki/sessions/socket-health.md",
      relativePath: "wiki/sessions/socket-health.md",
      wikilink: "[[wiki/sessions/socket-health]]",
      title: "Socket health",
      snippet: "Codex should check /tmp/sovereign.sock before using recall.",
      score: 61,
    },
  ]);

  assert.match(formatted, /Query: socket health/);
  assert.match(formatted, /agent=codex/);
  assert.match(formatted, /AI Context Pack/);
  assert.match(formatted, /\[\[wiki\/sessions\/socket-health\]\]/);
  assert.match(formatted, /Use \/tmp\/sovereign.sock/);
});

test("formatRecall includes backend badge when recall reports backend provenance", () => {
  const formatted = formatRecall("backend provenance", {
    results: "### result.md (score=1.000)",
    agent_id: "codex",
    backend: "faiss-disk+qdrant",
  });

  assert.match(formatted, /\[faiss-disk\+qdrant\]/);
});

test("buildStatusReport includes vault, socket, AFM, and audit state", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-status-"));
  try {
    const report = await buildStatusReport({
      vaultPath: root,
      socket: { ok: true, data: { status: "ok" } },
      afm: { ok: false, error: "offline" },
    });

    assert.equal(report.vault.exists, true);
    assert.equal(report.socket.ok, true);
    assert.equal(report.afm.ok, false);
    assert.equal(report.audit.entries, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("compileVault sends daemon.compile JSON-RPC with dry-run default", async () => {
  const calls = [];
  const result = await compileVault(
    { passName: "session_distillation" },
    async (_socketPath, method, params) => {
      calls.push({ method, params });
      return { ok: true, data: { status: "ok", dry_run: params.dry_run } };
    },
  );

  assert.equal(result.ok, true);
  assert.equal(calls[0].method, "daemon.compile");
  assert.equal(calls[0].params.pass_name, "session_distillation");
  assert.equal(calls[0].params.dry_run, true);
});

test("jsonRpcSocketRequest speaks line-delimited JSON-RPC over Unix sockets", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-jsonrpc-socket-"));
  const socketPath = path.join(root, "sovrd.sock");
  const seen = [];
  const server = net.createServer((socket) => {
    let data = "";
    socket.on("data", (chunk) => {
      data += chunk.toString("utf8");
      if (!data.includes("\n")) return;
      const request = JSON.parse(data.split("\n")[0]);
      seen.push(request);
      socket.write(JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          daemon: { socket_path: socketPath },
          engine: { db_ok: true },
        },
      }) + "\n");
    });
  });

  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(socketPath, resolve);
    });

    const result = await jsonRpcSocketRequest(socketPath, "status", {});

    assert.equal(result.ok, true);
    assert.deepEqual(result.data, {
      daemon: { socket_path: socketPath },
      engine: { db_ok: true },
    });
    assert.equal(seen[0].jsonrpc, "2.0");
    assert.equal(seen[0].method, "status");
    assert.deepEqual(seen[0].params, {});
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
