import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

// Issue #125 item 2 (F-LEARN-QUALITY-OPTIN): the quality floor must be
// default-on. A raw minni_learn call with junk content and requireQuality
// UNSET must be quality-blocked; requireQuality:false is the explicit
// opt-out that still writes.
test("minni_learn quality floor is default-on end-to-end through the MCP server", async (t) => {
  const { spawn } = await import("node:child_process");
  const net = await import("node:net");
  const root = await mkdtemp(path.join(tmpdir(), "sm-learn-quality-"));
  const home = path.join(root, "home");
  const socketPath = path.join(home, "minnid.sock");
  await mkdir(home, { recursive: true });
  const fakeDaemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.split("\n")[0]);
      const respond = (result) => {
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      };
      if (request.method === "gate.shared") {
        respond({ ok: true, status: "allowed" });
        return;
      }
      respond({ ok: true });
    });
  });
  await new Promise((resolve) => fakeDaemon.listen(socketPath, resolve));
  t.after(() => fakeDaemon.close());
  const serverPath = new URL("../dist/server.js", import.meta.url).pathname;
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: root,
      MINNI_CLAUDECODE_VAULT_PATH: root,
      MINNI_KILOCODE_VAULT_PATH: root,
      MINNI_GROK_VAULT_PATH: root,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  try {
    const responses = new Map();
    let buffered = "";
    const waiters = new Map();
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      let nl;
      while ((nl = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined) {
            responses.set(msg.id, msg);
            waiters.get(msg.id)?.(msg);
          }
        } catch {
          // non-JSON noise on stdout would be a protocol bug; surface via timeout
        }
      }
    });
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 15000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout waiting for response ${id}`)), ms);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });

    send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "learn-quality-e2e-test", version: "0.0.0" },
      },
    });
    const init = await awaitResponse(1);
    assert.ok(init.result, JSON.stringify(init));
    send({ jsonrpc: "2.0", method: "notifications/initialized" });

    const callLearn = async (id, args) => {
      send({
        jsonrpc: "2.0",
        id,
        method: "tools/call",
        params: { name: "minni_learn", arguments: args },
      });
      const reply = await awaitResponse(id);
      assert.ok(reply.result, JSON.stringify(reply));
      return JSON.parse(reply.result.content[0].text);
    };

    // Junk learning: short title, short vague content, no category/source.
    const junk = { title: "asdf", content: "asdf" };

    // 1. requireQuality UNSET -> blocked by default.
    const blockedDefault = await callLearn(2, junk);
    assert.equal(
      blockedDefault.status,
      "quality-blocked",
      `requireQuality unset must default to the quality floor: ${JSON.stringify(blockedDefault)}`,
    );
    assert.equal(blockedDefault.quality.ok, false);
    const sessionsAfterBlock = await readdir(path.join(root, "wiki", "sessions")).catch(() => []);
    assert.equal(
      sessionsAfterBlock.length,
      0,
      `quality-blocked learn must not write a vault session note: ${sessionsAfterBlock.join(", ")}`,
    );

    // 2. requireQuality: true -> still blocked (unchanged behavior).
    const blockedExplicit = await callLearn(3, { ...junk, requireQuality: true });
    assert.equal(blockedExplicit.status, "quality-blocked");

    // 3. requireQuality: false -> explicit opt-out still writes the weak note.
    const optOut = await callLearn(4, { ...junk, requireQuality: false });
    assert.equal(
      optOut.status,
      "learned",
      `requireQuality:false must bypass the floor: ${JSON.stringify(optOut)}`,
    );
    assert.equal(optOut.quality.ok, false);
    assert.ok(optOut.note, "opt-out learn must still write the vault note");
  } finally {
    child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
});
