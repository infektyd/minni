// The use detector (design rev5 §4.4, slice S1b).
//
// Problem this solves: the ledger records what each hook SHOWED the model
// (`surfaced` rows) and how many prompts there were (`turn` rows), but nothing
// says whether the model then USED what it was shown. The detector reads the
// host transcript after a turn and judges every surfacing against what the
// assistant did next. It is passive (the agent does nothing extra) and
// deterministic: there is no LLM judge in the hooks.
//
// Two units, both written to the ledger:
//   - A WINDOW per surfacing. A per-turn surfacing (pointer, guard, explicit
//     recall) is open from the surfacing to the Stop that ends its turn, capped
//     at 50 assistant actions; a boot surfacing until the end of the second
//     non-skip prompt turn after it. A reset of the same agent key closes open
//     windows; a turn with no Stop closes at the next prompt. Each window gets a
//     `judged` row. Those rows are diagnostics per channel.
//   - An EXPOSURE per (session_key, agent_key, reset_id, mkey), opened by the
//     memory's first visible surfacing in the reset epoch. It stays creditable,
//     named again or not, until 50 human prompt turns pass after its latest
//     visible surfacing. Each turn with used_ii evidence for the memory gives
//     one credit for that (exposure, turn). The `exposure` row is written once,
//     when the epoch closes: at a reset, at the session's end, or after 24 h
//     idle. Open epochs live in windows.json and are provisional by definition.
//
// Outcomes, strongest first: applied (a stored body shingle the injection did
// not carry, in assistant text or a tool input; for channels whose injected
// text IS the memory, injected shingles count only inside a tool input), cited
// (a handle, wikilink, slug or `learning #id`, or 3 distinctive title tokens in
// one sentence once the last 3 prompts' tokens are dropped), opened (a Read, a
// read-verb Bash, or a minni_drill/minni_recall that names it), dismissed (the
// first assistant text after the surfacing matches the lexicon), then ignored
// if visible, else unknown(<reason>). Dismissal is sticky: the window can still
// end applied, never cited. A citation followed by an open of the same memory
// is `opened` (naming before opening). used_ii = applied, plus title-cited.
//
// Placement. Ledger events are merged into the transcript stream: a surfacing
// that carries a prompt_id binds to the turn whose prompt line has that
// promptId (a guard surfacing additionally waits for its own timestamp inside
// that turn); anything else is placed by tx_off, else by timestamp. Contract
// for the Stop/SubagentStop wiring (a follow-up): a SessionStart writes its
// `reset` row before its boot surfacings, so the boot rows sort after it.
//
// Reading. A per-transcript cursor lives in .runtime/use-detector/cursors/.
// The fast path is a byte scan with no JSON parse: no window open, no
// creditable exposure, no new surfacing, and a delta that names neither
// minni_recall nor minni_drill. The slow path parses at most 2 MB of complete
// lines within 1.5 s; unfinished work carries forward to the next run.
//
// Safety. One writer per session: an O_EXCL lock (`windows.lock`), broken when
// older than 30 s; a run that cannot take it within 200 ms skips. Rows carry
// deterministic sids and are checked against the ledger before appending, so
// a re-read never writes a (sid, final) or exposure twice. State files are
// written by temp + rename inside the vault's containment checks. Nothing here
// throws out of a hook, and the detector never scans the vault: titles are
// judged against `.runtime/df-snapshot.json` (written by UPS from its own
// scan), and with no snapshot the title rule is off and reported as off.
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { featuresInEffect, flagOn } from "./flags.js";
import { classifyPromptEnvelope } from "./harness-envelope.js";
import {
  allShingleHashes,
  appendLedgerRows,
  buildExposureRow,
  buildJudgedRow,
  buildSurfacedRow,
  containedDir,
  exposureIdFor,
  handlesForKey,
  idHash,
  LEDGER_RELDIR,
  memoryKeyFor,
  readSessionLedger,
  shingleHashOf,
  SHINGLE_WORDS,
  type EvidenceKind,
  type LedgerContext,
  type LedgerRow,
  type ResetRow,
  type SurfaceChannel,
  type SurfacedRow,
  type TurnRow,
} from "./use-ledger.js";
import { assertWriteTargetUnder } from "./vault.js";

export const DETECTOR_RULE_VERSION = "s1b-4";
/** Detector state root under the vault. */
export const DETECTOR_RELDIR = path.join(".runtime", "use-detector");
/** Token document frequencies, written by UPS from its own vault scan. */
export const DF_SNAPSHOT_RELPATH = path.join(".runtime", "df-snapshot.json");
export const LOCK_STALE_MS = 30_000;
export const LOCK_WAIT_MS = 200;
export const MAX_READ_BYTES = 2 * 1024 * 1024;
export const RUN_BUDGET_MS = 1_500;
export const TURN_ACTION_CAP = 50;
export const CREDIT_IDLE_TURNS = 50;
export const IDLE_CLOSE_MS = 24 * 60 * 60 * 1000;

const SESSIONS_SEGMENT = "sessions";
const CURSORS_SEGMENT = "cursors";
const STATE_FILE = "windows.json";
const LOCK_FILE = "windows.lock";
/** A new cursor starts this far before the earliest open surfacing (or the end). */
const NEW_CURSOR_LOOKBACK = 64 * 1024;
const STATE_VERSION = 1;
const MAX_STATE_BYTES = 8 * 1024 * 1024;
const MAX_DF_BYTES = 16 * 1024 * 1024;
const MAX_DONE = 1024;
const MAX_WINDOWS = 512;
const MAX_EXPOSURES = 512;
const MAX_TOOL_IDS = 128;
const MAX_RESETS = 64;
const MAX_TEXT_HASHES = 256;
const MAX_PROMPT_TOKENS = 2000;
/** Shingle hashes kept per recent prompt for the text-act echo rule. */
const MAX_PROMPT_SHINGLES = 1024;
const MAX_SET = 128;
const MAX_CREDITS = 512;
const MAX_INPUT_CHARS = 200_000;
const MAX_CARDS = 64;
const FAST_SCAN_CHUNK = 256 * 1024;
const DISMISSAL_PREFIX_CHARS = 300;
const MIN_TITLE_TOKENS = 3;
const MIN_SLUG_CHARS = 8;
/** A title shorter than this is too generic to open a memory by substring. */
const MIN_TITLE_SUBSTRING_CHARS = 8;
const SESSION_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
/**
 * The agent keys a live producer emits (agentKeyFrom): `main`, or `a-` and 12
 * lowercase hex. Capture's own form (use-capture.ts), copied: any other
 * spelling (a crafted "Main") is refused rather than judged as a subagent.
 */
const AGENT_KEY_RE = /^(?:main|a-[0-9a-f]{12})$/;
const TX_KEY_RE = /^[0-9a-f]{16}$/;
const RESET_ID_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
const INITIAL_EPOCH = "initial";

/** Channels whose window is open across prompt turns. */
const BOOT_SCOPE: ReadonlySet<string> = new Set(["boot_recall", "boot_recent_learnings", "subagent_brief"]);
/** Channels whose injected text IS the memory: its shingles count only inside a tool input. */
const INJECTED_IS_MEMORY: ReadonlySet<string> = new Set(["boot_recall", "boot_recent_learnings"]);
const MINNI_TOOL_RE = /(?:^|__)minni_(recall|drill)$/;
const READ_VERB_RE = /(?:^|[\s;&|(`$])(?:cat|head|tail|less|more|bat|sed|awk|grep|rg|nl|wc|jq|view|xxd|od|strings)\s/;
const WORD_RE = /[\p{L}\p{N}]+/gu;
const SENTENCE_BREAK_RE = /(?<=[.!?;])\s+|\n+/gu;

/**
 * The dismissal lexicon (§4.4), matched against the opening of the first
 * assistant text after a surfacing. Phrases only; no topic words.
 */
export const DISMISSAL_PATTERNS: readonly RegExp[] = [
  /\b(?:not|isn['’]t|is not|wasn['’]t|was not|aren['’]t|are not)\s+(?:really\s+|directly\s+|particularly\s+)?(?:relevant|applicable|useful|helpful|pertinent)\b/i,
  /\b(?:irrelevant|inapplicable|unrelated)\b/i,
  /\bdo(?:es)?(?:n['’]t| not)\s+(?:seem to\s+)?(?:apply|help|matter|bear on)\b/i,
  /\b(?:ignor(?:e|ing)|disregard(?:ing)?|set(?:ting)? aside)\s+(?:the|this|that|these)\s+(?:recall(?:ed)?|memor(?:y|ies)|pointers?|notes?|suggestions?|hints?)\b/i,
  /\bno(?:thing)?\s+relevant\b/i,
];

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function errorCode(error: unknown): unknown {
  return (error as { code?: unknown } | null)?.code;
}

function toMs(value: unknown): number {
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.getTime() : Date.now();
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const ms = Date.parse(value);
    if (Number.isFinite(ms)) return ms;
  }
  return Date.now();
}

function tsMs(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function sha(text: string, length: number): string {
  return createHash("sha256").update(text, "utf8").digest("hex").slice(0, length);
}

function sleepSync(ms: number): void {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch {
    // No Atomics.wait on this thread: retry immediately instead.
  }
}

function pushBounded<T>(list: T[], value: T, max: number): void {
  list.push(value);
  if (list.length > max) list.splice(0, list.length - max);
}

function unionBounded(a: readonly string[], b: readonly string[] | undefined, max = MAX_SET): string[] {
  const out = [...a];
  const seen = new Set(out);
  for (const value of b ?? []) {
    if (out.length >= max) break;
    if (typeof value === "string" && !seen.has(value)) {
      seen.add(value);
      out.push(value);
    }
  }
  return out;
}

/** Lower-cased, NFKC-normalised runs of letters and digits, as the ledger's shingles use. */
function wordsOf(text: string): string[] {
  return text.normalize("NFKC").toLowerCase().match(WORD_RE) ?? [];
}

/**
 * The tokens the title rule and the DF snapshot count: distinct words of at
 * least 3 characters. Exported so the snapshot writer and the detector agree.
 */
export function useTokens(text: string): string[] {
  if (typeof text !== "string") return [];
  return [...new Set(wordsOf(text.slice(0, MAX_INPUT_CHARS)).filter((word) => word.length >= 3))];
}

function tokenHash(token: string): string {
  return sha(token, 12);
}

function replyHash(text: string): string {
  return sha(text.normalize("NFKC").replace(/\s+/g, " ").trim(), 16);
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Read-only open flags for every file the detector and the report read. With
 * O_NONBLOCK a FIFO opens at once (instead of waiting for a writer, which
 * under the Stop wiring would hold the host up to its Stop timeout) and is
 * then refused by the fstat below; O_NOFOLLOW refuses a symlinked leaf.
 */
const READ_FLAGS = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

/**
 * A regular file's text, or null: opened non-blocking, judged on the OPENED
 * descriptor (so a file swapped for a FIFO after a stat is still refused),
 * at most `maxBytes`. Never blocks, never throws.
 */
export function readRegularFileSync(file: string, maxBytes: number): string | null {
  let fd: number | undefined;
  try {
    fd = fs.openSync(file, READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > maxBytes) return null;
    const buffer = Buffer.alloc(stat.size);
    let filled = 0;
    while (filled < buffer.length) {
      const n = fs.readSync(fd, buffer, filled, buffer.length - filled, filled);
      if (n <= 0) break;
      filled += n;
    }
    return buffer.toString("utf8", 0, filled);
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // A read-only descriptor: nothing to lose.
      }
    }
  }
}

// ---------------------------------------------------------------------------
// DF snapshot (§4.4 "Distinctive tokens")
// ---------------------------------------------------------------------------

export interface DfSnapshot {
  notes: number;
  /** A token is distinctive when its DF is at most this: max(1, ceil(5% of notes)). */
  threshold: number;
  df: ReadonlyMap<string, number>;
  ts: string | null;
}

const dfCache = new Map<string, { signature: string; snapshot: DfSnapshot | null }>();

/**
 * `.runtime/df-snapshot.json`: `{"v":1,"ts":"…","notes":N,"df":{"token":count}}`.
 * A token absent from `df` has DF 0 (distinctive), so a writer may drop every
 * token at or under the threshold. Null when missing, refused or malformed:
 * the title rule is then off. Never throws.
 */
export function readDfSnapshot(vaultPath: string): DfSnapshot | null {
  try {
    const file = path.join(vaultPath, DF_SNAPSHOT_RELPATH);
    try {
      assertWriteTargetUnder(file, vaultPath);
    } catch {
      return null;
    }
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > MAX_DF_BYTES) return null;
    const signature = `${stat.mtimeMs}:${stat.size}`;
    const key = path.resolve(vaultPath);
    const cached = dfCache.get(key);
    if (cached && cached.signature === signature) return cached.snapshot;
    let snapshot: DfSnapshot | null = null;
    const text = readRegularFileSync(file, MAX_DF_BYTES);
    if (text === null) return null;
    const raw = JSON.parse(text) as unknown;
    if (isRecord(raw) && raw.v === 1 && Number.isSafeInteger(raw.notes) && (raw.notes as number) >= 1 && isRecord(raw.df)) {
      const df = new Map<string, number>();
      for (const [token, count] of Object.entries(raw.df)) {
        if (typeof count === "number" && Number.isFinite(count) && count >= 0) df.set(token, count);
      }
      const notes = raw.notes as number;
      snapshot = {
        notes,
        threshold: Math.max(1, Math.ceil(0.05 * notes)),
        df,
        ts: typeof raw.ts === "string" ? raw.ts : null,
      };
    }
    dfCache.set(key, { signature, snapshot });
    return snapshot;
  } catch {
    return null;
  }
}

/** Document frequencies of `useTokens` over `texts` (one text per non-noise note). */
export function documentFrequencies(texts: Iterable<string>): { notes: number; df: Record<string, number> } {
  const df: Record<string, number> = {};
  let notes = 0;
  for (const text of texts) {
    notes += 1;
    for (const token of useTokens(text)) df[token] = (df[token] ?? 0) + 1;
  }
  return { notes, df };
}

/**
 * Write the DF snapshot (temp + rename), keeping only tokens above the
 * distinctive threshold: absent means distinctive. Returns false on any
 * failure. For UPS, which owns the at-most-daily write.
 */
export function writeDfSnapshot(
  vaultPath: string,
  input: { notes: number; df: Readonly<Record<string, number>>; ts?: string },
): boolean {
  try {
    if (!Number.isSafeInteger(input.notes) || input.notes < 1) return false;
    const threshold = Math.max(1, Math.ceil(0.05 * input.notes));
    const df: Record<string, number> = {};
    for (const [token, count] of Object.entries(input.df)) {
      if (typeof count === "number" && Number.isFinite(count) && count > threshold) df[token] = count;
    }
    const dir = containedDir(vaultPath, [".runtime"], true);
    if (!dir) return false;
    writeJsonAtomic(vaultPath, path.join(vaultPath, DF_SNAPSHOT_RELPATH), {
      v: 1,
      ts: input.ts ?? new Date().toISOString(),
      notes: input.notes,
      df,
    });
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Files: containment, atomic JSON, the lock
// ---------------------------------------------------------------------------

function writeJsonAtomic(vaultPath: string, file: string, value: unknown): void {
  assertWriteTargetUnder(file, vaultPath);
  const text = `${JSON.stringify(value)}\n`;
  if (Buffer.byteLength(text, "utf8") > MAX_STATE_BYTES) throw new Error("use-detector: state too large");
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString("hex")}.tmp`;
  const fd = fs.openSync(
    tmp,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | (fs.constants.O_NOFOLLOW ?? 0),
    0o600,
  );
  try {
    fs.writeSync(fd, text);
  } finally {
    fs.closeSync(fd);
  }
  try {
    fs.renameSync(tmp, file);
  } catch (error) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      // Nothing left to clean up.
    }
    throw error;
  }
}

