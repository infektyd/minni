// Recall-to-use S1a-2 (§4.3): PreCompact, PostCompact and SessionStart (any
// source) reset the context of the agent that sent them. Each writes
// `.runtime/recall-session.d/<session_key>/<agent_key>.reset` with a fresh
// `reset_id`, and appends the matching `reset` row, immediately. A subagent's
// payload resets only that subagent, never `main`.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  agentKeyOf,
  exists,
  makeFixture,
  readLedger,
  runHook,
  sessionKeyOf,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

function resetPath(vault, sessionId, agentKey) {
  return path.join(vault, ".runtime", "recall-session.d", sessionKeyOf(sessionId), `${agentKey}.reset`);
}

test("PreCompact, PostCompact and SessionStart each write a reset file and row for main", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "reset-main");
  await startFakeDaemon(fixture);
  const session = "reset-session";
  const file = resetPath(fixture.vault, session, "main");
  const seen = [];
  const steps = [
    ["PreCompact", { session_id: session, trigger: "auto" }],
    ["PostCompact", { session_id: session, trigger: "auto", compact_summary: "" }],
    ["SessionStart", { session_id: session, source: "compact" }],
    ["SessionStart", { session_id: session, source: "resume" }],
  ];
  for (const [event, payload] of steps) {
    const { code } = await runHook(fixture, event, payload);
    assert.equal(code, 0);
    assert.ok(await exists(file), `${event} wrote main.reset`);
    const marker = JSON.parse(await readFile(file, "utf8"));
    assert.deepEqual(Object.keys(marker).sort(), ["at", "reason", "reset_id"]);
    assert.match(marker.reset_id, /^[0-9a-f]{16}$/);
    assert.ok(Number.isFinite(Date.parse(marker.at)));
    assert.ok(!seen.includes(marker.reset_id), "every reset gets a fresh reset_id");
    seen.push(marker.reset_id);

    const resets = (await readLedger(fixture.vault)).filter((row) => row.kind === "reset");
    const last = resets.at(-1);
    assert.equal(resets.length, seen.length, "one reset row per resetting event");
    assert.equal(last.event, event);
    assert.equal(last.reset_id, marker.reset_id, "the row and the file name the same reset");
    assert.equal(last.agent_key, "main");
    assert.equal(last.session_key, sessionKeyOf(session));
  }
  const reasons = (await readLedger(fixture.vault)).filter((row) => row.kind === "reset").map((row) => row.event);
  assert.deepEqual(reasons, ["PreCompact", "PostCompact", "SessionStart", "SessionStart"]);
});

test("a subagent payload resets its own agent_key and never writes main.reset", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "reset-subagent");
  await startFakeDaemon(fixture);
  const session = "reset-subagent-session";
  const agentId = "agent-abc123";
  await runHook(fixture, "PreCompact", { session_id: session, agent_id: agentId, agent_type: "Explore", trigger: "auto" });
  assert.ok(await exists(resetPath(fixture.vault, session, agentKeyOf(agentId))), "the subagent's own reset file");
  assert.ok(!(await exists(resetPath(fixture.vault, session, "main"))), "main.reset must not be written");
  const resets = (await readLedger(fixture.vault)).filter((row) => row.kind === "reset");
  assert.equal(resets.length, 1);
  assert.equal(resets[0].agent_key, agentKeyOf(agentId));
  assert.equal(resets[0].subagent_type, "Explore");
});
