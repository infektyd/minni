import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(__dirname, "..");
const kiloRoot = path.join(pluginRoot, ".kilocode-plugin");

async function readJson(relativePath) {
  return JSON.parse(await readFile(path.join(kiloRoot, relativePath), "utf8"));
}

test("KiloCode package points at shared parent dist build", async () => {
  const plugin = await readJson("plugin.json");
  const mcp = await readJson(".mcp.json");

  for (const manifest of [plugin, mcp]) {
    const server = manifest.mcpServers["sovereign-memory"];
    assert.deepEqual(server.args, ["${KILO_PLUGIN_ROOT}/../dist/server.js"]);
    assert.equal(server.cwd, "${KILO_PLUGIN_ROOT}/..");
  }
});

test("KiloCode hooks invoke kilocode-specific hook entrypoint", async () => {
  const hooks = await readJson("hooks/hooks.json");

  for (const [event, entries] of Object.entries(hooks)) {
    const command = entries[0].hooks[0].command;
    assert.match(command, /dist\/kilocode-hook\.js/);
    assert.doesNotMatch(command, /dist\/hook\.js/);
    assert.match(command, new RegExp(`${event}$`));
  }
});