function readJsonFile(vaultPath: string, file: string): unknown {
  try {
    assertWriteTargetUnder(file, vaultPath);
    const text = readRegularFileSync(file, MAX_STATE_BYTES);
    return text === null ? null : (JSON.parse(text) as unknown);
  } catch {
    return null;
  }
}

/** Null when missing (and not created) or refused: a component escapes the vault. */
function sessionDir(vaultPath: string, sessionKey: string, create: boolean): string | null {
  try {
    return containedDir(vaultPath, [...DETECTOR_RELDIR.split(path.sep), SESSIONS_SEGMENT, sessionKey], create);
  } catch {
    return null;
  }
}

function cursorDir(vaultPath: string, create: boolean): string | null {
  try {
    return containedDir(vaultPath, [...DETECTOR_RELDIR.split(path.sep), CURSORS_SEGMENT], create);
  } catch {
    return null;
  }
}

interface HeldLock {
  release(): void;
}

function breakStaleLock(file: string, stale: fs.Stats): void {
  const aside = `${file}.stale-${process.pid}-${randomBytes(4).toString("hex")}`;
  try {
    fs.renameSync(file, aside);
  } catch {
    return;
  }
  try {
    const moved = fs.lstatSync(aside);
    if (moved.ino !== stale.ino || moved.dev !== stale.dev) {
      // A peer replaced the stale lock between our stat and our rename: put
      // its fresh lock back (link fails rather than clobbering a third).
      try {
        fs.linkSync(aside, file);
      } catch {
        // The peer will find its lock gone and its run is still single-writer
        // through the next O_EXCL; nothing safer to do here.
      }
    }
  } finally {
    try {
      fs.unlinkSync(aside);
    } catch {
      // Already gone.
    }
  }
}

/** O_EXCL lock in `dir`; null when held by someone else past the wait budget. */
function acquireLock(vaultPath: string, dir: string): HeldLock | null {
  const file = path.join(dir, LOCK_FILE);
  assertWriteTargetUnder(file, vaultPath);
  const deadline = Date.now() + LOCK_WAIT_MS;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      const fd = fs.openSync(
        file,
        fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | (fs.constants.O_NOFOLLOW ?? 0),
        0o600,
      );
      let mine: fs.Stats;
      try {
        fs.writeSync(fd, `${process.pid} ${new Date().toISOString()}\n`);
        mine = fs.fstatSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      return {
        release() {
          try {
            const current = fs.lstatSync(file);
            if (current.ino === mine.ino && current.dev === mine.dev) fs.unlinkSync(file);
          } catch {
            // Already broken by a peer as stale; nothing of ours to remove.
          }
        },
      };
    } catch (error) {
      if (errorCode(error) !== "EEXIST") return null;
    }
    try {
      const held = fs.lstatSync(file);
      if (Date.now() - held.mtimeMs > LOCK_STALE_MS) {
        breakStaleLock(file, held);
        continue;
      }
    } catch (error) {
      if (errorCode(error) === "ENOENT") continue;
      return null;
    }
    if (Date.now() >= deadline) return null;
    sleepSync(10);
  }
  return null;
}

// ---------------------------------------------------------------------------
// State (windows.json)
// ---------------------------------------------------------------------------

/** What the detector has seen after a surfacing, kinds only. */
interface Acc {
  /** Assistant actions counted toward the per-turn cap. */
  actions: number;
  /** Assistant actions observed at all. */
  seen: number;
  /** True once the first assistant text was checked (or when no check is armed). */
  first_text: boolean;
  dismissed: boolean;
  applied: boolean;
  cite_handle: boolean;
  cite_title: boolean;
  opened: boolean;
  title_assessable: boolean;
}

interface MemoryCore {
  mkey: string;
  channel: SurfaceChannel;
  visible: boolean;
  title: string;
  handles: string[];
  shingles: string[];
  echo: string[];
  /** Injected text is the memory: echo shingles count inside tool inputs only. */
  injected_is_memory: boolean;
}

interface Provisional {
  outcome: string;
  evidence: EvidenceKind[];
  title_assessable: boolean;
  ts: string;
}

interface WindowState extends MemoryCore {
  sid: string;
  scope: "turn" | "boot";
  /** The turn a per-turn window belongs to; -1 until its turn begins. */
  turn_key: number;
  /** Non-skip prompt turns begun since a boot window opened. */
  boot_turns: number;
  acc: Acc;
  provisional?: Provisional;
}

interface ExposureState extends MemoryCore {
  exposure_id: string;
  reset_id: string;
  first_sid: string;
  first_ts: string;
  outcome: string | null;
  credits: number[];
  /** Human prompt turns since the latest visible surfacing. */
  idle_turns: number;
  seen_this_turn: boolean;
  /** Creditable turns whose first assistant text matched the dismissal lexicon (a diagnostic). */
  dismissed_turns: number;
  /** Assistant actions observed while creditable: none means the exposure was never reached. */
  seen_total: number;
  /** Evidence since the last fold (the current turn). */
  acc: Acc;
  /** The current turn's evidence plus last_assistant_message, while the reply is unflushed. */
  pending?: Acc;
}

interface TurnState {
  /** Byte offset of the turn's prompt line: a stable turn id. */
  key: number;
  /**
   * The `turn` row's turn_ix; for a turn with no row, -(key + 1): a
   * detector-local id that can never collide with a real turn_ix.
   */
  ix: number;
  prompt_id?: string;
  /** The prompt line's timestamp. */
  start_ts: number | null;
  human: boolean;
  non_skip: boolean;
  /** Hashes of the turn's assistant texts, for matching last_assistant_message. */
  text_hashes: string[];
  /** The turn was closed (final) at a Stop; later lines are its continuation. */
  closed: boolean;
  /** The turn ended at a Stop whose reply was not yet in the transcript. */
  ended?: { lam_hash: string | null; ts: string };
}

interface AgentState {
  wire: string;
  principal: string;
  subagent_type?: string;
  reset_id: string;
  applied_resets: string[];
  tx: string | null;
  offset: number;
  history_stale: boolean;
  turn: TurnState | null;
  /** Token hashes of the last 3 non-skip prompts, newest last. */
  prompts: string[][];
  /** Shingle hashes of the same prompts. */
  prompt_shingles: string[][];
  windows: WindowState[];
  exposures: ExposureState[];
  done: string[];
  tool_ids: Record<string, "recall" | "drill">;
  last_activity: string;
}

interface DetectorState {
  v: typeof STATE_VERSION;
  session_key: string;
  /** Newest ledger day-file signature when maintenance last closed this session. */
  idle_closed?: string;
  agents: Record<string, AgentState>;
}

function newAcc(firstTextChecked: boolean): Acc {
  return {
    actions: 0,
    seen: 0,
    first_text: firstTextChecked,
    dismissed: false,
    applied: false,
    cite_handle: false,
    cite_title: false,
    opened: false,
    title_assessable: false,
  };
}

function cloneAcc(acc: Acc): Acc {
  return { ...acc };
}

function newAgent(wire: string, principal: string, subagentType: string | undefined, nowIso: string): AgentState {
  const agent: AgentState = {
    wire,
    principal,
    reset_id: INITIAL_EPOCH,
    applied_resets: [],
    tx: null,
    offset: 0,
    history_stale: false,
    turn: null,
    prompts: [],
    prompt_shingles: [],
    windows: [],
    exposures: [],
    done: [],
    tool_ids: {},
    last_activity: nowIso,
  };
  if (subagentType) agent.subagent_type = subagentType;
  return agent;
}

const str = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);
const strList = (value: unknown, max: number): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(-max) : [];
const int = (value: unknown, fallback = 0): number => (Number.isSafeInteger(value) ? (value as number) : fallback);
const bool = (value: unknown): boolean => value === true;

function accFrom(value: unknown, firstTextChecked = true): Acc {
  const raw = isRecord(value) ? value : {};
  return {
    actions: int(raw.actions),
    seen: int(raw.seen),
    first_text: raw.first_text === undefined ? firstTextChecked : bool(raw.first_text),
    dismissed: bool(raw.dismissed),
    applied: bool(raw.applied),
    cite_handle: bool(raw.cite_handle),
    cite_title: bool(raw.cite_title),
    opened: bool(raw.opened),
    title_assessable: bool(raw.title_assessable),
  };
}

function coreFrom(raw: Json): MemoryCore | null {
  if (typeof raw.mkey !== "string" || raw.mkey.length === 0 || typeof raw.channel !== "string") return null;
  return {
    mkey: raw.mkey,
    channel: raw.channel as SurfaceChannel,
    visible: bool(raw.visible),
    title: str(raw.title),
    handles: strList(raw.handles, MAX_SET),
    shingles: strList(raw.shingles, MAX_SET),
    echo: strList(raw.echo, MAX_SET),
    injected_is_memory: bool(raw.injected_is_memory),
  };
}

/** Rebuild an agent from parsed JSON, dropping anything malformed. */
function agentFrom(value: unknown, nowIso: string): AgentState | null {
  if (!isRecord(value)) return null;
  const agent = newAgent(str(value.wire, "unknown"), str(value.principal, "unknown"), undefined, nowIso);
  if (typeof value.subagent_type === "string") agent.subagent_type = value.subagent_type;
  agent.reset_id = typeof value.reset_id === "string" && RESET_ID_RE.test(value.reset_id) ? value.reset_id : INITIAL_EPOCH;
  agent.applied_resets = strList(value.applied_resets, MAX_RESETS);
  agent.tx = typeof value.tx === "string" && TX_KEY_RE.test(value.tx) ? value.tx : null;
  agent.offset = Math.max(0, int(value.offset));
  agent.history_stale = bool(value.history_stale);
  agent.done = strList(value.done, MAX_DONE);
  agent.last_activity = typeof value.last_activity === "string" && tsMs(value.last_activity) !== null ? value.last_activity : nowIso;
  if (Array.isArray(value.prompts)) {
    agent.prompts = value.prompts.filter(Array.isArray).slice(-3).map((list) => strList(list, MAX_PROMPT_TOKENS));
  }
  if (Array.isArray(value.prompt_shingles)) {
    agent.prompt_shingles = value.prompt_shingles.filter(Array.isArray).slice(-3).map((list) => strList(list, MAX_PROMPT_SHINGLES));
  }
  if (isRecord(value.tool_ids)) {
    for (const [id, kind] of Object.entries(value.tool_ids).slice(-MAX_TOOL_IDS)) {
      if (kind === "recall" || kind === "drill") agent.tool_ids[id] = kind;
    }
  }
  if (isRecord(value.turn) && Number.isSafeInteger(value.turn.key)) {
    const t = value.turn;
    agent.turn = {
      key: t.key as number,
      ix: int(t.ix),
      start_ts: typeof t.start_ts === "number" && Number.isFinite(t.start_ts) ? t.start_ts : null,
      human: bool(t.human),
      non_skip: bool(t.non_skip),
      text_hashes: strList(t.text_hashes, MAX_TEXT_HASHES),
      closed: bool(t.closed),
    };
    if (typeof t.prompt_id === "string") agent.turn.prompt_id = t.prompt_id;
    if (isRecord(t.ended)) {
      agent.turn.ended = { lam_hash: typeof t.ended.lam_hash === "string" ? t.ended.lam_hash : null, ts: str(t.ended.ts, nowIso) };
    }
  }
  for (const raw of Array.isArray(value.windows) ? value.windows.slice(0, MAX_WINDOWS) : []) {
    if (!isRecord(raw) || typeof raw.sid !== "string") continue;
    const core = coreFrom(raw);
    if (!core) continue;
    const window: WindowState = {
      ...core,
      sid: raw.sid,
      scope: raw.scope === "boot" ? "boot" : "turn",
      turn_key: Number.isSafeInteger(raw.turn_key) ? (raw.turn_key as number) : -1,
      boot_turns: int(raw.boot_turns),
      acc: accFrom(raw.acc),
    };
    if (isRecord(raw.provisional) && typeof raw.provisional.outcome === "string") {
      window.provisional = {
        outcome: raw.provisional.outcome,
        evidence: strList(raw.provisional.evidence, 8) as EvidenceKind[],
        title_assessable: bool(raw.provisional.title_assessable),
        ts: str(raw.provisional.ts, nowIso),
      };
    }
    agent.windows.push(window);
  }
  for (const raw of Array.isArray(value.exposures) ? value.exposures.slice(0, MAX_EXPOSURES) : []) {
    if (!isRecord(raw) || typeof raw.exposure_id !== "string") continue;
    const core = coreFrom(raw);
    if (!core) continue;
    const exposure: ExposureState = {
      ...core,
      exposure_id: raw.exposure_id,
      reset_id: str(raw.reset_id, agent.reset_id),
      first_sid: str(raw.first_sid),
      first_ts: str(raw.first_ts, nowIso),
      outcome: typeof raw.outcome === "string" ? raw.outcome : null,
      credits: Array.isArray(raw.credits) ? raw.credits.filter((n): n is number => Number.isSafeInteger(n)).slice(0, MAX_CREDITS) : [],
      idle_turns: int(raw.idle_turns),
      seen_this_turn: bool(raw.seen_this_turn),
      dismissed_turns: int(raw.dismissed_turns),
      seen_total: int(raw.seen_total),
      acc: accFrom(raw.acc),
    };
    if (raw.pending !== undefined) exposure.pending = accFrom(raw.pending);
    agent.exposures.push(exposure);
  }
  return agent;
}

