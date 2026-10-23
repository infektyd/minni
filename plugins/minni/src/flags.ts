// Kill switches for the recall-to-use features (design rev5 §8.2).
//
// One switch per feature, every one defaulting to `on`. They are read from
// `<vault>/.runtime/flags.json` (`{"<FLAG>": "on" | "off"}`) because hooks the
// desktop app launches do not reliably inherit the user's shell environment: a
// switch that lives only in env cannot be thrown where it matters. The
// same-named env var still overrides the file, for a spot check from a shell.
//
// Parsing is strict. Only the exact strings "on" and "off" count; anything else
// is ignored and leaves a one-line note in `notes` for the caller to audit, so a
// typo can never flip a feature silently. This module does no audit write of its
// own: flags are read on synchronous paths, and the hook that loads them decides
// where the note goes.
//
// The parsed file is cached per vault and revalidated on every call with one
// statSync: it is re-read only when its mtime (or size) changed. A hook
// process therefore parses it once, and a long-lived importer (the MCP server,
// the console) still sees a switch thrown while it runs.
import fs from "node:fs";
import path from "node:path";

import { assertWriteTargetUnder } from "./vault.js";

export const FLAG_NAMES = [
  "MINNI_USE_LEDGER",
  "MINNI_BOOT_FIT",
  "MINNI_GUARD_V2",
  "MINNI_SUBAGENT_BRIEF",
  "MINNI_PROMPT_CLASSES",
  "MINNI_RECENT_LEARNINGS_RANK",
  "MINNI_UPS_RANK_V2",
  "MINNI_RECALL_NOISE_LAST",
] as const;

export type FlagName = (typeof FLAG_NAMES)[number];
export type FlagValue = "on" | "off";
export type FlagSource = "default" | "file" | "env";

/** Relative location of the flags file under the vault. */
export const FLAGS_RELPATH = path.join(".runtime", "flags.json");

/** A flags file larger than this is not a flags file; it is ignored. */
const MAX_FLAGS_FILE_BYTES = 64 * 1024;
const READ_FLAGS =
  fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

export interface FlagSnapshot {
  values: Record<FlagName, FlagValue>;
  sources: Record<FlagName, FlagSource>;
  /** One line per ignored value or refused read, for the caller to audit. */
  notes: string[];
}

interface FileFlags {
  values: Partial<Record<FlagName, FlagValue>>;
  notes: string[];
}

interface CachedFlags extends FileFlags {
  /** mtime and size of the file when it was parsed, or why it could not be stat'd. */
  signature: string;
}

const fileCache = new Map<string, CachedFlags>();

const DEFAULTS_ONLY: FileFlags = { values: {}, notes: [] };

const FLAG_SET: ReadonlySet<string> = new Set(FLAG_NAMES);

// Built from a string, as sanitizeRecallField does, to keep the literal out of
// no-control-regex.
const CONTROL_CHARS = new RegExp("[\\u0000-\\u001F\\u007F]+", "g");

function strictValue(raw: unknown): FlagValue | undefined {
  return raw === "on" || raw === "off" ? raw : undefined;
}

/** Bounded, single-line rendering of an ignored value for a note. */
function describe(raw: unknown): string {
  let text: string;
  try {
    text = typeof raw === "string" ? JSON.stringify(raw) : String(JSON.stringify(raw));
  } catch {
    text = typeof raw;
  }
  text = text.replace(CONTROL_CHARS, " ");
  return text.length > 40 ? `${text.slice(0, 39)}…` : text;
}

