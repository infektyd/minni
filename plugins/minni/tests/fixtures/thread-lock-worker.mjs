import { appendFile } from "node:fs/promises";

import { withThreadLock } from "../../dist/thread-lock.js";

const [vaultPath, planId, logPath] = process.argv.slice(2);

await withThreadLock(
  vaultPath,
  planId,
  `worker-${process.pid}`,
  async () => {
    const entered = Date.now();
    await new Promise((resolve) => setTimeout(resolve, 75));
    const left = Date.now();
    await appendFile(
      logPath,
      `${JSON.stringify({ entered, left, pid: process.pid })}\n`,
      "utf8",
    );
  },
);