function loadState(vaultPath: string, dir: string, sessionKey: string, nowIso: string): DetectorState {
  const fresh: DetectorState = { v: STATE_VERSION, session_key: sessionKey, agents: {} };
  const raw = readJsonFile(vaultPath, path.join(dir, STATE_FILE));
  if (!isRecord(raw) || raw.v !== STATE_VERSION || raw.session_key !== sessionKey || !isRecord(raw.agents)) return fresh;
  if (typeof raw.idle_closed === "string") fresh.idle_closed = raw.idle_closed;
  for (const [key, value] of Object.entries(raw.agents)) {
    if (!AGENT_KEY_RE.test(key)) continue;
    const agent = agentFrom(value, nowIso);
    if (agent) fresh.agents[key] = agent;
  }
  return fresh;
}

// ---------------------------------------------------------------------------
// Transcript
// ---------------------------------------------------------------------------

type Block =
  | { kind: "text"; text: string; msg?: string }
  | { kind: "tool_use"; id: string; name: string; input: unknown };

interface ToolResultBlock {
  tool_use_id: string;
  text: string;
  is_error: boolean;
}

interface TxEntry {
  off: number;
  end: number;
  ts: number | null;
  kind: "prompt" | "assistant" | "result" | "other";
  promptId?: string;
  sidechain: boolean;
  text?: string;
  blocks?: Block[];
  results?: ToolResultBlock[];
}

interface TranscriptHandle {
  fd: number;
  size: number;
  key: string;
  dev: number;
  ino: number;
}

function openTranscript(transcriptPath: unknown): TranscriptHandle | null {
  if (typeof transcriptPath !== "string" || transcriptPath.length === 0 || transcriptPath.includes("\0")) return null;
  let fd: number | undefined;
  try {
    const real = fs.realpathSync(transcriptPath);
    // O_NONBLOCK: a FIFO must open at once and be refused below, never wait for a writer.
    fd = fs.openSync(real, READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) {
      fs.closeSync(fd);
      return null;
    }
    return { fd, size: stat.size, key: sha(real, 16), dev: stat.dev, ino: stat.ino };
  } catch {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Ignore.
      }
    }
    return null;
  }
}

function readAt(handle: TranscriptHandle, from: number, length: number): Buffer {
  const buffer = Buffer.alloc(Math.max(0, length));
  let filled = 0;
  while (filled < buffer.length) {
    const n = fs.readSync(handle.fd, buffer, filled, buffer.length - filled, from + filled);
    if (n <= 0) break;
    filled += n;
  }
  return filled === buffer.length ? buffer : buffer.subarray(0, filled);
}

/** First line start at or after `offset` (offset itself when it already is one). */
function alignToLineStart(handle: TranscriptHandle, offset: number): number {
  if (offset <= 0) return 0;
  let position = offset - 1;
  while (position < handle.size) {
    const chunk = readAt(handle, position, Math.min(FAST_SCAN_CHUNK, handle.size - position));
    if (chunk.length === 0) break;
    const newline = chunk.indexOf(0x0a);
    if (newline >= 0) return position + newline + 1;
    position += chunk.length;
  }
  return handle.size;
}

interface RawLine {
  text: string;
  off: number;
  end: number;
}

interface LineChunk {
  lines: RawLine[];
  /** Offset after the last complete line consumed. */
  end: number;
  /** The read stopped at the byte cap with complete lines still unread. */
  more: boolean;
}

function readLines(handle: TranscriptHandle, from: number, maxBytes: number, deadline: number): LineChunk {
  const length = Math.min(maxBytes, handle.size - from);
  if (length <= 0) return { lines: [], end: from, more: false };
  const buffer = readAt(handle, from, length);
  const last = buffer.lastIndexOf(0x0a);
  if (last < 0) {
    if (from + buffer.length >= handle.size) return { lines: [], end: from, more: false };
    // One line longer than the cap: skip it by byte scan, unparsed.
    let position = from + buffer.length;
    while (position < handle.size && Date.now() < deadline) {
      const chunk = readAt(handle, position, Math.min(FAST_SCAN_CHUNK, handle.size - position));
      if (chunk.length === 0) break;
      const newline = chunk.indexOf(0x0a);
      if (newline >= 0) return { lines: [], end: position + newline + 1, more: true };
      position += chunk.length;
    }
    return { lines: [], end: from, more: true };
  }
  const lines: RawLine[] = [];
  let start = 0;
  while (start <= last) {
    const newline = buffer.indexOf(0x0a, start);
    if (newline < 0 || newline > last) break;
    if (newline > start) {
      lines.push({ text: buffer.toString("utf8", start, newline), off: from + start, end: from + newline + 1 });
    }
    start = newline + 1;
  }
  const end = from + last + 1;
  return { lines, end, more: length === maxBytes && end < handle.size };
}

/**
 * Byte scan for the fast path: where the complete lines end, whether the
 * delta names a minni recall tool, and whether the scan reached the end of
 * the file within the budget. No JSON is parsed.
 */
function fastScan(handle: TranscriptHandle, from: number, deadline: number): { end: number; minni: boolean; complete: boolean } {
  const needles = [Buffer.from("minni_recall"), Buffer.from("minni_drill")];
  const overlap = 16;
  let position = from;
  let end = from;
  while (position < handle.size) {
    if (Date.now() > deadline) return { end, minni: false, complete: false };
    const chunk = readAt(handle, position, Math.min(FAST_SCAN_CHUNK, handle.size - position));
    if (chunk.length === 0) break;
    if (needles.some((needle) => chunk.indexOf(needle) >= 0)) return { end, minni: true, complete: false };
    const newline = chunk.lastIndexOf(0x0a);
    if (newline >= 0) end = Math.max(end, position + newline + 1);
    if (position + chunk.length >= handle.size) break;
    // Overlap the next chunk so a needle split across chunks is still seen.
    position += Math.max(1, chunk.length - overlap);
  }
  return { end, minni: false, complete: true };
}

function resultText(content: unknown): string {
  if (typeof content === "string") return content.slice(0, MAX_INPUT_CHARS);
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const block of content) {
    if (isRecord(block) && block.type === "text" && typeof block.text === "string") parts.push(block.text);
  }
  return parts.join("\n").slice(0, MAX_INPUT_CHARS);
}

/** One transcript line in the Claude Code JSONL shape; null when it is not JSON. */
function parseEntry(line: RawLine): TxEntry | null {
  let raw: unknown;
  try {
    raw = JSON.parse(line.text);
  } catch {
    return null;
  }
  if (!isRecord(raw)) return null;
  const entry: TxEntry = {
    off: line.off,
    end: line.end,
    ts: tsMs(raw.timestamp),
    kind: "other",
    sidechain: raw.isSidechain === true,
  };
  if (typeof raw.promptId === "string" && raw.promptId.length > 0) entry.promptId = raw.promptId;
  const message = isRecord(raw.message) ? raw.message : null;
  const meta = raw.isMeta === true || raw.isCompactSummary === true;
  if (raw.type === "user" && message) {
    const content = message.content;
    if (typeof content === "string") {
      if (!meta) {
        entry.kind = "prompt";
        entry.text = content.slice(0, MAX_INPUT_CHARS);
      }
    } else if (Array.isArray(content)) {
      const results: ToolResultBlock[] = [];
      const texts: string[] = [];
      for (const block of content) {
        if (!isRecord(block)) continue;
        if (block.type === "tool_result" && typeof block.tool_use_id === "string") {
          results.push({ tool_use_id: block.tool_use_id, text: resultText(block.content), is_error: block.is_error === true });
        } else if (block.type === "text" && typeof block.text === "string") {
          texts.push(block.text);
        }
      }
      if (results.length > 0) {
        entry.kind = "result";
        entry.results = results;
      } else if (texts.length > 0 && !meta) {
        entry.kind = "prompt";
        entry.text = texts.join("\n").slice(0, MAX_INPUT_CHARS);
      }
    }
  } else if (raw.type === "assistant" && message && Array.isArray(message.content)) {
    entry.kind = "assistant";
    const msg = typeof message.id === "string" ? message.id : undefined;
    entry.blocks = [];
    for (const block of message.content) {
      if (!isRecord(block)) continue;
      if (block.type === "text" && typeof block.text === "string") {
        entry.blocks.push({ kind: "text", text: block.text.slice(0, MAX_INPUT_CHARS), msg });
      } else if (block.type === "tool_use" && typeof block.id === "string" && typeof block.name === "string") {
        entry.blocks.push({ kind: "tool_use", id: block.id, name: block.name, input: block.input });
      }
    }
  }
  return entry;
}

// ---------------------------------------------------------------------------
// Evidence
// ---------------------------------------------------------------------------

interface TextAct {
  kind: "text";
  /** The raw text: handle-style citations are looked for here. */
  text: string;
  /** Every word, tokenised as the ledger's shingles are: the applied and echo passes. */
  rawWords: string[];
  /** Hash of the 8-word shingle starting at each raw word; null near the end. */
  rawWindows: Array<string | null>;
  /** Every raw shingle. Handles are NOT stripped here (they are from the title/content pass). */
  shingles: Set<string>;
  /** The words outside handle-style forms (the title/content pass), each with its raw index and sentence. */
  keptWords: string[];
  keptRaw: number[];
  keptSentence: number[];
  /** Token sets per sentence, from the kept words. */
  sentences: Set<string>[];
  /** isDismissal(text), computed once per act (memo). */
  dismissal?: boolean;
}

interface ToolAct {
  kind: "tool";
  name: string;
  /** Every string (and number) leaf of the input, newline-joined. */
  leaves: string;
  shingles: Set<string>;
  filePath?: string;
  command?: string;
  /** A minni_recall's query. */
  query?: string;
  minni: "recall" | "drill" | null;
  // Per-act data, computed once per act and never per window (memos):
  /** The tool's base name (`mcp__x__minni_recall` -> `minni_recall`). */
  base: string;
  /** A Bash command that uses a read verb. */
  readVerb: boolean;
  /** The input's leaves, one per line, for doc-id opens. */
  leafLines?: string[];
  /** The recall query, NFKC. */
  queryText?: string;
  /** The recall query's kept words of 3+ characters, as one token set. */
  queryTokens?: Set<string>;
}

type Act = TextAct | ToolAct;

/**
 * Handle-style forms in assistant text: wikilinks, m:/d: handles,
 * `learning #id`, paths (anything with a slash, or a `.md` name), and joined
 * identifiers of 8+ characters (slugs such as `beryl-harbor-checkpoint-cadence`).
 * They are evidence for cited_handle only, and are removed before the
 * title/content pass so a handle's own words (a slug tokenises into title
 * words) never become title evidence: only the after pointer prints handles,
 * and letting them into used_ii would bias the comparison toward it. The
 * shingle (applied) and echo passes see every word: a body sentence that
 * contains a path or an identifier must still match.
 */
//
// Every pattern here runs over assistant text the detector does not control,
// inside a 1.5 s hook, so each is written to be linear: a repeated class may
// only start where its token starts (a lookbehind), or is bounded, so no
// start position re-scans a long token (use-detector-regex-cost.test.mjs).
const WIKILINK_RE = /\[\[[^\[\]\n]{1,512}\]\]/g;
const M_HANDLE_RE = /\bm:[0-9a-f]{8}\b/g;
const D_HANDLE_RE = /\bd:[A-Za-z0-9_-]{1,32}:[A-Za-z0-9_.-]{1,128}/g;
const LEARNING_REF_RE = /\blearning\s*#\s*[A-Za-z0-9_.-]+/gi;
const PATH_RE = /(?<![\p{L}\p{N}_.~-])[\p{L}\p{N}_.~-]*(?:\/[\p{L}\p{N}_.~-]+)+\/?/gu;
const MD_NAME_RE = /(?<![\p{L}\p{N}_-])[\p{L}\p{N}_-]+\.md\b/giu;
const HANDLE_FORM_RES: readonly RegExp[] = [WIKILINK_RE, M_HANDLE_RE, D_HANDLE_RE, LEARNING_REF_RE, PATH_RE, MD_NAME_RE];
/** Hyphen- or underscore-joined identifiers; removed when at least 8 characters long. */
const JOINED_RE = /(?<![\p{L}\p{N}])[\p{L}\p{N}]+(?:[-_][\p{L}\p{N}]+)+(?![\p{L}\p{N}])/gu;
const MIN_JOINED_CHARS = 8;

/** Sorted, merged [start, end) spans of handle-style forms in `text`. */
function handleSpans(text: string): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  for (const re of HANDLE_FORM_RES) {
    for (const match of text.matchAll(re)) {
      if (match[0].length > 0) spans.push([match.index as number, (match.index as number) + match[0].length]);
    }
  }
  for (const match of text.matchAll(JOINED_RE)) {
    if (match[0].length >= MIN_JOINED_CHARS) spans.push([match.index as number, (match.index as number) + match[0].length]);
  }
  spans.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: Array<[number, number]> = [];
  for (const span of spans) {
    const last = merged[merged.length - 1];
    if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
    else merged.push([span[0], span[1]]);
  }
  return merged;
}

/** Per-sentence token sets (words of 3+ characters), optionally only of the kept words. */
function tokenSets(words: readonly string[], sentenceOf: readonly number[], keep: readonly boolean[] | null): Set<string>[] {
  const bySentence = new Map<number, Set<string>>();
  for (let i = 0; i < words.length; i += 1) {
    if (keep && !keep[i]) continue;
    const word = words[i] as string;
    if (word.length < 3) continue;
    const sentence = sentenceOf[i] as number;
    let set = bySentence.get(sentence);
    if (!set) {
      set = new Set();
      bySentence.set(sentence, set);
    }
    set.add(word);
  }
  return [...bySentence.values()];
}

/**
 * One assistant text, tokenised once. Raw words (all of them, as the ledger
 * tokenises bodies and injected text) feed the shingle and echo passes; the
 * words outside handle-style spans feed the title/content pass. Both come from
 * one word scan with offsets, so an echo window covering raw words maps
 * exactly onto the kept words it touches.
 */
interface WordScan {
  text: string;
  rawWords: string[];
  keptWords: string[];
  keptRaw: number[];
  keptSentence: number[];
}

