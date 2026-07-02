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

test("minni_resolve_candidate carries server principal without model-facing identity spoofing", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const start = source.indexOf('"minni_resolve_candidate"');
  assert.notEqual(start, -1, "minni_resolve_candidate tool registration not found");
  const nextTool = source.indexOf("server.registerTool(", start + 1);
  const block = source.slice(start, nextTool === -1 ? undefined : nextTool);

  const schemaStart = block.indexOf("inputSchema:");
  const handlerStart = block.indexOf("async");
  const schema = block.slice(schemaStart, handlerStart);
  assert.doesNotMatch(schema, /\bagent(?:_id|Id)?\s*:/, "model-facing schema must not accept caller identity");
  assert.match(block, /agent_id:\s*DEFAULT_AGENT_ID/, "resolve RPC must stamp the configured server principal");
});

test("minni_resolve_candidate no longer advertises the unenforced mark_* decisions (issue #123)", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const start = source.indexOf('"minni_resolve_candidate"');
  assert.notEqual(start, -1, "minni_resolve_candidate tool registration not found");
  const nextTool = source.indexOf("server.registerTool(", start + 1);
  const block = source.slice(start, nextTool === -1 ? undefined : nextTool);

  for (const removed of ["mark_sensitive", "mark_temporary", "mark_project_scoped"]) {
    assert.doesNotMatch(
      block,
      new RegExp(removed),
      `${removed} maps to a plain accept with no privacy/expiry/scope enforcement and must not be advertised`,
    );
  }
  for (const kept of ["accept", "reject", "redact", "merge", "supersede"]) {
    assert.match(block, new RegExp(`"${kept}"`), `real decision ${kept} must remain in the enum`);
  }
});

test("Codex hook remains Codex-native instead of reusing Claude hook entrypoint", async () => {
  const codexHook = await readFile(new URL("../src/codex-hook.ts", import.meta.url), "utf8");
  assert.match(codexHook, /runtime:\s*"codex"/);
  assert.match(codexHook, /hookScript:\s*"codex-hook\.js"/);
  assert.doesNotMatch(codexHook, /CLAUDECODE_AGENT_ID|CLAUDECODE_VAULT_PATH|hookScript:\s*"hook\.js"/);
});
