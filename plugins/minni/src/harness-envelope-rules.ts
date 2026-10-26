// Harness-envelope rules (recall-to-use rev5, 5.2.1): re-exported from prompt-class.ts.
//
// Kept as a dedicated module boundary so existing parity tests and the ledger's
// capture-module load graph (`harness-envelope.js -> harness-envelope-rules.js`)
// stay unchanged while S3a callers (`hook-handlers.ts`, `jev-harvest.ts`) share
// the single rule table in `prompt-class.ts` without loading ledger capture modules.
export {
  HARNESS_ENVELOPE_RULES,
  HARNESS_TRIM_CHARS,
  classificationRuleIds,
  harnessEnvelopeRuleIds,
} from "./prompt-class.js";
