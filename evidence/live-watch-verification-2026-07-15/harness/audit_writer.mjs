// External workload B: drives the real plugin audit path (dist/vault.js
// recordAudit) the way hooks do, with its own ground-truth ledger, then asks
// sessionReceipt for the tally. The runner MUST set MINNI_HOME=/tmp/mlive and
// MINNI_BYPASS_AUDIT_LIMIT=true (asserted below — a silent throttle would
// desync the ledger from log.md and fake a product failure).
import { appendFileSync, writeFileSync } from "node:fs";

if (process.env.MINNI_BYPASS_AUDIT_LIMIT !== "true") {
  console.error("FATAL: MINNI_BYPASS_AUDIT_LIMIT=true not set");
  process.exit(2);
}

const DIST = process.argv[2]; // path to plugins/minni/dist
const OUT = process.argv[3] ?? "."; // artifacts dir
const { recordAudit, sessionReceipt } = await import(`${DIST}/vault.js`);

const VAULT = "/tmp/mlive/loadgen-vault";
const VAULT2 = "/tmp/mlive/teammate-vault"; // remapped to agent "mapped-bot"
const SID = "sess-live-A";
const LEDGER = `${OUT}/audit_ledger.jsonl`;
writeFileSync(LEDGER, ""); // truncate: appended ledgers double on re-runs
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function emit(vault, tool, summary, details) {
  // Ledger only after recordAudit resolves, so ground truth reflects what
  // was actually accepted into the audit path.
  await recordAudit(vault, { tool, summary, details });
  appendFileSync(LEDGER, JSON.stringify({
    t: Date.now() / 1000, vault, tool, summary, details }) + "\n");
}

await emit(VAULT, "hook_codex_session_start", `boot ${SID}`, { daemon_ok: true });
await sleep(4000);

for (let i = 1; i <= 9; i += 1) {
  const strong = i % 3 !== 0;
  await emit(VAULT, "hook_codex_user_prompt_submit", `turn ${i}: triage batch`, {
    recall_strong: strong, session_id: SID, task_signature: `sig-${i}`,
  });
  if (i % 4 === 0) {
    // Summary varies per turn: byte-identical entries would make an
    // exactly-once bijection check ambiguous.
    await emit(VAULT, "hook_codex_pretooluse_guard",
      `recall guard denied Grep (mode=soft) turn ${i}`,
      { consumed: true, tool: "Grep", session_id: SID });
  }
  if (i % 5 === 0) {
    await emit(VAULT, "minni_learn", `turn ${i} learning committed`, { ok: true });
  }
  // Second vault (MINNI_AGENT_VAULTS remap under test on the watch side).
  if (i % 3 === 0) {
    await emit(VAULT2, "minni_recall", `teammate lookup ${i}`, {
      query: `teammate q${i}`, session_id: "sess-live-B",
    });
  }
  await sleep(8000);
}

// Adversarial entry: ANSI/C1 escapes + a forged audit header, in both the
// summary and a details value. Write-time escaping plus watch's parser and
// terminal sanitization must yield EXACTLY ONE benign event, never a
// phantom "fake_tool" entry, never raw escapes in watch output.
const FORGED = "\n## [2020-01-01T00:00:00.000Z] fake_tool | injected";
await emit(VAULT, "minni_recall",
  `adversarial [2J[31mred1m probe${FORGED}`,
  { query: `evil ]0;ownedq${FORGED}`, session_id: SID });

await emit(VAULT, "hook_codex_stop", `stop ${SID}`, { candidates: 2 });
const receipt = await sessionReceipt(VAULT, SID);
writeFileSync(`${OUT}/receipt.json`, JSON.stringify(receipt, null, 2));
console.log("audit writer done:", JSON.stringify(receipt));