function readFileFlags(vaultPath: string): FileFlags {
  const notes: string[] = [];
  const values: Partial<Record<FlagName, FlagValue>> = {};
  const filePath = path.join(vaultPath, FLAGS_RELPATH);
  try {
    // Same containment check readRecallState uses: refuse a symlinked flags
    // file, or a .runtime parent that resolves outside the vault.
    assertWriteTargetUnder(filePath, vaultPath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/escapes root|is a symlink/.test(message)) {
      notes.push(`flags: ${FLAGS_RELPATH} refused (containment); using defaults`);
      return { values, notes };
    }
    // A missing .runtime fails the parent realpath; that is "no file", not a refusal.
    if (!fs.existsSync(path.dirname(filePath))) return { values, notes };
    notes.push(`flags: ${FLAGS_RELPATH} unreadable; using defaults`);
    return { values, notes };
  }
  let raw: string;
  // Opened non-blocking without following a link, and judged on the OPENED
  // descriptor (as ledger-gate.ts reads it): a flags file swapped for a FIFO
  // after any earlier stat would otherwise block the read, and Stop reaches
  // this through flagOn while the host waits for the hook to exit.
  let fd: number | undefined;
  try {
    fd = fs.openSync(filePath, READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) {
      notes.push(`flags: ${FLAGS_RELPATH} is not a regular file; using defaults`);
      return { values, notes };
    }
    if (stat.size > MAX_FLAGS_FILE_BYTES) {
      notes.push(`flags: ${FLAGS_RELPATH} exceeds ${MAX_FLAGS_FILE_BYTES} bytes; using defaults`);
      return { values, notes };
    }
    const buffer = Buffer.alloc(stat.size);
    let filled = 0;
    while (filled < buffer.length) {
      const n = fs.readSync(fd, buffer, filled, buffer.length - filled, filled);
      if (n <= 0) break;
      filled += n;
    }
    raw = buffer.toString("utf8", 0, filled);
  } catch (error) {
    const code = (error as { code?: unknown } | null)?.code;
    if (code !== "ENOENT") {
      notes.push(`flags: ${FLAGS_RELPATH} unreadable (${String(code ?? "error")}); using defaults`);
    }
    return { values, notes };
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // A read-only descriptor: nothing to lose.
      }
    }
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    notes.push(`flags: ${FLAGS_RELPATH} is not valid JSON; using defaults`);
    return { values, notes };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    notes.push(`flags: ${FLAGS_RELPATH} is not a JSON object; using defaults`);
    return { values, notes };
  }
  for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (!FLAG_SET.has(key)) {
      notes.push(`flags: unknown flag ${describe(key)} in ${FLAGS_RELPATH} ignored`);
      continue;
    }
    const strict = strictValue(value);
    if (strict === undefined) {
      notes.push(`flags: ${key}=${describe(value)} in ${FLAGS_RELPATH} ignored (want "on" or "off")`);
      continue;
    }
    values[key as FlagName] = strict;
  }
  return { values, notes };
}

/** Cheap change detector: one statSync. The read itself re-checks containment. */
function signatureOf(filePath: string): string {
  try {
    const stat = fs.statSync(filePath);
    return `${stat.mtimeMs}:${stat.size}`;
  } catch (error) {
    return `missing:${String((error as { code?: unknown } | null)?.code ?? "error")}`;
  }
}

function fileFlagsFor(vaultPath: string): FileFlags {
  try {
    // Inside the try: a non-string or NUL-bearing vault path must mean
    // defaults, never an exception out of a hook.
    const key = path.resolve(vaultPath);
    const signature = signatureOf(path.join(vaultPath, FLAGS_RELPATH));
    const cached = fileCache.get(key);
    if (cached && cached.signature === signature) return cached;
    let parsed: FileFlags;
    try {
      parsed = readFileFlags(vaultPath);
    } catch {
      // Never let a flags read fail a hook: an unreadable file means defaults.
      parsed = { values: {}, notes: [`flags: ${FLAGS_RELPATH} could not be read; using defaults`] };
    }
    const entry: CachedFlags = { ...parsed, signature };
    fileCache.set(key, entry);
    return entry;
  } catch {
    return DEFAULTS_ONLY;
  }
}

/**
 * Every flag's value in effect for `vaultPath`: env (strict) over the file
 * (strict) over the default `on`. Never throws.
 */
export function loadFlags(vaultPath: string, env: NodeJS.ProcessEnv = process.env): FlagSnapshot {
  const file = fileFlagsFor(vaultPath);
  const notes = [...file.notes];
  const values = {} as Record<FlagName, FlagValue>;
  const sources = {} as Record<FlagName, FlagSource>;
  for (const name of FLAG_NAMES) {
    const fromEnv = env[name];
    const strictEnv = strictValue(fromEnv);
    if (fromEnv !== undefined && strictEnv === undefined) {
      notes.push(`flags: env ${name}=${describe(fromEnv)} ignored (want "on" or "off")`);
    }
    if (strictEnv !== undefined) {
      values[name] = strictEnv;
      sources[name] = "env";
    } else if (file.values[name] !== undefined) {
      values[name] = file.values[name] as FlagValue;
      sources[name] = "file";
    } else {
      values[name] = "on";
      sources[name] = "default";
    }
  }
  return { values, sources, notes };
}

/** Whether one flag is on. */
export function flagOn(vaultPath: string, name: FlagName, env: NodeJS.ProcessEnv = process.env): boolean {
  return loadFlags(vaultPath, env).values[name] === "on";
}

/** The flags that are on, sorted: the ledger's `features` field. */
export function featuresInEffect(vaultPath: string, env: NodeJS.ProcessEnv = process.env): FlagName[] {
  const { values } = loadFlags(vaultPath, env);
  return FLAG_NAMES.filter((name) => values[name] === "on").sort();
}

/** Forget the per-process file snapshot. Tests only. */
export function resetFlagsCacheForTests(): void {
  fileCache.clear();
}
