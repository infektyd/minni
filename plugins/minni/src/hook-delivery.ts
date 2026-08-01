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
}

/** Run and clear every deferred commit, in registration order. */
export async function runDeliveryCommits(): Promise<void> {
  const commits = pendingCommits.splice(0);
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
  const delivered = await emitDelivered(output);
  if (delivered) {
    await runDeliveryCommits();
    return true;
  }
  const abandoned = pendingDeliveryCommitCount();
  discardDeliveryCommits();
  try {
    await recordAudit(context.vaultPath, {
      tool: `${context.auditPrefix}_undelivered`,
      summary: `${context.event}: output never reached the host; ${abandoned} deferred commit(s) rolled back (re-delivers next boot)`,
    });
  } catch {
    // stdout is already gone; an unavailable audit must not throw on top of it.
  }
  return false;
}
