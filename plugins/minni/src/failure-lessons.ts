// Phase 1b (design rev5 §7, slices S4b–S4d): failure-moment lesson surfacing
// across PreToolUse (pre-emptive command signatures for hook-denied commands,
// P2/P3), PostToolUse (output text matching on every Bash call, solving the
// 350/352 exit-masked `is_error: false` test-failure blocker), and
// PostToolUseFailure.
//
// IMPORTANT: This module MUST NOT import from use-capture.js, use-ledger.js,
// flags.js, or harness-envelope.js so `use-telemetry-flag-off-cost.test.mjs`
// (which asserts none of CAPTURE_MODULES load when MINNI_USE_LEDGER=off) stays
// green on PreToolUse and PostToolUse.
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { sanitizeRecallField, sessionStateKey } from "./recall-state.js";
import { assertWriteTargetUnder } from "./vault.js";

export const POST_TOOL_USE_EVENTS: ReadonlySet<string> = new Set([
  "PostToolUse",
  "PostToolUseFailure",
]);

export const POST_TOOL_USE_MATCHER = "^(?:Bash|run_terminal_command)$";

export interface FailureLessonRule {
  readonly ruleId: string;
  readonly stage: "PreToolUse" | "PostToolUse";
  readonly pattern: RegExp;
  readonly lessonId: string;
  readonly mkey: string;
  readonly handle: string;
  readonly title: string;
  readonly lessonText: string;
  readonly projectScope: "global" | string;
}

export interface FailureLessonMatch {
  readonly ruleId: string;
  readonly lessonId: string;
  readonly mkey: string;
  readonly handle: string;
  readonly title: string;
  readonly lessonText: string;
  readonly projectKey: string;
  readonly additionalContext: string;
}

export interface PostToolUseDecisionOutput {
  continue: true;
  hookSpecificOutput?: {
    hookEventName: "PostToolUse" | "PostToolUseFailure";
    additionalContext: string;
  };
}

const READ_FLAGS =
  fs.constants.O_RDONLY |
  (fs.constants.O_NOFOLLOW ?? 0) |
  (fs.constants.O_NONBLOCK ?? 0);

const MAX_CONFIG_FILE_BYTES = 64 * 1024;
const MAX_OUTPUT_SCAN_CHARS = 100_000;
const RECALL_STATE_DIR_REL = path.join(".runtime", "recall-state.d");

