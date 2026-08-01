import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";

import { assessLearningQuality, assessLearningQualityAsync, auditSafeTitle } from "../dist/policy.js";

const execFileAsync = promisify(execFile);

// Follow-ups from the PR #245 review. Fixtures are split-and-joined so
// file-level scanners do not flag this test file.
const j = (...parts) => parts.join("");
const FAKE_PAT = j("ghp_", "AbCdEf0123456789AbCdEf0123456789AbCd");

const CLEAN_CONTENT =
  "The staging publisher credential is rotated quarterly by the on-call engineer and recorded in the incident log.";

// Blocking the vault write while writing the same secret to the audit log
// would move the leak, not close it. The title is the channel most likely to
// carry it, because a title is what the audit summary is built from.
test("a blocked learning's title is withheld from the audit summary", () => {
  const report = assessLearningQuality({
    title: `Rotation note ${FAKE_PAT}`,
    content: CLEAN_CONTENT,
  });
  assert.equal(report.ok, false, "control: the title must be blocked");

  const label = auditSafeTitle(`Rotation note ${FAKE_PAT}`, report);
  assert.ok(!label.includes(FAKE_PAT), `audit label must not carry the credential: ${label}`);
  assert.match(label, /withheld/);
});

test("a clean learning's title passes through the audit summary unchanged", () => {
  const title = "Rotation runbook for the staging publisher";
  const report = assessLearningQuality({ title, content: CLEAN_CONTENT });
  assert.equal(report.ok, true, report.summary);
  assert.equal(auditSafeTitle(title, report), title);
});

// The AFM tier reported "A persisted field" without saying which one, while
// the regex tier named it. An operator reading the block needs to know where
// to look.
test("an AFM credential block names the channel that carried the span", async () => {
  const report = await assessLearningQualityAsync(
    {
      title: "Runbook password: correct horse battery staple",
      content: CLEAN_CONTENT,
    },
    { classifyInconclusive: async () => "credential" },
  );
  assert.equal(report.ok, false, report.summary);
  const warning = report.warnings.find((w) => w.includes("sensitive material"));
  assert.ok(warning.includes('"title"'), `the AFM warning must name the channel: ${warning}`);
});

// `minni learn` wrote straight to the vault with no gate at all, so the scan
// that guards minni_learn did not guard the CLI.
test("the CLI learn command refuses to persist a credential", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-cli-learn-gate-"));
  const cliPath = new URL("../dist/cli.js", import.meta.url).pathname;
  const env = {
    ...process.env,
    MINNI_HOME: root,
    MINNI_VAULT_PATH: root,
    MINNI_CLAUDECODE_VAULT_PATH: root,
  };
  const run = (args) =>
    execFileAsync(process.execPath, [cliPath, ...args], { env }).catch((error) => error);

  try {
    const blocked = await run(["learn", `Rotation note ${FAKE_PAT}`, CLEAN_CONTENT]);
    const out = JSON.parse(blocked.stdout);
    assert.equal(out.status, "quality-blocked", `CLI learn must block a credential: ${blocked.stdout}`);
    assert.ok(!blocked.stdout.includes(FAKE_PAT) || out.quality, "block must report a quality report");

    const sessions = await readdir(path.join(root, "wiki", "sessions")).catch(() => []);
    assert.equal(sessions.length, 0, `blocked CLI learn must write no vault note: ${sessions.join(", ")}`);

    // A clean note still writes — the CLI gate blocks material, not quality.
    const ok = await run(["learn", "Rotation runbook for the staging publisher", CLEAN_CONTENT]);
    const wrote = JSON.parse(ok.stdout);
    assert.ok(wrote.notePath ?? wrote.path ?? wrote.note, `clean CLI learn must still write: ${ok.stdout}`);

    // Nothing anywhere under the vault may contain the credential.
    const walk = async (dir) => {
      const entries = await readdir(dir, { withFileTypes: true }).catch(() => []);
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) await walk(full);
        else {
          const body = await readFile(full, "utf8").catch(() => "");
          assert.ok(!body.includes(FAKE_PAT), `credential reached ${full}`);
        }
      }
    };
    await walk(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
