// Recall-to-use S1b (§4.5, §4.6): the use report.
//
// auditReport() gains a `use` section (minni_audit_report's `useDays`),
// labelled with the principal it measures; `node dist/cli.js use-report`
// prints the same report. Contents: counts by outcome, used and used_ii per
// visible exposure (and per surfacing, as diagnostics) by channel and agent,
// used_ii credits per 100 human prompt turns, top-10 lists, the provisional,
// unknown and unnameable shares, the features in effect, a coverage table in
// which every other wire is `unmeasured`, and the fixed lower-bound line.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  appendLedgerRows,
  buildExposureRow,
  buildJudgedRow,
  buildSurfacedRow,
  buildTurnRow,
  sessionKeyFrom,
} from "../dist/use-ledger.js";
import { auditReport } from "../dist/vault.js";
import { useStatusBlock } from "../dist/use-report.js";
import { makeVault } from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const run = promisify(execFile);
const CLI = fileURLToPath(new URL("../dist/cli.js", import.meta.url));
const NOTE = "lower bounds; silent use is invisible";
const HOUR = 60 * 60 * 1000;

function context(overrides = {}) {
  return {
    session_key: sessionKeyFrom({ session_id: "synthetic-report-session" }),
    agent_key: "main",
    wire: "claude-code",
    principal: "claude-code",
    features: ["MINNI_USE_LEDGER", "MINNI_UPS_RANK_V2"],
    ...overrides,
  };
}

/** A small, fully-judged week: 4 human turns (+1 skip), 3 visible surfacings, 2 exposures. */
function seed(vault, base = Date.now() - 3 * HOUR) {
  const at = (minutes) => new Date(base + minutes * 60_000).toISOString();
  const main = context();
  const sub = context({ agent_key: "a-0123456789ab", subagent_type: "Explore" });
  const rows = [];
  for (let ix = 1; ix <= 4; ix += 1) {
    rows.push(buildTurnRow({ ...main, prompt_id: `p${ix}` }, { turn_ix: ix, envelope_kind: "human", subkind: null, exit: "strong" }, at(ix * 10)));
  }
  rows.push(buildTurnRow({ ...main, prompt_id: "p5" }, { turn_ix: 5, envelope_kind: "skip", subkind: "task_notification", exit: "skip" }, at(55)));

  const cited = buildSurfacedRow(main, { channel: "prompt_pointer", mkey: "vault:wiki/a.md", title: "Alpha cadence", rank: 0, visible: true, delivered: true, turn_ix: 1 }, at(11));
  const dismissed = buildSurfacedRow(main, { channel: "guard", mkey: "vault:wiki/b.md", title: "Beta harbor", rank: 0, visible: true, delivered: true, turn_ix: 2 }, at(21));
  const applied = buildSurfacedRow(sub, { channel: "subagent_brief", mkey: "vault:wiki/c.md", title: "Gamma drain", rank: 0, visible: true, delivered: true }, at(31));
  const hidden = buildSurfacedRow(main, { channel: "prompt_pointer", mkey: "vault:wiki/d.md", title: "Delta hidden", rank: 1, visible: false, delivered: true, turn_ix: 1 }, at(11));
  rows.push(cited, dismissed, applied, hidden);

  const judge = (ctx, row, outcome, evidence, final = true) =>
    buildJudgedRow(ctx, { sid: row.sid, outcome, via: "transcript", evidence, title_assessable: true, final, rule_version: "s1b-1" }, at(40));
  rows.push(
    judge(main, cited, "cited", ["title"]),
    judge(main, dismissed, "dismissed", ["lexicon"]),
    judge(sub, applied, "applied", ["shingle"], false),
    judge(main, hidden, "unknown(not_visible)", []),
  );

  rows.push(
    buildExposureRow(main, { reset_id: "initial", mkey: "vault:wiki/a.md", first_sid: cited.sid, channel: "prompt_pointer", visible: true, outcome: "cited", credits: [2], final: true }, at(60)),
    buildExposureRow(main, { reset_id: "initial", mkey: "vault:wiki/b.md", first_sid: dismissed.sid, channel: "guard", visible: true, outcome: "dismissed", credits: [], final: true }, at(60)),
  );

  // Another wire: counted in the coverage table only.
  const codex = context({ session_key: sessionKeyFrom({ session_id: "synthetic-codex-session" }), wire: "codex", principal: "codex" });
  rows.push(
    buildTurnRow(codex, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "strong" }, at(15)),
    buildSurfacedRow(codex, { channel: "guard", mkey: "vault:wiki/a.md", title: "Alpha cadence", rank: 0, visible: true, delivered: true }, at(16)),
  );
  const written = appendLedgerRows(vault, rows);
  assert.equal(written.written, rows.length);
  return { cited, dismissed, applied, hidden };
}

