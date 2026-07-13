import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(__dirname, "..");
const kiloRoot = path.join(pluginRoot, ".kilocode-plugin");

async function readJson(relativePath) {
  return JSON.parse(await readFile(path.join(kiloRoot, relativePath), "utf8"));
}

test("KiloCode legacy package does not claim hook or MCP authority", async () => {
  const plugin = await readJson("plugin.json");
  assert.equal(plugin.hooks, undefined);
  assert.equal(plugin.mcpServers, undefined);
  await assert.rejects(access(path.join(kiloRoot, ".mcp.json")));
  await assert.rejects(access(path.join(kiloRoot, "hooks/hooks.json")));
});

test("KiloCode ships the native global plugin template", async () => {
  const source = await readFile(path.join(pluginRoot, "kilo/minni-plugin.js"), "utf8");
  assert.match(source, /export default MinniPlugin/);
  assert.match(source, /"chat\.message"/);
  assert.match(source, /"tool\.execute\.before"/);
  assert.match(source, /"experimental\.session\.compacting"/);
  assert.match(source, /session\.idle/);
  assert.match(source, /dist\/kilocode-hook\.js|__MINNI_KILO_HOOK_SCRIPT__/);
});
