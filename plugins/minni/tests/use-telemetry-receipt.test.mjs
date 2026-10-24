// Recall-to-use S1b (§4.6): through Stop, a pointer surfacing the reply cites
// by title and one followed by a synthetic dismissal line give a receipt that
// reports cited: 1, dismissed: 1. Drives the detector and then the receipt on a
// synthetic transcript and ledger, as the Stop wiring will.
import assert from "node:assert/strict";
import test from "node:test";

import { maskTelemetry } from "../dist/jev-harvest.js";
import { runDetector } from "../dist/use-detector.js";
import { ensureVault, formatSessionReceiptLine, sessionReceipt } from "../dist/vault.js";
import {
  append,
  Clock,
  detectorArgs,
  finalJudged,
  makeVault,
  SESSION_ID,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

test("the Stop receipt reports one cited and one dismissed surfacing", async (t) => {
  const { vault, transcriptPath } = await makeVault(t, "minni-receipt-stop-");
  await ensureVault(vault);
  await writeDfSnapshot(vault);
  const clock = new Clock(Date.now() - 60 * 60 * 1000);
  const tx = new Transcript(transcriptPath, clock);

  tx.prompt("Why does the nightly job stall?", "p1");
  const cited = surfacedRow({ mkey: "vault:wiki/sessions/beryl-cadence.md", title: "Beryl harbor checkpoint cadence", promptId: "p1", ts: clock.next(10) });
  append(vault, [turnRow({ turnIx: 1, promptId: "p1", ts: cited.ts }), cited]);
  tx.say("Per the beryl harbor checkpoint cadence, the job now starts earlier.");
  await tx.flush();
  runDetector(detectorArgs(vault, transcriptPath, clock, { lastAssistantMessage: "Per the beryl harbor checkpoint cadence, the job now starts earlier." }));

  tx.prompt("Also rename the build target.", "p2");
  const dismissed = surfacedRow({ mkey: "vault:wiki/sessions/quartz-drain.md", title: "Quartz lattice drain ordering", promptId: "p2", ts: clock.next(10) });
  append(vault, [turnRow({ turnIx: 2, promptId: "p2", ts: dismissed.ts }), dismissed]);
  tx.say("Not relevant to this task.");
  tx.tool("Bash", { command: "git mv build.sh make.sh" });
  tx.say("Renamed.");
  await tx.flush();
  runDetector(detectorArgs(vault, transcriptPath, clock, { lastAssistantMessage: "Renamed." }));

  assert.equal(finalJudged(vault, cited.sid).outcome, "cited");
  assert.equal(finalJudged(vault, dismissed.sid).outcome, "dismissed");

  const receipt = await sessionReceipt(vault, SESSION_ID);
  assert.equal(receipt.use?.surfaced, 2);
  assert.equal(receipt.use?.cited, 1);
  assert.equal(receipt.use?.dismissed, 1);
  assert.equal(receipt.use?.applied, 0);
  const line = formatSessionReceiptLine(receipt);
  assert.match(line, / Use: 2 surfaced, 1 cited, 0 applied\.$/);
  assert.equal(maskTelemetry(line), "", "harvest never re-reads the receipt");
});
