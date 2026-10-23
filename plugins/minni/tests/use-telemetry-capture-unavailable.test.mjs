// Recall-to-use S1a-2 review F5: the capture module is loaded with a dynamic
// import. If that import fails (a stripped or half-updated install), the hook
// must run exactly as with the ledger off, and say so once, by code: never a
// degraded hook, and never the loader's message (it names an install path).
import assert from "node:assert/strict";
import { cp, mkdtemp, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { auditTail } from "../dist/vault.js";
import {
  DEFAULT_DIST,
  PLUGIN_ROOT,
  daemonResult,
  exists,
  makeFixture,
  runHook,
  runOutputScenarios,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

/** A copy of dist with use-capture.js removed, resolvable like the real one. */
async function strippedDist(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-stripped-dist-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dist = path.join(root, "dist");
  await cp(DEFAULT_DIST, dist, { recursive: true });
  await unlink(path.join(dist, "use-capture.js"));
  await writeFile(path.join(root, "package.json"), '{"type":"module"}\n', "utf8");
  await symlink(path.join(PLUGIN_ROOT, "node_modules"), path.join(root, "node_modules"));
  return dist;
}

test("use-capture.js missing, ledger on: every scenario's output equals ledger off", { timeout: 240_000 }, async (t) => {
  const dist = await strippedDist(t);
  const root = path.join(tmpdir(), `minni-capture-unavailable-${process.pid}`);
  t.after(() => rm(root, { recursive: true, force: true }));
  const on = await runOutputScenarios({ root, dist, env: { MINNI_USE_LEDGER: "on" } });
  const off = await runOutputScenarios({ root, dist, env: { MINNI_USE_LEDGER: "off" } });
  for (const name of Object.keys(off)) {
    assert.equal(on[name].code, 0, name);
    assert.doesNotMatch(on[name].raw, /Minni hook degraded/, `${name} must not degrade`);
    assert.equal(on[name].raw, off[name].raw, `${name}: output differs from ledger off`);
    assert.equal(on[name].rows, 0, `${name}: nothing can be recorded`);
  }
});

test("use-capture.js missing: one audit line by code, no path, and no capture files", { timeout: 60_000 }, async (t) => {
  const dist = await strippedDist(t);
  const fixture = await makeFixture(t, "capture-unavailable-audit");
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results: [daemonResult({ source: "/e/a.md", docId: 1, wikilink: "[[a]]", title: "A" })] }),
  });
  const { output, code } = await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: "unavailable", prompt: "where is the alpha note" },
    { dist },
  );
  assert.equal(code, 0);
  assert.equal(output.continue, true);
  assert.doesNotMatch(JSON.stringify(output), /degraded|Cannot find module/);
  const { entries } = await auditTail(fixture.vault, 50);
  const notes = entries.filter((entry) => entry.includes("hook_use_capture_unavailable"));
  assert.equal(notes.length, 1, JSON.stringify(entries));
  assert.ok(!notes[0].includes(dist) && !notes[0].includes("use-capture.js"), `no path in the audit: ${notes[0]}`);
  assert.ok(!(await exists(path.join(fixture.vault, ".runtime", "use-ledger"))));
  assert.ok(!(await exists(path.join(fixture.vault, ".runtime", "recall-session.d"))));
});