function checkReport(use) {
  assert.equal(use.principal, "claude-code", "labelled with the principal it measures");
  assert.equal(use.note, NOTE);
  assert.equal(use.turns.human, 4, "skip turns are not human prompt turns");

  assert.equal(use.surfacings.total, 4);
  assert.equal(use.surfacings.visible, 3);
  assert.deepEqual(use.surfacings.by_outcome, { applied: 1, cited: 1, dismissed: 1, "unknown(not_visible)": 1 });

  assert.equal(use.exposures.visible, 2);
  assert.equal(use.exposures.used, 1);
  assert.equal(use.exposures.used_ii, 1);
  assert.equal(use.exposures.used_ii_per_visible, 0.5);
  assert.equal(use.exposures.by_channel.prompt_pointer.used_ii, 1);
  assert.equal(use.exposures.by_channel.guard.used_ii, 0);
  assert.equal(use.used_ii_credits_per_100_human_turns, 25, "1 credit over 4 human turns");

  assert.equal(use.surfacings.by_agent.main.visible, 2);
  assert.equal(use.surfacings.by_agent.subagents.Explore.visible, 1);
  assert.equal(use.provisional_share, 0.25, "the subagent's judgment is still provisional");
  assert.equal(use.unknown_share, 0.25);

  assert.ok(use.top_used.some((entry) => entry.mkey === "vault:wiki/a.md"));
  assert.ok(use.top_unused.some((entry) => entry.mkey === "vault:wiki/b.md"));
  assert.ok(use.features.some((entry) => entry.features.includes("MINNI_UPS_RANK_V2")));
  const codex = use.coverage.find((entry) => entry.wire === "codex");
  assert.equal(codex?.status, "unmeasured");
  assert.equal(use.coverage.find((entry) => entry.wire === "claude-code")?.status, "measured");
}

test("auditReport with useDays carries a `use` section labelled with the principal", async (t) => {
  const { vault } = await makeVault(t, "minni-use-report-");
  seed(vault);
  const report = await auditReport(vault, 100, { useDays: 7 });
  assert.ok(report.use, "a use section");
  assert.equal(report.use.days, 7);
  checkReport(report.use);

  const plain = await auditReport(vault, 100);
  assert.equal("use" in plain, false, "without useDays the report is unchanged");
});

test("rows outside the window are not counted", async (t) => {
  const { vault } = await makeVault(t, "minni-use-report-old-");
  seed(vault, Date.now() - 30 * 24 * HOUR);
  const report = await auditReport(vault, 100, { useDays: 7 });
  assert.equal(report.use.surfacings.total, 0);
  assert.equal(report.use.used_ii_credits_per_100_human_turns, null, "no human turns: no rate");
  assert.equal(report.use.note, NOTE);
});

test("node dist/cli.js use-report --days N, --memory and --window", async (t) => {
  const { root, vault } = await makeVault(t, "minni-use-report-cli-");
  seed(vault);
  const env = { ...process.env, MINNI_VAULT_PATH: vault, MINNI_HOME: root, MINNI_SOCKET_PATH: path.join(root, "absent.sock") };

  const { stdout } = await run(process.execPath, [CLI, "use-report", "--days", "7"], { env });
  const use = JSON.parse(stdout);
  assert.equal(use.days, 7);
  checkReport(use);

  const one = JSON.parse((await run(process.execPath, [CLI, "use-report", "--days", "7", "--memory", "vault:wiki/a.md"], { env })).stdout);
  assert.equal(one.memory.mkey, "vault:wiki/a.md");
  assert.equal(one.surfacings.total, 1, "only that memory's surfacings");

  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  const since = new Date(Date.now() - 24 * HOUR).toISOString();
  await writeFile(path.join(vault, ".runtime", "use-windows.json"), JSON.stringify({ before: { since } }), "utf8");
  const before = JSON.parse((await run(process.execPath, [CLI, "use-report", "--window", "before"], { env })).stdout);
  assert.equal(before.window, "before");
  assert.equal(before.since, since);
  assert.equal(before.surfacings.total, 4);

  await assert.rejects(run(process.execPath, [CLI, "use-report", "--window", "after"], { env }), /no "after" window/);
});

