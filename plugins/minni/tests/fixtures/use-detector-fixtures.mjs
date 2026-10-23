// Synthetic fixtures for the recall-to-use S1b tests (design rev5 §4.6).
//
// Transcripts are written in the Claude Code JSONL shape the detector reads:
// `type: user/assistant` entries, `tool_use` and `tool_result` content blocks,
// `promptId` on every entry of a turn, and an ISO `timestamp`. Ledger rows are
// built with the landed use-ledger builders, never by hand. Nothing here is
// real session content: every id, title and path is made up, and every path
// lives in a per-test temporary directory.
import assert from "node:assert/strict";
import { appendFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  appendLedgerRows,
  buildResetRow,
  buildSurfacedRow,
  buildTurnRow,
  readSessionLedger,
  sessionKeyFrom,
} from "../../dist/use-ledger.js";

export const WIRE = "claude-code";
export const SESSION_ID = "synthetic-s1b-session";
export const FIXTURE_SESSION = sessionKeyFrom({ session_id: SESSION_ID });
/** Fixture time: rows and transcript entries share one synthetic clock. */
export const T0 = Date.parse("2026-10-23T10:00:00.000Z");

export async function makeVault(t, prefix = "minni-s1b-") {
  const root = await mkdtemp(path.join(tmpdir(), prefix));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "sessions"), { recursive: true });
  const transcriptPath = path.join(root, "projects", "synthetic-project", `${SESSION_ID}.jsonl`);
  await mkdir(path.dirname(transcriptPath), { recursive: true });
  await writeFile(transcriptPath, "", "utf8");
  return { root, vault, transcriptPath };
}

export async function writeNote(vault, rel, body) {
  const file = path.join(vault, rel);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, body, "utf8");
  return file;
}

/** Every title token distinctive: 100 notes, no token frequent. */
export async function writeDfSnapshot(vault, df = {}, notes = 100) {
  await mkdir(path.join(vault, ".runtime"), { recursive: true });
  await writeFile(
    path.join(vault, ".runtime", "df-snapshot.json"),
    JSON.stringify({ v: 1, ts: new Date(T0).toISOString(), notes, df }),
    "utf8",
  );
}

export function ctx(overrides = {}) {
  return {
    session_key: FIXTURE_SESSION,
    agent_key: "main",
    wire: WIRE,
    principal: WIRE,
    features: ["MINNI_USE_LEDGER"],
    ...overrides,
  };
}

export class Clock {
  constructor(start = T0) {
    this.ms = start;
  }
  /** Advance and return the new instant as ISO. */
  next(step = 1000) {
    this.ms += step;
    return new Date(this.ms).toISOString();
  }
  get now() {
    return this.ms;
  }
}

/** A transcript written turn by turn, flushed explicitly (as the host flushes). */
export class Transcript {
  constructor(file, clock) {
    this.file = file;
    this.clock = clock;
    this.pending = [];
    this.n = 0;
    this.promptId = undefined;
  }

  entry(fields) {
    this.n += 1;
    const line = {
      ...fields,
      uuid: `uuid-${this.n}`,
      sessionId: SESSION_ID,
      timestamp: this.clock.next(),
    };
    if (this.promptId !== undefined) line.promptId = this.promptId;
    this.pending.push(JSON.stringify(line));
    return line.timestamp;
  }

  /** A human prompt; starts a turn. Returns its timestamp. */
  prompt(text, promptId) {
    this.promptId = promptId;
    return this.entry({ type: "user", message: { role: "user", content: text } });
  }

  say(text) {
    return this.entry({
      type: "assistant",
      message: { id: `msg-${this.n + 1}`, role: "assistant", content: [{ type: "text", text }] },
    });
  }

  tool(name, input, id = `toolu_${this.n + 1}`) {
    this.entry({
      type: "assistant",
      message: { id: `msg-${this.n + 1}`, role: "assistant", content: [{ type: "tool_use", id, name, input }] },
    });
    return id;
  }

  result(toolUseId, content) {
    return this.entry({
      type: "user",
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: toolUseId, content }] },
    });
  }

  async flush() {
    if (this.pending.length === 0) return;
    await appendFile(this.file, this.pending.map((line) => `${line}\n`).join(""), "utf8");
    this.pending = [];
  }
}

/** A `surfaced` row through the real builder. */
export function surfacedRow({
  mkey,
  title,
  ts,
  promptId,
  channel = "prompt_pointer",
  rank = 0,
  visible = true,
  delivered = true,
  handles,
  shingles,
  echo,
  turnIx,
  agentKey = "main",
}) {
  const context = ctx({ agent_key: agentKey, ...(promptId ? { prompt_id: promptId } : {}) });
  return buildSurfacedRow(
    context,
    { channel, mkey, title, rank, visible, delivered, handles, shingles, echo, turn_ix: turnIx },
    ts,
  );
}

export function turnRow({ turnIx, promptId, ts, kind = "human", agentKey = "main" }) {
  return buildTurnRow(
    ctx({ agent_key: agentKey, prompt_id: promptId }),
    { turn_ix: turnIx, envelope_kind: kind, subkind: null, exit: "strong" },
    ts,
  );
}

export function resetRow({ ts, event = "PreCompact", agentKey = "main" }) {
  return buildResetRow(ctx({ agent_key: agentKey }), { event }, ts);
}

export function append(vault, rows) {
  const result = appendLedgerRows(vault, rows);
  assert.equal(result.written, rows.length, "fixture rows land");
}

export function ledgerRows(vault, kind) {
  const { rows } = readSessionLedger(vault, FIXTURE_SESSION);
  return kind ? rows.filter((row) => row.kind === kind) : rows;
}

export function judgedFor(vault, sid) {
  return ledgerRows(vault, "judged").filter((row) => row.sid === sid);
}

/** The final judgment of one surfacing; fails unless exactly one exists. */
export function finalJudged(vault, sid) {
  const finals = judgedFor(vault, sid).filter((row) => row.final === true);
  assert.equal(finals.length, 1, `exactly one final judgment for ${sid}`);
  return finals[0];
}

export function detectorArgs(vault, transcriptPath, clock, extra = {}) {
  return {
    vaultPath: vault,
    wire: WIRE,
    principal: WIRE,
    session_key: FIXTURE_SESSION,
    agent_key: "main",
    transcriptPath,
    now: clock.next(),
    ...extra,
  };
}
