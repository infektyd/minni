import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildPreToolUseMatcher,
  CORE_SCOPE_TOOLS,
  PRE_TOOL_USE_MATCHER,
  SHELL_SCOPE_TOOLS,
  SUBAGENT_SPAWN_TOOLS,
} from "../dist/recall-guard.js";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOKS_JSON_PATH = path.join(PLUGIN_ROOT, "hooks", "hooks.json");

test("hooks/hooks.json PreToolUse matcher equals buildPreToolUseMatcher() and PRE_TOOL_USE_MATCHER", () => {
  const manifest = JSON.parse(fs.readFileSync(HOOKS_JSON_PATH, "utf8"));
  const matcher = manifest?.hooks?.PreToolUse?.[0]?.matcher;
  assert.equal(typeof matcher, "string", "hooks.PreToolUse[0].matcher must be a string");
  assert.equal(matcher, PRE_TOOL_USE_MATCHER);
  assert.equal(matcher, buildPreToolUseMatcher());

  // Also verify SubagentStart and SubagentStop are registered in hooks.json
  assert.ok(Array.isArray(manifest?.hooks?.SubagentStart), "SubagentStart must be registered in hooks.json");
  assert.ok(Array.isArray(manifest?.hooks?.SubagentStop), "SubagentStop must be registered in hooks.json");
});

test("PreToolUse matcher regex matches every core scope, shell scope, subagent spawn, and consult tool", () => {
  const re = new RegExp(PRE_TOOL_USE_MATCHER);

  for (const tool of CORE_SCOPE_TOOLS) {
    assert.equal(re.test(tool), true, `CORE_SCOPE_TOOLS member '${tool}' must match`);
  }
  for (const tool of SHELL_SCOPE_TOOLS) {
    assert.equal(re.test(tool), true, `SHELL_SCOPE_TOOLS member '${tool}' must match`);
  }
  for (const tool of SUBAGENT_SPAWN_TOOLS) {
    assert.equal(re.test(tool), true, `SUBAGENT_SPAWN_TOOLS member '${tool}' must match`);
  }

  const consultNames = [
    "minni_recall",
    "minni_drill",
    "mcp__minni__minni_recall",
    "mcp__minni__minni_drill",
    "mcp__plugin_minni_minni__minni_recall",
    "mcp__plugin_minni_minni__minni_drill",
    "mcp__plugin_sovereign-memory_minni__minni_recall",
  ];
  for (const tool of consultNames) {
    assert.equal(re.test(tool), true, `consult tool '${tool}' must match`);
  }
});

test("PreToolUse matcher regex excludes unrelated tools so PreToolUse skips spawning on them", () => {
  const re = new RegExp(PRE_TOOL_USE_MATCHER);
  const unrelatedTools = [
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "ExitPlanMode",
    "mcp__minni__minni_learn",
    "mcp__minni__minni_status",
    "minni_learn",
  ];
  for (const tool of unrelatedTools) {
    assert.equal(re.test(tool), false, `unrelated tool '${tool}' must NOT match PreToolUse matcher`);
  }
});