/** The one word scan: NFKC, handle spans, sentence ids, lowercased words (raw and kept). */
function scanWords(raw: string): WordScan {
  const text = raw.slice(0, MAX_INPUT_CHARS).normalize("NFKC");
  const spans = handleSpans(text);
  let spanAt = 0;
  const insideSpan = (offset: number): boolean => {
    while (spanAt < spans.length && (spans[spanAt] as [number, number])[1] <= offset) spanAt += 1;
    return spanAt < spans.length && (spans[spanAt] as [number, number])[0] <= offset;
  };
  const breaks: number[] = [];
  for (const match of text.matchAll(SENTENCE_BREAK_RE)) breaks.push(match.index as number);
  spanAt = 0;
  const realBreaks = breaks.filter((offset) => !insideSpan(offset));
  spanAt = 0;
  const rawWords: string[] = [];
  const keptWords: string[] = [];
  const keptRaw: number[] = [];
  const keptSentence: number[] = [];
  let sentence = 0;
  for (const match of text.matchAll(WORD_RE)) {
    const offset = match.index as number;
    while (sentence < realBreaks.length && (realBreaks[sentence] as number) < offset) sentence += 1;
    const kept = !insideSpan(offset);
    // The ledger lowercases, then matches. Lowercasing can split a word
    // ("İ" -> "i" + U+0307, which the word rule does not match), so each
    // lowercased match is re-split by the same rule: identical words, and a
    // raw index for every piece.
    for (const word of match[0].toLowerCase().match(WORD_RE) ?? []) {
      if (kept) {
        keptWords.push(word);
        keptRaw.push(rawWords.length);
        keptSentence.push(sentence);
      }
      rawWords.push(word);
    }
  }
  return { text, rawWords, keptWords, keptRaw, keptSentence };
}

function textAct(raw: string): TextAct {
  const { text, rawWords, keptWords, keptRaw, keptSentence } = scanWords(raw);
  const rawWindows: Array<string | null> = [];
  const shingles = new Set<string>();
  for (let i = 0; i < rawWords.length; i += 1) {
    if (i + SHINGLE_WORDS <= rawWords.length) {
      const hash = shingleHashOf(rawWords.slice(i, i + SHINGLE_WORDS).join(" "));
      rawWindows.push(hash);
      shingles.add(hash);
    } else {
      rawWindows.push(null);
    }
  }
  return {
    kind: "text",
    text,
    rawWords,
    rawWindows,
    shingles,
    keptWords,
    keptRaw,
    keptSentence,
    sentences: tokenSets(keptWords, keptSentence, null),
  };
}

/**
 * Sentence token sets of the words NOT inside an 8-word echo of the injected
 * text. For channels whose injected text is the memory, a recital is not use:
 * title/content tokens count only outside it, so a paraphrase still does.
 */
function sentencesOutsideEcho(act: TextAct, echo: ReadonlySet<string>): Set<string>[] {
  // Fast path, O(|echo|): no echo shingle in this act means nothing to mask.
  let any = false;
  for (const hash of echo) {
    if (act.shingles.has(hash)) {
      any = true;
      break;
    }
  }
  if (!any) return act.sentences;
  // Coverage on the raw words, where the ledger's echo hashes live; then
  // mapped onto the kept words. A window that spans an identifier still
  // matches, because identifiers are stripped only from the title pass.
  let covered: boolean[] | null = null;
  for (let i = 0; i < act.rawWindows.length; i += 1) {
    const hash = act.rawWindows[i];
    if (hash === null || hash === undefined || !echo.has(hash)) continue;
    covered ??= new Array<boolean>(act.rawWords.length).fill(false);
    for (let j = i; j < i + SHINGLE_WORDS && j < act.rawWords.length; j += 1) covered[j] = true;
  }
  if (!covered) return act.sentences;
  const cover = covered;
  return tokenSets(act.keptWords, act.keptSentence, act.keptRaw.map((index) => !cover[index]));
}

function inputLeaves(input: unknown): string {
  const parts: string[] = [];
  let budget = MAX_INPUT_CHARS;
  const walk = (value: unknown, depth: number): void => {
    if (budget <= 0 || depth > 8) return;
    if (typeof value === "string") {
      parts.push(value.slice(0, budget));
      budget -= value.length;
    } else if (typeof value === "number" && Number.isFinite(value)) {
      parts.push(String(value));
    } else if (Array.isArray(value)) {
      for (const item of value) walk(item, depth + 1);
    } else if (isRecord(value)) {
      for (const item of Object.values(value)) walk(item, depth + 1);
    }
  };
  walk(input, 0);
  return parts.join("\n");
}

function toolAct(name: string, input: unknown): ToolAct {
  const leaves = inputLeaves(input);
  const act: ToolAct = {
    kind: "tool",
    name,
    leaves,
    shingles: new Set(allShingleHashes(leaves)),
    minni: (MINNI_TOOL_RE.exec(name)?.[1] as "recall" | "drill" | undefined) ?? null,
    base: name.split("__").pop() ?? name,
    readVerb: false,
  };
  if (isRecord(input)) {
    if (typeof input.file_path === "string") act.filePath = input.file_path;
    if (typeof input.command === "string") act.command = input.command;
    if (typeof input.query === "string") act.query = input.query.slice(0, MAX_INPUT_CHARS);
  }
  if (act.base === "Bash" && act.command) act.readVerb = READ_VERB_RE.test(`${act.command} `);
  return act;
}

export function isDismissal(text: string): boolean {
  const opening = text.slice(0, DISMISSAL_PREFIX_CHARS);
  return DISMISSAL_PATTERNS.some((pattern) => pattern.test(opening));
}

/** What the rules need to recognise one memory. Built per run, never stored. */
interface Profile {
  core: MemoryCore;
  shingles: Set<string>;
  echo: Set<string>;
  /** Distinctive title tokens with their hashes; null when the title rule is off. */
  title: Array<{ token: string; hash: string }> | null;
  /** Bounded patterns of every name of the memory: handles, wikilinks, paths, slug, learning #id. */
  names: RegExp[];
  /** The vault path (with or without .md), bounded: for Bash opens. */
  relRe: RegExp | null;
  slugRe: RegExp | null;
  /** Vault-relative path, for Read opens. */
  rel: string | null;
  doc: { src: string; id: string } | null;
  /** The full sanitized title, bounded and case-insensitive: a minni_recall query naming it opens it. */
  titleRe: RegExp | null;
}

/** `name` not inside a longer name: no name character on either side. */
function boundedName(name: string, suffix = ""): RegExp {
  return new RegExp(`(?<![\\p{L}\\p{N}_.:-])${escapeRegExp(name)}${suffix}(?![\\p{L}\\p{N}_-]|\\.[\\p{L}\\p{N}])`, "iu");
}

function profileFor(core: MemoryCore, df: DfSnapshot | null): Profile {
  const handles = unionBounded(core.handles, handlesForKey(core.mkey), 16);
  const profile: Profile = {
    core,
    shingles: new Set(core.shingles),
    echo: new Set(core.echo),
    title: null,
    names: handles.map((handle) => boundedName(handle)),
    relRe: null,
    slugRe: null,
    rel: null,
    doc: null,
    titleRe: null,
  };
  const fullTitle = core.title.normalize("NFKC").trim();
  if (fullTitle.length >= MIN_TITLE_SUBSTRING_CHARS) {
    profile.titleRe = new RegExp(
      `(?<![\\p{L}\\p{N}])${fullTitle.split(/\s+/).map(escapeRegExp).join("\\s+")}(?![\\p{L}\\p{N}])`,
      "iu",
    );
  }
  if (df) {
    profile.title = useTokens(core.title)
      .filter((token) => (df.df.get(token) ?? 0) <= df.threshold)
      .map((token) => ({ token, hash: tokenHash(token) }));
  }
  if (core.mkey.startsWith("vault:")) {
    const rel = core.mkey.slice("vault:".length);
    const bare = rel.replace(/\.md$/i, "");
    profile.rel = rel;
    profile.relRe = boundedName(bare, "(?:\\.md)?");
    profile.names.push(profile.relRe);
    const slug = path.posix.basename(bare);
    if (slug.length >= MIN_SLUG_CHARS) {
      profile.slugRe = boundedName(slug, "(?:\\.md)?");
      profile.names.push(profile.slugRe);
    }
  } else if (core.mkey.startsWith("learning:")) {
    const id = core.mkey.slice("learning:".length);
    profile.names.push(new RegExp(`\\blearning\\s*#\\s*${escapeRegExp(id)}(?![\\p{L}\\p{N}])`, "iu"));
  } else {
    const doc = /^doc:([^:]+):(.+)$/.exec(core.mkey);
    if (doc) profile.doc = { src: doc[1] as string, id: doc[2] as string };
  }
  return profile;
}

function citesHandle(profile: Profile, text: string): boolean {
  return profile.names.some((re) => re.test(text));
}

function citesTitle(tokens: ReadonlyArray<{ token: string }>, sentences: readonly Set<string>[]): boolean {
  for (const sentence of sentences) {
    let hits = 0;
    for (const { token } of tokens) {
      if (sentence.has(token)) hits += 1;
      if (hits >= MIN_TITLE_TOKENS) return true;
    }
  }
  return false;
}

/**
 * A stored body shingle found in an act. Never one the injection carried
 * (echo), except inside a tool input for channels whose injected text is the
 * memory; never, in assistant text, one the recent prompts carried (restating
 * the prompt is not applying the memory).
 */
function hitsShingle(
  profile: Profile,
  found: ReadonlySet<string>,
  inToolInput: boolean,
  promptShingles: ReadonlySet<string>,
): boolean {
  for (const hash of profile.shingles) {
    if (!found.has(hash)) continue;
    if (!inToolInput && promptShingles.has(hash)) continue;
    if (!profile.echo.has(hash)) return true;
    if (profile.core.injected_is_memory && inToolInput) return true;
  }
  return false;
}

function pathNames(filePath: string, rel: string): boolean {
  const normalized = filePath.split(path.sep).join("/").replace(/\\/g, "/");
  return normalized === rel || normalized.endsWith(`/${rel}`);
}

/**
 * A minni_recall query that cites the memory's title opens it, in both
 * windows (before has only the title to recall by): the same title rule as a
 * citation (3 distinctive, prompt-filtered title tokens, handle forms
 * removed), or the full sanitized title as a substring.
 */
function recallCitesTitle(profile: Profile, act: ToolAct, title: ReadonlyArray<{ token: string }> | null): boolean {
  // Tokenised once per act: this runs for every open window and exposure.
  if (act.queryText === undefined) act.queryText = (act.query ?? act.leaves).normalize("NFKC");
  if (profile.titleRe?.test(act.queryText)) return true;
  if (!title || title.length < MIN_TITLE_TOKENS) return false;
  if (act.queryTokens === undefined) {
    act.queryTokens = new Set(scanWords(act.queryText).keptWords.filter((word) => word.length >= 3));
  }
  return citesTitle(title, [act.queryTokens]);
}

function opens(profile: Profile, act: ToolAct, title: ReadonlyArray<{ token: string }> | null): boolean {
  if (profile.rel) {
    if (act.base === "Read" && act.filePath && pathNames(act.filePath, profile.rel)) return true;
    if (act.readVerb && act.command) {
      if (profile.relRe?.test(act.command) || profile.slugRe?.test(act.command)) return true;
    }
  }
  if (act.minni) {
    if (profile.names.some((re) => re.test(act.leaves))) return true;
    if (act.minni === "recall" && recallCitesTitle(profile, act, title)) return true;
    if (profile.doc) {
      act.leafLines ??= act.leaves.split("\n");
      if (act.leafLines.includes(profile.doc.id) && act.leafLines.includes(profile.doc.src)) return true;
    }
  }
  return false;
}

/** Title tokens left once the current and previous 2 prompts' tokens are dropped. */
function titleAfterPrompts(profile: Profile, promptHashes: ReadonlySet<string>): Array<{ token: string; hash: string }> | null {
  if (!profile.title) return null;
  return profile.title.filter((entry) => !promptHashes.has(entry.hash));
}

function observe(
  acc: Acc,
  profile: Profile,
  act: Act,
  title: ReadonlyArray<{ token: string }> | null,
  capped: boolean,
  promptShingles: ReadonlySet<string>,
): void {
  if (capped && acc.actions >= TURN_ACTION_CAP) return;
  acc.actions += 1;
  acc.seen += 1;
  if (act.kind === "text") {
    if (!acc.first_text) {
      acc.first_text = true;
      act.dismissal ??= isDismissal(act.text);
      if (act.dismissal) acc.dismissed = true;
    }
    if (hitsShingle(profile, act.shingles, false, promptShingles)) acc.applied = true;
    if (citesHandle(profile, act.text)) acc.cite_handle = true;
    if (title && title.length >= MIN_TITLE_TOKENS) {
      const sentences = profile.core.injected_is_memory ? sentencesOutsideEcho(act, profile.echo) : act.sentences;
      if (citesTitle(title, sentences)) acc.cite_title = true;
    }
  } else {
    if (hitsShingle(profile, act.shingles, true, promptShingles)) acc.applied = true;
    if (opens(profile, act, title)) {
      // Naming a handle and then opening it is the open, not a citation. A
      // title citation stands: it is identifier-free evidence in both windows,
      // and each window opens its own way (recall by title, drill by handle).
      acc.opened = true;
      acc.cite_handle = false;
    }
  }
}

interface Judgment {
  outcome: string;
  evidence: EvidenceKind[];
  title_assessable: boolean;
}

/**
 * A window's outcome. `cited` is a title/content citation (used_ii);
 * `cited_handle` a handle-style one, its own bucket and never used_ii.
 * Dismissal is sticky for both: the window can still end applied or opened.
 */
function judge(acc: Acc, visible: boolean, unreached: string): Judgment {
  const evidence: EvidenceKind[] = [];
  if (acc.applied) evidence.push("shingle");
  if (acc.cite_handle) evidence.push("handle");
  if (acc.cite_title) evidence.push("title");
  if (acc.opened) evidence.push("open");
  if (acc.dismissed) evidence.push("lexicon");
  let outcome: string;
  if (acc.applied) outcome = "applied";
  else if (acc.cite_title && !acc.dismissed) outcome = "cited";
  else if (acc.cite_handle && !acc.dismissed) outcome = "cited_handle";
  else if (acc.opened) outcome = "opened";
  else if (acc.dismissed) outcome = "dismissed";
  else if (!visible) outcome = "unknown(not_visible)";
  else if (acc.seen === 0) outcome = unreached;
  else outcome = "ignored";
  return { outcome, evidence, title_assessable: acc.title_assessable };
}

const OUTCOME_RANK: Record<string, number> = { applied: 7, cited: 6, cited_handle: 5, opened: 4, dismissed: 3, ignored: 2 };

function rank(outcome: string | null): number {
  if (outcome === null) return 0;
  return OUTCOME_RANK[outcome] ?? 1;
}

function strongest(a: string | null, b: string | null): string | null {
  return rank(b) > rank(a) ? b : a;
}

/**
 * An exposure turn's evidence outcome, or null. No dismissal gate: at the
 * exposure level dismissal is a counted diagnostic only, so a re-named and a
 * named-once memory are judged alike.
 */
