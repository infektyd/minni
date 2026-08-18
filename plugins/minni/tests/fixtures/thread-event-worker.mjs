import { appendOrderedThreadEvent } from "../../dist/thread-events.js";
import { withThreadLock } from "../../dist/thread-lock.js";

const [vaultPath, planId, journalPath, idempotencyKey, rev] =
  process.argv.slice(2);

await withThreadLock(
  vaultPath,
  planId,
  `event-${idempotencyKey}`,
  async () => {
    await appendOrderedThreadEvent({
      journalPath,
      planId,
      rev: Number(rev),
      idempotencyKey,
      actor: "test-worker",
      kind: "test.event",
      at: new Date().toISOString(),
    });
  },
);
