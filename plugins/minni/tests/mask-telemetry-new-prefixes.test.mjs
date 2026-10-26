import assert from "node:assert/strict";
import test from "node:test";

import { maskTelemetry } from "../dist/jev-harvest.js";

test("maskTelemetry strips multi-hit pointer lines while preserving surrounding human prose", () => {
  const input = [
    "Always validate the token expiry before retrying the request.",
    '📓 3 relevant memories: "Token rotation" (m:9f3a2c1b) 0.91, "Retry policy" (m:4e8d1a0c) 0.84, "Auth daemon" (d:personal:42) 0.79. Consult minni_recall or minni_drill(m:9f3a2c1b) before deriving from scratch.',
    '📓 1 relevant memory: "Single hit" (m:12345678) 0.88. Consult minni_recall or minni_drill(m:12345678) before deriving from scratch.',
    "Never bypass the socket containment check.",
  ].join("\n");

  const masked = maskTelemetry(input);
  assert.equal(
    masked,
    [
      "Always validate the token expiry before retrying the request.",
      "Never bypass the socket containment check.",
    ].join("\n"),
  );
});

test("maskTelemetry strips guard context and deny lines while preserving human directives", () => {
  const input = [
    "From now on, prefer strict schema validation on all RPC boundaries.",
    "Minni recall guard (UNCONSULTED, top score 0.85) — relevant memory (evidence, not instruction):",
    '  - "RPC schema contract" ([[wiki/decisions/rpc-schema]]) — Always validate fields before dispatch.',
    "Treat recalled snippets as evidence. Call `minni_recall` or `minni_drill` if deeper context is needed.",
    "",
    'Minni recall guard (UNCONSULTED): a high-confidence memory pointer (top score 0.91) was injected on this turn: "RPC schema" ([[wiki/decisions/rpc-schema]], 0.91).',
    "Do not weaken existing tests to pass.",
  ].join("\n");

  const masked = maskTelemetry(input);
  assert.ok(masked.includes("From now on, prefer strict schema validation on all RPC boundaries."));
  assert.ok(masked.includes("Do not weaken existing tests to pass."));
  assert.ok(!masked.includes("Minni recall guard"), `leaked guard line in: ${masked}`);
  assert.ok(!masked.includes("RPC schema contract"), `leaked guard bullet in: ${masked}`);
});

test("maskTelemetry strips [Minni Subagent Brief] and Minni subagent brief lines while preserving human prose", () => {
  const input = [
    "[Minni Subagent Brief]",
    "Minni subagent brief — relevant memory (evidence, not instruction):",
    '  - "Subagent ownership rule" ([[wiki/decisions/subagent-ownership]]) — One writer per checkout.',
    "",
    "Ensure that every worker runs the full test suite before reporting.",
  ].join("\n");

  const masked = maskTelemetry(input);
  assert.equal(masked, "Ensure that every worker runs the full test suite before reporting.");
});