function evidenceOutcome(acc: Acc): string | null {
  if (acc.applied) return "applied";
  if (acc.cite_title) return "cited";
  if (acc.cite_handle) return "cited_handle";
  if (acc.opened) return "opened";
  return null;
}

// ---------------------------------------------------------------------------
// Explicit recall cards (§4.4 "explicit_recall rows")
// ---------------------------------------------------------------------------

interface Card {
  rank: number;
  wikilink?: string;
  title?: string;
  source?: string;
  src?: unknown;
  doc_id?: unknown;
}

const CARD_LINE_RE = /^\s*(\d{1,3})\.\s+\[\[([^\]\n]{1,512})\]\](.*)$/;
/** Bounded: the attribute list and the value are scanned at most 512 / 2,048 chars per tag. */
const EVIDENCE_SOURCE_RE = /<EVIDENCE\b[^>\n]{0,512}?\bsource="([^"\n]{0,2048})"/i;
/** Leading whitespace only from the start of its run, and a bounded score value: linear. */
const SCORE_SUFFIX_RE = /(?<!\s)\s*\((?:vault )?score=[^)\n]{0,64}\)\s*$/;
/** Only this much of a card line's tail is examined for its title and score. */
const CARD_TAIL_CHARS = 1024;

function unescapeAttr(value: string): string {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

/** Cards of a formatted minni_recall result (`N. [[wikilink]] — title (score=…)`). */
function recallCards(text: string): Card[] {
  const cards: Card[] = [];
  let current: Card | null = null;
  for (const line of text.split("\n")) {
    const match = CARD_LINE_RE.exec(line);
    if (match) {
      if (cards.length >= MAX_CARDS) break;
      const rest = (match[3] as string).slice(0, CARD_TAIL_CHARS).replace(SCORE_SUFFIX_RE, "");
      const title = /^\s+—\s+(.+)$/.exec(rest)?.[1]?.trim();
      current = { rank: cards.length, wikilink: match[2] as string };
      if (title) current.title = title;
      cards.push(current);
      continue;
    }
    if (/^#{1,6}\s/.test(line)) {
      current = null;
      continue;
    }
    if (current && current.source === undefined) {
      const source = EVIDENCE_SOURCE_RE.exec(line)?.[1];
      if (source !== undefined) current.source = unescapeAttr(source);
    }
  }
  return cards;
}

/** Cards of a minni_drill result: JSON objects naming a `source` or `doc_id`. */
function drillCards(text: string): Card[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return recallCards(text);
  }
  const cards: Card[] = [];
  let visited = 0;
  const walk = (value: unknown, depth: number): void => {
    if (cards.length >= MAX_CARDS || depth > 6 || visited > 2000) return;
    visited += 1;
    if (Array.isArray(value)) {
      for (const item of value) walk(item, depth + 1);
      return;
    }
    if (!isRecord(value)) return;
    if (typeof value.source === "string" || value.doc_id !== undefined) {
      const card: Card = { rank: cards.length, src: value.src, doc_id: value.doc_id };
      if (typeof value.source === "string" && value.source.length > 0) card.source = value.source;
      if (typeof value.wikilink === "string") card.wikilink = value.wikilink.replace(/^\[\[|\]\]$/g, "");
      const title = typeof value.heading === "string" ? value.heading : typeof value.title === "string" ? value.title : undefined;
      if (title) card.title = title;
      cards.push(card);
      return; // a card's nested chunks name the same memory
    }
    for (const item of Object.values(value)) walk(item, depth + 1);
  };
  walk(parsed, 0);
  return cards;
}

/** A vault-relative path that is a regular file inside the vault, or null. */
function existingVaultRel(vaultPath: string, candidate: string): string | null {
  const rel = candidate.replace(/\\/g, "/").replace(/^\.\/+/, "");
  if (rel.length === 0 || rel.includes("\0") || path.posix.isAbsolute(rel) || rel.split("/").includes("..")) return null;
  try {
    return fs.statSync(path.join(vaultPath, rel)).isFile() ? rel : null;
  } catch {
    return null;
  }
}

/** The card's mkey through memoryKeyFor, keyed on its source; null = unnameable. */
function keyCard(vaultPath: string, card: Card): string | null {
  if (typeof card.source === "string" && card.source.length > 0) {
    if (path.isAbsolute(card.source)) {
      const key = memoryKeyFor({ kind: "daemon", source: card.source, src: card.src, doc_id: card.doc_id }, vaultPath);
      if (key) return key;
    } else {
      const rel = existingVaultRel(vaultPath, card.source);
      if (rel) return memoryKeyFor({ kind: "vault", path: rel }, vaultPath);
    }
  }
  if (card.src !== undefined && card.doc_id !== undefined) {
    const key = memoryKeyFor({ kind: "daemon", src: card.src, doc_id: card.doc_id }, vaultPath);
    if (key) return key;
  }
  if (typeof card.wikilink === "string") {
    const inner = (card.wikilink.split("|")[0] ?? "").split("#")[0]?.trim() ?? "";
    for (const candidate of [inner, `${inner}.md`]) {
      const rel = existingVaultRel(vaultPath, candidate);
      if (rel) return memoryKeyFor({ kind: "vault", path: rel }, vaultPath);
    }
  }
  return null;
}

/**
 * Every regex the detector runs over untrusted text, by name, for the
 * linear-cost test. Not used by the detector itself; frozen.
 */
export const DETECTOR_TEXT_PATTERNS: Readonly<Record<string, RegExp>> = Object.freeze({
  wikilink: WIKILINK_RE,
  m_handle: M_HANDLE_RE,
  d_handle: D_HANDLE_RE,
  learning_ref: LEARNING_REF_RE,
  path: PATH_RE,
  md_name: MD_NAME_RE,
  joined: JOINED_RE,
  word: WORD_RE,
  sentence_break: SENTENCE_BREAK_RE,
  read_verb: READ_VERB_RE,
  minni_tool: MINNI_TOOL_RE,
  card_line: CARD_LINE_RE,
  evidence_source: EVIDENCE_SOURCE_RE,
  score_suffix: SCORE_SUFFIX_RE,
  ...Object.fromEntries(DISMISSAL_PATTERNS.map((pattern, index) => [`dismissal_${index}`, pattern])),
});

// ---------------------------------------------------------------------------
// Ledger index and row writer
// ---------------------------------------------------------------------------

interface LedgerIndex {
  /** This agent's surfaced rows, oldest first. */
  surfaced: SurfacedRow[];
  surfacedSids: Set<string>;
  /** `${sid}:${final}` of every judged row in the session. */
  judgedKeys: Set<string>;
  finalSids: Set<string>;
  exposureFinal: Set<string>;
  turnsByPrompt: Map<string, TurnRow>;
  /** This agent's resets, oldest first. */
  resets: ResetRow[];
}

function indexLedger(rows: readonly LedgerRow[], agentKey: string): LedgerIndex {
  const index: LedgerIndex = {
    surfaced: [],
    surfacedSids: new Set(),
    judgedKeys: new Set(),
    finalSids: new Set(),
    exposureFinal: new Set(),
    turnsByPrompt: new Map(),
    resets: [],
  };
  for (const row of rows) {
    if (!isRecord(row)) continue;
    switch (row.kind) {
      case "surfaced":
        if (typeof row.sid !== "string" || typeof row.mkey !== "string") break;
        index.surfacedSids.add(row.sid);
        if (row.agent_key === agentKey && tsMs(row.ts) !== null) index.surfaced.push(row);
        break;
      case "judged":
        if (typeof row.sid !== "string") break;
        index.judgedKeys.add(`${row.sid}:${row.final === true}`);
        if (row.final === true) index.finalSids.add(row.sid);
        break;
      case "exposure":
        if (row.final === true && typeof row.exposure_id === "string") index.exposureFinal.add(row.exposure_id);
        break;
      case "turn":
        if (row.agent_key === agentKey && typeof row.prompt_id === "string") index.turnsByPrompt.set(row.prompt_id, row);
        break;
      case "reset":
        if (row.agent_key === agentKey && typeof row.reset_id === "string" && tsMs(row.ts) !== null) index.resets.push(row);
        break;
      default:
        break;
    }
  }
  const byTs = (a: { ts: string }, b: { ts: string }): number => (tsMs(a.ts) as number) - (tsMs(b.ts) as number);
  index.surfaced.sort(byTs);
  index.resets.sort(byTs);
  return index;
}

/**
 * An exposure's outcome: the strongest evidence across its creditable turns,
 * else dismissed (a dismissal on some turn), else ignored, or unknown when it
 * was never visible or never reached. Built from per-turn evidence only, never
 * from window judgments, so a re-named and a named-once memory get the same.
 */
function exposureOutcome(exposure: ExposureState): string {
  if (exposure.outcome) return exposure.outcome;
  if (!exposure.visible) return "unknown(not_visible)";
  if (exposure.dismissed_turns > 0) return "dismissed";
  return exposure.seen_total > 0 ? "ignored" : "unknown(unreached)";
}

class RowWriter {
  readonly rows: LedgerRow[] = [];
  judged = 0;
  exposures = 0;
  surfaced = 0;

  constructor(
    private readonly index: LedgerIndex,
    private readonly context: LedgerContext,
    private readonly ts: string,
  ) {}

  contextWith(promptId?: string): LedgerContext {
    return promptId ? { ...this.context, prompt_id: promptId } : this.context;
  }

  writeJudged(sid: string, judgment: Judgment, final: boolean, via: string, finalReason?: string): boolean {
    const key = `${sid}:${final}`;
    if (this.index.judgedKeys.has(key)) return false;
    this.index.judgedKeys.add(key);
    if (final) this.index.finalSids.add(sid);
    this.rows.push(
      buildJudgedRow(
        this.context,
        {
          sid,
          outcome: judgment.outcome,
          via,
          evidence: judgment.evidence,
          title_assessable: judgment.title_assessable,
          final,
          rule_version: DETECTOR_RULE_VERSION,
          ...(finalReason ? { final_reason: finalReason } : {}),
        },
        this.ts,
      ),
    );
    this.judged += 1;
    return true;
  }

  writeExposure(exposure: ExposureState): boolean {
    const credits = [...new Set(exposure.credits)].sort((a, b) => a - b).slice(0, MAX_CREDITS);
    const row = buildExposureRow(
      this.context,
      {
        reset_id: exposure.reset_id,
        mkey: exposure.mkey,
        first_sid: exposure.first_sid,
        channel: exposure.channel,
        visible: exposure.visible,
        outcome: exposureOutcome(exposure),
        credits,
        final: true,
        dismissed_turns: exposure.dismissed_turns,
      },
      this.ts,
    );
    // Keyed on the row's own id (session, agent, epoch, mkey): one per epoch and memory.
    if (this.index.exposureFinal.has(row.exposure_id)) return false;
    this.index.exposureFinal.add(row.exposure_id);
    this.rows.push(row);
    this.exposures += 1;
    return true;
  }

  writeSurfaced(row: SurfacedRow): boolean {
    if (this.index.surfacedSids.has(row.sid)) return false;
    this.index.surfacedSids.add(row.sid);
    this.rows.push(row);
    this.surfaced += 1;
    return true;
  }
}

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

interface PendingSurfacing {
  row: SurfacedRow;
  ts: number;
  /** Its prompt line is in this delta: it activates in that turn. */
  bound: boolean;
  armed: boolean;
  done: boolean;
}

interface RunEnv {
  vaultPath: string;
  sessionKey: string;
  agentKey: string;
  nowMs: number;
  nowIso: string;
  lam: string | null;
  deadline: number;
}

interface DueEvent {
  ts: number;
  reset: boolean;
  sessionStart: boolean;
  boot: boolean;
  apply: () => void;
}

/**
 * Apply one batch of events due with no transcript entry between them: in
 * time order, except that a SessionStart reset goes before the batch's boot
 * surfacings whatever their timestamps. A SessionStart opens the epoch its
 * boot rows belong to; written in the other order, a final judgment would
 * otherwise close those windows unreached (defence in depth: S1a writes the
 * reset first).
 */
function applyBatch(due: DueEvent[]): void {
  const bootTimes = due.filter((event) => event.boot).map((event) => event.ts);
  if (bootTimes.length > 0) {
    const firstBoot = Math.min(...bootTimes);
    for (const event of due) if (event.sessionStart && firstBoot < event.ts) event.ts = firstBoot - 0.5;
  }
  due.sort((a, b) => a.ts - b.ts || Number(b.reset) - Number(a.reset));
  for (const event of due) event.apply();
}

class DetectorRun {
  private readonly profiles = new Map<string, Profile>();
  private promptHashes = new Set<string>();
  private promptShingles = new Set<string>();
  private pending: PendingSurfacing[] = [];
  private resets: Array<{ row: ResetRow; ts: number; done: boolean }> = [];
  private readonly known = new Set<string>();
  private msgTexts = new Map<string, string[]>();
  linesParsed = 0;
  assistantSeen = 0;
  unnameable = 0;
  provisional = 0;
  timedOut = false;
  processedEnd: number | null = null;

  constructor(
    private readonly env: RunEnv,
    private readonly agent: AgentState,
    private readonly writer: RowWriter,
    private readonly df: DfSnapshot | null,
    private readonly index: LedgerIndex,
  ) {
    for (const window of agent.windows) this.known.add(window.sid);
    for (const sid of agent.done) this.known.add(sid);
    for (const sid of index.finalSids) this.known.add(sid);
    this.promptHashes = new Set(agent.prompts.flat());
    this.promptShingles = new Set(agent.prompt_shingles.flat());
  }

  get isMain(): boolean {
    return this.env.agentKey === "main";
  }

  /** New surfacings and resets from the ledger; undelivered ones are judged at once. */
  collectEvents(): void {
    for (const row of this.index.surfaced) {
      if (this.known.has(row.sid)) continue;
      this.known.add(row.sid);
      if (row.delivered !== true) {
        this.writer.writeJudged(row.sid, { outcome: "unknown(not_delivered)", evidence: [], title_assessable: false }, true, "not_delivered");
        pushBounded(this.agent.done, row.sid, MAX_DONE);
        continue;
      }
      this.pending.push({ row, ts: tsMs(row.ts) as number, bound: false, armed: false, done: false });
    }
    const applied = new Set(this.agent.applied_resets);
    for (const row of this.index.resets) {
      if (applied.has(row.reset_id)) continue;
      this.resets.push({ row, ts: tsMs(row.ts) as number, done: false });
    }
  }

  get hasNewEvents(): boolean {
    return this.pending.length > 0;
  }

  /** The earliest tx_off among new surfacings, for a new cursor. */
  earliestTxOff(): number | null {
    let min: number | null = null;
    for (const entry of this.pending) {
      const off = entry.row.tx_off;
      if (typeof off === "number" && Number.isFinite(off) && off >= 0) min = min === null ? off : Math.min(min, off);
    }
    return min;
  }

