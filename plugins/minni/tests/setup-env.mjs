// Test hygiene preload (wired via `node --test --import ./tests/setup-env.mjs`,
// which propagates to every spawned test-file process).
//
// The AFM generation-probe cache now persists across processes under
// ~/.minni/run/afm-probe-cache.json (see src/afm.ts). Tests must never read or
// write live ~/.minni state, so each test process gets the
// MINNI_AFM_PROBE_CACHE override pointed at its own tmpdir. Tests that
// exercise the persistent cache itself re-point the same env var at their own
// fixture file.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

if (!process.env.MINNI_AFM_PROBE_CACHE) {
  process.env.MINNI_AFM_PROBE_CACHE = path.join(
    mkdtempSync(path.join(tmpdir(), "minni-test-probe-cache-")),
    "afm-probe-cache.json",
  );
}

if (!process.env.MINNI_CONSOLE_TOKEN) {
  process.env.MINNI_CONSOLE_TOKEN = "test-console-token";
}
if (!process.env.MINNI_CONSOLE_DEEP_RESEARCH) {
  process.env.MINNI_CONSOLE_DEEP_RESEARCH = "1";
}
