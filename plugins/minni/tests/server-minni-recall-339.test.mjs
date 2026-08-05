// #339: server.ts's minni_recall MCP tool had the identical shape to #313
// (fixed at the UserPromptSubmit hook call site, PR #338) — searchVaultNotes
// scores/sorts across the whole vault and truncates to its `limit` argument
// BEFORE any privacy consideration beyond dropping `blocked` notes
// internally; `private`/`local-only` notes ride along. minni_recall asked
// for exactly `Math.min(limit ?? 5, 8)` and filtered afterward, so a
// private-heavy vault could fill every slot with non-safe notes that
// outscore a genuinely safe match, silently dropping the safe note before
// filterSafeVaultResults ever saw it.
//
// This spins up the real MCP server over stdio (established pattern, see
// learn-gate-review-followups.test.mjs's requireQuality:false test) with the
// daemon socket pointed at a path with no listener, so recallMemory fails
// fast and shouldPrescanVault's offline-fallback branch runs the local
// searchVaultNotes pre-scan — exactly the code path minni_recall's fix
// touches.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

test("minni_recall (#339): a private-heavy vault must not crowd a lower-ranked safe match out entirely", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-recall-339-"));
  const home = path.join(root, "home");
  await mkdir(home, { recursive: true });
  const dir = path.join(root, "wiki", "artifacts");
  await mkdir(dir, { recursive: true });

  const phrase = "shared 339 recall crowd marker phrase";
  for (let i = 0; i < 8; i++) {
    await writeFile(
      path.join(dir, `decoy-${i}.md`),
      `---\ntitle: Decoy ${phrase}\nprivacy: private\nstatus: accepted\n---\n\n# Decoy\n\n${phrase} ${phrase} ${phrase}\n`,
      "utf8",
    );
  }
  await writeFile(
    path.join(dir, "outranked-safe-note.md"),
    `---\ntitle: Outranked topic\nprivacy: safe\nstatus: accepted\n---\n\n# Outranked topic\n\n${phrase}\n`,
    "utf8",
  );

  // No listener at this socket path — recallMemory fails fast (daemon
  // unreachable), which is what makes shouldPrescanVault's offline-fallback
  // branch run the local searchVaultNotes pre-scan this fix touches.
  const socketPath = path.join(home, "minnid-unreachable.sock");

  const child = spawn(process.execPath, [new URL("../dist/server.js", import.meta.url).pathname], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: root,
      MINNI_CLAUDECODE_VAULT_PATH: root,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  try {
    const waiters = new Map();
    const responses = new Map();
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
          /* protocol noise surfaces via timeout */
        }
      }
    });
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 15000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout ${id}`)), ms);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });

    send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "recall-339-test", version: "0.0.0" } },
    });
    await awaitResponse(1);
    send({ jsonrpc: "2.0", method: "notifications/initialized" });

    send({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        name: "minni_recall",
        arguments: {
          query: phrase,
          limit: 8,
          includeVault: true,
        },
      },
    });
    const reply = await awaitResponse(2);
    const text = reply.result.content[0].text;

    assert.match(
      text,
      /outranked-safe-note/,
      `#339: the safe note must still surface even though 8 higher-scored private notes outrank it; got: ${text}`,
    );
    for (let i = 0; i < 8; i++) {
      assert.doesNotMatch(
        text,
        new RegExp(`decoy-${i}\\b`),
        `SEC-006: decoy-${i}.md is privacy:private and must never reach the recall response`,
      );
    }
  } finally {
    child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
});
