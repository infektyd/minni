import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const PLUGIN_ROOT = path.resolve(__dirname, "..");

export const DEFAULT_VAULT_PATH =
  process.env.MINNI_VAULT_PATH ??
  process.env.MINNI_CODEX_VAULT_PATH ??
  path.join(os.homedir(), ".minni", "codex-vault");

export const SOCKET_PATH =
  process.env.MINNI_SOCKET_PATH ??
  path.join(os.homedir(), ".minni", "run", "sovrd.sock");

export const AFM_HEALTH_URL =
  process.env.MINNI_AFM_HEALTH_URL ?? "http://127.0.0.1:11437/health";

export const AFM_PREPARE_TASK_URL =
  process.env.MINNI_AFM_PREPARE_TASK_URL ?? "http://127.0.0.1:11437/v1/chat/completions";

export const AFM_PREPARE_TASK_MODEL =
  process.env.MINNI_AFM_PREPARE_TASK_MODEL ?? "apple-foundation-models";

// G13 (SEC-004): explicit operator allowlist for non-loopback AFM targets.
// Comma-separated hosts (e.g. "192.168.1.10,afm.internal"). Loopback (127.0.0.1,localhost,::1) always allowed.
// If a non-local target is configured without being listed, callAfmJson will deny with structured error.
export const AFM_ALLOWED_TARGETS: string[] = (process.env.MINNI_AFM_ALLOWED_TARGETS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

export type AFM_PROVIDER_MODE = "auto" | "bridge" | "native" | "off";

function parseAfmProviderMode(value: string | undefined): AFM_PROVIDER_MODE {
  return value === "auto" || value === "native" || value === "off" ? value : "bridge";
}

export const AFM_PROVIDER_MODE =
  parseAfmProviderMode(process.env.MINNI_AFM_PROVIDER_MODE);

export const DEFAULT_AGENT_ID =
  process.env.MINNI_AGENT_ID ??
  process.env.MINNI_CODEX_AGENT_ID ??
  "codex";

export const DEFAULT_WORKSPACE_ID =
  process.env.MINNI_WORKSPACE_ID ??
  process.env.MINNI_CODEX_WORKSPACE_ID ??
  "workspace-codex";

export const CODEX_HOOKS_ENABLED =
  (process.env.MINNI_CODEX_HOOKS ?? "on").toLowerCase() !== "off";

export const CODEX_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.CODEX_CONTEXT_WINDOW ?? process.env.MINNI_CODEX_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

export const CLAUDECODE_AGENT_ID =
  process.env.MINNI_CLAUDECODE_AGENT_ID ?? "claude-code";

export const CLAUDECODE_WORKSPACE_ID =
  process.env.MINNI_CLAUDECODE_WORKSPACE_ID ??
  `workspace-${path.basename(process.cwd())}`;

export const CLAUDECODE_VAULT_PATH =
  process.env.MINNI_CLAUDECODE_VAULT_PATH ??
  path.join(os.homedir(), ".minni", "claudecode-vault");

export const CLAUDECODE_HOOKS_ENABLED =
  (process.env.MINNI_CLAUDECODE_HOOKS ?? "on").toLowerCase() !== "off";

export const CLAUDECODE_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.CLAUDE_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

// --- KiloCode agent defaults ---

export const KILOCODE_AGENT_ID =
  process.env.MINNI_KILOCODE_AGENT_ID ?? "kilocode";

export const KILOCODE_WORKSPACE_ID =
  process.env.MINNI_KILOCODE_WORKSPACE_ID ??
  `workspace-${path.basename(process.cwd())}`;

export const KILOCODE_VAULT_PATH =
  process.env.MINNI_KILOCODE_VAULT_PATH ??
  path.join(os.homedir(), ".minni", "kilocode-vault");

export const KILOCODE_HOOKS_ENABLED =
  (process.env.MINNI_KILOCODE_HOOKS ?? "on").toLowerCase() !== "off";

export const KILOCODE_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.KILO_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();
