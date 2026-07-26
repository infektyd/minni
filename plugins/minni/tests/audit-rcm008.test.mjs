import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { recordAudit, ensureVault, vaultFirstLearn } from "../dist/vault.js";
import { buildStatusReport } from "../dist/sovereign.js";

test("RCM-008: hook rate-limiting drops duplicate hook audit entries without failing", async () => {
  // Use a folder ending in -vault so the agent ID is mapped cleanly to the basename
  const root = await mkdtemp(path.join(tmpdir(), "rate-limit-vault-"));
  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));

  const origBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  const origHome = process.env.MINNI_HOME;

  try {
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "false";
    process.env.MINNI_HOME = home;

    await ensureVault(root);

    const now = new Date();
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "First audit",
      timestamp: now,
    });

    // Same event again, inside the window: a genuine duplicate -> dropped.
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "Second audit too fast",
      timestamp: new Date(now.getTime() + 2000),
    });

    // A DIFFERENT event inside the window must survive. The throttle used to
    // key on the agent alone, so a burst of distinct events collapsed into
    // one record -- on agy, SessionStart and PreInvocation fire in the same
    // second, so PreInvocation was never audited and looked like it had never
    // dispatched at all. Distinct events are not duplicates.
    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "Distinct event in window",
      timestamp: new Date(now.getTime() + 2000),
    });

    const okPath = await recordAudit(root, {
      tool: "hook_session_start",
      summary: "Third audit ok",
      timestamp: new Date(now.getTime() + 8000),
    });
    assert.ok(okPath);

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /First audit/);
    assert.doesNotMatch(log, /Second audit too fast/);
    assert.match(log, /Distinct event in window/);
    assert.match(log, /Third audit ok/);
  } finally {
    if (origBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = origBypass;
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
    await rm(home, { recursive: true, force: true });
  }
});

test("RCM-008: hook rate-limiting timestamp file has strict permissions", async () => {
  const tmpRoot = await mkdtemp(path.join(tmpdir(), "sm-rate-limit-"));
  const root = path.join(tmpRoot, "rate-limit-vault");
  await mkdir(root, { recursive: true });

  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));

  const origBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  const origHome = process.env.MINNI_HOME;

  try {
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "false";
    process.env.MINNI_HOME = home;

    await ensureVault(root);

    const now = new Date();
    await recordAudit(root, {
      tool: "hook_session_start",
      summary: "First audit",
      timestamp: now,
    });

    await recordAudit(root, {
      tool: "hook_user_prompt_submit",
      summary: "Second audit too fast",
      timestamp: new Date(now.getTime() + 2000),
    });

    const okPath = await recordAudit(root, {
      tool: "hook_pre_compact",
      summary: "Third audit ok",
      timestamp: new Date(now.getTime() + 5000),
    });
    assert.ok(okPath);

    // Verify rate limit file has mode 0o600 (on UNIX platforms).
    // The throttle key is per (agent, event), so the filename carries both.
    const agentTsFile = path.join(home, ".hook-audit-ts", "rate-limit__hook_session_start.ts");
    const st = await stat(agentTsFile);
    if (process.platform !== "win32") {
      const mode = st.mode & 0o777;
      assert.equal(mode, 0o600);
    }
  } finally {
    if (origBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = origBypass;
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(tmpRoot, { recursive: true, force: true });
    await rm(home, { recursive: true, force: true });
  }
});

test("RCM-008: daily-log pruning (older than 30 days relative to audit timestamp)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-daily-prune-"));
  const origHome = process.env.MINNI_HOME;
  process.env.MINNI_HOME = root;

  try {
    await ensureVault(root);

    const logsDir = path.join(root, "logs");
    await mkdir(logsDir, { recursive: true });

    // Write a log from 31 days ago and 29 days ago
    const day31Path = path.join(logsDir, "2026-04-10.md");
    const day29Path = path.join(logsDir, "2026-04-12.md");

    await writeFile(day31Path, "old logs", "utf8");
    await writeFile(day29Path, "recent logs", "utf8");

    // Record audit with a timestamp set to 2026-05-11 (31 days after 2026-04-10, 29 days after 2026-04-12)
    const auditTime = new Date("2026-05-11T12:00:00.000Z");

    const oldBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
    try {
      await recordAudit(root, {
        tool: "test_tool",
        summary: "Pruning run",
        timestamp: auditTime,
      });
    } finally {
      process.env.MINNI_BYPASS_AUDIT_LIMIT = oldBypass;
    }

    // 31-day-old file should be deleted
    await assert.rejects(stat(day31Path));
    // 29-day-old file should still exist
    const st29 = await stat(day29Path);
    assert.ok(st29.isFile());
  } finally {
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
  }
});

test("RCM-008: rotation at 5 MB (log.md -> log.1.md -> log.2.md -> log.3.md)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-rotation-"));
  const origHome = process.env.MINNI_HOME;
  process.env.MINNI_HOME = root;

  try {
    await ensureVault(root);

    const logPath = path.join(root, "log.md");

    // Write 5 MB of dummy data to log.md
    const dummy = "a".repeat(5 * 1024 * 1024);
    await writeFile(logPath, dummy, "utf8");

    const oldBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
    try {
      await recordAudit(root, {
        tool: "test_tool",
        summary: "Trigger rotation",
      });
    } finally {
      process.env.MINNI_BYPASS_AUDIT_LIMIT = oldBypass;
    }

    // log.md should have been rotated to log.1.md
    const st1 = await stat(path.join(root, "log.1.md"));
    assert.ok(st1.size >= 5 * 1024 * 1024);

    // A new empty/small log.md should exist
    const stLog = await stat(logPath);
    assert.ok(stLog.size < 1000);
  } finally {
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
  }
});

