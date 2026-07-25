import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditTail,
  auditReport,
  ensureVault,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  vaultFirstLearn,
  writeVaultPage,
} from "../dist/vault.js";
import { symlink } from "node:fs/promises"; // for RCM-005 escape test

// Hermetic guard: recordAudit writes per-agent rate-limit state under
// MINNI_HOME (falling back to ~/.minni) — point it at a temp dir so the
// suite never touches the real home (CI smoke asserts zero ~ pollution).
process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-test-home-"));

test("ensureVault creates the Codex LLM wiki structure and schema", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-"));
  try {
    const result = await ensureVault(root);

    assert.equal(result.vaultPath, root);
    assert.ok(result.created.includes(path.join(root, "raw")));
    assert.ok(result.created.includes(path.join(root, "wiki", "entities")));
    assert.ok(result.created.includes(path.join(root, "outbox")));

    const schema = await readFile(
      path.join(root, "schema", "AGENTS.md"),
      "utf8",
    );
    assert.match(schema, /Codex Minni Vault/);
    assert.match(schema, /raw sources/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveInboxHandoffContext resolves wikilink refs for boot priming", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-handoff-prime-"));
  try {
    await ensureVault(root);
    const decisionDir = path.join(root, "wiki", "decisions");
    await mkdir(decisionDir, { recursive: true });
    await writeFile(
      path.join(decisionDir, "auth-migration.md"),
      "---\ntitle: Auth Migration\n---\n\nUse the short-lived token exchange for auth migration.",
      "utf8",
    );

    const snippets = await resolveInboxHandoffContext(root, [
      {
        slug: "auth-handoff",
        filePath: path.join(root, "inbox", "auth.json"),
        createdAt: "2026-04-26T00:00:00.000Z",
        payload: {
          kind: "handoff",
          wikilink_refs: ["wiki/decisions/auth-migration"],
        },
      },
    ]);

    assert.equal(snippets.length, 1);
    assert.equal(snippets[0].ref, "wiki/decisions/auth-migration");
    assert.match(snippets[0].snippet, /short-lived token exchange/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("vaultFirstLearn writes a note, updates index, and appends audit logs", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-learn-"));
  try {
    const result = await vaultFirstLearn({
      vaultPath: root,
      title: "Socket daemon health check",
      content:
        "Minni daemon health is checked through ~/.minni/run/minnid.sock.",
      category: "fact",
      source: "unit-test",
      agentId: "codex",
      storeResult: { ok: true, detail: "learned" },
    });

    assert.match(
      result.notePath,
      /wiki\/sessions\/\d{8}-socket-daemon-health-check\.md$/,
    );

    const note = await readFile(result.notePath, "utf8");
    assert.match(note, /agent: codex/);
    assert.match(note, /category: fact/);
    assert.match(note, /Minni daemon health/);

    const index = await readFile(path.join(root, "index.md"), "utf8");
    assert.match(
      index,
      /\[\[wiki\/sessions\/\d{8}-socket-daemon-health-check\]\]/,
    );

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /minni_learn/);
    assert.match(log, /Socket daemon health check/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeVaultPage supports raw and wiki pages without treating them as learnings", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-page-"));
  try {
    const raw = await writeVaultPage({
      vaultPath: root,
      title: "Session Excerpt",
      content: "Immutable session source.",
      section: "raw",
      source: "unit-test",
    });
    const concept = await writeVaultPage({
      vaultPath: root,
      title: "Recall Transparency",
      content: "Memory tools should show what they read and write.",
      section: "concepts",
      source: "unit-test",
    });

    assert.match(raw.notePath, /raw\/\d{8}-session-excerpt\.md$/);
    assert.match(concept.notePath, /wiki\/concepts\/recall-transparency\.md$/);

    const rawNote = await readFile(raw.notePath, "utf8");
    assert.match(rawNote, /immutable: true/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("auditTail returns recent audit entries from daily logs", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "minni_status",
      summary: "status checked",
      details: { socket: "ok" },
    });

    const tail = await auditTail(root, 5);

    assert.equal(tail.entries.length, 1);
    assert.match(tail.text, /minni_status/);
    assert.match(tail.text, /status checked/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("auditReport summarizes recent tool activity", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-report-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "minni_recall",
      summary: "recall checked",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learning_quality",
      summary: "quality checked",
      details: { ok: true },
    });

    const report = await auditReport(root, 10);

    assert.equal(report.entries, 2);
    assert.equal(report.tools.minni_recall, 1);
    assert.equal(report.tools.minni_learning_quality, 1);
    assert.deepEqual(report.recentSummaries, [
      "minni_recall: recall checked",
      "minni_learning_quality: quality checked",
    ]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("searchVaultNotes ranks Codex wiki learnings for recall context", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-search-"));
  try {
    await vaultFirstLearn({
      vaultPath: root,
      title: "Codex plugin full suite marker",
      content:
        "SM_SEARCH_MARKER confirms vault-first learning is visible to AI recall context packs.",
      category: "fact",
      source: "unit-test",
      agentId: "codex",
      storeResult: { ok: true },
    });
    await writeVaultPage({
      vaultPath: root,
      title: "Unrelated concept",
      content: "This note discusses a different subject.",
      section: "concepts",
      source: "unit-test",
    });

    const results = await searchVaultNotes(
      root,
      "SM_SEARCH_MARKER AI recall context",
      3,
    );

    assert.equal(results.length, 1);
    assert.match(
      results[0].relativePath,
      /wiki\/sessions\/\d{8}-codex-plugin-full-suite-marker\.md$/,
    );
    assert.match(
      results[0].wikilink,
      /\[\[wiki\/sessions\/\d{8}-codex-plugin-full-suite-marker\]\]/,
    );
    assert.match(results[0].snippet, /SM_SEARCH_MARKER/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// RCM-005: concrete escape test (symlink to outside root must be rejected)
test("resolveInboxHandoffContext and search reject symlink escape from vault (RCM-005)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-escape-"));
  try {
    await ensureVault(root);
    const wiki = path.join(root, "wiki");
    const evilLink = path.join(wiki, "evil.md");
    // create symlink pointing outside
    await symlink("/etc/passwd", evilLink);

    // via handoff context (uses resolveVaultRef)
    const fakeHandoff = {
      payload: {
        kind: "handoff",
        wikilink_refs: ["evil", "[[evil]]"],
      },
    };
    const snippets = await resolveInboxHandoffContext(root, [fakeHandoff], 8);
    assert.equal(
      snippets.length,
      0,
      "escaped symlink must not resolve to content",
    );

    // via search (uses listMarkdownFiles which guards)
    const searchRes = await searchVaultNotes(root, "passwd", 5);
    // must not include content from /etc/passwd (strong zero-results for symmetry with resolveInboxHandoffContext)
    assert.equal(
      searchRes.length,
      0,
      "search must return zero results on symlink escape (RCM-005)",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Cross-platform frontmatter: a CRLF note's status must still be parsed, so
// superseded/rejected pages stay filtered on Windows-authored vaults.
test("searchVaultNotes filters superseded notes with CRLF line endings", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-crlf-"));
  try {
    await ensureVault(root);
    const note =
      "---\r\ntitle: CRLF superseded note\r\nstatus: superseded\r\n---\r\n\r\nSM_CRLF_MARKER stale belief body.\r\n";
    await writeFile(path.join(root, "wiki", "crlf-superseded.md"), note, "utf8");

    const results = await searchVaultNotes(root, "SM_CRLF_MARKER stale belief", 5);
    assert.equal(results.length, 0, "CRLF superseded note must not re-surface");

    // Positive control: same CRLF shape with a live status IS found, proving
    // the zero above comes from the status filter, not a failed read.
    const live = note.replace("status: superseded", "status: accepted");
    await writeFile(path.join(root, "wiki", "crlf-live.md"), live, "utf8");
    const found = await searchVaultNotes(root, "SM_CRLF_MARKER stale belief", 5);
    assert.equal(found.length, 1);
    assert.match(found[0].relativePath, /crlf-live\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Scoring fixtures live in wiki/concepts and wiki/sessions so the section
// bonus is exercised deliberately rather than by accident.
async function seedScoringVault(notes) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-score-"));
  await ensureVault(root);
  await mkdir(path.join(root, "wiki", "concepts"), { recursive: true });
  await mkdir(path.join(root, "wiki", "sessions"), { recursive: true });
  for (const [relative, body] of Object.entries(notes)) {
    await mkdir(path.dirname(path.join(root, relative)), { recursive: true });
    await writeFile(
      path.join(root, relative),
      `---\nstatus: accepted\n---\n\n${body}`,
      "utf8",
    );
  }
  return root;
}

// Whole-word matching, negative direction: a query term must never score a note
// that only contains it as a substring of a longer word.
test("searchVaultNotes excludes substring-only matches but keeps standalone words", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/substring-note.md":
      "# Handbook of methods\n\nThe project approach will reproduce the issue.\n",
    "wiki/concepts/standalone-note.md":
      "# Tooling notes\n\nA pro tip about tooling.\n",
  });
  try {
    const results = await searchVaultNotes(root, "pro", 5);
    assert.equal(
      results.length,
      1,
      "project/approach/reproduce must not match the term 'pro'",
    );
    assert.match(results[0].relativePath, /standalone-note\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Whole-word matching, positive direction: the +50 exact-phrase bonus is the
// strongest ranking signal in the scorer, and recall queries are natural
// language. A terminal "?" must not silently delete it — anchoring the raw
// query with \b on both edges made the bonus unreachable for every question.
test("searchVaultNotes keeps the exact-phrase bonus for punctuation-terminated queries", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/phrase-note.md":
      "# Runbook\n\nDocumented answer: how do i fix ci is covered below.\n",
    "wiki/concepts/scatter-note.md":
      "# Fix log\n\nThe ci job broke. We fix ci again. fix ci. fix ci. fix ci.\n",
  });
  try {
    const asked = await searchVaultNotes(root, "how do i fix ci?", 5);
    const bare = await searchVaultNotes(root, "how do i fix ci", 5);

    assert.match(asked[0].relativePath, /phrase-note\.md$/);
    assert.ok(
      asked[0].score >= 50,
      `phrase bonus must fire for a question (got ${asked[0].score})`,
    );
    // The scatter note has far more term hits, so it only loses because the
    // phrase bonus applied — this is the ranking the question form regressed.
    assert.match(asked[1].relativePath, /scatter-note\.md$/);
    assert.equal(
      asked[0].score,
      bare[0].score,
      "trailing punctuation must not change the score",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// queryTerms tokenises "-" and "/", so CLI flags arrive as terms like
// "--verbose". A leading \b requires a word character where the "-" sits, which
// made every flag-shaped token unmatchable.
test("searchVaultNotes matches flag-shaped query terms without matching the bare word", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/flag-note.md":
      "# Suite options\n\nRun the suite with --verbose to see output.\n",
    "wiki/concepts/bareword-note.md":
      "# Logging\n\nThe verbose logging mode is on.\n",
  });
  try {
    const results = await searchVaultNotes(root, "--verbose", 5);
    assert.equal(results.length, 1, "'--verbose' must match, and only the flag");
    assert.match(results[0].relativePath, /flag-note\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// MIN_TERM_SCORE is a noise floor, not a recall cliff. One whole-word hit is
// evidence; a floor of 2 discarded ~30% of the notes where main had a genuine
// whole-word match, because whole-word matching had already removed the noise
// the floor was aimed at.
test("searchVaultNotes recalls a note with a single whole-word hit", async () => {
  const root = await seedScoringVault({
    "wiki/sessions/session-note.md":
      "# Daily log\n\nWe touched alpha today.\n",
    "wiki/concepts/weak-note.md": "# Concept\n\nAn alpha mention only.\n",
  });
  try {
    const results = await searchVaultNotes(root, "alpha beta", 5);
    assert.equal(results.length, 2, "one body hit is enough to be recalled");
    assert.match(results[0].relativePath, /session-note\.md$/);
    assert.equal(results[0].score, 2, "1 term hit + 1 session bonus");
    assert.equal(results[1].score, 1, "1 term hit, no bonus");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// The floor gates term evidence, never the final score. Applied to the final
// score it inverted correction salience: a correction note with one hit
// finished at 1 * 1.25 = 1.25 and was cut while a plain session note with the
// same evidence finished at 2 and survived — the boosted class was the deleted
// class.
test("searchVaultNotes does not let the noise floor delete boosted correction notes", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-score-"));
  await ensureVault(root);
  await mkdir(path.join(root, "wiki", "concepts"), { recursive: true });
  try {
    await writeFile(
      path.join(root, "wiki", "concepts", "correction-note.md"),
      "---\nstatus: accepted\ntype: correction\n---\n\n# Note\n\nWe corrected alpha.\n",
      "utf8",
    );
    const results = await searchVaultNotes(root, "alpha beta", 5);
    assert.equal(results.length, 1, "a correction-class note must survive");
    assert.equal(results[0].score, 1.25, "1 term hit * (1 + 0.25) salience");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Morphological tolerance — the three exact regressions measured against main.
// Vault prose does not agree with query wording on number, and strict
// whole-word matching turned real notes into zero-score notes outright.
for (const [name, query, body] of [
  [
    // plan-3a21c152c0d609b3.md: `learnings` x9, `learnings_fts` x10,
    // `learning_id` x2, `search_learnings` x3, standalone `learning` x0.
    "learning/learnings",
    "learning",
    "# Schema\n\nThe learnings table and learnings_fts index share a learning_id; search_learnings reads both.\n",
  ],
  [
    // plan-98572c3eb1ea4394.md: `pipelines` only.
    "pipeline/pipelines",
    "pipeline",
    "# Build\n\nBoth pipelines run nightly.\n",
  ],
  [
    // minniplan-acceptance-spec-...md: `timeouts` only.
    "timeout/timeouts",
    "timeout",
    "# Spec\n\nAll timeouts are bounded.\n",
  ],
]) {
  test(`searchVaultNotes matches the ${name} inflection`, async () => {
    const root = await seedScoringVault({
      "wiki/concepts/inflected-note.md": body,
    });
    try {
      const results = await searchVaultNotes(root, query, 5);
      assert.equal(results.length, 1, `'${query}' must reach '${name}'`);
      assert.ok(
        results[0].score > 0,
        `an inflected hit is evidence (got ${results[0].score})`,
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}

// The inflection is TERM evidence only. Letting it satisfy the exact-phrase
// check handed the +50 bonus to notes that never contain the query, and they
// then took the user-visible top slots: on claudecode-vault `land` had 4 of its
// 5 top results carrying no literal "land", all of them via "landed".
test("searchVaultNotes never awards the exact-phrase bonus for an inflection", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/inflected-note.md":
      "# Ladder\n\nStage 7 landed, then the promotion landed, and the gate landed.\n",
    "wiki/concepts/literal-note.md": "# Survey\n\nWe surveyed the land once.\n",
  });
  try {
    const results = await searchVaultNotes(root, "land", 5);
    assert.equal(results.length, 2, "both notes carry evidence");

    const literal = results.find((r) => /literal-note\.md$/.test(r.relativePath));
    const inflected = results.find((r) =>
      /inflected-note\.md$/.test(r.relativePath),
    );
    assert.ok(
      literal.score >= 50,
      `a literal occurrence still earns the bonus (got ${literal.score})`,
    );
    assert.ok(
      inflected.score > 0 && inflected.score < 50,
      `an inflection-only note must be admitted but unbonused (got ${inflected.score})`,
    );
    // The ranking is the user-visible defect: three "landed" hits outscored one
    // literal "land" only because the bonus applied to both.
    assert.match(results[0].relativePath, /literal-note\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// `_` is a word-edge character (so `_private` still anchors) but NOT a word
// boundary character (so identifier compounds count). Both halves matter and
// they pull in opposite directions.
test("searchVaultNotes treats underscore as a boundary without unanchoring underscore terms", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/compound-note.md":
      "# Frontmatter\n\nA note carrying minni_learning: true.\n",
    "wiki/concepts/private-note.md": "# Flags\n\nThe x_private column.\n",
  });
  try {
    const compound = await searchVaultNotes(root, "learning", 5);
    assert.equal(compound.length, 1, "minni_learning must count for 'learning'");
    assert.match(compound[0].relativePath, /compound-note\.md$/);

    const flagged = await searchVaultNotes(root, "_private", 5);
    assert.equal(flagged.length, 0, "'_private' must not match inside x_private");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// `\b` is ASCII-only, so it treated an accented letter as a word boundary and
// let terms bleed inside accented words — the exact failure the whole-word
// change exists to prevent. The mirror-image trap is Unicode-correct classes
// blocking CJK, which is written without separators and where an embedded
// ASCII word IS standalone.
test("searchVaultNotes anchors on accented letters but not on ideographs", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/accented-note.md": "# Files\n\nThe résumé file is here.\n",
    "wiki/concepts/cjk-note.md": "# Notes\n\n日本語sum語 appears inline.\n",
  });
  try {
    const results = await searchVaultNotes(root, "sum", 5);
    assert.equal(results.length, 1, "'sum' must not match inside 'résumé'");
    assert.match(results[0].relativePath, /cjk-note\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// The 2-character token minimum admits real short terms ("ci", "db") but
// without an alphanumeric requirement it also emits pure-punctuation tokens,
// and "//" matched every URL in the vault.
test("searchVaultNotes ignores pure-punctuation query tokens", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/url-note.md":
      "# Links\n\nSee https://example.com/a/b and https://example.com/c/d.\n",
    "wiki/concepts/topic-note.md": "# Style\n\nWe use comments sparingly.\n",
  });
  try {
    const results = await searchVaultNotes(root, "use // for comments", 5);
    assert.equal(results.length, 1, "'//' must not admit unrelated URL notes");
    assert.match(results[0].relativePath, /topic-note\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// A query made entirely of stopwords must not tokenise to nothing: an empty
// term list previously short-circuited the whole search and returned zero
// results for a question the vault could answer.
test("searchVaultNotes still answers stopword-only queries", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/why-note.md": "# FAQ\n\nWhy the daemon restarts.\n",
    "wiki/concepts/other-note.md": "# Other\n\nUnrelated content.\n",
  });
  try {
    const results = await searchVaultNotes(root, "why?", 5);
    assert.equal(results.length, 1, "'why?' must not return nothing");
    assert.match(results[0].relativePath, /why-note\.md$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// A query that tokenises to nothing must not reach the scorer with a phrase
// that has no substance. Each fixture below trimmed down to a single character
// or to pure punctuation and was then matched as a whole "phrase", collecting
// the full +50 bonus on notes with no relation to the query: on the real
// claudecode vault (437 notes) `c#` and `c++` returned 93 each, `r?` 16, and
// `-` and `/` returned 376 apiece.
//
// `c#`, `c++` and `r?` are excluded here by ABSENCE, not by a length rule: the
// untrimmed form is now tried first and none of these fixtures contains it. The
// note that does contain `C#` is the next test.
const EMPTY_PHRASE_QUERIES = [
  ["c-sharp", "c#"],
  ["c-plus-plus", "c++"],
  ["r-question", "r?"],
  ["bare-hyphen", "-"],
  ["bare-underscore", "_"],
  ["bare-slash", "/"],
  ["double-hyphen", "--"],
  // Alphanumeric under Unicode, so only the 2-character minimum excludes it —
  // on the same ground as the ASCII "c", not because it is punctuation. A lone
  // ideograph is the one single-character exception; see the CJK test below.
  ["accented-letter", "é"],
  ["bare-letter", "c"],
];

for (const [name, query] of EMPTY_PHRASE_QUERIES) {
  test(`searchVaultNotes returns nothing for the ${name} query`, async () => {
    const root = await seedScoringVault({
      // Every character the fixtures could latch onto, in prose that has
      // nothing to do with any of them.
      "wiki/concepts/hyphen-note.md":
        "# Kit\n\nThe delegate-and-idle pattern uses c and r flags.\n",
      "wiki/concepts/slash-note.md":
        "# Paths\n\nSee scripts/build and docs/notes for x_private, a lone _ marker, é and 節.\n",
      "wiki/sessions/session-note.md":
        "# Log\n\nA C compiler ran; the R report followed.\n",
    });
    try {
      const results = await searchVaultNotes(root, query, 5);
      assert.equal(
        results.length,
        0,
        `'${query}' has no searchable content (got ${results.length})`,
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}

// The second half of the same defect, isolated: a term whose edge characters
// are non-word at BOTH ends gets no boundary assertion at either edge, so the
// pattern degrades to a substring search. For a term with no alphanumeric at
// all that is ruinous — "--" is a substring of every long option and every
// horizontal rule in the corpus — so those terms are refused outright.
//
// A term that carries an alphanumeric run is NOT refused, and the run is the
// only specificity it can have: "-a-" therefore does match inside "x-a-y",
// which the pre-round-4 rule excluded. That exclusion was not free — it applied
// to "/api/" and "/users/hans/projects/" identically and deleted them — and
// there is no assertion that separates the two cases, since both occurrences
// sit between separators the term supplies itself.
test("searchVaultNotes refuses terms with no alphanumeric content", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/embedded-note.md":
      "# Ids\n\nThe x-a-y identifier is used with --flag and a --- rule.\n",
  });
  try {
    for (const query of ["--", "---", "//"]) {
      const results = await searchVaultNotes(root, query, 5);
      assert.equal(results.length, 0, `'${query}' carries no lexical content`);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Defect 1 (round 4). phraseForm trimmed `#`/`+`/`?` BEFORE matching, so `c#`
// became a bare `c` — which is what produced the 93-note bleed the trim was
// blamed for, and which the length gate then papered over by deleting the query
// outright. The untrimmed form is tried first and is safe on its own: wordRegex
// anchors only the leading edge, so it matches `C#` and nothing else.
test("searchVaultNotes finds punctuation-suffixed terms in notes that contain them", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/interop-note.md":
      "# Interop\n\nWe use C# and C++ in the interop layer.\n",
    // The decoy the trimmed form used to match: a bare standalone `c`.
    "wiki/sessions/compiler-note.md":
      "# Toolchain\n\nA C compiler ran; the R report followed.\n",
  });
  try {
    for (const query of ["c#", "c++"]) {
      const results = await searchVaultNotes(root, query, 5);
      assert.equal(results.length, 1, `'${query}' must find the note saying it`);
      assert.match(results[0].relativePath, /interop-note\.md$/);
      assert.ok(
        results[0].score >= 50,
        `'${query}' must take the exact-phrase bonus (got ${results[0].score})`,
      );
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Defect 2 (round 4). The 2-character phrase minimum is an assumption about
// scripts that separate words. `節` is a whole word, and the whole-word matcher
// already admits the surrounding kana as boundaries — the length rule was the
// only thing rejecting it.
test("searchVaultNotes admits a single-ideograph query", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/cjk-config-note.md":
      "# 設定\n\n設定ファイルは節ごとに分割する\n",
    "wiki/concepts/other-note.md": "# Other\n\nUnrelated content.\n",
  });
  try {
    for (const query of ["節", "設定", "分割する"]) {
      const results = await searchVaultNotes(root, query, 5);
      assert.equal(results.length, 1, `'${query}' is a whole word here`);
      assert.match(results[0].relativePath, /cjk-config-note\.md$/);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Defect 3 (round 4). queryTerms tokenises `/`, so a path arrives at wordRegex
// with a separator at both edges. Refusing it dropped the only term the query
// had, and a literal path returned nothing against a note quoting it verbatim.
test("searchVaultNotes matches path-shaped query terms", async () => {
  const root = await seedScoringVault({
    "wiki/concepts/route-note.md":
      "# Routes\n\nThe router mounts /api/ under /Users/hans/Projects/ during local dev.\n",
    "wiki/concepts/other-note.md": "# Other\n\nUnrelated content.\n",
  });
  try {
    for (const query of ["/api/", "/Users/hans/Projects/"]) {
      const results = await searchVaultNotes(root, query, 5);
      assert.equal(results.length, 1, `'${query}' occurs verbatim in the note`);
      assert.match(results[0].relativePath, /route-note\.md$/);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Defect 4 (round 4). The whole-word window is tier 1 and stays tier 1 (see the
// `pro` test above). What was missing is tier 2: a note admitted on its PATH
// whose prose only ever says a longer word has no whole-word hit anywhere, and
// falling straight to offset 0 showed boilerplate instead of the sentence the
// query is actually in.
test("searchVaultNotes falls back to a substring window before offset 0", async () => {
  const filler = "Boilerplate intro that says nothing useful at all. ".repeat(6);
  const root = await seedScoringVault({
    "wiki/auth/notes.md": `# Session notes\n\n${filler}The authentication flow uses a rotating token.\n`,
  });
  try {
    const results = await searchVaultNotes(root, "auth", 5);
    assert.equal(results.length, 1, "'auth' qualifies through the path");
    assert.match(
      results[0].snippet,
      /authentication flow/,
      "the window must show where the query text actually is",
    );
    assert.doesNotMatch(
      results[0].snippet,
      /^Boilerplate intro/,
      "offset 0 is for notes whose prose never says the query at all",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// The snippet is the evidence the caller reads. Locating its window with a
// substring search centred it on occurrences the scorer had explicitly
// rejected — 44 of 458 top-5 snippets on the real vaults, with `pro` opening
// windows on "proof", "PROVEN" and "promises".
test("searchVaultNotes centres the snippet on a whole-word hit", async () => {
  const filler = "Filler prose that carries no query evidence at all. ".repeat(8);
  const root = await seedScoringVault({
    "wiki/concepts/window-note.md":
      `# Handbook\n\nThe project approach will reproduce the issue. ${filler}A pro tip about tooling closes it out.\n`,
  });
  try {
    const results = await searchVaultNotes(root, "pro", 5);
    assert.equal(results.length, 1, "'pro tip' is a genuine whole-word hit");
    assert.match(
      results[0].snippet,
      /pro tip/,
      "the snippet must show the hit the scorer counted",
    );
    assert.doesNotMatch(
      results[0].snippet,
      /project approach/,
      "the window must not open on the rejected substring",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