  bindTo(entries: readonly TxEntry[]): void {
    const prompts = new Set<string>();
    for (const entry of entries) if (entry.kind === "prompt" && entry.promptId) prompts.add(entry.promptId);
    for (const pending of this.pending) {
      const promptId = pending.row.prompt_id;
      pending.bound = typeof promptId === "string" && prompts.has(promptId);
    }
  }

  private profile(core: MemoryCore & { sid?: string; exposure_id?: string }): Profile {
    const key = core.sid ?? core.exposure_id ?? core.mkey;
    let profile = this.profiles.get(key);
    if (!profile) {
      profile = profileFor(core, this.df);
      this.profiles.set(key, profile);
    }
    return profile;
  }

  private titleFor(core: MemoryCore & { sid?: string; exposure_id?: string }): Array<{ token: string; hash: string }> | null {
    return titleAfterPrompts(this.profile(core), this.promptHashes);
  }

  private unreachedReason(): string {
    return this.linesParsed > 0 && this.assistantSeen === 0 && !this.env.lam
      ? "unknown(unparsed_transcript)"
      : "unknown(unreached)";
  }

  // -- events ---------------------------------------------------------------

  /**
   * Which turn a per-turn surfacing belongs to: its own (matched by
   * prompt_id), else the live turn, else the next one (-1). Null when its
   * prompt_id names a turn that started after it and is already gone: the
   * window was missed.
   */
  private turnKeyFor(row: SurfacedRow): number | null {
    const turn = this.agent.turn;
    if (!turn) return -1;
    const promptId = row.prompt_id;
    if (promptId && turn.prompt_id) {
      if (promptId === turn.prompt_id) return turn.key;
      const ts = tsMs(row.ts);
      if (ts !== null && turn.start_ts !== null && ts < turn.start_ts) return null;
    }
    return !turn.closed && !turn.ended ? turn.key : -1;
  }

  private activate(row: SurfacedRow): void {
    const channel = row.channel;
    const core: MemoryCore = {
      mkey: row.mkey,
      channel,
      visible: row.visible === true,
      title: typeof row.title === "string" ? row.title : "",
      handles: Array.isArray(row.handles) ? row.handles.filter((h) => typeof h === "string").slice(0, 8) : [],
      shingles: Array.isArray(row.shingles) ? row.shingles.slice(0, MAX_SET) : [],
      echo: Array.isArray(row.echo) ? row.echo.slice(0, MAX_SET) : [],
      injected_is_memory: INJECTED_IS_MEMORY.has(channel),
    };
    const scope = BOOT_SCOPE.has(channel) ? "boot" : "turn";
    const turnKey = scope === "turn" ? this.turnKeyFor(row) : -1;
    if (turnKey === null) {
      this.writer.writeJudged(row.sid, { outcome: "unknown(missed_window)", evidence: [], title_assessable: false }, true, "transcript");
      pushBounded(this.agent.done, row.sid, MAX_DONE);
    } else if (this.agent.windows.length < MAX_WINDOWS) {
      const window: WindowState = { ...core, sid: row.sid, scope, turn_key: turnKey, boot_turns: 0, acc: newAcc(false) };
      // Assessable for a turn it is judged in; a window not yet in a turn is
      // decided when it joins one (boundary or Stop), never latched here.
      const turn = this.agent.turn;
      const live = turn !== null && !turn.closed && !turn.ended;
      if (scope === "turn" ? turnKey !== -1 : live) window.acc.title_assessable = this.assessable(window);
      this.agent.windows.push(window);
    }
    let exposure = this.agent.exposures.find((entry) => entry.mkey === core.mkey);
    if (!exposure) {
      if (this.agent.exposures.length >= MAX_EXPOSURES) return;
      exposure = {
        ...core,
        exposure_id: exposureIdFor(this.env.sessionKey, this.env.agentKey, this.agent.reset_id, core.mkey),
        reset_id: this.agent.reset_id,
        first_sid: row.sid,
        first_ts: row.ts,
        outcome: null,
        credits: [],
        idle_turns: 0,
        seen_this_turn: false,
        dismissed_turns: 0,
        seen_total: 0,
        acc: newAcc(!core.visible),
      };
      this.agent.exposures.push(exposure);
    } else {
      exposure.handles = unionBounded(exposure.handles, core.handles, 8);
      exposure.shingles = unionBounded(exposure.shingles, core.shingles);
      exposure.echo = unionBounded(exposure.echo, core.echo);
      exposure.injected_is_memory = exposure.injected_is_memory || core.injected_is_memory;
      if (!exposure.title) exposure.title = core.title;
      this.profiles.delete(exposure.exposure_id);
    }
    if (core.visible) {
      if (!exposure.visible) {
        exposure.visible = true;
        exposure.first_sid = row.sid;
        exposure.first_ts = row.ts;
        exposure.channel = channel;
        exposure.acc.first_text = false; // its first visible turn: check its first text
      }
      exposure.idle_turns = 0;
      exposure.seen_this_turn = true;
      // Re-naming never re-arms the dismissal check: an exposure checks the
      // first assistant text of each turn, named or not (symmetry).
    }
  }

  /** Whether the title rule can assess this memory on the current turn. */
  private assessable(core: MemoryCore & { sid?: string; exposure_id?: string }): boolean {
    const title = this.titleFor(core);
    return title !== null && title.length >= MIN_TITLE_TOKENS;
  }

  private dueSurfacing(pending: PendingSurfacing, entry: TxEntry): boolean {
    if (pending.bound) {
      if (!pending.armed) return false;
      return pending.row.channel !== "guard" || entry.ts === null || entry.ts > pending.ts;
    }
    const txOff = pending.row.tx_off;
    if (typeof txOff === "number" && Number.isFinite(txOff)) return entry.off >= txOff;
    return entry.ts !== null && entry.ts > pending.ts;
  }

  private resetEvent(reset: { row: ResetRow; ts: number }): DueEvent {
    return { ts: reset.ts, reset: true, sessionStart: reset.row.event === "SessionStart", boot: false, apply: () => this.applyReset(reset.row) };
  }

  private surfaceEvent(pending: PendingSurfacing): DueEvent {
    return { ts: pending.ts, reset: false, sessionStart: false, boot: BOOT_SCOPE.has(pending.row.channel), apply: () => this.activate(pending.row) };
  }

  /** Apply every reset and surfacing positioned before `entry` (see applyBatch). */
  private applyDue(entry: TxEntry): void {
    const due: DueEvent[] = [];
    for (const reset of this.resets) {
      if (!reset.done && entry.ts !== null && reset.ts < entry.ts) {
        due.push(this.resetEvent(reset));
        reset.done = true;
      }
    }
    for (const pending of this.pending) {
      if (!pending.done && this.dueSurfacing(pending, entry)) {
        due.push(this.surfaceEvent(pending));
        pending.done = true;
      }
    }
    applyBatch(due);
  }

  /** End of the stream: everything still pending happened after the last line read. */
  flushEvents(): void {
    const due: DueEvent[] = [];
    for (const reset of this.resets) {
      if (!reset.done) {
        due.push(this.resetEvent(reset));
        reset.done = true;
      }
    }
    for (const pending of this.pending) {
      if (pending.done) continue;
      pending.done = true;
      due.push(this.surfaceEvent(pending));
    }
    applyBatch(due);
  }

  /** A reset of this agent closes its open windows and its epoch. */
  private applyReset(row: ResetRow): void {
    for (const window of [...this.agent.windows]) this.closeWindow(window, "reset");
    this.closeEpoch();
    this.agent.reset_id = RESET_ID_RE.test(row.reset_id) ? row.reset_id : idHash([row.reset_id]);
    pushBounded(this.agent.applied_resets, row.reset_id, MAX_RESETS);
  }

  /** Resets with no transcript to place them in (the fast path, an idle close). */
  applyResetsNow(): void {
    for (const reset of this.resets) {
      if (reset.done) continue;
      reset.done = true;
      this.applyReset(reset.row);
    }
  }

  // -- windows and exposures ------------------------------------------------

  /**
   * Write a window's final judgment. A window closed by the transcript (its
   * turn's end was read) is judged on transcript evidence alone. One closed
   * any other way while its provisional judgment stands (a reset, the session
   * end, 24 h idle) never had its reply verified: the stronger of the two
   * judgments is kept, marked final_reason "unflushed_tail".
   */
  private closeWindow(window: WindowState, via: string): void {
    let judgment = judge(window.acc, window.visible, this.unreachedReason());
    let finalReason: string | undefined;
    if (window.provisional && via !== "transcript") {
      if (rank(window.provisional.outcome) >= rank(judgment.outcome)) {
        judgment = {
          outcome: window.provisional.outcome,
          evidence: window.provisional.evidence,
          title_assessable: window.provisional.title_assessable || judgment.title_assessable,
        };
      }
      finalReason = "unflushed_tail";
      if (via === "idle") via = "unflushed_tail";
    }
    this.writer.writeJudged(window.sid, judgment, true, via, finalReason);
    pushBounded(this.agent.done, window.sid, MAX_DONE);
    this.agent.windows = this.agent.windows.filter((entry) => entry !== window);
  }

  /** Fold each exposure's evidence since the last fold into the turn `ix`. */
  private foldExposures(ix: number | null, human: boolean, countIdle: boolean, usePending: boolean): void {
    for (const exposure of this.agent.exposures) {
      const acc = usePending && exposure.pending ? exposure.pending : exposure.acc;
      const creditable = exposure.visible && exposure.idle_turns < CREDIT_IDLE_TURNS;
      if (creditable) {
        exposure.outcome = strongest(exposure.outcome, evidenceOutcome(acc));
        exposure.seen_total += acc.seen;
        if (acc.dismissed) exposure.dismissed_turns += 1;
        // used_ii: applied, or a title/content citation. A handle citation is
        // never used_ii, and dismissal never gates a credit (symmetry).
        const usedII = acc.applied || acc.cite_title;
        if (usedII && ix !== null && !exposure.credits.includes(ix) && exposure.credits.length < MAX_CREDITS) {
          exposure.credits.push(ix);
        }
      }
      if (countIdle && human && !exposure.seen_this_turn) exposure.idle_turns += 1;
      exposure.acc = newAcc(true);
      delete exposure.pending;
    }
  }

  /** Close the epoch: one final exposure row per memory. */
  private closeEpoch(): void {
    const turn = this.agent.turn;
    this.foldExposures(turn ? turn.ix : null, false, false, turn?.ended !== undefined);
    for (const exposure of this.agent.exposures) this.writer.writeExposure(exposure);
    this.agent.exposures = [];
  }

  /** The current turn is over: close its windows and fold its exposure evidence. */
  private closeTurn(via: string): void {
    const turn = this.agent.turn;
    if (!turn) return;
    for (const window of [...this.agent.windows]) {
      const closes = window.scope === "turn" ? window.turn_key === turn.key : window.boot_turns >= 2;
      if (closes) this.closeWindow(window, via);
    }
    this.foldExposures(turn.ix, turn.human, !turn.closed, false);
    turn.closed = true;
    delete turn.ended;
  }

  private boundary(entry: TxEntry): void {
    this.closeTurn("transcript");
    const promptId = entry.promptId;
    const row = promptId ? this.index.turnsByPrompt.get(promptId) : undefined;
    const kind = typeof row?.envelope_kind === "string" ? row.envelope_kind : classifyPromptEnvelope(entry.text ?? "").kind;
    const ix = Number.isSafeInteger(row?.turn_ix) ? (row?.turn_ix as number) : -(entry.off + 1);
    const turn: TurnState = {
      key: entry.off,
      ix,
      start_ts: entry.ts,
      human: kind === "human",
      non_skip: kind !== "skip",
      text_hashes: [],
      closed: false,
    };
    if (promptId) turn.prompt_id = promptId;
    this.agent.turn = turn;
    this.msgTexts = new Map();
    if (turn.non_skip) {
      pushBounded(this.agent.prompts, useTokens(entry.text ?? "").slice(0, MAX_PROMPT_TOKENS).map(tokenHash), 3);
      pushBounded(this.agent.prompt_shingles, allShingleHashes(entry.text ?? "").slice(0, MAX_PROMPT_SHINGLES), 3);
    }
    this.promptHashes = new Set(this.agent.prompts.flat());
    this.promptShingles = new Set(this.agent.prompt_shingles.flat());
    for (const window of this.agent.windows) {
      if (window.scope === "turn" && window.turn_key === -1) {
        window.turn_key = turn.key;
        window.acc.title_assessable = this.assessable(window);
      } else if (window.scope === "boot") {
        if (turn.non_skip) window.boot_turns += 1;
        // A boot window spans turns: assessable when the rule could fire on any of them.
        window.acc.title_assessable = window.acc.title_assessable || this.assessable(window);
      }
    }
    for (const exposure of this.agent.exposures) {
      exposure.seen_this_turn = false;
      exposure.acc = newAcc(!exposure.visible); // dismissal: each turn's first text
    }
    for (const pending of this.pending) {
      if (pending.done || !pending.bound || pending.row.prompt_id !== promptId) continue;
      pending.armed = true;
      if (pending.row.channel !== "guard") {
        pending.done = true;
        this.activate(pending.row);
      }
    }
  }

  private observeAll(act: Act): void {
    for (const window of this.agent.windows) {
      if (window.scope === "turn" && window.turn_key === -1) continue;
      observe(window.acc, this.profile(window), act, this.titleFor(window), window.scope === "turn", this.promptShingles);
    }
    for (const exposure of this.agent.exposures) {
      if (!exposure.visible || exposure.idle_turns >= CREDIT_IDLE_TURNS) continue;
      observe(exposure.acc, this.profile(exposure), act, this.titleFor(exposure), false, this.promptShingles);
    }
  }

  private rememberText(text: string, msg: string | undefined): void {
    const turn = this.agent.turn;
    if (!turn) return;
    pushBounded(turn.text_hashes, replyHash(text), MAX_TEXT_HASHES);
    if (msg) {
      const parts = this.msgTexts.get(msg) ?? [];
      parts.push(text);
      this.msgTexts.set(msg, parts);
      if (parts.length > 1) pushBounded(turn.text_hashes, replyHash(parts.join("\n")), MAX_TEXT_HASHES);
    }
  }

