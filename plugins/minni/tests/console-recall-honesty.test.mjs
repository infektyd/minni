import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { fileURLToPath } from "node:url";
import { prepareTask } from "../dist/task.js";

const output = new URL("./.compiled/console-recall-test.mjs", import.meta.url);
await build({
  stdin: {
    contents: 'export { RecallScreen } from "./screens/RecallScreen"; export { deriveStatusStats } from "./components/StatusBand"; export { Rail } from "./components/Rail";',
    resolveDir: fileURLToPath(new URL("../frontend-src/src", import.meta.url)),
    loader: "tsx",
  },
  outfile: fileURLToPath(output), bundle: true, platform: "node", format: "esm",
  packages: "external", jsx: "automatic", logLevel: "silent",
});
const { RecallScreen, deriveStatusStats, Rail } = await import(output.href);
const noop = () => {};
async function packetFor(response) {
  return prepareTask({ task: "projects", useAfm: false, includeVault: false }, {
    recall: async () => response, audit: async () => "unused",
  });
}
function render(packet) {
  return renderToStaticMarkup(createElement(RecallScreen, {
    selected: new Set(), setSelected: noop, focusId: null, setFocusId: noop,
    query: "projects", setQuery: noop, profile: "standard", setProfile: noop,
    packet, setPacket: noop, evidence: [], setEvidence: noop,
  }));
}

test("console shows cross-project evidence separately, inert and with degradation", async () => {
  const text = '<EVIDENCE source="other-project">cross-project <script>alert(1)</script></EVIDENCE>';
  const packet = await packetFor({ ok: true, data: {
    results: [{ text, source: "/other-project/note.md", trace_id: "trace-1" }],
    learnings: [{ content: "learning-only" }], episodic: [{ summary: "episode-only" }],
    agent_id: "codex", workspace_id: "fleet", backend: "hybrid", degraded: true,
  } });
  assert.equal(packet.recall.state, "degraded");
  assert.equal(packet.recall.evidence.length, 3);
  assert.equal(packet.relevantSources.length, 0);
  assert.ok(!packet.contextMarkdown.includes("learning-only"));
  const html = render(packet);
  for (const token of ["Local task-packet notes", "Workspace-unscoped", "Daemon recall", "cross-project", "learning-only", "episode-only", "trace-1", "fleet", "degraded"]) assert.ok(html.includes(token), token);
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("authorization filtering remains visible without claiming service degradation", async () => {
  const response = {
    results: [], degraded: false,
    auth_suppression: [{ src: "other-vault", suppressed: 3 }],
    degradation: [{ src: "personal", degraded: false }],
  };
  const packet = await packetFor({ ok: true, data: response });
  assert.equal(packet.recall.state, "responded");
  assert.match(packet.recall.diagnostic, /auth-suppressed: other-vault: 3/);
  const html = render(packet);
  assert.match(html, /Daemon recall · responded/);
  assert.match(html, /withheld by the read gate/);
  assert.doesNotMatch(html, /Daemon recall · degraded/);
  // Real degradation still wins when authorization filtering co-occurs,
  // even if the top-level roll-up is absent.
  const degraded = await packetFor({ ok: true, data: {
    ...response, degraded: undefined,
    degradation: [{ src: "personal", vector_degraded: "model unavailable" }],
  } });
  assert.equal(degraded.recall.state, "degraded");
  assert.match(degraded.recall.diagnostic, /auth-suppressed/);
});

test("daemon error and missing reply are not empty healthy recalls", async () => {
  const failed = await packetFor({ ok: false, error: "unknown_identity" });
  assert.equal(failed.recall.state, "error");
  assert.match(render(failed), /unknown_identity/);
  assert.match(render(failed), /Daemon recall unavailable/);
  const unknown = await packetFor({ ok: true });
  assert.equal(unknown.recall.state, "unknown");
  assert.match(render(unknown), /Daemon response unknown/);
  assert.equal((await packetFor({ ok: true, data: {} })).recall.state, "unknown");
  const empty = await packetFor({ ok: true, data: { results: [] } });
  assert.match(render(empty), /No daemon evidence returned/);
});

test("status reports generation health without claiming a scheduled AFM loop", () => {
  const status = { socket: { ok: true }, afm: { ok: true }, vault: { exists: true }, audit: { entries: 0 } };
  const stats = deriveStatusStats(status, { ok: true, port: 8765 });
  assert.equal(stats.find(s => s.label === "Daemon").value, "minnid · ready");
  assert.equal(stats.find(s => s.label === "AFM").value, "generation · verified");
  assert.equal(deriveStatusStats(null, null).find(s => s.label === "AFM").value, "generation · unknown");
  assert.equal(deriveStatusStats({ ...status, afm: { ok: false } }, null).find(s => s.label === "AFM").value, "generation · not verified");
  const html = renderToStaticMarkup(createElement(Rail, { active: "recall", onSelect: noop, counts: {} }));
  assert.ok(!html.includes("v4.2"));
  assert.ok(!html.includes("62%"));
});
