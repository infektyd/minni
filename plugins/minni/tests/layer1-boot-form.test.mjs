import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { fitEnvelope } from "../dist/agent_envelope.js";
import { LAYER1_SHELF_MAX_BYTES, readLayer1Shelf } from "../dist/vault.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const CLAUDE_HOOK = path.join(PLUGIN_ROOT, "dist", "hook.js");

function runSessionStart(vault, root, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CLAUDE_HOOK, "SessionStart"], {
      cwd: PLUGIN_ROOT,
      env: {
        ...process.env,
        MINNI_HOME: root,
        MINNI_CLAUDECODE_VAULT_PATH: vault,
        MINNI_SOCKET_PATH: path.join(root, "missing.sock"),
        MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
        MINNI_BYPASS_AUDIT_LIMIT: "true",
        MINNI_CLAUDECODE_HOOKS: "on",
        ...extraEnv,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    child.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    child.on("error", reject);
    child.on("close", (code) => {
      assert.equal(code, 0, `SessionStart failed: ${stderr}`);
      const out = JSON.parse(stdout);
      const ctx = out?.hookSpecificOutput?.additionalContext ?? "";
      const m = ctx.match(/<minni:context[^>]*>\n([\s\S]*?)\n<\/minni:context>/);
      assert.ok(m, `missing <minni:context> in ${ctx}`);
      resolve({ body: JSON.parse(m[1]), additionalContext: ctx });
    });
    child.stdin.write(JSON.stringify({ session_id: "sess-boot-form", cwd: root }));
    child.stdin.end();
  });
}

test(
  "layer1 boot form clips at paragraph boundary <= 4,000 chars with marker and preserves reader fields",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "sm-s2a-bootform-"));
    t.after(() => rm(root, { recursive: true, force: true }));

    const vault = path.join(root, "claudecode-vault");
    await mkdir(path.join(vault, "layer1"), { recursive: true });

    const paragraphs = Array.from(
      { length: 45 },
      (_, i) => `### Paragraph ${i + 1}\n${"Rule content for paragraph boundary testing. ".repeat(4)}`,
    ).join("\n\n");
    assert.ok(paragraphs.length > LAYER1_SHELF_MAX_BYTES, "test fixture must exceed 8 KB");
    await writeFile(path.join(vault, "layer1", "core.md"), paragraphs, "utf8");

    // Raw reader still returns 8192 bytes and exact omittedBytes
    const rawRead = await readLayer1Shelf(vault);
    assert.equal(rawRead.ok, true);
    assert.equal(rawRead.truncated, true);
    assert.equal(rawRead.content.length, LAYER1_SHELF_MAX_BYTES);
    assert.equal(rawRead.omittedBytes, Buffer.byteLength(paragraphs, "utf8") - LAYER1_SHELF_MAX_BYTES);

    const { body } = await runSessionStart(vault, root);
    assert.equal(body.layer1_shelf.ok, true);
    assert.equal(body.layer1_shelf.truncated, true);
    assert.equal(body.layer1_shelf.inline_trim, true);
    assert.equal(body.layer1_shelf.omitted_bytes, paragraphs.length - LAYER1_SHELF_MAX_BYTES);
    assert.ok(body.layer1_shelf.content.length <= 4000);

    const markerMatch = body.layer1_shelf.content.match(
      /\n\n\[layer1 shelf: boot form, (\d+) of (\d+) chars; full file: layer1\/core\.md\]$/,
    );
    assert.ok(markerMatch, `expected paragraph-separated boot form marker in:\n${body.layer1_shelf.content.slice(-140)}`);
    const excerptLen = Number(markerMatch[1]);
    const totalLen = Number(markerMatch[2]);
    assert.equal(totalLen, paragraphs.length);
    const excerpt = body.layer1_shelf.content.slice(0, body.layer1_shelf.content.length - markerMatch[0].length);
    assert.equal(excerpt.length, excerptLen);
    assert.ok(
      paragraphs.startsWith(excerpt + "\n\n"),
      "excerpt should end cleanly on a paragraph boundary (\\n\\n)",
    );
  },
);

