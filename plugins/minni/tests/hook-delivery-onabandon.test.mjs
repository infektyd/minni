// Recall-to-use S1a (§4.3): deliver first, record second — and record the
// abandonment too.
//
// Surfacing rows are registered with `deferUntilDelivered` so a row claiming
// "the model saw this" is written only once the output reached the host. But
// an output that never lands is ALSO a fact the ledger needs: without it a
// dropped boot envelope looks like a turn with nothing surfaced. So a deferral
// may carry an `onAbandon` handler that runs exactly when its commit is thrown
// away — on a failed flush, on a wire drop, and on the `discardDeliveryCommits`
// path `runHookMain` takes when a handler throws.
//
// `emitAndCommit` writes to stdout, so the emit cases run in a child process
// (as in sessionstart-delivery.test.mjs); the bare discard path runs in-process.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const DELIVERY_URL = pathToFileURL(path.join(PLUGIN_ROOT, "dist", "hook-delivery.js")).href;

// The child: register deferrals per MODE, settle them the way a hook does, and
// journal every handler that ran to RESULT (synchronously, so the journal
// survives the process.exit inside failAndExit).
const CHILD = `
import { appendFileSync } from "node:fs";
const d = await import(process.env.DELIVERY_URL);
const RESULT = process.env.RESULT;
const mark = (what) => appendFileSync(RESULT, what + "\\n");
const ctx = { vaultPath: process.env.VAULT, auditPrefix: "test", event: "SessionStart" };
const mode = process.env.MODE;

if (mode === "legacy-drop" || mode === "legacy-flush") {
  d.deferUntilDelivered(async () => mark("commit"));
  if (mode === "legacy-drop") d.markOutputDropped();
  const delivered = await d.emitAndCommit({ continue: true }, ctx);
  mark("delivered=" + delivered + " pending=" + d.pendingDeliveryCommitCount());
  d.exitAfterDelivery();
}

d.deferUntilDelivered(async () => mark("commit"), { onAbandon: () => mark("abandon") });

if (mode === "drop") {
  d.deferUntilDelivered(async () => mark("commit2"), {
    onAbandon: () => { throw new Error("sync abandon failure"); },
  });
  d.deferUntilDelivered(async () => mark("commit3"), {
    onAbandon: async () => { throw new Error("async abandon failure"); },
  });
  d.deferUntilDelivered(async () => mark("commit4"), { onAbandon: () => mark("abandon4") });
  d.markOutputDropped();
}
if (mode === "epipe") {
  // Wait until the parent has closed our stdout, then try to deliver.
  await new Promise((resolve) => { process.stdin.on("data", () => {}); process.stdin.on("end", resolve); });
}
if (mode === "throw-path") {
  // runHookMain's catch: discard, audit, then failAndExit (which discards again).
  d.discardDeliveryCommits();
  mark("after-discard");
  await d.failAndExit({ continue: true, systemMessage: "degraded" });
}
if (mode === "throw-path-async") {
  d.deferUntilDelivered(async () => mark("commit-late"), {
    onAbandon: async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
      mark("abandon-late");
    },
  });
  d.discardDeliveryCommits();
  await d.failAndExit({ continue: true, systemMessage: "degraded" });
}
const delivered = await d.emitAndCommit({ continue: true }, ctx);
mark("delivered=" + delivered);
d.exitAfterDelivery();
`;

async function makeFixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-onabandon-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const fixture = {
    root,
    vault: path.join(root, "vault"),
    home: path.join(root, "home"),
    script: path.join(root, "child.mjs"),
    result: path.join(root, "result.txt"),
  };
  await mkdir(fixture.vault, { recursive: true });
  await mkdir(fixture.home, { recursive: true });
  await writeFile(fixture.script, CHILD, "utf8");
  await writeFile(fixture.result, "", "utf8");
  return fixture;
}

function runChild(fixture, mode, { closeStdout = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fixture.script], {
      env: {
        PATH: process.env.PATH,
        HOME: fixture.home,
        MINNI_HOME: fixture.home,
        DELIVERY_URL,
        RESULT: fixture.result,
        VAULT: fixture.vault,
        MODE: mode,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", async (code) => {
      const journal = (await readFile(fixture.result, "utf8")).split("\n").filter(Boolean);
      resolve({ code, stdout, stderr, journal });
    });
    if (closeStdout) child.stdout.destroy();
    child.stdin.end();
  });
}

test("on a dropped output, onAbandon runs and the commits do not", async (t) => {
  const fixture = await makeFixture(t);
  const { code, journal, stderr } = await runChild(fixture, "drop");
  assert.equal(code, 0, stderr);
  assert.ok(journal.includes("abandon"), `onAbandon must run: ${journal}`);
  assert.ok(journal.includes("abandon4"), "a throwing handler does not stop the ones after it");
  assert.ok(!journal.some((line) => line.startsWith("commit")), "no commit may run for a dropped output");
  assert.ok(journal.includes("delivered=false"));
});