export const APPROVED_FAILURE_LESSON_RULES: ReadonlyArray<FailureLessonRule> = [
  {
    ruleId: "git_diff_show_hook_block",
    stage: "PreToolUse",
    pattern:
      /(?:^|[;&|]\s*)git\s+(?:show\s+(?!--stat\b|--numstat\b|--name-only\b|--name-status\b|-s\b|--no-patch\b)[^\n|;]+|checkout\s+(?!-b\b|-B\b|--orphan\b)\S+\s+--\s+\S+)/i,
    lessonId: "scar-git-cat-file-instead-of-show",
    mkey: "learning:phase1b-git-cat-file-instead-of-show",
    handle: "m:7a11e505",
    title: "Scar #6: use git cat-file -p instead of unbounded git show / git checkout",
    lessonText:
      "Scar #6: use `git cat-file -p <rev>:<path>` to read historical file contents and `git diff --stat` or `git diff -- <path>` instead of unbounded `git show` or `git checkout <rev> -- <path>`.",
    projectScope: "global",
  },
  {
    ruleId: "swift_testing_expectation_failed",
    stage: "PostToolUse",
    pattern: /(?:Expectation failed:|✘\s+Test\s+)/i,
    lessonId: "scar-swift-testing-expectation-failed",
    mkey: "learning:phase1b-swift-testing-expectation-failed",
    handle: "m:7a11e501",
    title: "Swift Testing Expectation failed (masked exit code)",
    lessonText:
      "Never mask `swift test` exit codes with `| tail` or `; echo exit=$?`; run the failing `@Test` filter directly and inspect the exact `Expectation failed:` operand diff.",
    projectScope: "global",
  },
  {
    ruleId: "praxis_verify_source_baseline",
    stage: "PostToolUse",
    pattern: /(?:verify_source_baseline\.sh[^\n]*(?:FAIL|error|unregistered|missing|not registered)|unregistered test file)/i,
    lessonId: "scar-praxis-verify-source-baseline",
    mkey: "learning:phase1b-praxis-verify-source-baseline",
    handle: "m:7a11e503",
    title: "Scar #5: unregistered Swift test file in verify_source_baseline.sh",
    lessonText:
      "Scar #5: register every new `*Tests.swift` file in `project.pbxproj` (`PBXBuildFile` + `PBXFileReference` + `Sources`) or `membershipExceptions` in `scripts/verify_source_baseline.sh`.",
    projectScope: "global",
  },
  {
    ruleId: "swift_compiler_error",
    stage: "PostToolUse",
    pattern: /\.swift:\d+:\d+:\s+error:/i,
    lessonId: "scar-swift-compiler-error",
    mkey: "learning:phase1b-swift-compiler-error",
    handle: "m:7a11e502",
    title: "Swift compiler diagnostic error",
    lessonText:
      "Fix: check module target imports, visibility modifiers, and `project.pbxproj` target membership before re-running a full build.",
    projectScope: "global",
  },
  {
    ruleId: "xctest_assertion_failed",
    stage: "PostToolUse",
    pattern: /(?:XCTAssert[A-Za-z]*\s+failed|Test Case\s+'[^']+'\s+failed)/i,
    lessonId: "scar-xctest-assertion-failed",
    mkey: "learning:phase1b-xctest-assertion-failed",
    handle: "m:7a11e504",
    title: "XCTest assertion failure",
    lessonText:
      "Fix: isolate the failing `XCTestCase` method with `-only-testing:` and verify fixture state before running the full test plan.",
    projectScope: "global",
  },
  {
    ruleId: "git_diff_show_hook_block_post",
    stage: "PostToolUse",
    pattern: /Blocked:\s*unbounded\s+git\s+diff\/show\s+output/i,
    lessonId: "scar-git-cat-file-instead-of-show",
    mkey: "learning:phase1b-git-cat-file-instead-of-show",
    handle: "m:7a11e505",
    title: "Scar #6: use git cat-file -p instead of unbounded git show / git checkout",
    lessonText:
      "Scar #6: use `git cat-file -p <rev>:<path>` to read historical file contents and `git diff --stat` or `git diff -- <path>` instead of unbounded `git show` or `git checkout <rev> -- <path>`.",
    projectScope: "global",
  },
  {
    ruleId: "jev_guard_block",
    stage: "PostToolUse",
    pattern: /Blocked by Jev guard/i,
    lessonId: "scar-jev-guard-destructive-command",
    mkey: "learning:phase1b-jev-guard-destructive-command",
    handle: "m:7a11e506",
    title: "Jev guard blocked destructive shell command",
    lessonText:
      "Never run `rm -rf` or `git worktree remove` when blocked by the Jev guard; use non-destructive `mv` or explicit single-file operations within the owned worktree.",
    projectScope: "global",
  },
  {
    ruleId: "process_wait_until_exit_forbidden",
    stage: "PostToolUse",
    pattern: /(?:waitUntilExit\s*\(|waitpid\s*\()/i,
    lessonId: "scar-process-wait-until-exit-forbidden",
    mkey: "learning:phase1b-process-wait-until-exit-forbidden",
    handle: "m:7a11e507",
    title: "Scar #1: never call blocking waitUntilExit() / waitpid() after bounded timeout",
    lessonText:
      "Scar #1: never call blocking `waitUntilExit()` or `waitpid()` on a timed-out subprocess; terminate the process group and drain pipes asynchronously.",
    projectScope: "global",
  },
  {
    ruleId: "subagent_update_plan_blocked",
    stage: "PostToolUse",
    pattern: /Not shown:\s*you are a subagent/i,
    lessonId: "scar-subagent-update-plan-blocked",
    mkey: "learning:phase1b-subagent-update-plan-blocked",
    handle: "m:7a11e508",
    title: "Subagent update_plan hook restriction",
    lessonText:
      "Subagents cannot call `update_plan`; return structured slice evidence to the parent coordinator, which owns plan state updates.",
    projectScope: "global",
  },
  {
    ruleId: "unknown_tool_name",
    stage: "PostToolUse",
    pattern: /No such tool available:/i,
    lessonId: "scar-unknown-tool-name",
    mkey: "learning:phase1b-unknown-tool-name",
    handle: "m:7a11e509",
    title: "Unknown MCP or host tool name",
    lessonText:
      "Verify the exact registered tool name and MCP server prefix before invoking a tool.",
    projectScope: "global",
  },
];

function readSmallRegularFileSync(filePath: string, maxBytes = MAX_CONFIG_FILE_BYTES): string | null {
  let fd: number | undefined;
  try {
    fd = fs.openSync(filePath, READ_FLAGS);
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.size > maxBytes) return null;
    const buf = Buffer.alloc(st.size);
    let offset = 0;
    while (offset < buf.length) {
      const n = fs.readSync(fd, buf, offset, buf.length - offset, offset);
      if (n <= 0) break;
      offset += n;
    }
    return buf.toString("utf8", 0, offset);
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Ignore close errors on read-only descriptors.
      }
    }
  }
}

