import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { fileURLToPath } from "node:url";
const output = new URL("./.compiled/agent-summary.mjs", import.meta.url);
await build({ stdin: { contents: 'export { AgentSummary, agentDisplayName } from "./components/AgentSummary"; export { mapAgents, mapAgentRow } from "./board/boardData";', resolveDir: fileURLToPath(new URL("../frontend-src/src", import.meta.url)), loader: "tsx" }, outfile: fileURLToPath(output), bundle: true, platform: "node", format: "esm", packages: "external", jsx: "automatic", logLevel: "silent" });
const { AgentSummary, agentDisplayName, mapAgents, mapAgentRow } = await import(output.href);
const render = agent => renderToStaticMarkup(createElement(AgentSummary, { agent }));
test("registered identities precede opaque vaults without inferring process liveness", () => {
  const rows = mapAgents([
    { id: "opaque", staged: null },
    { id: "codex", displayName: "Codex", description: "Coding memory", registered: true, registrationKnown: true, capabilitiesKnown: true, caps: { R: 1, L: 0, H: 1 }, staged: 5, stagedAtLimit: true, seen: "2h ago" },
  ]);
  assert.equal(rows[0].id, "codex");
  const html = render(rows[0]);
  assert.match(html, /Coding memory/);
  assert.match(html, /Memory reading: listed/);
  assert.match(html, /Memory writing: not listed/);
  assert.match(html, /Sharing \/ governance: listed/);
  assert.match(html, /Last recorded memory activity: 2h ago/);
  assert.match(html, /5\+ suggestions awaiting review/);
  assert.match(html, /Activity records do not indicate a running process/);
  assert.match(html, /<details><summary>Identity and storage details/);
});
test("unregistered and missing capability records stay unknown and distinct", () => {
  const id = "abcdeffedcba12345678901234567890";
  const row = mapAgentRow({ id, vaultPath: "/private/vault", registered: false, registrationKnown: true, caps: { R: 1, L: 1, H: 1 } });
  assert.match(agentDisplayName(row), /Unregistered identity · abcdeffe…567890/);
  const html = render(row);
  assert.match(html, /Memory reading: unknown/);
  assert.match(html, /Review count unavailable/);
  assert.match(html, /No registration record/);
  assert.match(html, /Identity: abcdeffedcba12345678901234567890/);
  assert.ok(html.indexOf("/private/vault") > html.indexOf("<details>"));
  assert.match(agentDisplayName(mapAgentRow({ id })), /Unknown identity/);
});