  private explicitRecall(result: ToolResultBlock, kind: "recall" | "drill", entry: TxEntry): void {
    if (result.is_error) return;
    const cards = kind === "drill" ? drillCards(result.text) : recallCards(result.text);
    const seen = new Set<string>();
    const turn = this.agent.turn;
    const ts = entry.ts !== null ? new Date(entry.ts).toISOString() : this.env.nowIso;
    for (const card of cards) {
      const mkey = keyCard(this.env.vaultPath, card);
      if (!mkey) {
        this.unnameable += 1;
        const sid = idHash([result.tool_use_id, `unnameable:${card.rank}`]);
        this.writer.writeJudged(sid, { outcome: "unknown(unnameable)", evidence: [], title_assessable: false }, true, "explicit_recall");
        continue;
      }
      if (seen.has(mkey)) continue;
      seen.add(mkey);
      const row = buildSurfacedRow(
        this.writer.contextWith(turn?.prompt_id),
        {
          channel: "explicit_recall",
          mkey,
          handles: handlesForKey(mkey),
          title: card.title ?? card.wikilink ?? "",
          rank: card.rank,
          turn_ix: turn && turn.ix >= 0 ? turn.ix : undefined,
          visible: true,
          delivered: true,
          tool_use_id: result.tool_use_id,
        },
        ts,
      );
      this.writer.writeSurfaced(row);
      if (this.known.has(row.sid)) continue;
      this.known.add(row.sid);
      this.activate(row);
    }
  }

  private consume(entry: TxEntry): void {
    if (this.isMain && entry.sidechain) return;
    if (entry.kind === "prompt") {
      this.boundary(entry);
      return;
    }
    if (entry.kind === "assistant") {
      this.assistantSeen += 1;
      for (const block of entry.blocks ?? []) {
        if (block.kind === "text") {
          this.rememberText(block.text, block.msg);
          this.observeAll(textAct(block.text));
        } else {
          const act = toolAct(block.name, block.input);
          if (act.minni) {
            this.agent.tool_ids[block.id] = act.minni;
            const ids = Object.keys(this.agent.tool_ids);
            if (ids.length > MAX_TOOL_IDS) delete this.agent.tool_ids[ids[0] as string];
          }
          this.observeAll(act);
        }
      }
      return;
    }
    if (entry.kind === "result") {
      for (const result of entry.results ?? []) {
        const kind = this.agent.tool_ids[result.tool_use_id];
        if (!kind) continue;
        delete this.agent.tool_ids[result.tool_use_id];
        this.explicitRecall(result, kind, entry);
      }
    }
  }

  process(entries: readonly TxEntry[]): void {
    for (const entry of entries) {
      if (Date.now() > this.env.deadline) {
        this.timedOut = true;
        break;
      }
      this.applyDue(entry);
      this.consume(entry);
      this.processedEnd = entry.end;
    }
  }

  /** Prompt history (and the current turn) from lines before the cursor. */
  restoreHistory(entries: readonly TxEntry[]): void {
    const prompts: TxEntry[] = [];
    let lastPrompt: TxEntry | null = null;
    const texts: string[] = [];
    for (const entry of entries) {
      if (this.isMain && entry.sidechain) continue;
      if (entry.kind === "prompt") {
        lastPrompt = entry;
        texts.length = 0;
        const row = entry.promptId ? this.index.turnsByPrompt.get(entry.promptId) : undefined;
        const kind = typeof row?.envelope_kind === "string" ? row.envelope_kind : classifyPromptEnvelope(entry.text ?? "").kind;
        if (kind !== "skip") prompts.push(entry);
      } else if (entry.kind === "assistant") {
        for (const block of entry.blocks ?? []) if (block.kind === "text") texts.push(block.text);
      }
    }
    this.agent.prompts = prompts
      .slice(-3)
      .map((entry) => useTokens(entry.text ?? "").slice(0, MAX_PROMPT_TOKENS).map(tokenHash));
    this.agent.prompt_shingles = prompts
      .slice(-3)
      .map((entry) => allShingleHashes(entry.text ?? "").slice(0, MAX_PROMPT_SHINGLES));
    this.promptHashes = new Set(this.agent.prompts.flat());
    this.promptShingles = new Set(this.agent.prompt_shingles.flat());
    if (lastPrompt) {
      const row = lastPrompt.promptId ? this.index.turnsByPrompt.get(lastPrompt.promptId) : undefined;
      const kind = typeof row?.envelope_kind === "string" ? row.envelope_kind : classifyPromptEnvelope(lastPrompt.text ?? "").kind;
      const ix = Number.isSafeInteger(row?.turn_ix) ? (row?.turn_ix as number) : -(lastPrompt.off + 1);
      this.agent.turn = {
        key: lastPrompt.off,
        ix,
        start_ts: lastPrompt.ts,
        human: kind === "human",
        non_skip: kind !== "skip",
        text_hashes: texts.slice(-MAX_TEXT_HASHES).map(replyHash),
        closed: false,
      };
      if (lastPrompt.promptId) this.agent.turn.prompt_id = lastPrompt.promptId;
    }
    this.agent.history_stale = false;
  }

  /** Stop(N): the current turn ended. Final when its reply is in the transcript. */
  endTurnAtStop(): void {
    const turn = this.agent.turn;
    const lam = this.env.lam;
    const lamHash = lam ? replyHash(lam) : null;
    const flushed = lam === null || (turn !== null && lamHash !== null && turn.text_hashes.includes(lamHash));
    // Every surfacing in the ledger at Stop(N) came from a turn <= N: one not
    // yet placed in a turn belongs to the one ending now.
    if (turn) {
      for (const window of this.agent.windows) {
        if (window.scope === "turn" && window.turn_key === -1) {
          window.turn_key = turn.key;
          window.acc.title_assessable = this.assessable(window);
        }
      }
    }
    const closing = this.agent.windows.filter((window) =>
      window.scope === "turn" ? turn === null || window.turn_key === turn.key : window.boot_turns >= 2,
    );
    if (flushed) {
      if (turn) {
        this.closeTurn("transcript");
      } else {
        for (const window of closing) this.closeWindow(window, "transcript");
      }
      return;
    }
    const act = textAct(lam as string);
    for (const window of closing) {
      const acc = cloneAcc(window.acc);
      observe(acc, this.profile(window), act, this.titleFor(window), window.scope === "turn", this.promptShingles);
      const judgment = judge(acc, window.visible, this.unreachedReason());
      window.provisional = { ...judgment, ts: this.env.nowIso };
      if (this.writer.writeJudged(window.sid, judgment, false, "last_message")) this.provisional += 1;
    }
    for (const exposure of this.agent.exposures) {
      if (!exposure.visible || exposure.idle_turns >= CREDIT_IDLE_TURNS) continue;
      const acc = cloneAcc(exposure.acc);
      observe(acc, this.profile(exposure), act, this.titleFor(exposure), false, this.promptShingles);
      exposure.pending = acc;
    }
    if (turn) turn.ended = { lam_hash: lamHash, ts: this.env.nowIso };
  }