test("RCM-008: quota check (50 MB cap, pruning oldest daily logs first)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-quota-"));
  const origHome = process.env.MINNI_HOME;
  process.env.MINNI_HOME = root;

  try {
    await ensureVault(root);

    const logsDir = path.join(root, "logs");
    await mkdir(logsDir, { recursive: true });

    // Write three daily logs of 20 MB each (total 60 MB, exceeding 50 MB quota)
    // Make their dates within 30 days of the audit time so they are NOT pruned by the 30-day prune.
    const auditTime = new Date("2026-05-20T12:00:00.000Z");

    const log1 = path.join(logsDir, "2026-05-18.md");
    const log2 = path.join(logsDir, "2026-05-19.md");
    const log3 = path.join(logsDir, "2026-05-20.md");

    const dummy = "x".repeat(20 * 1024 * 1024);
    await writeFile(log1, dummy, "utf8");
    await writeFile(log2, dummy, "utf8");
    await writeFile(log3, dummy, "utf8");

    const oldBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
    try {
      await recordAudit(root, {
        tool: "test_tool",
        summary: "Trigger quota prune",
        timestamp: auditTime,
      });
    } finally {
      process.env.MINNI_BYPASS_AUDIT_LIMIT = oldBypass;
    }

    // The oldest daily log (2026-05-18.md) should be deleted
    await assert.rejects(stat(log1));
    // The newer daily logs should still exist
    const st2 = await stat(log2);
    const st3 = await stat(log3);
    assert.ok(st2.isFile());
    assert.ok(st3.isFile());
  } finally {
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
  }
});

test("RCM-008: vaultFirstLearn succeeds with production audit limiter enabled", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-learn-limit-"));
  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));

  const origBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  const origHome = process.env.MINNI_HOME;

  try {
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "false";
    process.env.MINNI_HOME = home;

    const result = await vaultFirstLearn({
      vaultPath: root,
      title: "Production limiter smoke",
      content: "Learning writes must not fail because the audit path emits multiple entries.",
      category: "regression",
      source: "unit-test",
      agentId: "codex",
      storeResult: { ok: true },
    });

    const note = await readFile(result.notePath, "utf8");
    assert.match(note, /Production limiter smoke/);

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /minni_vault_write/);
    assert.match(log, /minni_learn/);
  } finally {
    if (origBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = origBypass;
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
    await rm(home, { recursive: true, force: true });
  }
});

test("RCM-008: test_audit_concurrent_writers_no_drop", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-concurrent-audit-"));
  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));

  const origBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  const origHome = process.env.MINNI_HOME;

  try {
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "false";
    process.env.MINNI_HOME = home;

    await ensureVault(root);

    const count = 20;
    await Promise.all(
      Array.from({ length: count }, (_, index) =>
        recordAudit(root, {
          tool: "test_concurrent_writer",
          summary: `concurrent audit ${index}`,
        }),
      ),
    );

    const log = await readFile(path.join(root, "log.md"), "utf8");
    for (let index = 0; index < count; index += 1) {
      assert.match(log, new RegExp(`concurrent audit ${index}\\b`));
    }
  } finally {
    if (origBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = origBypass;
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
    await rm(home, { recursive: true, force: true });
  }
});

test("RCM-008/Status: buildStatusReport returns correct audit volume in bytes", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-status-vol-"));

  try {
    await ensureVault(root);

    const logPath = path.join(root, "log.md");
    await writeFile(logPath, "x".repeat(1234), "utf8");

    const report = await buildStatusReport({ vaultPath: root });
    // Total size should be exactly 1234 (since there's no daily log created by buildStatusReport itself)
    assert.equal(report.audit.volume, 1234);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("two intent drops on DIFFERENT events both survive the 5s window", async () => {
  // Found by adversarial review (2026-07-25). The throttle buckets on
  // `entry.tool`, which is a fine stand-in for the event on normal hook audits
  // (hook_stop, hook_session_start) but NOT for drops: every drop on a platform
  // writes the same `hook_<agent>_intent_dropped` tool whatever event produced
  // it. So a Stop drop landing within 5s of a UserPromptSubmit drop was
  // swallowed -- and recordAudit returns a path either way, so the caller's
  // try/catch never saw it. Losing the record that memory failed to land is the
  // exact silent failure this whole module exists to end.
  const root = await mkdtemp(path.join(tmpdir(), "drop-throttle-vault-"));
  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));

  const origBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  const origHome = process.env.MINNI_HOME;

  try {
    process.env.MINNI_BYPASS_AUDIT_LIMIT = "false";
    process.env.MINNI_HOME = home;
    await ensureVault(root);

    const now = new Date();
    const drop = async (event, offsetMs) =>
      recordAudit(root, {
        tool: "hook_cursor_intent_dropped",
        summary: `${event}: cursor cannot carry this`,
        throttleKey: `hook_cursor_intent_dropped__${event}`,
        timestamp: new Date(now.getTime() + offsetMs),
      });

    await drop("UserPromptSubmit", 0);
    await drop("Stop", 1000);

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /UserPromptSubmit: cursor cannot carry this/);
    assert.match(log, /Stop: cursor cannot carry this/, "the second drop must survive");

    // A genuine repeat of the SAME event inside the window is still collapsed.
    await drop("Stop", 2000);
    const after = await readFile(path.join(root, "log.md"), "utf8");
    const stops = [...after.matchAll(/Stop: cursor cannot carry this/g)];
    assert.equal(stops.length, 1, "duplicate suppression must still work per event");
  } finally {
    if (origBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = origBypass;
    if (origHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = origHome;
    await rm(root, { recursive: true, force: true });
    await rm(home, { recursive: true, force: true });
  }
});
