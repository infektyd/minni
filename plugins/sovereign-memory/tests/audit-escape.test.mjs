import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { ensureVault, recordAudit, auditTail } from "../dist/vault.js";

// SEC-014: a forged summary containing `\n## [...]` must not be split into
// multiple audit entries by either the TS or Python reader.

test("recordAudit escapes newlines in summary so injected `## ` cannot forge entries", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-escape-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "sovereign_learn",
      summary: "x\n## [2099-01-01] sovereign_learn | injected",
      timestamp: new Date("2026-04-26T00:00:00.000Z"),
    });

    const tail = await auditTail(root, 50);
    assert.equal(tail.entries.length, 1, "only one audit entry should be parsed");

    const dailyPath = path.join(root, "logs", "2026-04-26.md");
    const body = await readFile(dailyPath, "utf8");

    // The literal escaped sequence must appear in the body.
    assert.ok(
      body.includes("\\n## "),
      "escaped \\n## sequence should appear verbatim in the log body",
    );
    // The forged header must NOT appear as a real heading line.
    assert.ok(
      !/^## \[2099-01-01\]/m.test(body),
      "forged `## [2099-...]` heading must not appear at start of any line",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("recordAudit escapes newlines in tool field too", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-escape-tool-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "sovereign_learn\n## [2099-01-01] forged | bad",
      summary: "ok",
      timestamp: new Date("2026-04-26T00:00:00.000Z"),
    });

    const tail = await auditTail(root, 50);
    assert.equal(tail.entries.length, 1);

    const body = await readFile(path.join(root, "logs", "2026-04-26.md"), "utf8");
    assert.ok(body.includes("\\n## "), "tool newline should be escaped");
    assert.ok(!/^## \[2099-01-01\]/m.test(body));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("recordAudit caps summary at 500 chars with ellipsis", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-cap-"));
  try {
    await ensureVault(root);
    const huge = "a".repeat(2000);
    await recordAudit(root, {
      tool: "sovereign_learn",
      summary: huge,
      timestamp: new Date("2026-04-26T00:00:00.000Z"),
    });
    const body = await readFile(path.join(root, "logs", "2026-04-26.md"), "utf8");
    // header line: "## [<ts>] sovereign_learn | <500-char summary>\n"
    const headerLine = body.split("\n").find((ln) => ln.startsWith("## [")) ?? "";
    const afterPipe = headerLine.split("| ")[1] ?? "";
    assert.equal(afterPipe.length, 500, "summary must be capped to 500 chars");
    assert.ok(afterPipe.endsWith("…"), "capped summary should end with ellipsis");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
