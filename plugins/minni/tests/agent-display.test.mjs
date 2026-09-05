import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const output = new URL("./.compiled/agent-display-test.mjs", import.meta.url);
await build({
  stdin: { contents: 'export * from "./agent-display"; export { buildAgentsCatalogue, loadAgentCapsMap } from "./ui-server";',
    resolveDir: fileURLToPath(new URL("../src", import.meta.url)), loader: "ts" },
  outfile: fileURLToPath(output), bundle: true, platform: "node", format: "esm",
  packages: "external", logLevel: "silent",
});
const { agentDisplayMetadata, recordedUnknownIdentity, buildAgentsCatalogue, loadAgentCapsMap } = await import(output.href);
const id = "1234567890abcdef1234567890abcdef";
const entry = (details, tool = "minni_recall", summary = "query") =>
  `## [2026-09-04T12:00:00Z] ${tool} | ${summary}\n\n\`\`\`json\n${JSON.stringify(details)}\n\`\`\`\n`;

test("runtime names preserve identity and unknown IDs are not guessed to be runtimes", () => {
  assert.equal(agentDisplayMetadata({ id: "codex", registered: true, registrationKnown: true }).displayName, "Codex");
  const unknown = agentDisplayMetadata({ id, registered: false, registrationKnown: true });
  assert.match(unknown.displayName, /^Unregistered identity · 12345678/);
  assert.equal(unknown.capabilitiesKnown, false);
  assert.ok(!unknown.recallFailure);
  const registered = agentDisplayMetadata({ id, registered: true, registrationKnown: true });
  assert.match(registered.displayName, /^Registered identity/);
  assert.equal(registered.capabilitiesKnown, true);
  assert.match(registered.activityDescription, /not whether a process is running/);
  assert.match(agentDisplayMetadata({ id, registered: false, registrationKnown: false }).description, /could not check/);
});

test("invalid or missing capability records never turn filename fallback into known registration", async t => {
  const home = await mkdtemp(path.join(tmpdir(), "minni-invalid-registration-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(path.join(home, "principals"));
  await mkdir(path.join(home, "codex-vault"));
  for (const record of [[], null, "scalar", 42, {}, { agent_id: "codex" },
    { capabilities: "*" }, { capabilities: [null] },
    { capabilities: [], platform_agent_capabilities: { cursor: "*" } }]) {
    await writeFile(path.join(home, "principals", "codex.json"), JSON.stringify(record));
    assert.equal((await loadAgentCapsMap(home)).has("codex"), false);
    const { agents } = await buildAgentsCatalogue({ homePath: home,
      daemonRpc: async () => ({ candidates: [] }), auditTailFn: async () => ({ entries: [] }) });
    assert.equal(agents[0].registered, false, JSON.stringify(record));
    assert.equal(agents[0].registrationKnown, false, JSON.stringify(record));
    assert.equal(agents[0].capabilitiesKnown, false, JSON.stringify(record));
    assert.match(agents[0].description, /could not check/);
  }
  await writeFile(path.join(home, "principals", "codex.json"), JSON.stringify({ capabilities: [], platform_agent_capabilities: { cursor: ["read"] } }));
  const valid = await loadAgentCapsMap(home);
  assert.deepEqual(valid.get("codex"), { R: 0, L: 0, H: 0 });
  assert.deepEqual(valid.get("cursor"), { R: 1, L: 0, H: 0 });
});

test("only structured failed recall errors identify historical registration rejection", () => {
  const details = { ok: false, error: "unknown_identity: caller not registered", agentId: "12345678-90ab-cdef-1234-567890abcdef" };
  assert.equal(recordedUnknownIdentity(id, [entry(details)]), true);
  assert.equal(recordedUnknownIdentity(id, [entry({ ...details, ok: true })]), false);
  assert.equal(recordedUnknownIdentity(id, [entry({ ok: false, error: "unknown_identity" })]), false);
  assert.equal(recordedUnknownIdentity(id, [entry({ ...details, agentId: "codex" })]), false);
  assert.equal(recordedUnknownIdentity(id, [entry(details, "minni_learn")]), false);
  assert.equal(recordedUnknownIdentity(id, [entry({ ok: false }, "minni_recall", "unknown_identity")]), false);
  assert.equal(recordedUnknownIdentity(id, [entry({ ok: false, content: "unknown_identity" })]), false);
  assert.equal(recordedUnknownIdentity(id, ["unknown_identity"]), false);
  assert.equal(recordedUnknownIdentity(id, [entry(details).replace('"ok":false', '"ok":')]), false);
  assert.match(agentDisplayMetadata({ id, registered: true, registrationKnown: true, auditEntries: [entry(details)] }).description,
    /Past attempts to retrieve memories were rejected because this identity was not registered\./);
});

test("catalogue registration comes from principal records, preserves legacy fields, and does not write vaults", async t => {
  const home = await mkdtemp(path.join(tmpdir(), "minni-agent-display-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(path.join(home, "principals"));
  for (const agent of [id, "codex", "custom-runtime"]) await mkdir(path.join(home, `${agent}-vault`));
  await writeFile(path.join(home, "principals", "custom-runtime.json"), JSON.stringify({ agent_id: "custom-runtime", capabilities: [] }));
  await writeFile(path.join(home, "principals", "codex.json"), JSON.stringify({ agent_id: "codex", capabilities: ["recall", "learn"] }));
  const catalogue = await buildAgentsCatalogue({ homePath: home,
    daemonRpc: async () => ({ candidates: [] }), auditTailFn: async () => ({ entries: [] }) });
  const codex = catalogue.agents.find(a => a.id === "codex");
  assert.equal(codex.displayName, "Codex");
  assert.equal(codex.registered, true);
  assert.deepEqual(codex.caps, { R: 1, L: 1, H: 0 });
  const custom = catalogue.agents.find(a => a.id === "custom-runtime");
  assert.equal(custom.registered, true);
  assert.equal(custom.capabilitiesKnown, true);
  const unknown = catalogue.agents.find(a => a.id === id);
  assert.equal(unknown.registered, false);
  assert.equal(unknown.capabilitiesKnown, false);
  assert.equal(unknown.id, id);
  const { readdir } = await import("node:fs/promises");
  assert.deepEqual(await readdir(path.join(home, `${id}-vault`)), []);
  await writeFile(path.join(home, "principals", `${id}.json`), "{broken");
  const incomplete = await buildAgentsCatalogue({ homePath: home,
    daemonRpc: async () => ({ candidates: [] }), auditTailFn: async () => ({ entries: [] }) });
  const unreadable = incomplete.agents.find(a => a.id === id);
  assert.equal(unreadable.registrationKnown, false);
  assert.match(unreadable.description, /could not check/);
});
