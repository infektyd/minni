import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditTail,
  auditReport,
  ensureVault,
  formatSessionReceiptLine,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  sessionReceipt,
  vaultFirstLearn,
  writeVaultPage,
} from "../dist/vault.js";
import { symlink } from "node:fs/promises"; // for RCM-005 escape test

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