  /** Session end or 24 h idle: every window and the epoch close, final. */
  closeAll(via: string): void {
    for (const window of [...this.agent.windows]) this.closeWindow(window, via);
    this.closeEpoch();
    if (this.agent.turn) {
      delete this.agent.turn.ended;
      this.agent.turn.closed = true;
    }
  }
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

export interface RunDetectorArgs {
  vaultPath: string;
  /** The hook wire (`claude-code`, …); rows record it. */
  wire: string;
  /** Defaults to `wire`. */
  principal?: string;
  /** `sessionKeyFrom(payload)`. */
  session_key: string;
  /** `agentKeyFrom(payload)`: `main`, or `a-…` for a subagent. */
  agent_key: string;
  /** The transcript to read: `transcript_path`, or SubagentStop's `agent_transcript_path`. */
  transcriptPath?: string | null;
  /** The payload's `last_assistant_message`: evidence for the final reply. */
  lastAssistantMessage?: string | null;
  /** The run ends the current turn (Stop, SubagentStop). Default true. */
  endsTurn?: boolean;
  /** The agent's session is over (SubagentStop, SessionEnd): close every window and the epoch. */
  sessionEnd?: boolean;
  /** The payload's agent_type, recorded on rows. */
  subagent_type?: string;
  /** For tests: the run's clock (ms, ISO or Date). */
  now?: number | string | Date;
  /** For tests: the slow path's caps. */
  budgetMs?: number;
  maxReadBytes?: number;
}

export type DetectorStatus =
  | "ran"
  | "disabled"
  | "locked"
  | "unlabeled"
  | "invalid"
  | "refused"
  | "write_failed"
  | "error";

export interface DetectorResult {
  status: DetectorStatus;
  /** `fast` (byte scan only), `slow` (lines parsed), `none` (no transcript). */
  path: "fast" | "slow" | "none";
  transcript_lines_parsed: number;
  judged_written: number;
  exposures_written: number;
  surfaced_written: number;
  /** Explicit-recall cards that could not be keyed. */
  unnameable: number;
  /** Windows judged provisionally (reply not yet in the transcript). */
  provisional: number;
  /** The caps stopped the read; the rest carries forward. */
  carried: boolean;
  /** Whether a DF snapshot was available for the title rule. */
  title_rule: "on" | "off";
}

function emptyResult(status: DetectorStatus): DetectorResult {
  return {
    status,
    path: "none",
    transcript_lines_parsed: 0,
    judged_written: 0,
    exposures_written: 0,
    surfaced_written: 0,
    unnameable: 0,
    provisional: 0,
    carried: false,
    title_rule: "off",
  };
}

function boundedString(value: unknown, max: number): string | undefined {
  return typeof value === "string" && value.length > 0 ? value.slice(0, max) : undefined;
}

interface CursorFile {
  offset: number;
  dev: number;
  ino: number;
}

function readCursor(vaultPath: string, key: string): CursorFile | null {
  try {
    const dir = cursorDir(vaultPath, false);
    if (!dir) return null;
    const raw = readJsonFile(vaultPath, path.join(dir, `${key}.json`));
    if (!isRecord(raw) || raw.v !== 1 || !Number.isSafeInteger(raw.offset)) return null;
    return { offset: raw.offset as number, dev: int(raw.dev, -1), ino: int(raw.ino, -1) };
  } catch {
    return null;
  }
}

function writeCursor(vaultPath: string, handle: TranscriptHandle, offset: number, nowIso: string): void {
  const dir = cursorDir(vaultPath, true);
  if (!dir) return;
  writeJsonAtomic(vaultPath, path.join(dir, `${handle.key}.json`), {
    v: 1,
    offset,
    size: handle.size,
    dev: handle.dev,
    ino: handle.ino,
    ts: nowIso,
  });
}

/** Where this run starts reading `handle` for `agent`. */
function startOffset(vaultPath: string, agent: AgentState, handle: TranscriptHandle, run: DetectorRun): number {
  if (agent.tx === handle.key && agent.offset <= handle.size) return agent.offset;
  const saved = readCursor(vaultPath, handle.key);
  agent.tx = handle.key;
  agent.turn = null;
  if (saved && saved.offset <= handle.size && saved.dev === handle.dev && saved.ino === handle.ino) {
    agent.offset = saved.offset;
    agent.history_stale = saved.offset > 0;
    return saved.offset;
  }
  // A new cursor: 64 KB before the earliest open surfacing, else 64 KB before
  // the end; never a full read of an old transcript. A surfacing placed
  // before that point and bound to an earlier turn is judged missed.
  const txOff = run.earliestTxOff();
  let start = handle.size - NEW_CURSOR_LOOKBACK;
  if (txOff !== null) start = Math.min(start, txOff - NEW_CURSOR_LOOKBACK);
  start = alignToLineStart(handle, Math.max(0, start));
  agent.offset = start;
  agent.history_stale = start > 0;
  return start;
}

function parseLines(lines: readonly RawLine[]): TxEntry[] {
  const entries: TxEntry[] = [];
  for (const line of lines) {
    const entry = parseEntry(line);
    if (entry) entries.push(entry);
  }
  return entries;
}

function ledgerSignature(vaultPath: string, sessionKey: string): string {
  try {
    const dir = containedDir(vaultPath, [...LEDGER_RELDIR.split(path.sep), sessionKey], false);
    if (!dir) return "none";
    const parts: string[] = [];
    for (const name of fs.readdirSync(dir).sort()) {
      if (!/^\d{4}-\d{2}-\d{2}\.jsonl$/.test(name)) continue;
      const stat = fs.statSync(path.join(dir, name));
      parts.push(`${name}:${stat.size}:${stat.mtimeMs}`);
    }
    return sha(parts.join("|"), 16);
  } catch {
    return "error";
  }
}

interface SessionRun {
  vaultPath: string;
  sessionKey: string;
  nowMs: number;
}

interface SessionBody {
  /** Rows to append, or null for "nothing to do" (no write, state untouched). */
  rows: LedgerRow[] | null;
  /** Runs after the rows landed, before the state is written. */
  afterAppend?: () => void;
}

/**
 * Shared frame of runDetector and the idle close: take the session lock,
 * load the state and the ledger, run `body`, append its rows, and only then
 * write the state. If any row fails to land the old state is kept: the next
 * run redoes the work, and the ledger's sids make what did land idempotent.
 */
function withSession(
  input: SessionRun,
  result: { status: string },
  body: (state: DetectorState, ledgerRows: readonly LedgerRow[]) => SessionBody,
): boolean {
  const dir = sessionDir(input.vaultPath, input.sessionKey, true);
  if (!dir) {
    result.status = "refused";
    return false;
  }
  const lock = acquireLock(input.vaultPath, dir);
  if (!lock) {
    result.status = "locked";
    return false;
  }
  try {
    const nowIso = new Date(input.nowMs).toISOString();
    const state = loadState(input.vaultPath, dir, input.sessionKey, nowIso);
    const ledger = readSessionLedger(input.vaultPath, input.sessionKey);
    if (ledger.refused) {
      result.status = "refused";
      return false;
    }
    const { rows, afterAppend } = body(state, ledger.rows);
    if (rows === null) return false;
    if (rows.length > 0) {
      const appended = appendLedgerRows(input.vaultPath, rows);
      if (appended.disabled || appended.failed > 0) {
        result.status = "write_failed";
        return false;
      }
    }
    afterAppend?.();
    writeJsonAtomic(input.vaultPath, path.join(dir, STATE_FILE), state);
    return true;
  } finally {
    lock.release();
  }
}

/**
 * Judge this agent's surfacings against its transcript (§4.4). Call it from
 * Stop (main thread: `transcript_path`, `last_assistant_message`) and from
 * SubagentStop (the subagent's `agent_transcript_path`, its `a-…` agent key,
 * and `sessionEnd: true`). Synchronous, bounded by 1.5 s and 2 MB of
 * transcript, gated by MINNI_USE_LEDGER, and never throws: the result's
 * status says what happened.
 */
export function runDetector(args: RunDetectorArgs): DetectorResult {
  const result = emptyResult("invalid");
  try {
    if (!isRecord(args)) return result;
    const vaultPath = args.vaultPath;
    const sessionKey = args.session_key;
    const agentKey = args.agent_key;
    if (typeof vaultPath !== "string" || vaultPath.length === 0 || vaultPath.includes("\0")) return result;
    if (typeof sessionKey !== "string" || !SESSION_KEY_RE.test(sessionKey)) return result;
    if (typeof agentKey !== "string" || !AGENT_KEY_RE.test(agentKey)) return result;
    if (isUnlabeledKey(sessionKey)) {
      result.status = "unlabeled";
      return result;
    }
    // A cased spelling of a real key would alias its directory (see isCanonicalSessionKey).
    if (sessionKey !== sessionKey.toLowerCase()) return result;
    if (!fs.existsSync(vaultPath)) return result;
    if (!flagOn(vaultPath, "MINNI_USE_LEDGER")) {
      result.status = "disabled";
      return result;
    }
    const nowMs = toMs(args.now);
    const nowIso = new Date(nowMs).toISOString();
    const budget =
      typeof args.budgetMs === "number" && args.budgetMs > 0 ? Math.min(args.budgetMs, RUN_BUDGET_MS) : RUN_BUDGET_MS;
    const deadline = Date.now() + budget;
    const maxRead =
      typeof args.maxReadBytes === "number" && args.maxReadBytes > 0
        ? Math.min(Math.floor(args.maxReadBytes), MAX_READ_BYTES)
        : MAX_READ_BYTES;
    const wire = boundedString(args.wire, 64) ?? "unknown";
    const principal = boundedString(args.principal, 64) ?? wire;
    const subagentType = boundedString(args.subagent_type, 128);
    const lam =
      typeof args.lastAssistantMessage === "string" && args.lastAssistantMessage.trim().length > 0
        ? args.lastAssistantMessage.slice(0, MAX_INPUT_CHARS)
        : null;
    result.status = "ran";
    const cursor: { handle: TranscriptHandle | null; offset: number } = { handle: null, offset: 0 };

    const committed = withSession({ vaultPath, sessionKey, nowMs }, result, (state, ledgerRows) => {
      const agent = state.agents[agentKey] ?? newAgent(wire, principal, subagentType, nowIso);
      agent.wire = wire;
      agent.principal = principal;
      if (subagentType) agent.subagent_type = subagentType;
      const index = indexLedger(ledgerRows, agentKey);
      const df = readDfSnapshot(vaultPath);
      result.title_rule = df ? "on" : "off";
      const context: LedgerContext = { session_key: sessionKey, agent_key: agentKey, wire, principal, features: featuresInEffect(vaultPath) };
      if (agent.subagent_type) context.subagent_type = agent.subagent_type;
      const writer = new RowWriter(index, context, nowIso);
      const run = new DetectorRun({ vaultPath, sessionKey, agentKey, nowMs, nowIso, lam, deadline }, agent, writer, df, index);
      run.collectEvents();

      let reachedEnd = true;
      const handle = openTranscript(args.transcriptPath);
      try {
        if (handle) {
          const offset = startOffset(vaultPath, agent, handle, run);
          const quiet =
            agent.windows.length === 0 &&
            !agent.exposures.some((exposure) => exposure.visible && exposure.idle_turns < CREDIT_IDLE_TURNS) &&
            !run.hasNewEvents &&
            agent.turn?.ended === undefined;
          const scan = quiet ? fastScan(handle, offset, deadline) : null;
          if (scan && !scan.minni) {
            // Fast path: nothing to judge and no recall to rebuild. Bytes only.
            result.path = "fast";
            run.applyResetsNow();
            if (scan.end > offset) {
              agent.history_stale = true;
              agent.turn = null;
            }
            agent.offset = scan.end;
            reachedEnd = scan.complete;
          } else {
            result.path = "slow";
            if (agent.history_stale && offset > 0) {
              const from = alignToLineStart(handle, Math.max(0, offset - NEW_CURSOR_LOOKBACK));
              const back = readLines(handle, from, Math.max(0, offset - from), deadline);
              run.linesParsed += back.lines.length;
              run.restoreHistory(parseLines(back.lines));
            }
            const chunk = readLines(handle, offset, maxRead, deadline);
            const entries = parseLines(chunk.lines);
            run.linesParsed += chunk.lines.length;
            run.bindTo(entries);
            run.process(entries);
            agent.offset = run.timedOut ? (run.processedEnd ?? offset) : chunk.end;
            reachedEnd = !run.timedOut && !chunk.more;
          }
          cursor.handle = handle;
          cursor.offset = agent.offset;
        }
      } finally {
        if (handle) {
          try {
            fs.closeSync(handle.fd);
          } catch {
            // A read-only descriptor: nothing to lose.
          }
        }
      }
      result.transcript_lines_parsed = run.linesParsed;
      if (reachedEnd) run.flushEvents();
      else result.carried = true;
      if (args.endsTurn !== false && reachedEnd) run.endTurnAtStop();
      if (args.sessionEnd === true) run.closeAll("session_end");
      agent.last_activity = nowIso;
      state.agents[agentKey] = agent;
      result.unnameable = run.unnameable;
      result.provisional = run.provisional;
      result.judged_written = writer.judged;
      result.exposures_written = writer.exposures;
      result.surfaced_written = writer.surfaced;
      return { rows: writer.rows };
    });
    if (committed && cursor.handle) {
      try {
        // The cursor follows the state: written only once the rows and the state landed.
        writeCursor(vaultPath, cursor.handle, cursor.offset, nowIso);
      } catch {
        // The state holds the offset too; a lost cursor file costs a re-read.
      }
    }
    if (!committed && result.status === "ran") result.status = "error";
    if (!committed) {
      result.judged_written = 0;
      result.exposures_written = 0;
      result.surfaced_written = 0;
    }
    return result;
  } catch {
    result.status = "error";
    return result;
  }
}

export interface IdleCloseResult {
  status: "ran" | "disabled" | "error";
  /** Sessions closed by this call. */
  sessions: number;
  judged_written: number;
  exposures_written: number;
  /**
   * Candidate sessions left unexamined when the call stopped early (the time
   * budget ran out, or `limit` sessions were closed): retry them. Present only
   * when `sessionKeys` or `budgetMs` was passed. A session that was examined
   * but `locked` (another run held it) or `refused` (containment) is counted
   * neither in `sessions` nor in `remaining`; the next sweep sees it again.
   */
  remaining?: number;
}

export interface IdleCloseOptions {
  now?: number | string | Date;
  /** At most this many sessions are closed per call (default 50). */
  limit?: number;
  /**
   * Examine ONLY these sessions, in this order: no walk of the session
   * directories and no read of any other session's ledger. Each key must pass
   * the walk's own rule for a session directory name, in its canonical
   * lowercase spelling; others are skipped. A value that is not an array
   * yields no candidates (never the full walk).
   */
  sessionKeys?: readonly string[];
  /**
   * Wall-clock budget, checked between sessions and never inside one (the
   * first candidate is always examined); what is left is in `remaining`.
   */
  budgetMs?: number;
}

/**
 * Any spelling of `unlabeled`. On a case-insensitive filesystem (APFS by
 * default) "Unlabeled" names the same directory, so the exclusion must not
 * depend on case.
 */
export function isUnlabeledKey(key: string): boolean {
  return key.toLowerCase() === "unlabeled";
}

/** The walk's rule for a session directory name. */
function isSessionKeyName(name: unknown): name is string {
  return typeof name === "string" && SESSION_KEY_RE.test(name) && !isUnlabeledKey(name);
}

/**
 * A caller-supplied session key: a valid name in its canonical spelling.
 * Every key the ledger writes is lowercase (hex digests, `tx-…`), and on a
 * case-insensitive filesystem any other spelling aliases a real session's
 * directory: its rows would be finalised under the alias.
 */
export function isCanonicalSessionKey(key: unknown): key is string {
  return isSessionKeyName(key) && key === key.toLowerCase();
}

function listSessionKeys(vaultPath: string): string[] {
  const keys = new Set<string>();
  for (const segments of [LEDGER_RELDIR.split(path.sep), [...DETECTOR_RELDIR.split(path.sep), SESSIONS_SEGMENT]]) {
    try {
      const dir = containedDir(vaultPath, segments, false);
      if (!dir) continue;
      for (const name of fs.readdirSync(dir)) {
        if (isSessionKeyName(name)) keys.add(name);
      }
    } catch {
      // A refused or unreadable root contributes no sessions.
    }
  }
  return [...keys].sort();
}

/**
 * Whether a session has been idle for more than 24 h (by its rows and the
 * detector's own activity) and still has something to close: an open window
 * or epoch, or a surfacing with no final judgment.
 */
function idleWork(state: DetectorState, ledgerRows: readonly LedgerRow[], nowMs: number): { close: boolean; agentKeys: string[] } {
  let lastActivity = 0;
  let work = false;
  const agentKeys = new Set(Object.keys(state.agents));
  for (const agent of Object.values(state.agents)) {
    lastActivity = Math.max(lastActivity, tsMs(agent.last_activity) ?? 0);
    if (agent.windows.length > 0 || agent.exposures.length > 0 || agent.turn?.ended !== undefined) work = true;
  }
  const finalSids = new Set<string>();
  for (const row of ledgerRows) {
    if (row.kind === "judged" && row.final === true) finalSids.add(row.sid);
  }
  for (const row of ledgerRows) {
    lastActivity = Math.max(lastActivity, tsMs(row.ts) ?? 0);
    if (row.kind !== "surfaced" || typeof row.agent_key !== "string" || !AGENT_KEY_RE.test(row.agent_key)) continue;
    agentKeys.add(row.agent_key);
    if (!finalSids.has(row.sid) && !(state.agents[row.agent_key]?.done.includes(row.sid) ?? false)) work = true;
  }
  return { close: work && nowMs - lastActivity > IDLE_CLOSE_MS, agentKeys: [...agentKeys].sort() };
}

/** Lock-free pre-check: closed before, and the ledger has not changed since. */
function alreadyClosed(vaultPath: string, sessionKey: string, signature: string): boolean {
  try {
    const dir = sessionDir(vaultPath, sessionKey, false);
    if (!dir) return false;
    const raw = readJsonFile(vaultPath, path.join(dir, STATE_FILE));
    return isRecord(raw) && raw.idle_closed === signature;
  } catch {
    return false;
  }
}

/**
 * Maintenance (§4.4): close every agent of every session idle for more than
 * 24 h. Provisional windows are finalized with final_reason "unflushed_tail";
 * other open windows by their evidence so far; surfacings no run reached as
 * unknown(unreached); and each open epoch writes its exposure rows. Run from
 * `use-report`. At most `limit` sessions are closed per call. Never throws.
 */
export function finalizeIdleSessions(vaultPath: string, options: IdleCloseOptions = {}): IdleCloseResult {
  const out: IdleCloseResult = { status: "ran", sessions: 0, judged_written: 0, exposures_written: 0 };
  try {
    if (typeof vaultPath !== "string" || vaultPath.length === 0 || vaultPath.includes("\0") || !fs.existsSync(vaultPath)) {
      out.status = "error";
      return out;
    }
    if (!flagOn(vaultPath, "MINNI_USE_LEDGER")) {
      out.status = "disabled";
      return out;
    }
    const nowMs = toMs(options.now);
    const nowIso = new Date(nowMs).toISOString();
    const limit = Number.isSafeInteger(options.limit) && (options.limit as number) > 0 ? (options.limit as number) : 50;
    const named = options.sessionKeys !== undefined;
    const budgeted = typeof options.budgetMs === "number" && Number.isFinite(options.budgetMs) && options.budgetMs > 0;
    const deadline = budgeted ? Date.now() + (options.budgetMs as number) : null;
    const candidates = !named
      ? listSessionKeys(vaultPath)
      : Array.isArray(options.sessionKeys)
        ? [...new Set((options.sessionKeys as readonly unknown[]).filter(isCanonicalSessionKey))]
        : [];
    if (named || options.budgetMs !== undefined) out.remaining = 0;
    for (let index = 0; index < candidates.length; index += 1) {
      const sessionKey = candidates[index] as string;
      if (out.sessions >= limit || (deadline !== null && index > 0 && Date.now() >= deadline)) {
        if (out.remaining !== undefined) out.remaining = candidates.length - index;
        break;
      }
      if (alreadyClosed(vaultPath, sessionKey, ledgerSignature(vaultPath, sessionKey))) continue;
      // Lock-free look first, so a busy or finished session costs no lock and no directory.
      const existing = sessionDir(vaultPath, sessionKey, false);
      const peekState: DetectorState = existing
        ? loadState(vaultPath, existing, sessionKey, nowIso)
        : { v: STATE_VERSION, session_key: sessionKey, agents: {} };
      const peekLedger = readSessionLedger(vaultPath, sessionKey);
      if (peekLedger.refused || !idleWork(peekState, peekLedger.rows, nowMs).close) continue;
      const status = { status: "ran" };
      const closed = withSession({ vaultPath, sessionKey, nowMs }, status, (state, ledgerRows) => {
        const { close, agentKeys } = idleWork(state, ledgerRows, nowMs);
        if (!close) return { rows: null };
        const rows: LedgerRow[] = [];
        let judged = 0;
        let exposures = 0;
        for (const agentKey of agentKeys) {
          const sample = ledgerRows.find((row) => row.agent_key === agentKey);
          const agent =
            state.agents[agentKey] ??
            newAgent(sample?.wire ?? "unknown", sample?.principal ?? "unknown", sample?.subagent_type, nowIso);
          const index = indexLedger([...ledgerRows, ...rows], agentKey);
          const context: LedgerContext = {
            session_key: sessionKey,
            agent_key: agentKey,
            wire: agent.wire,
            principal: agent.principal,
            features: featuresInEffect(vaultPath),
          };
          if (agent.subagent_type) context.subagent_type = agent.subagent_type;
          const writer = new RowWriter(index, context, nowIso);
          const run = new DetectorRun(
            { vaultPath, sessionKey, agentKey, nowMs, nowIso, lam: null, deadline: Date.now() + RUN_BUDGET_MS },
            agent,
            writer,
            readDfSnapshot(vaultPath),
            index,
          );
          run.collectEvents();
          run.flushEvents();
          run.closeAll("idle");
          state.agents[agentKey] = agent;
          rows.push(...writer.rows);
          judged += writer.judged;
          exposures += writer.exposures;
        }
        out.judged_written += judged;
        out.exposures_written += exposures;
        return {
          rows,
          afterAppend: () => {
            state.idle_closed = ledgerSignature(vaultPath, sessionKey);
          },
        };
      });
      if (closed) out.sessions += 1;
    }
    return out;
  } catch {
    out.status = "error";
    return out;
  }
}
