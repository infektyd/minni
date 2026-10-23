// Recall-to-use S1a-2: on a wire that cannot carry an event's injection (Grok
// Build injects only at Stop), what the hook would have surfaced is recorded as
// abandoned (`delivered: false, visible: false`), and the ledger's deferred
// rows are NOT counted as rollbacks: the undelivered audit stays exactly as it
// is today, with `_intent_dropped` the one record of the drop.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { auditTail } from "../dist/vault.js";
import { daemonResult, makeFixture, readLedger, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

function grokEnv(fixture) {
  return { MINNI_GROK_VAULT_PATH: fixture.vault, MINNI_GROK_AGENT_ID: "grok-build" };
}

test("grok SessionStart and a strong UPS: rows abandoned, no undelivered rollback audit", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "dropped-wire");
  fixture.vault = path.join(fixture.root, "grok-vault");
  await mkdir(fixture.vault, { recursive: true });
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "grok-build",
      results: [daemonResult({ source: "/elsewhere/deploy.md", docId: 4, wikilink: "[[deploy]]", title: "Deploy" })],
    }),
  });
  const boot = await runHook(fixture, "SessionStart", { session_id: "grok-s", source: "startup" }, {
    bin: "grok-hook.js",
    env: grokEnv(fixture),
  });
  assert.equal(boot.code, 0);
  const ups = await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: "grok-s", prompt: "where is the deploy procedure" },
    { bin: "grok-hook.js", env: grokEnv(fixture) },
  );
  assert.equal(ups.code, 0);

  const surfaced = (await readLedger(fixture.vault)).filter((row) => row.kind === "surfaced");
  const channels = surfaced.map((row) => row.channel).sort();
  assert.deepEqual(channels, ["boot_recall", "prompt_pointer"]);
  for (const row of surfaced) {
    assert.equal(row.delivered, false, `${row.channel} never reached the model`);
    assert.equal(row.visible, false);
    assert.equal(row.wire, "grok-build");
  }

  const audit = (await auditTail(fixture.vault, 50)).text;
  assert.match(audit, /hook_grok_intent_dropped/, "the drop is recorded where it always was");
  assert.ok(!/hook_grok_undelivered/.test(audit), "ledger rows are not rollbacks: no second, double-counting row");
});
