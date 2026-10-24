// Work a hook may only do AFTER its output has reached the host.
//
// SessionStart re-injects corrections stashed by PreCompact and then archives
// the inbox entry that carried them. Archiving inside the handler — before the
// envelope has left the process — makes everything between "entry archived" and
// "host received the envelope" a permanent-loss window: a hook killed there has
// CONSUMED the correction without ever delivering it, and because the entry is
// gone nothing re-delivers it. That window is not hypothetical on a platform
// that kills the hook on a deadline.
//
// The rule this module enforces is ordering, not speed: deliver first, consume
// second. A commit deferred here runs only once `emitDelivered` has confirmed
// the flush; if delivery failed, the commit is DISCARDED and the entry stays on
// disk, so the correction re-injects on the next boot. That trades exactly-once
// for at-least-once in the failure case, which is the honest trade — a
// correction delivered twice is noise, a correction consumed unsent is gone.
//
// Process-scoped state is the right shape here: a hook process handles exactly
// one event and exits (see the note on findProjectRoot in hook-utils.ts).
import { emitDelivered } from "./hook-utils.js";
import { recordAudit } from "./vault.js";

/** Deferred work; rejections are absorbed by `runDeliveryCommits`. */
export type DeliveryCommit = () => Promise<void>;

/**
 * Work that records an ABANDONED delivery: it runs exactly when the commit it
 * was registered with is thrown away unrun. May be sync or async; throws and
 * rejections are absorbed.
 */
export type AbandonHandler = () => void | Promise<void>;

export interface DeferOptions {
  /**
   * Runs instead of the commit when the output never reaches the host (failed
   * flush, wire drop, or a handler that threw and took the discard path). The
   * use ledger uses it to write `delivered: false` rows, so a dropped output is
   * a recorded fact rather than an absence.
   */
  onAbandon?: AbandonHandler;
  /**
   * The commit only RECORDS this delivery (the use ledger) and consumes
   * nothing. It runs and abandons exactly like any other, but it is left out of
   * the rollback count in the undelivered audit: nothing it carried "stays
   * re-deliverable", and counting it would turn a wire drop that
   * `_intent_dropped` already recorded into a second, double-counting row.
   */
  recordOnly?: boolean;
}

const pendingCommits: DeliveryCommit[] = [];
// How many of pendingCommits are recordOnly; settled with them.
let pendingRecordOnly = 0;
// Registered alongside their commits and settled together: a delivery runs the
// commits and drops these; a discard drops the commits and runs these.
const pendingAbandons: AbandonHandler[] = [];
// Async abandon handlers still in flight, awaited (bounded) before exit.
const inflightAbandons: Promise<void>[] = [];
let outputDropped = false;

/**
 * Ceiling on how long an exit path waits for async abandon handlers. They are
 * local appends, so this is generous; it exists so a wedged handler cannot hold
 * the process into the harness kill that this module is built to avoid.
 */
const ABANDON_SETTLE_BUDGET_MS = 500;

/**
 * Record that the platform wire could not carry this output — the payload never
 * went on the wire, and what will be written instead is a noop the host ignores.
 *
 * This is a SECOND way to fail to deliver, and the one that is easy to miss: the
 * stdout write SUCCEEDS, so a flush-only definition of "delivered" reports it as
 * a success and consumes whatever the payload was carrying. Grok is the live
 * case — its wire declares SessionStart un-injectable, so a boot envelope full
 * of corrections renders as `{continue:true}` and the corrections reach nobody.
 * Archiving them on the strength of that write destroys the only durable copy.
 *
 * Delivery therefore means BOTH: the envelope went on the wire, and the bytes
 * reached the host.
 */
export function markOutputDropped(): void {
  outputDropped = true;
}

/**
 * Register work that must not run until this hook's output reaches the host.
 * Deliberately NOT async: the caller registers intent and moves on, so nothing
 * about the ordering depends on where in the handler this is called.
 */
export function deferUntilDelivered(commit: DeliveryCommit, options?: DeferOptions): void {
  pendingCommits.push(commit);
  if (options?.recordOnly === true) pendingRecordOnly += 1;
  if (options?.onAbandon) pendingAbandons.push(options.onAbandon);
}

/** Deferred commits not yet run — the ordering seam the tests assert on. */
export function pendingDeliveryCommitCount(): number {
  return pendingCommits.length;
}

/**
 * Drop every deferred commit unrun (the output never landed), and run the
 * abandon handlers registered with them.
 *
 * Stays synchronous on purpose: `runHookMain` calls it bare in its catch, and a
 * sync abandon handler (the ledger's append is one) has therefore finished by
 * the time this returns. An async handler is started here and awaited, within a
 * budget, by `settleAbandonHandlers` on the exit paths.
 */
export function discardDeliveryCommits(): void {
  pendingCommits.length = 0;
  pendingRecordOnly = 0;
  outputDropped = false;
  const abandons = pendingAbandons.splice(0);
  for (const onAbandon of abandons) {
    try {
      const pending = onAbandon();
      if (pending && typeof (pending as Promise<void>).then === "function") {
        inflightAbandons.push(Promise.resolve(pending).then(noop, noop));
      }
    } catch {
      // Best effort: recording an abandonment must never fail the hook.
    }
  }
}

function noop(): void {}

