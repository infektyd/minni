// Opt-in queue stall diagnostics for waitForJournal (measure-before-fix).
// Active only when MINNI_QUEUE_DIAG=1. Observational only: no drains started,
// no timers changed, no behavior changed. Logged fields are restricted to
// error names/codes, allowlisted slice ids/actions, counts, and pids — never
// messages, tokens, digests, keys, or file contents.
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

import {
  describeWorkerWriteDrains,
  queueStallDiagnosticsEnabled,
  readSwallowedDrainErrors,
} from "../dist/thread-worker.js";

const SLICE_PATTERN = /^s\d+$/;
const ACTION_ALLOWLIST = new Set(["start", "complete", "progress", "propose_structure"]);

function safeSlice(sliceId) {
  return typeof sliceId === "string" && SLICE_PATTERN.test(sliceId) ? sliceId : "other";
}

function safeAction(action) {
  const name = action?.action;
  return typeof name === "string" && ACTION_ALLOWLIST.has(name) ? name : "other";
}

async function lockAndReservation(vaultPath) {
  let lock = "none";
  let reserv = "none";
  try {
    const root = path.join(vaultPath, ".runtime", "thread-locks");
    for (const name of await readdir(root)) {
      if (name.endsWith(".lock")) {
        try {
          const raw = JSON.parse(
            await readFile(path.join(root, name, "owner.json"), "utf8"),
          );
          const pid = Number.isInteger(raw.pid) ? raw.pid : "?";
          const op = typeof raw.operationId === "string" ? raw.operationId.slice(0, 20) : "?";
          lock = `pid=${pid}:op=${op}`;
        } catch {
          lock = "unreadable";
        }
      } else if (name.endsWith(".exclusive-replan.json")) {
        reserv = "present";
      }
    }
  } catch {
    lock = "no-lockdir";
  }
  return { lock, reserv };
}

function liveChildProcesses() {
  try {
    return process._getActiveHandles().filter(
      (handle) => handle?.constructor?.name === "ChildProcess",
    ).length;
  } catch {
    return -1;
  }
}

/**
 * Capture one stall snapshot. `readQueue` is waitForJournal's queue reader;
 * `last` is its latest journal state. Returns a single log line.
 */
export async function captureQueueStallDiagnostics(fixture, readQueue, last) {
  let head = "none";
  let leftover = "?";
  try {
    const pending = await readQueue();
    leftover = String(pending.length);
    const first = pending[0];
    head = first ? `${safeSlice(first.sliceId)}:${safeAction(first.action)}` : "empty";
  } catch {
    head = "queue-read-fail";
  }
  const { lock, reserv } = await lockAndReservation(fixture.vaultPath);
  const drains = describeWorkerWriteDrains(fixture.vaultPath, fixture.planId).map(
    (entry) => ({
      oneShot: entry.oneShot,
      followUpFull: entry.followUpFull,
      taskPending: entry.taskPending,
      lastYieldedLive: entry.lastYieldedLive,
      operation: entry.operation,
      operationElapsedMs: entry.operationElapsedMs,
    }),
  );
  const errors = readSwallowedDrainErrors().map((entry) => `${entry.name}:${entry.code ?? "-"}`);
  return `STALL_DIAG drains=${JSON.stringify(drains)} errors=[${errors.join(",")}] head=${head} leftover=${leftover} lock=${lock} reserv=${reserv} selfPid=${process.pid} childProcs=${liveChildProcesses()} started=${last?.started?.length ?? "?"} completed=${last?.completed?.length ?? "?"}`;
}

export { queueStallDiagnosticsEnabled };
