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

const pendingCommits: DeliveryCommit[] = [];
let outputDropped = false;

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
export function deferUntilDelivered(commit: DeliveryCommit): void {
  pendingCommits.push(commit);
}

/** Deferred commits not yet run — the ordering seam the tests assert on. */
export function pendingDeliveryCommitCount(): number {
  return pendingCommits.length;
}

/** Drop every deferred commit unrun (the output never landed). */
export function discardDeliveryCommits(): void {
  pendingCommits.length = 0;
  outputDropped = false;
}

/** Run and clear every deferred commit, in registration order. */
export async function runDeliveryCommits(): Promise<void> {
  const commits = pendingCommits.splice(0);
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
  const abandoned = pendingDeliveryCommitCount();
  discardDeliveryCommits();
  if (abandoned === 0) return false;
  try {
    await recordAudit(context.vaultPath, {
      tool: `${context.auditPrefix}_undelivered`,
      summary: dropped
        ? `${context.event}: platform cannot carry this output; ${abandoned} deferred commit(s) rolled back (stays re-deliverable)`
        : `${context.event}: output never reached the host; ${abandoned} deferred commit(s) rolled back (re-delivers next boot)`,
    });
  } catch {
    // stdout may already be gone; an unavailable audit must not throw on top.
  }
  return false;
}
