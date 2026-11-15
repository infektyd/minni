// Test hygiene preload (wired via `node --test --import ./tests/setup-env.mjs`,
// which propagates to every spawned test-file process).
//
// The AFM generation-probe cache now persists across processes under
// ~/.minni/run/afm-probe-cache.json (see src/afm.ts). Tests must never read or
// write live ~/.minni state, so each test process gets the
// MINNI_AFM_PROBE_CACHE override pointed at its own tmpdir. Tests that
// exercise the persistent cache itself re-point the same env var at their own
// fixture file.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

if (!process.env.MINNI_HOME) {
  const testMinniHome = mkdtempSync(path.join(tmpdir(), "minni-plugin-test-home-"));
  process.env.MINNI_HOME = testMinniHome;
  process.on("exit", () => {
    try {
      rmSync(testMinniHome, { recursive: true, force: true });
    } catch {
      // Best-effort temp home cleanup.
    }
  });
}

if (!process.env.MINNI_VAULT_PATH) {
  process.env.MINNI_VAULT_PATH = path.join(process.env.MINNI_HOME, "unknown-vault");
}

if (!process.env.MINNI_AFM_PROBE_CACHE) {
  process.env.MINNI_AFM_PROBE_CACHE = path.join(
    mkdtempSync(path.join(tmpdir(), "minni-test-probe-cache-")),
    "afm-probe-cache.json",
  );
}

if (!process.env.MINNI_SOCKET_PATH) {
  process.env.MINNI_SOCKET_PATH = path.join(
    mkdtempSync(path.join(tmpdir(), "minni-test-socket-")),
    "minnid.sock",
  );
}

// Host agent session identity must not leak into tests. When the suite runs
// from inside an agent (Claude Code, Codex, agy, ...), that host's session id
// env var is inherited and resolveMcpSessionIdentity treats it as an explicit
// session ("explicit_or_env"), so multi-session tests that resolve by hostPid
// failed only when run from an agent shell. Mirror of AGENT_SESSION_ENV_VARS
// in src/config.ts, plus the generic MINNI_SESSION_ID. Tests that exercise env
// precedence set these explicitly.
for (const name of [
  "CLAUDE_CODE_SESSION_ID",
  "CODEX_SESSION_ID",
  "GEMINI_SESSION_ID",
  "ANTIGRAVITY_SESSION_ID",
  "GROK_SESSION_ID",
  "CURSOR_SESSION_ID",
  "KILOCODE_SESSION_ID",
  "DEVIN_SESSION_ID",
  "MINNI_SESSION_ID",
]) {
  delete process.env[name];
}
