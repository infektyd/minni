// A tripwire, not a bug fix.
//
// MCP revision 2026-07-28 makes the protocol stateless, adds `server/discover`,
// and replaces server-initiated requests with Multi Round-Trip Requests. Minni
// cannot adopt it yet: the shipped `@modelcontextprotocol/sdk` v1 line tops out
// at 2025-11-25, and support lives in a DIFFERENT package family
// (`@modelcontextprotocol/server` v2), so no `npm update` will ever reach it.
//
// That is the trap this file guards. Because the migration is a package swap
// rather than a version bump, the wire protocol Minni speaks can change without
// any obvious signal in package.json — a transitive resolution, an inattentive
// dependency edit, or an unexpected v1 release. Under 37 registered tools, that
// change would otherwise be invisible until something failed in the field.
//
// These assertions therefore lock a CURRENT-STATE FACT rather than correcting a
// defect: there is no old behaviour for them to fail on. When one of them
// fires, nothing is broken — the world moved, and the plan needs re-reading:
//
//   docs/design/DESIGN-mcp-2026-07-28-readiness.md
//
// Do not "fix" a failure here by loosening the assertion. Follow the sequencing
// in that document (migrate src/server.ts:6-7 and the transport call to
// `serveStdio`, verify all tools still list and dispatch), then update these
// expectations to match what Minni deliberately speaks.

import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DESIGN_DOC = "docs/design/DESIGN-mcp-2026-07-28-readiness.md";

const SRC = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "src",
  "server.ts",
);

// Read the SDK's own constants rather than the version string in package.json.
// The dependency range (`^1.30.0`) is a statement of intent; LATEST_PROTOCOL_VERSION
// is what actually goes on the wire, and only the second one can regress silently.
async function sdkProtocol() {
  return await import("@modelcontextprotocol/sdk/types.js");
}

test("the shipped SDK still negotiates 2025-11-25, not 2026-07-28", async () => {
  const { LATEST_PROTOCOL_VERSION } = await sdkProtocol();

  assert.equal(
    LATEST_PROTOCOL_VERSION,
    "2025-11-25",
    `The MCP protocol version Minni speaks changed to ${LATEST_PROTOCOL_VERSION}. ` +
      `This is not a bug — it means the SDK moved. Re-read ${DESIGN_DOC} before adjusting this test.`,
  );
});

test("no 2026-07-28 support has appeared in the v1 SDK line", async () => {
  const { SUPPORTED_PROTOCOL_VERSIONS } = await sdkProtocol();

  // If 2026-07-28 ever shows up here, the v1 line gained support after all and
  // the design doc's core premise — that adoption requires a package migration
  // to @modelcontextprotocol/server v2 — is obsolete.
  assert.ok(
    !SUPPORTED_PROTOCOL_VERSIONS.includes("2026-07-28"),
    `The v1 SDK now supports 2026-07-28 (${SUPPORTED_PROTOCOL_VERSIONS.join(", ")}). ` +
      `${DESIGN_DOC} assumes this is impossible; its Q1/Q2 decisions need revisiting.`,
  );
});

test("server.ts still binds the v1 SDK entry points", async () => {
  const source = await readFile(SRC, "utf8");

  // The migration's entire code surface is these two imports plus the transport
  // call. Asserting on them means a partial or accidental migration trips here
  // rather than at runtime on a host that cannot speak the new protocol.
  assert.match(
    source,
    /from "@modelcontextprotocol\/sdk\/server\/mcp\.js"/,
    `server.ts no longer imports the v1 McpServer. If this is the 2026-07-28 migration, follow ${DESIGN_DOC}.`,
  );
  assert.match(
    source,
    /from "@modelcontextprotocol\/sdk\/server\/stdio\.js"/,
    `server.ts no longer imports the v1 StdioServerTransport. If this is the 2026-07-28 migration, follow ${DESIGN_DOC}.`,
  );

  // @modelcontextprotocol/server is the v2 package. Its presence means the
  // migration started; these expectations must then be rewritten, not deleted.
  assert.ok(
    !source.includes("@modelcontextprotocol/server"),
    `server.ts imports the v2 SDK (@modelcontextprotocol/server). Update this test to lock the NEW protocol version per ${DESIGN_DOC}.`,
  );
});

test("Minni uses no capability deprecated by 2026-07-28", async () => {
  const source = await readFile(SRC, "utf8");

  // Roots, Sampling, and Logging enter a >=12-month deprecation window in this
  // revision. Minni uses none of them today, which is why its deprecation
  // exposure is nil. This asserts that stays true: adding one now would take on
  // a migration obligation with a deadline, for a capability already on the way
  // out.
  // Match the MCP-qualified forms only: the wire method string, or the call
  // made through a server/client object. A bare `createMessage(` would also
  // match an unrelated local helper of that name and report it as "uses
  // Sampling" — a wrong diagnosis is worse than a missed one here, because this
  // test's whole value is that a failure means exactly what it says.
  for (const [capability, pattern] of [
    ["Sampling", /["']sampling\/createMessage["']|\.\s*createMessage\s*\(/],
    ["Roots", /["']roots\/list["']|\.\s*listRoots\s*\(/],
    ["Logging", /["']logging\/setLevel["']|\.\s*(?:sendLoggingMessage|setLoggingLevel)\s*\(/],
  ]) {
    assert.ok(
      !pattern.test(source),
      `server.ts now uses ${capability}, which 2026-07-28 deprecates. See the deprecation section of ${DESIGN_DOC}.`,
    );
  }
});
