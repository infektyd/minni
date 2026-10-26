// Parity gate for the shared durability predicate + boot noise ordering + DF
// non-noise filter (recall-to-use rev5 §4.4, §5.2.3, §5.3, §6.2.1).
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { buildBootRecallSlice } from "../dist/daemon_transport.js";
import {
  CATEGORY_SYNONYMS,
  CONTENT_SECTION_PATTERN,
  CONTENT_TEST_PREFIXES,
  DURABILITY_RULES_VERSION,
  DURABLE_CATEGORIES,
  classifyDurability,
  durabilityOf,
  durabilityRuleIds,
} from "../dist/durability.js";
import { readVaultNoteTextForDf } from "../dist/vault.js";

const CASES_URL = new URL("../../../tests/fixtures/durability_cases.json", import.meta.url);
const cases = JSON.parse(await readFile(CASES_URL, "utf8"));

test("durability case file is versioned and non-trivial", () => {
  assert.equal(cases.version, 1);
  assert.ok(cases.cases.length >= 30);
  const ids = cases.cases.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length, "case ids must be unique");
});

for (const c of cases.cases) {
  test(`durability ${c.id}: ${c.note}`, () => {
    const opts = c.options ?? {};
    const got = classifyDurability(c.input, {
      provenanceGateSince: opts.provenance_gate_since,
      contentTestEnabled: opts.content_test_enabled,
    });
    assert.deepEqual(got, c.expected);
    assert.equal(
      durabilityOf(c.input, {
        provenanceGateSince: opts.provenance_gate_since,
        contentTestEnabled: opts.content_test_enabled,
      }),
      c.expected.kind,
    );
  });
}

test("pinned constants in durability_cases.json match TypeScript constants", () => {
  const pinned = cases.constants;
  assert.equal(DURABILITY_RULES_VERSION, pinned.version);
  assert.deepEqual(CATEGORY_SYNONYMS, pinned.categorySynonyms);
  assert.deepEqual(DURABLE_CATEGORIES, pinned.durableCategories);
  assert.deepEqual(CONTENT_TEST_PREFIXES, pinned.contentTestPrefixes);
  assert.equal(CONTENT_SECTION_PATTERN, pinned.contentSectionPattern);
  assert.deepEqual(durabilityRuleIds(), pinned.ruleIds);
});

test("every rule_id in TypeScript constants has at least one case", () => {
  const expectedIds = new Set(cases.cases.map((c) => c.expected.rule_id));
  const missing = durabilityRuleIds().filter((id) => !expectedIds.has(id));
  assert.deepEqual(missing, [], `rules with no case: ${missing.join(", ")}`);
});

test("every rule_id a case expects exists in TypeScript constants", () => {
  const known = new Set(durabilityRuleIds());
  const unknown = cases.cases
    .map((c) => c.expected.rule_id)
    .filter((id) => !known.has(id));
  assert.deepEqual(unknown, [], `cases naming unknown rules: ${unknown.join(", ")}`);
});

test("buildBootRecallSlice moves noise results (compaction summaries, plan journals, finished plans) after non-noise before capping at 3", () => {
  const response = {
    agent_id: "claude-code",
    count: 6,
    results: [
      {
        wikilink: "[[wiki/sessions/2026-10-20-compact-111]]",
        title: "Compact Summary 1",
        text: "<EVIDENCE>Compaction summary 1</EVIDENCE>",
        source: "/vault/wiki/sessions/2026-10-20-compact-111.md",
        layer: "knowledge",
        score: 2.0,
      },
      {
        wikilink: "[[wiki/artifacts/plan-abc.log]]",
        title: "Plan Journal ABC",
        text: "<EVIDENCE>Plan log entry</EVIDENCE>",
        source: "/vault/wiki/artifacts/plan-abc.log.md",
        layer: "knowledge",
        score: 1.9,
      },
      {
        wikilink: "[[wiki/artifacts/plan-finished]]",
        title: "Finished Plan",
        review_state: "complete",
        text: '<EVIDENCE source="/vault/wiki/artifacts/plan-finished.md" status="complete"># Finished Plan\nDone.</EVIDENCE>',
        source: "/vault/wiki/artifacts/plan-finished.md",
        layer: "knowledge",
        score: 1.85,
      },
      {
        wikilink: "[[wiki/decisions/wal-mode]]",
        title: "Keep SQLite WAL mode",
        text: "<EVIDENCE>Decision: keep WAL mode enabled</EVIDENCE>",
        source: "/vault/wiki/decisions/wal-mode.md",
        layer: "knowledge",
        score: 1.5,
      },
      {
        wikilink: "[[wiki/concepts/uds-auth]]",
        title: "UDS Socket Auth",
        text: "<EVIDENCE>Principal resolution over UDS</EVIDENCE>",
        source: "/vault/wiki/concepts/uds-auth.md",
        layer: "knowledge",
        score: 1.4,
      },
      {
        wikilink: "[[wiki/procedures/promote]]",
        title: "Promotion Gate",
        text: "<EVIDENCE>Run make check before promote</EVIDENCE>",
        source: "/vault/wiki/procedures/promote.md",
        layer: "knowledge",
        score: 1.3,
      },
    ],
    episodic: [],
    episodic_count: 0,
  };

  const slice = buildBootRecallSlice(response, "claude-code");
  assert.equal(slice.results.length, 3);
  assert.deepEqual(
    slice.results.map((r) => r.wikilink),
    [
      "[[wiki/decisions/wal-mode]]",
      "[[wiki/concepts/uds-auth]]",
      "[[wiki/procedures/promote]]",
    ],
  );
});

test("readVaultNoteTextForDf excludes noise notes (compaction summaries, plan journals, finished plans) and keeps non-noise notes", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-df-durability-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "sessions"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "artifacts"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });

  await writeFile(
    path.join(vault, "wiki", "sessions", "2026-10-20-compact-abc.md"),
    "# Compact Session\n\nSummary text.\n",
    "utf8",
  );
  await writeFile(
    path.join(vault, "wiki", "artifacts", "plan-123.log.md"),
    "# Plan Journal\n\nLog entry.\n",
    "utf8",
  );
  await writeFile(
    path.join(vault, "wiki", "artifacts", "plan-done.md"),
    "---\ntype: plan\nstatus: complete\n---\n# Done Plan\n\nFinished.\n",
    "utf8",
  );
  await writeFile(
    path.join(vault, "wiki", "artifacts", "plan-accepted.md"),
    "---\ntype: plan\nstatus: accepted\n---\n# Accepted Plan\n\n- [x] Slice 1\n",
    "utf8",
  );
  await writeFile(
    path.join(vault, "wiki", "decisions", "adr-001.md"),
    "# ADR 001\n\nDecision: keep WAL mode.\n",
    "utf8",
  );

  const maxBytes = 1024 * 1024;
  assert.equal(
    readVaultNoteTextForDf(vault, "wiki/sessions/2026-10-20-compact-abc.md", maxBytes),
    null,
  );
  assert.equal(
    readVaultNoteTextForDf(vault, "wiki/artifacts/plan-123.log.md", maxBytes),
    null,
  );
  assert.equal(
    readVaultNoteTextForDf(vault, "wiki/artifacts/plan-done.md", maxBytes),
    null,
  );
  assert.ok(
    readVaultNoteTextForDf(vault, "wiki/artifacts/plan-accepted.md", maxBytes) !== null,
    "accepted plan note is not noise and must be included in DF",
  );
  assert.ok(
    readVaultNoteTextForDf(vault, "wiki/decisions/adr-001.md", maxBytes) !== null,
    "decision note must be included in DF",
  );
});