test(
  "layer1 boot form prefers layer1/boot.md when present and refuses symlinked boot.md",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "sm-s2a-bootmd-"));
    t.after(() => rm(root, { recursive: true, force: true }));

    const vault = path.join(root, "claudecode-vault");
    await mkdir(path.join(vault, "layer1"), { recursive: true });

    const bigCore = "Core paragraph.\n\n".repeat(600);
    const curatedBoot = "# Curated Boot Shelf\n\nEssential orientation for SessionStart.";
    await writeFile(path.join(vault, "layer1", "core.md"), bigCore, "utf8");
    await writeFile(path.join(vault, "layer1", "boot.md"), curatedBoot, "utf8");

    const { body } = await runSessionStart(vault, root);
    assert.equal(body.layer1_shelf.inline_trim, true);
    assert.ok(
      body.layer1_shelf.content.startsWith(curatedBoot),
      `expected boot.md content to be used, got: ${body.layer1_shelf.content.slice(0, 100)}`,
    );
    assert.match(
      body.layer1_shelf.content,
      new RegExp(`\\[layer1 shelf: boot form, ${curatedBoot.length} of ${bigCore.length} chars; full file: layer1/core\\.md\\]$`),
    );

    // Replace boot.md with a symlink -> must be refused (O_NOFOLLOW) and fall back to core.md
    await rm(path.join(vault, "layer1", "boot.md"));
    const outsideTarget = path.join(root, "outside-boot.md");
    await writeFile(outsideTarget, "SECRET OUTSIDE CONTENT", "utf8");
    await symlink(outsideTarget, path.join(vault, "layer1", "boot.md"));

    const { body: symlinkBody } = await runSessionStart(vault, root);
    assert.ok(
      !symlinkBody.layer1_shelf.content.includes("SECRET OUTSIDE CONTENT"),
      "symlinked layer1/boot.md must be refused",
    );
    assert.ok(symlinkBody.layer1_shelf.content.startsWith("Core paragraph."));
  },
);

test(
  "fitEnvelope trims layer1_shelf (Step 6) BEFORE active_thread (Step 8) so active plan keeps full slices (Appendix B F8)",
  () => {
    const shelfText = Array.from(
      { length: 30 },
      (_, i) => `## Shelf Rule ${i + 1}\n${"Shelf invariant text that takes space. ".repeat(6)}`,
    ).join("\n\n");

    const activeThread = {
      plan_id: "plan-s2a",
      rev: 3,
      view: {
        headline: "Recall-to-Use S2a Boot Fit",
        goal: "Keep SessionStart under the 10k-char Claude Code inline cap without losing active_thread.",
        next_action: "Verify Step 6 trims shelf before Step 8.",
        progress: { total: 2, completed: 1, in_progress: 1, pending: 0, blocked: 0 },
        slices: [
          {
            id: "S2a",
            title: "Boot Envelope & Shelf Boot Form",
            status: "in_progress",
            description: "Implement fitEnvelope, serializeEnvelope, diet, and layer1 boot form.",
          },
        ],
        open_questions: ["Should layer1/boot.md override core.md excerpt when present?"],
        scar_tissue: 2,
      },
    };

    const body = {
      contract: "MEMORY CONTRACT",
      identity_body: { ok: true, content: "# Identity\nShort identity." },
      layer1_shelf: {
        ok: true,
        path: "layer1/core.md",
        content: shelfText,
        truncated: false,
      },
      active_thread: activeThread,
      recall: { ok: true, results: [], episodic: [], note: "no boot recall results; call minni_recall" },
      audit_tail: [],
    };

    // Total raw envelope is ~8.5 KB, set charLimit to 6,000 so Step 6 (layer1_shelf) is required
    // and sufficient without reaching Step 8 (active_thread).
    const fitted = fitEnvelope(body, { tokenLimit: 1800, charLimit: 6000 });

    assert.equal(fitted.layer1_shelf.inline_trim, true, "layer1_shelf must be trimmed at Step 6");
    assert.ok(fitted.layer1_shelf.content.length <= 4000);
    assert.ok(
      fitted.trimmed?.layer1_shelf,
      "trimmed map must record layer1_shelf",
    );
    assert.equal(
      fitted.trimmed?.active_thread,
      undefined,
      "active_thread must NOT be trimmed when layer1_shelf boot form brings envelope within budget",
    );
    assert.ok(
      Array.isArray(fitted.active_thread.view?.slices) &&
        fitted.active_thread.view.slices.length === 1,
      "active_thread must retain its full view.slices (not demoted to pointer form)",
    );
    assert.equal(fitted.active_thread.pull, undefined);
  },
);
