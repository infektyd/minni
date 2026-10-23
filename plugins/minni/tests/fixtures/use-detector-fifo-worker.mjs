// Runs one detector or report call in its own process, for
// use-detector-fifo.test.mjs: a call that blocks on a FIFO hangs only this
// child, which the parent kills at its timeout.
//
// `raceStat` models the stat-then-read race: fs.statSync reports the FIFO as
// a regular file (as if it was swapped in after the stat), so only a guard
// on the OPENED descriptor (O_NONBLOCK + fstat) can refuse it.
import fs from "node:fs";

import { runDetector } from "../../dist/use-detector.js";
import { buildUseReport } from "../../dist/use-report.js";

const args = JSON.parse(process.argv[2]);
if (args.raceStat) {
  const realStat = fs.statSync;
  const regular = realStat(args.regularFile);
  fs.statSync = function statSync(target, ...rest) {
    return String(target) === args.raceStat ? regular : realStat.call(fs, target, ...rest);
  };
}
const result = args.report
  ? buildUseReport(args.vaultPath, { days: 7 }).ledger_off
  : runDetector(args.detector);
process.stdout.write(JSON.stringify(result));
