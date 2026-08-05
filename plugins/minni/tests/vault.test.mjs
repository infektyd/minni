import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditTail,
  auditReport,
  ensureVault,
  formatSessionReceiptLine,
  listSessions,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  sessionReceipt,
  vaultFirstLearn,
  writeVaultPage,
  writeFileAtomic,
} from "../dist/vault.js";
import { chmod, stat, symlink } from "node:fs/promises"; // for RCM-005 escape test

// Hermetic guard: recordAudit writes per-agent rate-limit state under
// MINNI_HOME (falling back to ~/.minni) — point it at a temp dir so the
// suite never touches the real home (CI smoke asserts zero ~ pollution).
process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-test-home-"));

// Session-receipt tests write `hook_*` audit entries in quick succession;
// recordAudit throttles those within 5s of each other, so bypass the limit to
// keep every crafted entry (the existing tests use non-hook tools and are
// unaffected by this flag).
process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";

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
      // #312: plain body text, deliberately free of any word
      // heuristicPrivacyForSource (task.ts) escalates on — this test is
      // about wikilink ref resolution, not the privacy gate; a body
      // mentioning "token" would now be correctly excluded by the #312 fix
      // and this fixture would stop testing what its name says.
      "---\ntitle: Auth Migration\n---\n\nUse the short-lived credential exchange for auth migration.",
      "utf8",
    );

    const { snippets, withheldCount } = await resolveInboxHandoffContext(root, [
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
    assert.match(snippets[0].snippet, /short-lived credential exchange/);
    assert.equal(withheldCount, 0, "#340: nothing was privacy-gated, so withheldCount must be 0");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#312: resolveInboxHandoffContext never surfaces a privacy:private note's body text", async () => {
  // resolveInboxHandoffContext feeds hook-handlers.ts's SessionStart
  // envelope handoff_context field directly (a 1:1 map, no filtering in
  // between) — before this fix, a handoff's wikilink_refs could name ANY
  // note in the vault, including privacy:private ones, and their BODY TEXT
  // (a 520-char snippet) reached the model-facing envelope with zero
  // gating. Worse than the SEC-006 gap #308 fixed for UserPromptSubmit's
  // recall pointer, since that one only leaked titles/paths.
  const root = await mkdtemp(path.join(tmpdir(), "sm-handoff-privacy-"));
  try {
    await ensureVault(root);
    const decisionDir = path.join(root, "wiki", "decisions");
    await mkdir(decisionDir, { recursive: true });
    await writeFile(
      path.join(decisionDir, "safe-migration.md"),
      "---\ntitle: Safe Migration\nprivacy: safe\n---\n\nBoot priming marker phrase, safe note.",
      "utf8",
    );
    await writeFile(
      path.join(decisionDir, "private-migration.md"),
      "---\ntitle: Private Migration\nprivacy: private\n---\n\nBoot priming marker phrase, CONFIDENTIAL body text that must never leave this vault.",
      "utf8",
    );

    const { snippets, withheldCount } = await resolveInboxHandoffContext(root, [
      {
        slug: "mixed-handoff",
        filePath: path.join(root, "inbox", "mixed.json"),
        createdAt: "2026-04-26T00:00:00.000Z",
        payload: {
          kind: "handoff",
          wikilink_refs: [
            "wiki/decisions/safe-migration",
            "wiki/decisions/private-migration",
          ],
        },
      },
    ]);

    const refs = snippets.map((s) => s.ref);
    assert.ok(refs.includes("wiki/decisions/safe-migration"), "the safe note must still resolve");
    assert.ok(
      !refs.includes("wiki/decisions/private-migration"),
      "SEC (#312): a privacy:private note's ref must not resolve at all",
    );
    assert.ok(
      !snippets.some((s) => /CONFIDENTIAL/.test(s.snippet)),
      "SEC (#312): the private note's body text must never appear in any returned snippet",
    );
    assert.equal(
      withheldCount,
      1,
      "#340: exactly one ref (the private note) was withheld, and it must be counted without naming it",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#312: a leading blank line before the frontmatter delimiter must not bypass the privacy gate", async () => {
  // Adversarial-review finding: frontmatterBlock (privacy/status/title
  // parsing) used to be anchored strictly at string offset 0 with no `/m`
  // flag, while snippetFor's frontmatter-strip used `/m` (matches `---` at
  // the start of ANY line). A note with a leading blank line (or BOM, or
  // CRLF line endings) made frontmatterBlock return "" — so the authored
  // `privacy: private` was silently ignored — while snippetFor still
  // correctly stripped the block before handing text to the heuristic
  // scanner, so the heuristic layer never saw the word "private" either.
  // Both privacy layers missed the same note for opposite reasons and its
  // body text leaked in full. FRONTMATTER_RE is now the single shared
  // anchor for both functions.
  const root = await mkdtemp(path.join(tmpdir(), "sm-handoff-leadnl-"));
  try {
    await ensureVault(root);
    const decisionDir = path.join(root, "wiki", "decisions");
    await mkdir(decisionDir, { recursive: true });
    const cases = {
      "leading-newline": "\n---\ntitle: T\nprivacy: private\n---\n\nCONFIDENTIAL layoff list: Alice, Bob.",
      "leading-space": " ---\ntitle: T\nprivacy: private\n---\n\nCONFIDENTIAL layoff list: Alice, Bob.",
      "crlf-with-leading-newline": "\r\n---\r\ntitle: T\r\nprivacy: private\r\n---\r\n\r\nCONFIDENTIAL layoff list: Alice, Bob.",
    };
    for (const [name, body] of Object.entries(cases)) {
      await writeFile(path.join(decisionDir, `${name}.md`), body, "utf8");
    }

    const { snippets, withheldCount } = await resolveInboxHandoffContext(root, [
      {
        slug: "leadnl-handoff",
        filePath: path.join(root, "inbox", "leadnl.json"),
        createdAt: "2026-04-26T00:00:00.000Z",
        payload: {
          kind: "handoff",
          wikilink_refs: Object.keys(cases).map((name) => `wiki/decisions/${name}`),
        },
      },
    ]);

    assert.equal(
      snippets.length,
      0,
      "SEC (#312): every case declares privacy: private in frontmatter — none may resolve, regardless of leading whitespace/BOM/CRLF noise before the delimiter",
    );
    assert.equal(
      withheldCount,
      3,
      "#340: all three privacy-gated refs must be counted as withheld, not silently dropped",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("#312: heuristic-only privacy (no frontmatter declaration) still gates a handoff ref", async () => {
  // Coverage gap flagged in adversarial review: the earlier #312 tests only
  // exercised the frontmatter-authored path (explicit `privacy: private`).
  // task.ts's heuristicPrivacyForSource escalates on unlabelled body text
  // too (defense-in-depth) — this note declares no privacy: field at all,
  // so it must be caught purely by the heuristic scanning relativePath +
  // title + snippet.
  const root = await mkdtemp(path.join(tmpdir(), "sm-handoff-heuristic-"));
  try {
    await ensureVault(root);
    const decisionDir = path.join(root, "wiki", "decisions");
    await mkdir(decisionDir, { recursive: true });
    await writeFile(
      path.join(decisionDir, "safe-note.md"),
      "---\ntitle: Safe Note\n---\n\nBoot priming marker phrase, nothing sensitive here.",
      "utf8",
    );
    await writeFile(
      path.join(decisionDir, "unlabelled-secret.md"),
      "---\ntitle: Unlabelled\n---\n\nBoot priming marker phrase. The api_key for staging is embedded here.",
      "utf8",
    );

    const { snippets, withheldCount } = await resolveInboxHandoffContext(root, [
      {
        slug: "heuristic-handoff",
        filePath: path.join(root, "inbox", "heuristic.json"),
        createdAt: "2026-04-26T00:00:00.000Z",
        payload: {
          kind: "handoff",
          wikilink_refs: [
            "wiki/decisions/safe-note",
            "wiki/decisions/unlabelled-secret",
          ],
        },
      },
    ]);

    const refs = snippets.map((s) => s.ref);
    assert.ok(refs.includes("wiki/decisions/safe-note"), "the safe note must still resolve");
    assert.ok(
      !refs.includes("wiki/decisions/unlabelled-secret"),
      "SEC (#312): heuristic-flagged content must be gated even with no privacy: frontmatter",
    );
    assert.ok(
      !snippets.some((s) => /api_key/.test(s.snippet)),
      "SEC (#312): the heuristically-blocked note's body text must never appear in any returned snippet",
    );
    assert.equal(withheldCount, 1, "#340: the heuristically-gated ref must be counted as withheld");
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

// SEC-G6 / #237: successful minni_learn audits were audit-dark on quality —
// fail-open (AFM unavailable) writes looked identical to never-assessed in the
// durable log. Quality-blocked paths already carry details.quality; the write
// path must too so operators can grep semanticTier.
test("fail-open async assess + learn audit carries semanticTier unavailable (SEC-G6)", async () => {
  const { assessLearningQualityAsync } = await import("../dist/policy.js");
  const root = await mkdtemp(path.join(tmpdir(), "sm-learn-audit-tier-"));
  try {
    const content =
      "password: correct horse battery staple — documented so the quality score clears the short-content floor for this gate test.";
    const quality = await assessLearningQualityAsync(
      {
        title: "Staging box rotation note for the on-call",
        content,
        category: "ops",
        source: "unit-test",
      },
      { classifyInconclusive: async () => "unavailable" },
    );
    assert.equal(quality.ok, true, "control: unavailable must fail-open");
    assert.equal(quality.semanticTier, "unavailable");

    await vaultFirstLearn({
      vaultPath: root,
      title: "Staging box rotation note for the on-call",
      content,
      category: "ops",
      source: "unit-test",
      agentId: "codex",
      storeResult: { ok: true },
      quality,
    });

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /minni_learn/);
    // Durable audit must embed quality.semanticTier so AFM-unavailable writes
    // are distinguishable from never-ran / examined-and-cleared.
    assert.match(log, /"semanticTier":\s*"unavailable"/);
    assert.match(log, /"quality"/);
    // Warnings already avoid echoing secret values; pin that the passphrase
    // does not leak via the quality block either.
    assert.ok(
      !log.includes("correct horse battery staple"),
      "learn audit must not echo the inconclusive passphrase",
    );
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

// #293 review round: writeFileAtomic's rename onto an existing file used to
// silently discard whatever mode the destination had, re-widening it to the
// umask default on every atomic rewrite — a permanent, silent permission
// downgrade for any operator-hardened vault page.
test("writeFileAtomic preserves the destination's existing file mode across a rewrite", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-atomic-mode-"));
  try {
    const target = path.join(root, "note.md");
    await writeFile(target, "first version", "utf8");
    await chmod(target, 0o600);
    const before = (await stat(target)).mode & 0o777;
    assert.equal(before, 0o600, "test setup: chmod did not take");

    await writeFileAtomic(target, "second version");

    const after = (await stat(target)).mode & 0o777;
    assert.equal(
      after,
      0o600,
      "writeFileAtomic must preserve the destination's mode across a rewrite, not reset it to the umask default",
    );
    assert.equal(await readFile(target, "utf8"), "second version");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeFileAtomic on a brand-new file: content lands, no leftover .tmp sibling", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-atomic-newmode-"));
  try {
    const target = path.join(root, "brand-new.md");
    await writeFileAtomic(target, "content");
    assert.equal(await readFile(target, "utf8"), "content");
    const siblings = await readdir(root);
    assert.deepEqual(
      siblings.filter((name) => name.endsWith(".tmp")),
      [],
      "writeFileAtomic must not leave a temp file behind",
    );
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
    const { snippets, withheldCount } = await resolveInboxHandoffContext(root, [fakeHandoff], 8);
    assert.equal(
      snippets.length,
      0,
      "escaped symlink must not resolve to content",
    );
    assert.equal(
      withheldCount,
      0,
      "#340: a containment reject is ABSENT, not privacy-WITHHELD — must not be counted as withheld",
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

// Moved from issue-173-verification.test.mjs when the retrieval rework was
// split onto this branch: these assert scorer behaviour, not hook governance.
test("searchVaultNotes preserves 2-letter technical terms like CI/CD, PR, UI, DB", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-173-"));
  const vaultPath = path.join(root, "vault");
  try {
    await ensureVault(vaultPath);
    await vaultFirstLearn({
      vaultPath,
      title: "CI/CD Setup and PR Workflow",
      content: "Set up CI CD pipeline for PR validation and DB migrations.",
      category: "procedures",
      agentId: "codex",
    });

    const results = await searchVaultNotes(vaultPath, "CI CD pipeline PR DB", 5);
    assert.equal(results.length > 0, true, "searchVaultNotes should return hits for 2-letter term queries");
    assert.match(results[0].title, /CI\/CD Setup/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("searchVaultNotes word-boundary matching prevents substring false positives", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-173-wordbound-"));
  const vaultPath = path.join(root, "vault");
  try {
    await ensureVault(vaultPath);
    // Create a note containing "project", "approach", "reproduce" but NOT the standalone term "pro"
    await vaultFirstLearn({
      vaultPath,
      title: "Project Approach and Reproduce Steps",
      content: "This document describes the project approach to reproduce errors.",
      category: "concepts",
      agentId: "codex",
    });

    // Query for "pro" — should NOT match "project" / "approach" / "reproduce"
    const results = await searchVaultNotes(vaultPath, "pro", 5);
    assert.equal(results.length, 0, "standalone term 'pro' must not false-match inside 'project', 'approach', or 'reproduce'");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt tallies per-session memory activity from the audit log", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-"));
  try {
    await ensureVault(root);
    const sid = "sess-A";

    // Boot marker (predates session_id-stamped details; window opens here).
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: `boot ${sid}`,
      details: { daemon_ok: true },
    });
    // Strong + weak recalls, both stamped with the session id.
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "first task",
      details: { recall_strong: true, session_id: sid },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "second task",
      details: { recall_strong: false, session_id: sid },
    });
    // Guard nudge (denied) + an unstamped learn/vault write caught by the window.
    await recordAudit(root, {
      tool: "hook_codex_pretooluse_guard",
      summary: `recall guard denied Edit (mode=strict)`,
      details: { consumed: true, session_id: sid },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "committed a learning",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "vault_write",
      summary: "wrote a page",
      details: { ok: true },
    });
    // Stop marker with a candidates count (window closes here).
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: `stop ${sid}`,
      details: { candidates: 2 },
    });

    // A different session's activity AFTER the stop must not leak in.
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "other-session task",
      details: { recall_strong: true, session_id: "sess-B" },
    });

    const receipt = await sessionReceipt(root, sid);
    assert.equal(receipt.session_id, sid);
    assert.equal(receipt.entries, 7);
    assert.equal(receipt.recalls_strong, 1);
    assert.equal(receipt.recalls_weak, 1);
    assert.equal(receipt.guard_denied, 1);
    assert.equal(receipt.guard_allowed, 0);
    assert.equal(receipt.learns, 1);
    assert.equal(receipt.vault_writes, 1);
    assert.equal(receipt.candidates_drafted, 2);

    assert.equal(
      formatSessionReceiptLine(receipt),
      "Minni session receipt: 2 recalls (1 strong), 1 guard nudge, 1 learn committed, 2 candidates staged.",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt returns an all-zero receipt when the session did no memory work", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-zero-"));
  try {
    await ensureVault(root);
    // Only another session's entries exist; the queried session is a no-op.
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "unrelated",
      details: { recall_strong: true, session_id: "sess-other" },
    });

    const receipt = await sessionReceipt(root, "sess-empty");
    assert.equal(receipt.entries, 0);
    assert.equal(receipt.recalls_strong, 0);
    assert.equal(receipt.recalls_weak, 0);
    assert.equal(receipt.guard_denied, 0);
    assert.equal(receipt.candidates_drafted, 0);
    assert.equal(
      formatSessionReceiptLine(receipt),
      "Minni session receipt: 0 recalls (0 strong), 0 guard nudges, 0 learns committed, 0 candidates staged.",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt counts allowed guard nudges and stamped entries out of window", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-guard-"));
  try {
    await ensureVault(root);
    const sid = "sess-C";
    // No boot marker: attribution here rests purely on stamped session_id.
    await recordAudit(root, {
      tool: "hook_codex_pretooluse_guard",
      summary: `recall guard allowed (consume write failed) Read (mode=strict)`,
      details: { consumed: false, session_id: sid },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "task",
      details: { recall_strong: true, session_id: sid },
    });

    const receipt = await sessionReceipt(root, sid);
    assert.equal(receipt.entries, 2);
    assert.equal(receipt.guard_denied, 0);
    assert.equal(receipt.guard_allowed, 1);
    assert.equal(receipt.recalls_strong, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt window closes at the next session's boot when stop is missing", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-nostop-"));
  try {
    await ensureVault(root);
    // Session A boots, does one unstamped learn, then crashes (no stop).
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-A",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "session A learning",
      details: { ok: true },
    });
    // An entry stamped for another session inside A's window must never count.
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "interleaved foreign turn",
      details: { recall_strong: true, session_id: "sess-Z" },
    });
    // Session B boots — this closes A's window even without a `stop sess-A`.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-B",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "session B learning",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "session B turn",
      details: { recall_strong: true, session_id: "sess-B" },
    });

    const receipt = await sessionReceipt(root, "sess-A");
    assert.equal(receipt.learns, 1, "must not absorb session B's learn");
    assert.equal(receipt.recalls_strong, 0, "foreign-stamped turns never count");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt synthetic fallback counts stamped in-window turns when opted in", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-synth-"));
  try {
    await ensureVault(root);
    // Runtime stamped real ids on turns but omitted session_id at Stop:
    // the Stop handler falls back to "session" and must opt into counting
    // stamped rows inside its window, else the receipt lies with zeros.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot session",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "real turn",
      details: { recall_strong: true, session_id: "real-id-1" },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop session",
      details: { candidates: 0 },
    });

    const strict = await sessionReceipt(root, "session");
    assert.equal(strict.recalls_strong, 0, "strict mode excludes foreign stamps");
    const merged = await sessionReceipt(root, "session", 500, {
      includeStamped: true,
    });
    assert.equal(merged.recalls_strong, 1, "opt-in counts stamped in-window turns");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("hook-audit throttle is per tool: one turn's lifecycle entries all land", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-throttle-"));
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  const savedHome = process.env.MINNI_HOME;
  delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  try {
    const vault = path.join(root, "codex-vault");
    await ensureVault(vault);
    // Same turn, seconds apart: prompt-submit then guard denial. Distinct
    // hook tools must BOTH be recorded (else receipts undercount guards),
    // while a repeat of the SAME tool within 5s still throttles.
    await recordAudit(vault, {
      tool: "hook_codex_user_prompt_submit",
      summary: "turn",
      details: { recall_strong: true },
    });
    await recordAudit(vault, {
      tool: "hook_codex_pretooluse_guard",
      summary: "recall guard denied Grep (mode=soft)",
      details: { consumed: true },
    });
    await recordAudit(vault, {
      tool: "hook_codex_pretooluse_guard",
      summary: "recall guard denied Read (mode=soft) repeat",
      details: { consumed: true },
    });

    const tail = await auditTail(vault, 10);
    const text = tail.entries.join("\n");
    assert.ok(text.includes("hook_codex_user_prompt_submit"));
    assert.ok(text.includes("recall guard denied Grep"),
      "a distinct hook tool within 5s must not be throttled");
    assert.ok(!text.includes("repeat"),
      "a same-tool repeat within 5s must still throttle");
  } finally {
    if (savedBypass !== undefined) process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    if (savedHome !== undefined) process.env.MINNI_HOME = savedHome;
    else delete process.env.MINNI_HOME;
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt counts only the LATEST cycle when a session id is reused", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-cycle-"));
  try {
    await ensureVault(root);
    // Cycle 1 (finished): must not leak into cycle 2's receipt even though
    // the session id and stamps are identical.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-R",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "cycle one turn",
      details: { recall_strong: true, session_id: "sess-R" },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-R",
      details: { candidates: 3 },
    });
    // Cycle 2 (current): one strong turn, no stop yet (receipt runs at Stop).
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-R",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "cycle two turn",
      details: { recall_strong: true, session_id: "sess-R" },
    });

    const receipt = await sessionReceipt(root, "sess-R");
    assert.equal(receipt.recalls_strong, 1, "must not count cycle one's turn");
    assert.equal(receipt.candidates_drafted, 0,
      "must not count cycle one's stop candidates");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt synthetic fallback opens the window at the real boot marker", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-realboot-"));
  try {
    await ensureVault(root);
    // SessionStart had the real id (boot marker says so); Stop's payload
    // omitted it, so the receipt runs with the synthetic id + includeStamped.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot real-id-9",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "stamped turn",
      details: { recall_strong: true, session_id: "real-id-9" },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "unstamped learn",
      details: { ok: true },
    });

    const receipt = await sessionReceipt(root, "session", 500, {
      includeStamped: true,
    });
    assert.equal(receipt.recalls_strong, 1,
      "the window must open at the real boot marker, not 'boot session'");
    assert.equal(receipt.learns, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt reads the rolling log so a boot outside today's daily file is found", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-midnight-"));
  try {
    await ensureVault(root);
    // Session A's boot/learn/stop happened "yesterday": present in the rolling
    // log.md but absent from today's daily file (which recordAudit would have
    // dual-written yesterday, into yesterday's date file).
    const yesterday = [
      "## [2026-07-14T23:50:00.000Z] hook_codex_session_start | boot sess-mid\n\n",
      "## [2026-07-14T23:55:00.000Z] minni_learn | pre-midnight learning\n\n",
    ].join("");
    const { appendFile } = await import("node:fs/promises");
    await appendFile(path.join(root, "log.md"), yesterday, "utf8");
    // Today's activity for the same session (dual-written normally).
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "post-midnight turn",
      details: { recall_strong: true, session_id: "sess-mid" },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-mid",
      details: { candidates: 1 },
    });

    const receipt = await sessionReceipt(root, "sess-mid");
    assert.equal(receipt.learns, 1, "pre-midnight learn must be attributed");
    assert.equal(receipt.recalls_strong, 1);
    assert.equal(receipt.candidates_drafted, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// listSessions: read-only, per-boot-cycle rollup over the rolling log.md.
// ---------------------------------------------------------------------------

test("listSessions returns completed sessions newest-first with per-window tallies", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-two-"));
  try {
    await ensureVault(root);
    // Session 1: one strong recall + one learn, stop with 2 candidates.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-1",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "s1 turn",
      details: { recall_strong: true, session_id: "sess-1" },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "s1 learn",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-1",
      details: { candidates: 2 },
    });
    // Session 2: one weak recall, one vault write, stop with 5 candidates.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-2",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "s2 turn",
      details: { recall_strong: false, session_id: "sess-2" },
    });
    await recordAudit(root, {
      tool: "vault_write",
      summary: "s2 write",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-2",
      details: { candidates: 5 },
    });

    const sessions = await listSessions(root);
    assert.equal(sessions.length, 2);

    // Newest first: sess-2 leads.
    assert.equal(sessions[0].session_id, "sess-2");
    assert.equal(sessions[0].open, false);
    assert.ok(sessions[0].boot_at, "boot_at is the marker ISO timestamp");
    assert.ok(sessions[0].stop_at, "stop_at is the marker ISO timestamp");
    assert.equal(sessions[0].receipt.recalls_weak, 1);
    assert.equal(sessions[0].receipt.recalls_strong, 0);
    assert.equal(sessions[0].receipt.vault_writes, 1);
    assert.equal(sessions[0].receipt.candidates_drafted, 5);
    assert.equal(sessions[0].receipt.session_id, "sess-2");
    assert.equal(
      sessions[0].receipt_line,
      formatSessionReceiptLine(sessions[0].receipt),
    );

    assert.equal(sessions[1].session_id, "sess-1");
    assert.equal(sessions[1].open, false);
    assert.equal(sessions[1].receipt.recalls_strong, 1);
    assert.equal(sessions[1].receipt.learns, 1);
    assert.equal(sessions[1].receipt.candidates_drafted, 2);
    // Cross-window isolation: sess-1 must not absorb sess-2 activity.
    assert.equal(sessions[1].receipt.vault_writes, 0);
    assert.equal(sessions[1].receipt.recalls_weak, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("listSessions marks an unstopped session open with a null stop_at", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-open-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-live",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "live turn",
      details: { recall_strong: true, session_id: "sess-live" },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "live learn",
      details: { ok: true },
    });

    const sessions = await listSessions(root);
    assert.equal(sessions.length, 1);
    assert.equal(sessions[0].session_id, "sess-live");
    assert.equal(sessions[0].open, true);
    assert.equal(sessions[0].stop_at, null);
    assert.ok(sessions[0].boot_at);
    // Activity up to the tail end is counted.
    assert.equal(sessions[0].receipt.recalls_strong, 1);
    assert.equal(sessions[0].receipt.learns, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("listSessions closes an orphaned window at the next boot, excluding its successor", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-orphan-"));
  try {
    await ensureVault(root);
    // Session A boots, learns, then dies without a stop marker.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-A",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "A learn",
      details: { ok: true },
    });
    // Session B boots (closes A's window) and does its own work + stop.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-B",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "B turn",
      details: { recall_strong: true, session_id: "sess-B" },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-B",
      details: { candidates: 1 },
    });

    const sessions = await listSessions(root);
    assert.equal(sessions.length, 2);
    // Newest first: B leads.
    const byId = Object.fromEntries(sessions.map((s) => [s.session_id, s]));
    assert.equal(byId["sess-A"].receipt.learns, 1, "A keeps its own learn");
    assert.equal(
      byId["sess-A"].receipt.recalls_strong,
      0,
      "A's orphaned window must exclude B's recall",
    );
    assert.equal(byId["sess-A"].open, true, "A never stopped, so it is open");
    assert.equal(byId["sess-A"].stop_at, null);
    assert.equal(byId["sess-B"].receipt.recalls_strong, 1);
    assert.equal(byId["sess-B"].open, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("listSessions yields one row per boot cycle for a reused session id", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-reuse-"));
  try {
    await ensureVault(root);
    // Cycle 1: 3 candidates staged at stop.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-R",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "cycle 1 turn",
      details: { recall_strong: true, session_id: "sess-R" },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-R",
      details: { candidates: 3 },
    });
    // Cycle 2: same id, different work, still open.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-R",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "cycle 2 learn",
      details: { ok: true },
    });

    const sessions = await listSessions(root);
    assert.equal(sessions.length, 2, "two boot cycles yield two rows");
    assert.equal(sessions[0].session_id, "sess-R");
    assert.equal(sessions[1].session_id, "sess-R");
    // Newest first: cycle 2 (open, one learn, no candidates).
    assert.equal(sessions[0].open, true);
    assert.equal(sessions[0].receipt.learns, 1);
    assert.equal(sessions[0].receipt.candidates_drafted, 0);
    assert.equal(sessions[0].receipt.recalls_strong, 0);
    // Cycle 1 (completed, one recall, 3 candidates).
    assert.equal(sessions[1].open, false);
    assert.equal(sessions[1].receipt.learns, 0);
    assert.equal(sessions[1].receipt.candidates_drafted, 3);
    assert.equal(sessions[1].receipt.recalls_strong, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("listSessions is read-only: a nonexistent vault yields [] and creates nothing", async () => {
  const parent = await mkdtemp(path.join(tmpdir(), "sm-list-readonly-"));
  const missing = path.join(parent, "no-such-vault");
  try {
    const sessions = await listSessions(missing);
    assert.deepEqual(sessions, []);
    // Nothing was created — the path must still not exist.
    const { stat } = await import("node:fs/promises");
    await assert.rejects(stat(missing), "listSessions must not create the vault");
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

test("listSessions tallies agree with sessionReceipt for the same completed session", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-agree-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot sess-X",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "x turn",
      details: { recall_strong: true, session_id: "sess-X" },
    });
    await recordAudit(root, {
      tool: "hook_codex_pretooluse_guard",
      summary: "recall guard denied Edit (mode=strict)",
      details: { consumed: true, session_id: "sess-X" },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "x learn",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-X",
      details: { candidates: 4 },
    });

    const direct = await sessionReceipt(root, "sess-X");
    const sessions = await listSessions(root);
    assert.equal(sessions.length, 1);
    assert.deepEqual(sessions[0].receipt, direct);
    assert.equal(sessions[0].receipt_line, formatSessionReceiptLine(direct));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("listSessions counts stamped rows inside a synthetic boot window (Stop parity)", async () => {
  // Runtimes whose Stop payload lacks a session id write synthetic
  // `boot session` / `stop session` markers while turn rows carry the real
  // runtime id in details.session_id. The Stop hook tallies those with
  // includeStamped: true — the Sessions catalogue must agree, not show zeros.
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-synth-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "hook_grok_session_start",
      summary: "boot session",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_grok_user_prompt_submit",
      summary: "turn",
      details: { recall_strong: true, session_id: "real-runtime-id" },
    });
    await recordAudit(root, {
      tool: "hook_grok_pretooluse_guard",
      summary: "recall guard denied Grep",
      details: { session_id: "real-runtime-id" },
    });
    await recordAudit(root, {
      tool: "hook_grok_stop",
      summary: "stop session",
      details: { candidates: 1 },
    });

    const rows = await listSessions(root);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].session_id, "session");
    assert.equal(rows[0].receipt.recalls_strong, 1);
    assert.equal(rows[0].receipt.guard_denied, 1);
    assert.equal(rows[0].receipt.candidates_drafted, 1);

    // Parity with what the Stop hook computed for this window.
    const stopReceipt = await sessionReceipt(root, "session", 500, {
      includeStamped: true,
    });
    assert.deepEqual(
      { ...rows[0].receipt, session_id: "session" },
      { ...stopReceipt, session_id: "session" },
    );

    // A real-id boot window keeps strict stamp filtering: foreign stamps drop.
    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot real-2",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "turn2",
      details: { recall_strong: true, session_id: "someone-else" },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop real-2",
      details: { candidates: 0 },
    });
    const rows2 = await listSessions(root);
    const real2 = rows2.find((r) => r.session_id === "real-2");
    assert.ok(real2);
    assert.equal(real2.receipt.recalls_strong, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("listSessions caps rows at limit and honors the parseLimit horizon", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-list-limit-"));
  try {
    await ensureVault(root);
    // Three complete sessions, three entries each (boot, turn, stop) = 9 rows.
    for (const n of [1, 2, 3]) {
      await recordAudit(root, {
        tool: "hook_codex_session_start",
        summary: `boot sess-${n}`,
        details: { daemon_ok: true },
      });
      await recordAudit(root, {
        tool: "hook_codex_user_prompt_submit",
        summary: `turn ${n}`,
        details: { recall_strong: true, session_id: `sess-${n}` },
      });
      await recordAudit(root, {
        tool: "hook_codex_stop",
        summary: `stop sess-${n}`,
        details: { candidates: n },
      });
    }

    // limit caps the newest N rows.
    const capped = await listSessions(root, 2);
    assert.equal(capped.length, 2);
    assert.equal(capped[0].session_id, "sess-3");
    assert.equal(capped[1].session_id, "sess-2");

    // parseLimit slices the entry tail: the last 3 entries are sess-3's cycle
    // only, so only its boot marker survives the horizon.
    const horizoned = await listSessions(root, 10, 3);
    assert.equal(horizoned.length, 1);
    assert.equal(horizoned[0].session_id, "sess-3");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Stop-marker boundary: legacy/suffixed stop summaries.
//
// handleStopCore now writes a bare `stop <id>` on every path (breadcrumb
// reasons live in details), but a rolling log.md outlives the upgrade: vaults
// written by pre-receipts builds still carry `stop <id>: no draftable signal`
// and `stop <id>: no candidates after scrub` rows. Those must still close
// their own window, while a FOREIGN suffixed stop — or a stop whose id merely
// shares our prefix — must not be mistaken for ours.
// ---------------------------------------------------------------------------
test("sessionReceipt window closes at a `no draftable signal` stop variant", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-stopvar1-"));
  try {
    await ensureVault(root);
    const sid = "sess-nds";

    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: `boot ${sid}`,
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "a task",
      details: { recall_strong: true, session_id: sid },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: `stop ${sid}: no draftable signal`,
      details: { candidates: 0 },
    });
    // Post-stop activity from another session must stay out of the window.
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "later task",
      details: { recall_strong: true, session_id: "sess-later" },
    });

    const receipt = await sessionReceipt(root, sid);
    // boot + turn + stop, inclusive of the suffixed stop row.
    assert.equal(receipt.entries, 3);
    assert.equal(receipt.recalls_strong, 1);
    assert.equal(receipt.candidates_drafted, 0);

    const [row] = await listSessions(root, 10);
    assert.equal(row.session_id, sid);
    assert.equal(row.open, false, "suffixed stop must close the session");
    assert.ok(row.stop_at, "suffixed stop must supply a stop_at timestamp");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt window closes at a `no candidates after scrub` stop variant", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-stopvar2-"));
  try {
    await ensureVault(root);
    const sid = "sess-scrub";

    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: `boot ${sid}`,
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "committed a learning",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: `stop ${sid}: no candidates after scrub`,
      details: { candidates: 0 },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "later task",
      details: { recall_strong: true, session_id: "sess-later" },
    });

    const receipt = await sessionReceipt(root, sid);
    assert.equal(receipt.entries, 3);
    assert.equal(receipt.learns, 1);
    assert.equal(receipt.recalls_strong, 0, "post-stop rows must not leak in");

    const [row] = await listSessions(root, 10);
    assert.equal(row.session_id, sid);
    assert.equal(row.open, false);
    assert.notEqual(row.stop_at, null);
    assert.equal(row.receipt.entries, 3);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt closes exclusively at a FOREIGN suffixed stop", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-foreign-stop-"));
  try {
    await ensureVault(root);
    const sid = "sess-mine";

    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: `boot ${sid}`,
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "my task",
      details: { recall_strong: true, session_id: sid },
    });
    // Another session's suffixed stop: closes our window EXCLUSIVELY and is
    // never read as our own stop.
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop sess-other: no draftable signal",
      details: { candidates: 4 },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "other task",
      details: { recall_strong: true, session_id: "sess-other" },
    });

    const receipt = await sessionReceipt(root, sid);
    assert.equal(receipt.entries, 2, "boot + own turn only");
    assert.equal(receipt.recalls_strong, 1);
    assert.equal(
      receipt.candidates_drafted,
      0,
      "a foreign stop's candidates must not be attributed to us",
    );

    const [row] = await listSessions(root, 10);
    assert.equal(row.session_id, sid);
    assert.equal(row.open, true, "a foreign stop must not stop our session");
    assert.equal(row.stop_at, null);
    assert.equal(row.receipt.entries, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("sessionReceipt does not claim a stop row whose id merely shares a prefix", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-receipt-prefix-"));
  try {
    await ensureVault(root);

    await recordAudit(root, {
      tool: "hook_codex_session_start",
      summary: "boot abc",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "abc task",
      details: { recall_strong: true, session_id: "abc" },
    });
    // `stop abcdef` belongs to session "abcdef", NOT to "abc".
    await recordAudit(root, {
      tool: "hook_codex_stop",
      summary: "stop abcdef",
      details: { candidates: 9 },
    });
    await recordAudit(root, {
      tool: "hook_codex_user_prompt_submit",
      summary: "abcdef task",
      details: { recall_strong: true, session_id: "abcdef" },
    });

    const receipt = await sessionReceipt(root, "abc");
    assert.equal(receipt.entries, 2, "prefix-sharing stop must not close us");
    assert.equal(receipt.candidates_drafted, 0);

    const rows = await listSessions(root, 10);
    const abc = rows.find((row) => row.session_id === "abc");
    assert.ok(abc);
    assert.equal(abc.open, true);
    assert.equal(abc.stop_at, null);
    assert.equal(abc.receipt.entries, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// --- per-turn Stop hooks: one boot cycle owns MANY stop rows ---------------
// Claude Code maps Stop to end-of-TURN, not end-of-session, so a single
// `boot <id>` is followed by a `stop <id>` per turn. Closing the window at the
// first own-stop froze every later receipt at turn one and reported a live
// session as closed. These lock the last-own-stop rule in.

test("sessionReceipt spans every turn when Stop fires per turn, not just the first", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-multistop-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sess-T",
      details: { daemon_ok: true },
    });
    // Turn 1: recall + its end-of-turn stop.
    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "turn one",
      details: { recall_strong: true, session_id: "sess-T" },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-T",
      details: { candidates: 1 },
    });
    // Turn 2: more work, then a SECOND stop for the same boot.
    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "turn two",
      details: { recall_strong: true, session_id: "sess-T" },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "turn two learn",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-T",
      details: { candidates: 2 },
    });

    const receipt = await sessionReceipt(root, "sess-T");
    assert.equal(
      receipt.recalls_strong,
      2,
      "turn two's recall must count, not just turn one's",
    );
    assert.equal(receipt.learns, 1, "turn two's learn must count");
    assert.equal(
      receipt.candidates_drafted,
      3,
      "candidates from BOTH stop rows belong to this cycle",
    );

    const [row] = await listSessions(root, 10);
    assert.equal(row.session_id, "sess-T");
    assert.equal(row.open, false);
    assert.equal(
      row.stop_at,
      (await auditTail(root, 1)).entries.at(-1)?.timestamp ?? row.stop_at,
      "stop_at reports the LAST turn boundary",
    );
    assert.equal(row.receipt.recalls_strong, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a cycle survives foreign boots interleaved between its own stop rows", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-interleave-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sess-M",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-M",
      details: { candidates: 0 },
    });
    // Subagent / sibling-session boots land in the SAME vault log mid-cycle.
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sub-1",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sub-2",
      details: { daemon_ok: true },
    });
    // The parent keeps working and stops again — proof it outlived them.
    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "post-subagent turn",
      details: { recall_strong: true, session_id: "sess-M" },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-M",
      details: { candidates: 4 },
    });

    const receipt = await sessionReceipt(root, "sess-M");
    assert.equal(
      receipt.recalls_strong,
      1,
      "work after an interleaved foreign boot still belongs to this cycle",
    );
    assert.equal(receipt.candidates_drafted, 4);

    const rows = await listSessions(root, 10);
    const parent = rows.find((r) => r.session_id === "sess-M");
    assert.ok(parent);
    assert.equal(parent.open, false, "the parent's last stop closes it");
    assert.equal(parent.receipt.recalls_strong, 1);
    // The subagent boots are still their own rows, and stay empty.
    assert.equal(rows.find((r) => r.session_id === "sub-1")?.receipt.learns, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a re-boot of the same id still splits cycles even when both cycles stop", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-reboot-split-"));
  try {
    await ensureVault(root);
    // Cycle 1: one recall, one stop.
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sess-S",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "cycle one turn",
      details: { recall_strong: true, session_id: "sess-S" },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-S",
      details: { candidates: 1 },
    });
    // Cycle 2: SAME id re-booted, its own work and stop.
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sess-S",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "cycle two learn",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-S",
      details: { candidates: 9 },
    });

    const rows = await listSessions(root, 10);
    assert.equal(rows.length, 2, "two boot cycles still yield two rows");
    const [newest, oldest] = rows;
    assert.equal(
      oldest.receipt.candidates_drafted,
      1,
      "cycle one must NOT swallow cycle two's stop",
    );
    assert.equal(oldest.receipt.learns, 0, "cycle two's learn is not cycle one's");
    assert.equal(newest.receipt.candidates_drafted, 9);
    assert.equal(newest.receipt.learns, 1);

    // sessionReceipt targets the LATEST cycle only.
    const receipt = await sessionReceipt(root, "sess-S");
    assert.equal(receipt.candidates_drafted, 9);
    assert.equal(receipt.recalls_strong, 0, "cycle one's recall stays behind");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("an orphaned cycle is still cut at the foreign boot when it never stops", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-orphan-guard-"));
  try {
    await ensureVault(root);
    // Session D boots and dies with no stop row of its own.
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sess-D",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "minni_learn",
      summary: "D learn",
      details: { ok: true },
    });
    // Successor E boots, works, stops — none of it may leak backwards into D.
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "boot sess-E",
      details: { daemon_ok: true },
    });
    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "E turn",
      details: { recall_strong: true, session_id: "sess-E" },
    });
    await recordAudit(root, {
      tool: "hook_stop",
      summary: "stop sess-E",
      details: { candidates: 7 },
    });

    const rows = await listSessions(root, 10);
    const d = rows.find((r) => r.session_id === "sess-D");
    assert.ok(d);
    assert.equal(d.open, true, "D never stopped, so it stays open");
    assert.equal(d.stop_at, null);
    assert.equal(d.receipt.learns, 1, "D keeps its own learn");
    assert.equal(d.receipt.recalls_strong, 0, "D must not absorb E's recall");
    assert.equal(d.receipt.candidates_drafted, 0, "D must not absorb E's stop");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