/**
 * Check whether Phase 1b failure lessons are enabled (`MINNI_FAILURE_LESSONS`),
 * defaulting to `true` ("on"). Never loads `flags.ts`.
 */
export function isFailureLessonsEnabled(
  vaultPath: string,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  const rawEnv = env.MINNI_FAILURE_LESSONS;
  if (rawEnv === "off") return false;
  if (rawEnv === "on") return true;
  if (typeof vaultPath !== "string" || !vaultPath || vaultPath.includes("\0")) return true;
  for (const rel of [
    path.join(".runtime", "failure-lessons.json"),
    path.join(".runtime", "flags.json"),
  ]) {
    try {
      const target = path.join(vaultPath, rel);
      assertWriteTargetUnder(target, vaultPath);
      const raw = readSmallRegularFileSync(target);
      if (!raw) continue;
      const parsed = JSON.parse(raw) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        const flag = (parsed as Record<string, unknown>).MINNI_FAILURE_LESSONS;
        if (flag === "off") return false;
        if (flag === "on") return true;
      }
    } catch {
      // Fall through to default ("on").
    }
  }
  return true;
}

function nearestExistingRealpath(inputPath: string): string {
  const resolved = path.resolve(inputPath);
  const tail: string[] = [];
  let curr = resolved;
  for (let i = 0; i < 64; i += 1) {
    try {
      return path.join(fs.realpathSync(curr), ...tail);
    } catch {
      const parent = path.dirname(curr);
      if (parent === curr) return resolved;
      tail.unshift(path.basename(curr));
      curr = parent;
    }
  }
  return resolved;
}

function resolveGitCommonDirWalk(startDir: string): string | null {
  let curr = startDir;
  for (let i = 0; i < 64; i += 1) {
    const dotGit = path.join(curr, ".git");
    try {
      const st = fs.lstatSync(dotGit);
      if (st.isDirectory()) {
        const commonFile = path.join(dotGit, "commondir");
        const commonRel = readSmallRegularFileSync(commonFile, 4096)?.trim();
        if (commonRel) {
          return fs.realpathSync(path.resolve(dotGit, commonRel));
        }
        return fs.realpathSync(dotGit);
      }
      if (st.isFile()) {
        const raw = readSmallRegularFileSync(dotGit, 4096)?.trim() ?? "";
        if (raw.startsWith("gitdir:")) {
          const gitdirRaw = raw.slice("gitdir:".length).trim();
          const gitdirPath = path.resolve(curr, gitdirRaw);
          const gitdirReal = fs.realpathSync(gitdirPath);
          const commonFile = path.join(gitdirReal, "commondir");
          const commonRel = readSmallRegularFileSync(commonFile, 4096)?.trim();
          if (commonRel) {
            return fs.realpathSync(path.resolve(gitdirReal, commonRel));
          }
          return gitdirReal;
        }
      }
    } catch {
      // Continue walking up.
    }
    const parent = path.dirname(curr);
    if (parent === curr) break;
    curr = parent;
  }
  return null;
}

/**
 * Resolve `pathOrCwd` to a canonical project key derived from the repository's
 * git common directory (§7), so all git worktrees of the same repository share
 * one project key.
 */
export function resolveProjectKey(pathOrCwd?: string): string {
  const raw = typeof pathOrCwd === "string" && pathOrCwd.trim() ? pathOrCwd.trim() : process.cwd();
  const realTarget = nearestExistingRealpath(raw);
  let startDir = realTarget;
  try {
    if (fs.statSync(realTarget).isFile()) {
      startDir = path.dirname(realTarget);
    }
  } catch {
    // Keep realTarget.
  }
  const commonDir = resolveGitCommonDirWalk(startDir);
  const canonical = commonDir ?? startDir;
  const digest = createHash("sha256").update(canonical, "utf8").digest("hex").slice(0, 12);
  const repoRoot =
    commonDir !== null && path.basename(commonDir) === ".git"
      ? path.dirname(commonDir)
      : startDir;
  const slug =
    path
      .basename(repoRoot)
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "project";
  return `${slug}-${digest}`;
}

function isBashLikeTool(toolName: string): boolean {
  return toolName === "Bash" || toolName === "run_terminal_command";
}

