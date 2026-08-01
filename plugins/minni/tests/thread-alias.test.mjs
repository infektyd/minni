// minni:plan -> minni:threads rename, deprecation window.
//
// The 11 canonical tools are now `minni_thread_*`. For ONE release the old
// `minni_plan_*` names stay registered as aliases bound to the canonical
// handler, so in-flight agents and installed configs keep working while the
// leading "DEPRECATED" in each description drives migration.
//
// DELETE THIS FILE when DEPRECATED_TOOL_ALIASES is removed from server.ts.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const VERBS = [
  "create",
  "update",
  "scar",
  "status",
  "replan",
  "history",
  "revision",
  "diff",
  "restore",
  "activate",
  "deactivate",
];

// A live stdio MCP server over a fake daemon socket, matching plan.test.mjs's
// end-to-end harness. Aliases are a REGISTRATION-time construct, so a source
// pin would prove nothing — only a real tools/list + tools/call can.
async function withLiveServer(t, fn) {
  const { spawn } = await import("node:child_process");
  const net = await import("node:net");
  const root = await mkdtemp(path.join(tmpdir(), "sm-thread-alias-"));
  const home = path.join(root, "home");
  const socketPath = path.join(home, "minnid.sock");
  await mkdir(home, { recursive: true });
  const fakeDaemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.split("\n")[0]);
      socket.write(
        `${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { ok: true, status: "allowed" } })}\n`,
      );
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
    const waiters = new Map();
    let buffered = "";
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
          // non-JSON stdout would be a protocol bug; surfaced via the timeout
        }
      }
    });
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 15000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout waiting for ${id}`)), ms);
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
        clientInfo: { name: "thread-alias-test", version: "0.0.0" },
      },
    });
    const init = await awaitResponse(1);
    assert.ok(init.result, JSON.stringify(init));
    send({ jsonrpc: "2.0", method: "notifications/initialized" });

    await fn({ send, awaitResponse });
  } finally {
    child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
}

test("every renamed thread tool is registered, and its old plan name still resolves", async (t) => {
  await withLiveServer(t, async ({ send, awaitResponse }) => {
    send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
    const listed = await awaitResponse(2);
    assert.ok(listed.result, JSON.stringify(listed));
    const byName = new Map(listed.result.tools.map((tool) => [tool.name, tool]));

    for (const verb of VERBS) {
      const canonical = byName.get(`minni_thread_${verb}`);
      assert.ok(canonical, `canonical minni_thread_${verb} must be registered`);

      const alias = byName.get(`minni_plan_${verb}`);
      assert.ok(alias, `deprecated alias minni_plan_${verb} must still be registered`);
      // Leading "DEPRECATED" is the whole migration mechanism: models re-read
      // tool descriptions every turn, so the notice is continuously in view.
      assert.ok(
        alias.description.startsWith("DEPRECATED"),
        `minni_plan_${verb} description must lead with DEPRECATED, got: ${alias.description.slice(0, 40)}`,
      );
      assert.ok(
        alias.description.includes(`minni_thread_${verb}`),
        `minni_plan_${verb} must name its replacement`,
      );
      // titles must differ too, or tools/list renders 11 duplicate title pairs
      // and a client picking by title cannot tell alias from canonical
      assert.notEqual(
        alias.title,
        canonical.title,
        `minni_plan_${verb} must not reuse the canonical title verbatim`,
      );
      assert.match(alias.title, /\(deprecated\)$/, `minni_plan_${verb} title must be marked`);
      // The alias replays the canonical schema, so the model cannot be handed a
      // narrower or emptier contract under the old name.
      assert.deepEqual(
        alias.inputSchema,
        canonical.inputSchema,
        `minni_plan_${verb} must expose the canonical input schema`,
      );
    }
  });
});

test("calling a deprecated plan alias runs the canonical thread handler end-to-end", async (t) => {
  await withLiveServer(t, async ({ send, awaitResponse }) => {
    // Round-trip through the SHIM, not the canonical name: this is the only
    // assertion that would catch the alias loop registering an undefined
    // handler (e.g. after an SDK bump renames RegisteredTool.handler).
    send({
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: {
        name: "minni_plan_create",
        arguments: { goal: "verify the deprecated alias reaches the canonical handler" },
      },
    });
    const created = await awaitResponse(3);
    assert.ok(created.result, JSON.stringify(created));
    const body = JSON.parse(created.result.content[0].text);
    assert.ok(!body.error, `alias call must not error: ${JSON.stringify(body)}`);
    assert.match(body.plan_id, /^plan-[0-9a-f]{16}$/, "alias must mint a real plan artifact");

    // ...and the canonical name observes the same state, proving one handler
    // backs both names rather than two divergent registrations.
    send({
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: { name: "minni_thread_status", arguments: { plan_id: body.plan_id } },
    });
    const status = await awaitResponse(4);
    assert.ok(status.result, JSON.stringify(status));
    const statusBody = JSON.parse(status.result.content[0].text);
    assert.ok(!statusBody.error, `canonical status must resolve the alias-created plan: ${JSON.stringify(statusBody)}`);
    assert.equal(
      statusBody.view.goal,
      "verify the deprecated alias reaches the canonical handler",
      "both names must address ONE artifact, not two divergent registrations",
    );
  });
});

test("RegisteredTool still exposes .handler, which the alias loop depends on", async () => {
  // SDK shape pin. `handler` is the field the alias loop replays; the SDK also
  // has a `callback` name in update()'s parameter type, so a future rename is
  // plausible AND would fail silently (aliases registered with undefined).
  const { McpServer } = await import("@modelcontextprotocol/sdk/server/mcp.js");
  const probe = new McpServer({ name: "alias-shape-probe", version: "0.0.0" });
  const registered = probe.registerTool(
    "probe_tool",
    { title: "Probe", description: "shape probe", inputSchema: {} },
    async () => ({ content: [{ type: "text", text: "ok" }] }),
  );
  assert.equal(typeof registered.handler, "function", "RegisteredTool.handler must be the callback");
  assert.ok(registered.inputSchema, "RegisteredTool.inputSchema must be replayable");
});