test("handle citations are their own column; exposure dismissals are counted; the undercount caveat is fixed", async (t) => {
  const { vault } = await makeVault(t, "minni-use-report-handle-");
  const main = context();
  const at = (minutes) => new Date(Date.now() - 2 * HOUR + minutes * 60_000).toISOString();
  const handleCited = buildSurfacedRow(main, { channel: "prompt_pointer", mkey: "vault:wiki/h.md", title: "Handle note", rank: 0, visible: true, delivered: true, turn_ix: 1 }, at(1));
  const rows = [
    buildTurnRow({ ...main, prompt_id: "p1" }, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "strong" }, at(0)),
    handleCited,
    buildJudgedRow(main, { sid: handleCited.sid, outcome: "cited_handle", via: "transcript", evidence: ["handle"], title_assessable: true, final: true, rule_version: "s1b-2" }, at(2)),
    buildExposureRow(main, { reset_id: "initial", mkey: "vault:wiki/h.md", first_sid: handleCited.sid, channel: "prompt_pointer", visible: true, outcome: "cited_handle", credits: [], final: true, dismissed_turns: 1 }, at(3)),
  ];
  assert.equal(appendLedgerRows(vault, rows).written, rows.length);
  const { use } = await auditReport(vault, 100, { useDays: 7 });
  assert.equal(use.surfacings.cited_handle, 1);
  assert.equal(use.surfacings.used_ii, 0, "a handle citation is never used_ii");
  assert.equal(use.exposures.cited_handle, 1);
  assert.equal(use.exposures.used_ii, 0);
  assert.equal(use.exposures.dismissed, 1);
  assert.equal(
    use.caveat,
    "Known undercount: a short rule applied by quoting it word for word reads as recital and is not counted; this affects both windows equally, and the used rate is a floor.",
  );
});

test("the title_assessable share is reported per channel and overall, in the report and the status block", async (t) => {
  const { vault } = await makeVault(t, "minni-use-report-assessable-");
  const main = context();
  const at = (minutes) => new Date(Date.now() - 2 * HOUR + minutes * 60_000).toISOString();
  const rows = [];
  const judged = (channel, mkey, assessable, minute) => {
    const surfaced = buildSurfacedRow(main, { channel, mkey, title: "T", rank: 0, visible: true, delivered: true }, at(minute));
    rows.push(
      surfaced,
      buildJudgedRow(main, { sid: surfaced.sid, outcome: "ignored", via: "transcript", evidence: [], title_assessable: assessable, final: true, rule_version: "s1b-3" }, at(minute + 1)),
    );
  };
  judged("prompt_pointer", "vault:wiki/a.md", true, 1);
  judged("prompt_pointer", "vault:wiki/b.md", false, 2);
  judged("prompt_pointer", "vault:wiki/c.md", false, 3);
  judged("guard", "vault:wiki/d.md", true, 4);
  assert.equal(appendLedgerRows(vault, rows).written, rows.length);

  const { use } = await auditReport(vault, 100, { useDays: 7 });
  assert.equal(use.surfacings.title_assessable, 2);
  assert.equal(use.surfacings.title_assessable_share, 0.5);
  assert.equal(use.surfacings.by_channel.prompt_pointer.title_assessable_share, 0.3333);
  assert.equal(use.surfacings.by_channel.guard.title_assessable_share, 1);
  assert.equal(use.surfacings.by_agent.main.title_assessable_share, 0.5);

  const status = useStatusBlock(vault);
  assert.equal(status.surfacings.title_assessable_share, 0.5, "the minni_status block carries it too");
  assert.equal(status.surfacings.by_channel.prompt_pointer.title_assessable_share, 0.3333);
});
