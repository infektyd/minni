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

test("model-facing MCP input schemas expose exact 12 v1 tools and no local path authority", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const schemas = extractInputSchemas(source);
  assert.equal(schemas.length, 12, "expected exact 12 schemas for the v1 MCP tool surface");

  const toolNames = [...source.matchAll(/server\.registerTool\(\s*"([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(toolNames, [
    "minni_recall",
    "minni_drill",
    "minni_learn",
    "minni_list_candidates",
    "minni_resolve_candidate",
    "minni_status",
    "minni_handoff_send",
    "minni_handoff_inbox",
    "minni_thread_create",
    "minni_thread_status",
    "minni_thread_update",
    "minni_thread_scar",
  ]);

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

test("minni_handoff_send and minni_handoff_inbox schemas enforce server caller principal and 1-step handoff fields", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );

  for (const [toolName, expectedFields, forbiddenIdentityFields] of [
    [
      "minni_handoff_send",
      ["to_agent", "task", "summary", "files", "next_steps", "open_questions", "workspace_id"],
      ["from_agent", "agent_id", "agentId"],
    ],
    [
      "minni_handoff_inbox",
      ["ack", "lease_id", "status", "note"],
      ["from_agent", "to_agent", "agent_id", "agentId"],
    ],
  ]) {
    const start = source.indexOf(`"${toolName}"`);
    assert.notEqual(start, -1, `${toolName} registration not found`);
    const nextTool = source.indexOf("server.registerTool(", start + 1);
    const block = source.slice(start, nextTool === -1 ? undefined : nextTool);
    const schema = block.slice(block.indexOf("inputSchema:"), block.indexOf("async"));
    for (const f of expectedFields) {
      assert.match(schema, new RegExp(`\\b${f}\\s*:`), `${toolName} must expose ${f}`);
    }
    for (const bad of forbiddenIdentityFields) {
      assert.doesNotMatch(schema, new RegExp(`\\b${bad}\\s*:`), `${toolName} must not expose ${bad}`);
    }
  }
});

test("4 compact thread tools expose v1 schema boundaries (activate on update, optional slice_id on scar)", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  for (const toolName of [
    "minni_thread_create",
    "minni_thread_status",
    "minni_thread_update",
    "minni_thread_scar",
  ]) {
    const start = source.indexOf(`"${toolName}"`);
    assert.notEqual(start, -1, `${toolName} registration not found`);
    const nextTool = source.indexOf("server.registerTool(", start + 1);
    const block = source.slice(start, nextTool === -1 ? undefined : nextTool);
    const schema = block.slice(block.indexOf("inputSchema:"), block.indexOf("async"));
    assert.doesNotMatch(schema, /\b(?:vaultPath|vault_path|notePath|note_path)\s*:/);
    if (toolName === "minni_thread_update") {
      assert.match(schema, /\bactivate\s*:\s*z\.boolean\(\)\.optional\(\)/);
      assert.match(schema, /\bcompact\s*:\s*z\.boolean\(\)\.optional\(\)/);
    }
    if (toolName === "minni_thread_scar") {
      assert.match(schema, /\bslice_id\s*:[\s\S]*?\.optional\(\)/);
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

test("minni_list_candidates carries server principal without model-facing identity spoofing", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const start = source.indexOf('"minni_list_candidates"');
  assert.notEqual(start, -1, "minni_list_candidates tool registration not found");
  const nextTool = source.indexOf("server.registerTool(", start + 1);
  const block = source.slice(start, nextTool === -1 ? undefined : nextTool);

  const schemaStart = block.indexOf("inputSchema:");
  const handlerStart = block.indexOf("async");
  const schema = block.slice(schemaStart, handlerStart);
  assert.doesNotMatch(schema, /\bagent(?:_id|Id)?\s*:/, "model-facing schema must not accept caller identity");
  assert.match(block, /agent_id:\s*DEFAULT_AGENT_ID/, "list RPC must stamp the configured server principal");
  assert.match(block, /list_candidates/, "list tool must call the daemon list_candidates method");
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

test("minni_thread_create schema exposes shelf_ref so a passed shelf actually configures drift checking (punch-list §4b)", async () => {
  const source = stripLineComments(
    await readFile(new URL("../src/server.ts", import.meta.url), "utf8"),
  );
  const start = source.indexOf('"minni_thread_create"');
  assert.notEqual(start, -1, "minni_thread_create tool registration not found");
  const nextTool = source.indexOf("server.registerTool(", start + 1);
  const block = source.slice(start, nextTool === -1 ? undefined : nextTool);

  const schemaStart = block.indexOf("inputSchema:");
  const handlerStart = block.indexOf("async");
  const schema = block.slice(schemaStart, handlerStart);
  assert.match(schema, /shelf_ref\s*:/, "minni_thread_create must expose shelf_ref in its inputSchema");
  assert.match(schema, /shelf_ref[\s\S]*?\.optional\(\)/, "shelf_ref must be optional (additive, back-compat schema evolution)");

  // The handler must actually thread shelf_ref through to createPlan, not just
  // declare it — otherwise shelfDrift() keeps reporting configured:false.
  const handlerBlock = block.slice(handlerStart);
  assert.match(handlerBlock, /shelf_ref/, "handler must pass shelf_ref through to createPlan");
});

test("Codex hook remains Codex-native instead of reusing Claude hook entrypoint", async () => {
  const codexHook = await readFile(new URL("../src/codex-hook.ts", import.meta.url), "utf8");
  assert.match(codexHook, /runtime:\s*"codex"/);
  assert.match(codexHook, /hookScript:\s*"codex-hook\.js"/);
  assert.match(codexHook, /agentId:\s*CODEX_AGENT_ID/);
  assert.match(codexHook, /vaultPath:\s*CODEX_VAULT_PATH/);
  // `alwaysWriteStopInbox` was retired: Stop's zero-candidate skip is now
  // unconditional, so re-introducing a per-agent always-write knob here would
  // silently restore the empty-inbox litter issue #173 removed.
  assert.doesNotMatch(codexHook, /alwaysWriteStopInbox/);
  assert.doesNotMatch(codexHook, /DEFAULT_AGENT_ID|DEFAULT_VAULT_PATH|DEFAULT_WORKSPACE_ID/);
  assert.doesNotMatch(codexHook, /CLAUDECODE_AGENT_ID|CLAUDECODE_VAULT_PATH|hookScript:\s*"hook\.js"/);

  const configSrc = await readFile(new URL("../src/config.ts", import.meta.url), "utf8");
  const codexWorkspaceBlock = configSrc.match(
    /export const CODEX_WORKSPACE_ID =[\s\S]*?;\n/,
  )?.[0];
  assert.ok(codexWorkspaceBlock, "CODEX_WORKSPACE_ID export must exist");
  assert.match(codexWorkspaceBlock, /"workspace-unknown"/);
  assert.doesNotMatch(codexWorkspaceBlock, /process\.cwd/);
});
