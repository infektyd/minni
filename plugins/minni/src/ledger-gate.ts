// The one cheap check in front of the use-ledger capture (S1a-2).
//
// With MINNI_USE_LEDGER off, a hook must cost what it cost before the ledger
// existed: hook-handlers.ts loads use-capture.js (and with it use-ledger,
// flags and harness-envelope) through a dynamic import, and only when this
// check says the ledger may be on. It is a pre-check, not the authority: when
// it answers "maybe on", the full flags.ts read runs and decides, notes audit
// included. So it may only answer "off" where flags.ts would answer "off" too,
// and every doubt answers "maybe on":
//   - env MINNI_USE_LEDGER, strict "on"/"off", wins (an env value that is
//     neither is a doubt: flags.ts would audit it);
//   - else `<vault>/.runtime/flags.json`, read as safely as flags.ts reads it:
//     a `.runtime` that is a symlink is a doubt (flags.ts judges its
//     containment), the file is opened non-blocking without following a link
//     (a FIFO cannot hang the hook), it must be a regular file under the size
//     cap, valid JSON, and the value strict;
//   - else the default, on.
//
// Imports node built-ins only, so the check itself loads nothing new.
import fs from "node:fs";
import path from "node:path";

/** Events whose dispatch opens a use-ledger capture. */
export const LEDGER_EVENTS: ReadonlySet<string> = new Set([
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "PreCompact",
  "PostCompact",
  "SubagentStart",
]);

const FLAG = "MINNI_USE_LEDGER";
/** Same cap as flags.ts: anything larger is not a flags file. */
const MAX_FLAGS_FILE_BYTES = 64 * 1024;
const READ_FLAGS =
  fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

/** The file's MINNI_USE_LEDGER when it is certain, else undefined. Never throws. */
function fileValue(vaultPath: string): "on" | "off" | undefined {
  let fd: number | undefined;
  try {
    const runtime = path.join(vaultPath, ".runtime");
    if (fs.lstatSync(runtime).isSymbolicLink()) return undefined;
    fd = fs.openSync(path.join(runtime, "flags.json"), READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > MAX_FLAGS_FILE_BYTES) return undefined;
    const buffer = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < buffer.length) {
      const n = fs.readSync(fd, buffer, offset, buffer.length - offset, offset);
      if (n <= 0) break;
      offset += n;
    }
    const parsed: unknown = JSON.parse(buffer.subarray(0, offset).toString("utf8"));
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
    const value = (parsed as Record<string, unknown>)[FLAG];
    return value === "on" || value === "off" ? value : undefined;
  } catch {
    // Absent, unreadable, not JSON: no certainty either way.
    return undefined;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Nothing was written; nothing to lose.
      }
    }
  }
}

/**
 * False only when the ledger is certainly off, so the capture modules need not
 * load. True means "maybe on": the caller loads them and flags.ts decides.
 */
export function ledgerMayBeOn(vaultPath: string, env: NodeJS.ProcessEnv = process.env): boolean {
  const fromEnv = env[FLAG];
  if (fromEnv === "off") return false;
  if (fromEnv !== undefined) return true;
  if (typeof vaultPath !== "string" || vaultPath.length === 0 || vaultPath.includes("\0")) return true;
  return fileValue(vaultPath) !== "off";
}