/**
 * Wait for async abandon handlers started by `discardDeliveryCommits`, for at
 * most `budgetMs`. Never rejects. The exit paths call it so an abandonment
 * record is not cut off by `process.exit`.
 */
export async function settleAbandonHandlers(budgetMs = ABANDON_SETTLE_BUDGET_MS): Promise<void> {
  const pending = inflightAbandons.splice(0);
  if (pending.length === 0) return;
  let timer: NodeJS.Timeout | undefined;
  // Deliberately NOT unref'd: a handler whose promise never settles and holds no
  // handle would otherwise let the loop drain mid-await and skip the exit path.
  const budget = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, budgetMs);
  });
  try {
    await Promise.race([Promise.all(pending).then(noop), budget]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Run and clear every deferred commit, in registration order. */
export async function runDeliveryCommits(): Promise<void> {
  const commits = pendingCommits.splice(0);
  pendingRecordOnly = 0;
  // Delivered: whatever would have recorded an abandonment no longer applies.
  pendingAbandons.length = 0;
  outputDropped = false;
  for (const commit of commits) {
    try {
      await commit();
    } catch {
      // Best effort: a commit that fails leaves its entry in place, which
      // re-delivers next boot. Losing the cleanup must not lose the output.
    }
  }
}

/**
 * End the hook process now that delivery is fully settled.
 *
 * Threading `timeoutMs` into each RPC bounds the calls we know about, but it
 * cannot bound the ones we do not: `withBudget` RACES a promise, it does not
 * cancel it, so any abandoned handle — a socket mid-read, an AFM probe, a
 * wedged filesystem call — keeps the event loop alive after the envelope has
 * been written. A hook still running when its harness deadline expires is
 * KILLED, and a killed hook's output is discarded even though the bytes were
 * already flushed — which, with commits already run, is the correction consumed
 * for an envelope the host threw away. Exiting closes that class of failure
 * whole, rather than one handle at a time.
 *
 * Safe precisely HERE and nowhere earlier: `emitAndCommit` has awaited the
 * stdout flush, run or discarded every deferred commit, and awaited its audit
 * write, so there is no outstanding work whose loss would matter.
 */
export function exitAfterDelivery(): never {
  process.exit(0);
}

/**
 * Close out a FAILED event: discard every deferred commit, flush the degraded
 * output, and exit.
 *
 * The failure path needs the same protocol as the success path, and had neither
 * half. It emitted fire-and-forget and returned: nothing awaited the flush, so a
 * host that kills the hook on a deadline could SIGKILL it with the degraded
 * envelope still sitting in the buffer; and nothing exited, so the abandoned
 * handles from budgeted RPCs kept the loop alive until that kill arrived. The
 * corrections were safe — the commits are discarded first — but the "this boot
 * degraded" signal, which exists precisely so a bad boot cannot pass for a clean
 * one, was the thing most likely to be lost.
 *
 * Discarding is repeated here rather than assumed: callers already discard
 * before auditing, and making this function responsible for the whole protocol
 * means a future caller cannot get the order wrong.
 */
export async function failAndExit(output: object): Promise<never> {
  discardDeliveryCommits();
  await emitDelivered(output);
  // Includes handlers a caller's own earlier discard started (runHookMain's catch).
  await settleAbandonHandlers();
  exitAfterDelivery();
}

export interface DeliveryContext {
  vaultPath: string;
  /** Audit tool-name prefix, e.g. "hook" -> hook_undelivered. */
  auditPrefix: string;
  event: string;
}

/**
 * Emit the hook's output and settle its deferred work on the result: commits
 * run only on a confirmed delivery, and a non-delivery is AUDITED rather than
 * swallowed — a boot whose memory never reached the host must be visible as
 * such, not indistinguishable from a clean one (hooks-PL-5).
 *
 * Returns whether the output was delivered.
 */
export async function emitAndCommit(
  output: object,
  context: DeliveryContext,
): Promise<boolean> {
  const dropped = outputDropped;
  const flushed = await emitDelivered(output);
  // BOTH conditions, not either: a dropped envelope still flushes (as a noop),
  // and a flushed noop delivered nothing.
  if (flushed && !dropped) {
    await runDeliveryCommits();
    return true;
  }
  // Record-only commits (ledger rows) are not rollbacks; see DeferOptions.
  const abandoned = pendingDeliveryCommitCount() - pendingRecordOnly;
  discardDeliveryCommits();
  await settleAbandonHandlers();
  // A non-delivery is audited even when nothing was rolled back: an envelope
  // carrying identity, recall and the active plan reaching nobody is exactly
  // the "degraded boot that looks clean" hooks-PL-5 forbids, and it has no
  // deferred commits to signal it. The one case that needs no row here is a
  // WIRE DROP with nothing pending — `_intent_dropped` already recorded it, and
  // a second row would just double-count the same event.
  if (dropped && abandoned === 0) return false;
  try {
    await recordAudit(context.vaultPath, {
      tool: `${context.auditPrefix}_undelivered`,
      summary: dropped
        ? `${context.event}: platform cannot carry this output; ${abandoned} deferred commit(s) rolled back (stays re-deliverable)`
        : `${context.event}: output never reached the host (${abandoned} deferred commit(s) rolled back)`,
    });
  } catch {
    // stdout may already be gone; an unavailable audit must not throw on top.
  }
  return false;
}
