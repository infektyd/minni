// The MCP `instructions` field is the only always-load context channel that
// works WITHOUT hooks, and it is what carries Minni on the surfaces where hooks
// structurally cannot hydrate: Grok Build (passive-event stdout is ignored),
// Cursor (sessionStart injection is a confirmed open vendor bug), and Claude
// Desktop (no hook system at all).
//
// It is billed into EVERY MCP session on EVERY host, so its size is a real
// cost, and Codex documents a 512-character self-containment budget.

import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src", "server.ts");

// Import the BUILT value rather than re-parsing string literals out of the
// source. Reconstructing it with a regex measured a budget against text that
// only resembled what ships.
//
// Import the LEAF module, never server.js: importing the server constructs it
// as a side effect, and the test then hangs forever instead of failing.
async function instructions() {
  const { MINNI_INSTRUCTIONS } = await import("../dist/mcp-instructions.js");
  assert.equal(typeof MINNI_INSTRUCTIONS, "string", "MINNI_INSTRUCTIONS must exist");
  return MINNI_INSTRUCTIONS;
}

// Strip comments before matching. The un-stripped version passed on a
// commented-out constructor, so it could not fail for the right reason: the
// real `new McpServer({name, version})` could lose `instructions` entirely
// while a comment above it kept the test green.
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

test("the server actually passes instructions to McpServer", async () => {
  const src = stripComments(await readFile(SRC, "utf8"));
  assert.match(
    src,
    /new McpServer\([\s\S]{0,200}\{\s*instructions:\s*MINNI_INSTRUCTIONS\s*\}/,
    "declaring the constant is useless unless it reaches the constructor",
  );
});

test("the first 512 characters are self-contained", async () => {
  const text = await instructions();
  const head = text.slice(0, 512);

  // Codex documents this budget explicitly: a host may surface only the head.
  assert.ok(head.includes("Minni"), "must name itself in the first 512 chars");
  assert.ok(
    head.includes("minni_recall"),
    "the actionable instruction must survive truncation at 512 chars",
  );
});

test("instructions stay small -- every session on every host pays for this", async () => {
  const text = await instructions();
  assert.ok(
    text.length < 900,
    `instructions are ${text.length} chars; keep them terse or they tax every session`,
  );
});

test("instructions carry the evidence-not-instruction boundary", async () => {
  const text = await instructions();
  // Recalled memory is untrusted data. Saying so in the server's own guidance
  // is cheap insurance against a poisoned vault note steering a host agent.
  assert.match(text, /EVIDENCE, not instruction/);
  assert.match(text, /no authority/i);
});
