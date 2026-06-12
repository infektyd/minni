import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

function stripLineComments(text) {
  return text
    .split("\n")
    .map((line) => line.replace(/\/\/.*$/, ""))
    .join("\n");
}

function extractInputSchemas(source) {
  const schemas = [];
  let offset = 0;
  while (true) {
    const marker = source.indexOf("inputSchema:", offset);
    if (marker === -1) return schemas;
    const start = source.indexOf("{", marker);
    if (start === -1) return schemas;
    let depth = 0;
    let quote = null;
    let escaped = false;
    for (let i = start; i < source.length; i += 1) {
      const char = source[i];
      if (quote) {
        if (escaped) {
          escaped = false;
        } else if (char === "\\") {
          escaped = true;
        } else if (char === quote) {
          quote = null;
        }
        continue;
      }
      if (char === "\"" || char === "'" || char === "`") {
        quote = char;
        continue;
      }
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) {
        schemas.push(source.slice(start, i + 1));
        offset = i + 1;
        break;
      }
    }
    if (offset <= marker) return schemas;
  }
}

test("model-facing MCP input schemas do not expose local path authority", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const schemas = extractInputSchemas(source);
  assert.equal(schemas.length, 37, "expected one schema per registered MCP tool");

  const forbiddenFields = [
    "vaultPath",
    "vault_path",
    "filePath",
    "rootPath",
    "afmPrepareUrl",
  ];
  for (const schema of schemas) {
    for (const field of forbiddenFields) {
      assert.doesNotMatch(
        schema,
        new RegExp(`\\b${field}\\s*:`),
        `model-facing inputSchema exposes forbidden path field ${field}`,
      );
    }
  }
});

test("minni_recall schema exposes scope enum and keeps cross_agent back-compat", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const recallStart = source.indexOf('"minni_recall"');
  assert.notEqual(recallStart, -1, "minni_recall tool registration not found");
  const nextTool = source.indexOf("server.registerTool(", recallStart + 1);
  const recallBlock = source.slice(recallStart, nextTool === -1 ? undefined : nextTool);

  assert.match(recallBlock, /scope:\s*z\.enum\(\["personal",\s*"combined",\s*"both"\]\)\.optional\(\)/);
  assert.match(recallBlock, /cross_agent:\s*z\.boolean\(\)\.optional\(\)/);
});