function failureLessonStateFilePath(vaultPath: string, sessionId?: string): string | null {
  if (typeof vaultPath !== "string" || !vaultPath || vaultPath.includes("\0")) return null;
  const key = (sessionId ? sessionStateKey(sessionId) : null) ?? "unlabeled";
  return path.join(vaultPath, RECALL_STATE_DIR_REL, `${key}.failure-lessons.json`);
}

function hasFailureLessonFired(vaultPath: string, sessionId: string | undefined, lessonId: string): boolean {
  const filePath = failureLessonStateFilePath(vaultPath, sessionId);
  if (!filePath) return false;
  try {
    assertWriteTargetUnder(filePath, vaultPath);
    const raw = readSmallRegularFileSync(filePath);
    if (!raw) return false;
    const parsed = JSON.parse(raw) as { v?: unknown; fired?: Record<string, unknown> } | null;
    if (!parsed || typeof parsed !== "object" || parsed.v !== 1 || !parsed.fired) return false;
    return typeof parsed.fired[lessonId] === "string";
  } catch {
    return false;
  }
}

function ensureContainedStateDir(vaultPath: string): string | null {
  try {
    const root = fs.realpathSync(vaultPath);
    let current = vaultPath;
    for (const seg of [".runtime", "recall-state.d"]) {
      current = path.join(current, seg);
      let st: fs.Stats | undefined;
      try {
        st = fs.lstatSync(current);
      } catch (err) {
        if ((err as { code?: unknown } | null)?.code !== "ENOENT") return null;
      }
      if (!st) {
        assertWriteTargetUnder(current, vaultPath);
        try {
          fs.mkdirSync(current, { mode: 0o700 });
        } catch (err) {
          if ((err as { code?: unknown } | null)?.code !== "EEXIST") return null;
        }
        st = fs.lstatSync(current);
      }
      if (st.isSymbolicLink()) {
        const real = fs.realpathSync(current);
        const rel = path.relative(root, real);
        if (rel.startsWith("..") || path.isAbsolute(rel)) return null;
        st = fs.statSync(current);
      }
      if (!st.isDirectory()) return null;
    }
    return current;
  } catch {
    return null;
  }
}

