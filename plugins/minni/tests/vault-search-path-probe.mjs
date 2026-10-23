// Preload for vault-search-process-path.test.mjs (`node --import <this>`), not
// a test file itself. It shares the process's dist/vault.js module instance,
// and when the process ends — normal exit, or SIGTERM for a server that never
// exits on its own — writes what that process's vault-search cache holds for
// MINNI_TEST_SEARCH_PROBE_VAULT to stderr: one `VAULT_SEARCH_PROBE <json>`
// line, `null` when the process never searched that vault.
import { writeSync } from "node:fs";

import { vaultSearchCacheState } from "../dist/vault.js";

const vault = process.env.MINNI_TEST_SEARCH_PROBE_VAULT ?? "";
let reported = false;

function report() {
  if (reported) return;
  reported = true;
  const state = vaultSearchCacheState(vault) ?? null;
  writeSync(2, `\nVAULT_SEARCH_PROBE ${JSON.stringify(state)}\n`);
}

process.on("exit", report);
process.on("SIGTERM", () => {
  report();
  process.exit(0);
});
