// Shared fixture for the recall-to-use Stop wiring tests (S1b): one prompt turn
// in a spawned hook's vault, with the ledger rows UPS would have written (a
// `turn` row and a delivered `prompt_pointer` surfacing) and the host
// transcript of that turn, so a real Stop has one window to judge. Not a test
// file itself (no `.test.mjs`).
//
// Rows are built with the landed use-ledger builders through the S1b detector
// fixtures; every id, title and path is synthetic, and every path lives in the
// hook fixture's temporary root.
import { createHash } from "node:crypto";
import { lstat, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { Clock, SESSION_ID, Transcript, append, surfacedRow, turnRow } from "./fixtures/use-detector-fixtures.mjs";

export { SESSION_ID };

export const NOTE_REL = "wiki/concepts/pelican-ledger-horizon.md";
export const NOTE_TITLE = "Pelican ledger horizon";
export const RECALL_NOTE_REL = "wiki/concepts/heron-cursor-drift.md";
export const FINAL_REPLY = "The pelican ledger horizon is ninety days, so nothing older is kept.";

/**
 * Seed one judgeable turn into `fixture.vault`. Returns the Stop payload, the
 * surfacing row, and the transcript path. With `explicitRecall`, the turn also
 * calls minni_recall, whose card the detector rebuilds as an `explicit_recall`
 * surfaced row.
 */
export async function seedStopTurn(fixture, { explicitRecall = false, at = Date.now() - 60_000 } = {}) {
  const vault = fixture.vault;
  await mkdir(path.join(vault, path.dirname(NOTE_REL)), { recursive: true });
  await writeFile(path.join(vault, NOTE_REL), `# ${NOTE_TITLE}\n\nLedger directories go after ninety days.\n`, "utf8");
  await writeFile(path.join(vault, RECALL_NOTE_REL), "# Heron cursor drift\n\nCursors start 64 KB back.\n", "utf8");
  const transcriptPath = path.join(fixture.root, "projects", "stop-wiring", `${SESSION_ID}.jsonl`);
  await mkdir(path.dirname(transcriptPath), { recursive: true });
  await writeFile(transcriptPath, "", "utf8");

  const clock = new Clock(at);
  const tx = new Transcript(transcriptPath, clock);
  tx.prompt("How long do we keep old session files around?", "p1");
  const surfaced = surfacedRow({ mkey: `vault:${NOTE_REL}`, title: NOTE_TITLE, promptId: "p1", ts: clock.next(10), turnIx: 1 });
  append(vault, [turnRow({ turnIx: 1, promptId: "p1", ts: surfaced.ts }), surfaced]);
  if (explicitRecall) {
    const id = tx.tool("mcp__minni__minni_recall", { query: "cursor drift" }, "toolu_stop_recall_1");
    tx.result(
      id,
      [
        "# Minni Recall",
        "",
        "Query: cursor drift [faiss-disk]",
        "",
        "## AI Context Pack",
        "",
        "1. [[wiki/concepts/heron-cursor-drift]] — Heron cursor drift (score=0.91)",
        `   <EVIDENCE source="${RECALL_NOTE_REL}" agent="claude-code" status="accepted" privacy="safe" score="0.910">cursor snippet</EVIDENCE>`,
      ].join("\n"),
    );
  }
  tx.say(FINAL_REPLY);
  await tx.flush();
  return {
    surfaced,
    transcriptPath,
    payload: {
      session_id: SESSION_ID,
      hook_event_name: "Stop",
      transcript_path: transcriptPath,
      last_assistant_message: FINAL_REPLY,
    },
  };
}

/** Every file under `dir` (recursive, links not followed): rel path -> {size, sha256, mtimeMs, link}. */
export async function snapshotTree(dir) {
  const out = new Map();
  async function walk(current, rel) {
    let names;
    try {
      names = await readdir(current);
    } catch {
      return;
    }
    for (const name of names.sort()) {
      const full = path.join(current, name);
      const relPath = rel ? `${rel}/${name}` : name;
      const stat = await lstat(full);
      if (stat.isDirectory()) {
        await walk(full, relPath);
      } else if (stat.isSymbolicLink()) {
        out.set(relPath, { link: true, size: stat.size, mtimeMs: stat.mtimeMs, ino: stat.ino, sha256: null });
      } else {
        const bytes = await readFile(full);
        out.set(relPath, {
          link: false,
          size: stat.size,
          mtimeMs: stat.mtimeMs,
          ino: stat.ino,
          sha256: createHash("sha256").update(bytes).digest("hex"),
          bytes,
        });
      }
    }
  }
  await walk(dir, "");
  return out;
}

/** Paths that are new, gone, or changed (size, hash or mtime) between two snapshots. */
export function changedPaths(before, after) {
  const changed = [];
  for (const [rel, entry] of after) {
    const old = before.get(rel);
    if (!old || old.size !== entry.size || old.sha256 !== entry.sha256 || old.mtimeMs !== entry.mtimeMs) changed.push(rel);
  }
  for (const rel of before.keys()) if (!after.has(rel)) changed.push(rel);
  return changed.sort();
}

/**
 * Append one more judgeable turn (turn row, pointer surfacing, transcript
 * lines) to a session seedStopTurn started. Returns the Stop payload and the
 * new surfacing row; `after` is any extra transcript text appended after the
 * reply (tool chatter), flushed with the turn.
 */
export async function seedNextTurn(fixture, transcriptPath, { turnIx, at, title, after = "" }) {
  const clock = new Clock(at);
  const tx = new Transcript(transcriptPath, clock);
  const promptId = `p${turnIx}`;
  tx.prompt(`What else changed in step ${turnIx}?`, promptId);
  const surfaced = surfacedRow({ mkey: `vault:wiki/concepts/turn-${turnIx}.md`, title, promptId, ts: clock.next(10), turnIx });
  append(fixture.vault, [turnRow({ turnIx, promptId, ts: surfaced.ts }), surfaced]);
  const reply = `Step ${turnIx} is done.`;
  tx.say(reply);
  await tx.flush();
  if (after) {
    const { appendFile } = await import("node:fs/promises");
    await appendFile(transcriptPath, after, "utf8");
  }
  return {
    surfaced,
    payload: { session_id: SESSION_ID, hook_event_name: "Stop", transcript_path: transcriptPath, last_assistant_message: reply },
  };
}

/** The detector's cursor offset for a transcript, or null (one cursor file per transcript). */
export async function detectorCursorOffset(vault) {
  const dir = path.join(vault, ".runtime", "use-detector", "cursors");
  let names = [];
  try {
    names = (await readdir(dir)).filter((name) => name.endsWith(".json"));
  } catch {
    return null;
  }
  if (names.length !== 1) return null;
  return JSON.parse(await readFile(path.join(dir, names[0]), "utf8")).offset;
}