export function markFailureLessonFired(
  vaultPath: string,
  sessionId: string | undefined,
  lessonId: string,
): boolean {
  const filePath = failureLessonStateFilePath(vaultPath, sessionId);
  if (!filePath) return false;
  try {
    const dir = ensureContainedStateDir(vaultPath);
    if (!dir) return false;
    assertWriteTargetUnder(filePath, vaultPath);
    let existing: Record<string, string> = {};
    const raw = readSmallRegularFileSync(filePath);
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as { v?: unknown; fired?: Record<string, unknown> } | null;
        if (parsed && parsed.v === 1 && parsed.fired && typeof parsed.fired === "object") {
          for (const [k, v] of Object.entries(parsed.fired)) {
            if (typeof v === "string") existing[k] = v;
          }
        }
      } catch {
        existing = {};
      }
    }
    existing[lessonId] = new Date().toISOString();
    const tmpPath = `${filePath}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`;
    assertWriteTargetUnder(tmpPath, vaultPath);
    try {
      fs.writeFileSync(tmpPath, `${JSON.stringify({ v: 1, fired: existing })}\n`, {
        encoding: "utf8",
        mode: 0o600,
        flag: "wx",
      });
      fs.renameSync(tmpPath, filePath);
    } catch (writeErr) {
      try {
        fs.rmSync(tmpPath, { force: true });
      } catch {
        // Ignore cleanup failure.
      }
      throw writeErr;
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Clear per-session failure-lesson rate-limit state on SessionStart / PreCompact / PostCompact.
 */
export function clearSessionFailureLessonsOnReset(vaultPath: string, sessionId?: string): void {
  const filePath = failureLessonStateFilePath(vaultPath, sessionId);
  if (!filePath) return;
  try {
    assertWriteTargetUnder(filePath, vaultPath);
    fs.unlinkSync(filePath);
  } catch {
    // Missing or already removed.
  }
}

export function formatFailureLessonContext(rule: FailureLessonRule): string {
  const safeTitle = sanitizeRecallField(rule.title, 100);
  const safeLesson = sanitizeRecallField(rule.lessonText, 360);
  const safeHandle = sanitizeRecallField(rule.handle, 32);
  return `<EVIDENCE source="failure_lesson:${sanitizeRecallField(rule.lessonId, 64)}" confidence="1.00">\n📓 Minni failure lesson (${safeHandle}): "${safeTitle}" — ${safeLesson}\n</EVIDENCE>`;
}

/**
 * Match a PreToolUse Bash command against pre-emptive failure lesson rules (P2/P3).
 */
export function matchPreToolUseFailureLesson(args: {
  vaultPath: string;
  toolName: string;
  toolInput: Record<string, unknown>;
  sessionId?: string;
  cwd?: string;
}): FailureLessonMatch | null {
  try {
    if (!isBashLikeTool(args.toolName)) return null;
    if (!isFailureLessonsEnabled(args.vaultPath)) return null;
    const command =
      typeof args.toolInput.command === "string"
        ? args.toolInput.command
        : typeof args.toolInput.cmd === "string"
          ? args.toolInput.cmd
          : "";
    if (!command.trim()) return null;
    // Allow bounded git show/diff commands piped to head/wc or with --stat/--numstat/--name-only
    if (/\|\s*(?:head|wc)\b/.test(command)) return null;

    const projectKey = resolveProjectKey(args.cwd);
    for (const rule of APPROVED_FAILURE_LESSON_RULES) {
      if (rule.stage !== "PreToolUse") continue;
      if (rule.projectScope !== "global" && rule.projectScope !== projectKey) continue;
      if (!rule.pattern.test(command)) continue;
      if (hasFailureLessonFired(args.vaultPath, args.sessionId, rule.lessonId)) {
        return null;
      }
      return {
        ruleId: rule.ruleId,
        lessonId: rule.lessonId,
        mkey: rule.mkey,
        handle: rule.handle,
        title: rule.title,
        lessonText: rule.lessonText,
        projectKey,
        additionalContext: formatFailureLessonContext(rule),
      };
    }
    return null;
  } catch {
    return null;
  }
}

function extractToolResponseText(payload: Record<string, unknown>): string {
  const parts: string[] = [];
  const pushVal = (val: unknown): void => {
    if (typeof val === "string" && val.length > 0) {
      parts.push(val.slice(0, MAX_OUTPUT_SCAN_CHARS));
    } else if (val && typeof val === "object" && !Array.isArray(val)) {
      const obj = val as Record<string, unknown>;
      for (const key of ["stdout", "stderr", "output", "content", "text", "error", "message"]) {
        if (typeof obj[key] === "string" && (obj[key] as string).length > 0) {
          parts.push((obj[key] as string).slice(0, MAX_OUTPUT_SCAN_CHARS));
        }
      }
    } else if (Array.isArray(val)) {
      for (const item of val.slice(0, 16)) {
        pushVal(item);
      }
    }
  };
  pushVal(payload.tool_response);
  pushVal(payload.toolResponse);
  pushVal(payload.tool_output);
  pushVal(payload.toolOutput);
  pushVal(payload.output);
  pushVal(payload.stdout);
  pushVal(payload.stderr);
  pushVal(payload.error);
  return parts.join("\n").slice(0, MAX_OUTPUT_SCAN_CHARS);
}

/**
 * Match a PostToolUse or PostToolUseFailure payload against recurring error
 * signatures. Inspects `tool_response` output text on EVERY Bash call even when
 * `is_error === false` (solving the 350/352 exit-code masking blocker).
 */
export function matchPostToolUseFailureLesson(args: {
  vaultPath: string;
  event: "PostToolUse" | "PostToolUseFailure";
  payload: Record<string, unknown>;
  sessionId?: string;
  cwd?: string;
}): FailureLessonMatch | null {
  try {
    if (!isFailureLessonsEnabled(args.vaultPath)) return null;
    const toolName =
      typeof args.payload.tool_name === "string"
        ? args.payload.tool_name
        : typeof args.payload.toolName === "string"
          ? args.payload.toolName
          : "";
    if (args.event === "PostToolUse" && toolName && !isBashLikeTool(toolName)) {
      return null;
    }
    const outputText = extractToolResponseText(args.payload);
    if (!outputText.trim()) return null;

    const projectKey = resolveProjectKey(args.cwd);
    for (const rule of APPROVED_FAILURE_LESSON_RULES) {
      if (rule.stage !== "PostToolUse") continue;
      if (rule.projectScope !== "global" && rule.projectScope !== projectKey) continue;
      if (!rule.pattern.test(outputText)) continue;
      if (hasFailureLessonFired(args.vaultPath, args.sessionId, rule.lessonId)) {
        return null;
      }
      return {
        ruleId: rule.ruleId,
        lessonId: rule.lessonId,
        mkey: rule.mkey,
        handle: rule.handle,
        title: rule.title,
        lessonText: rule.lessonText,
        projectKey,
        additionalContext: formatFailureLessonContext(rule),
      };
    }
    return null;
  } catch {
    return null;
  }
}