test("on a failed flush (EPIPE), onAbandon runs and the commit does not", async (t) => {
  const fixture = await makeFixture(t);
  const { code, journal, stderr } = await runChild(fixture, "epipe", { closeStdout: true });
  assert.equal(code, 0, stderr);
  assert.deepEqual(journal, ["abandon", "delivered=false"]);
});

test("on a delivered output, the commit runs and onAbandon does not", async (t) => {
  const fixture = await makeFixture(t);
  const { code, journal, stdout, stderr } = await runChild(fixture, "flush");
  assert.equal(code, 0, stderr);
  assert.deepEqual(journal, ["commit", "delivered=true"]);
  assert.deepEqual(JSON.parse(stdout.trim()), { continue: true });
});

test("the throw path (discard, then failAndExit) runs onAbandon exactly once", async (t) => {
  const fixture = await makeFixture(t);
  const { code, journal, stderr } = await runChild(fixture, "throw-path");
  assert.equal(code, 0, stderr);
  assert.deepEqual(journal, ["abandon", "after-discard"], "abandoned once, synchronously, never committed");
});

test("failAndExit lets an async onAbandon finish before the process exits", async (t) => {
  const fixture = await makeFixture(t);
  const { code, journal, stderr } = await runChild(fixture, "throw-path-async");
  assert.equal(code, 0, stderr);
  assert.ok(journal.includes("abandon"));
  assert.ok(journal.includes("abandon-late"), `the async handler was cut off by exit: ${journal}`);
  assert.ok(!journal.some((line) => line.startsWith("commit")));
});

test("discardDeliveryCommits runs onAbandon in-process and never throws", async (t) => {
  const { deferUntilDelivered, discardDeliveryCommits, pendingDeliveryCommitCount, runDeliveryCommits } =
    await import("../dist/hook-delivery.js");
  t.after(() => discardDeliveryCommits());
  discardDeliveryCommits();

  const ran = [];
  deferUntilDelivered(async () => void ran.push("commit"), { onAbandon: () => void ran.push("abandon") });
  deferUntilDelivered(async () => void ran.push("commit-b"), {
    onAbandon: () => {
      throw new Error("boom");
    },
  });
  deferUntilDelivered(async () => void ran.push("commit-c"), { onAbandon: () => void ran.push("abandon-c") });
  assert.equal(pendingDeliveryCommitCount(), 3);

  assert.doesNotThrow(() => discardDeliveryCommits());
  assert.deepEqual(ran, ["abandon", "abandon-c"]);
  assert.equal(pendingDeliveryCommitCount(), 0);

  // Abandoned work is gone: neither a later commit pass nor a second discard re-runs it.
  await runDeliveryCommits();
  discardDeliveryCommits();
  assert.deepEqual(ran, ["abandon", "abandon-c"]);
});

test("a delivered commit pass never runs onAbandon later", async (t) => {
  const { deferUntilDelivered, discardDeliveryCommits, runDeliveryCommits } = await import(
    "../dist/hook-delivery.js"
  );
  t.after(() => discardDeliveryCommits());
  discardDeliveryCommits();

  const ran = [];
  deferUntilDelivered(async () => void ran.push("commit"), { onAbandon: () => void ran.push("abandon") });
  await runDeliveryCommits();
  discardDeliveryCommits();
  assert.deepEqual(ran, ["commit"]);
});

test("existing callers (no options) behave exactly as before", async (t) => {
  const dropped = await makeFixture(t);
  const drop = await runChild(dropped, "legacy-drop");
  assert.equal(drop.code, 0, drop.stderr);
  assert.deepEqual(drop.journal, ["delivered=false pending=0"], "a dropped output discards the commit");

  const flushed = await makeFixture(t);
  const flush = await runChild(flushed, "legacy-flush");
  assert.equal(flush.code, 0, flush.stderr);
  assert.deepEqual(flush.journal, ["commit", "delivered=true pending=0"]);

  const { deferUntilDelivered, discardDeliveryCommits, pendingDeliveryCommitCount, runDeliveryCommits } =
    await import("../dist/hook-delivery.js");
  t.after(() => discardDeliveryCommits());
  discardDeliveryCommits();
  const ran = [];
  deferUntilDelivered(async () => void ran.push("first"));
  deferUntilDelivered(async () => void ran.push("second"));
  assert.equal(pendingDeliveryCommitCount(), 2);
  await runDeliveryCommits();
  assert.deepEqual(ran, ["first", "second"]);
  deferUntilDelivered(async () => void ran.push("never"));
  discardDeliveryCommits();
  await runDeliveryCommits();
  assert.deepEqual(ran, ["first", "second"]);
});
