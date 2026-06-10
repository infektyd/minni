import { readFileSync, statSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const PLUGIN_ROOT = path.resolve(__dirname, "..");

/**
 * Expand a leading `~` to the home dir. Plugin manifests pass env values like
 * `~/.minni/run/minnid.sock` literally; Node does NOT expand `~`, so an
 * unexpanded value reaches net.createConnection()/existsSync() and the daemon
 * socket + vault lookups fail. Expand centrally so every consumer gets an
 * absolute path.
 */
function expandTilde(p: string): string {
  return p.replace(/^~(?=$|\/)/, os.homedir());
}

export const DEFAULT_VAULT_PATH = expandTilde(
  process.env.MINNI_VAULT_PATH ??
    process.env.MINNI_CODEX_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "codex-vault"),
);

export const SOCKET_PATH = expandTilde(
  process.env.MINNI_SOCKET_PATH ??
    path.join(os.homedir(), ".minni", "run", "minnid.sock"),
);

export const AFM_HEALTH_URL =
  process.env.MINNI_AFM_HEALTH_URL ?? "http://127.0.0.1:11437/health";

export const AFM_PREPARE_TASK_URL =
  process.env.MINNI_AFM_PREPARE_TASK_URL ?? "http://127.0.0.1:11437/v1/chat/completions";

export const AFM_PREPARE_TASK_MODEL =
  process.env.MINNI_AFM_PREPARE_TASK_MODEL ?? "apple-foundation-models";

// G13 (SEC-004): explicit operator allowlist for non-loopback model targets.
// Comma-separated hosts (e.g. "192.168.1.10,afm.internal"). Loopback (127.0.0.1,localhost,::1) always allowed.
// MINNI_MODEL_ALLOWED_TARGETS is the provider-protocol alias for MINNI_AFM_ALLOWED_TARGETS;
// both are honored (union). Non-loopback targets additionally require HTTPS.
// If a non-local target is configured without being listed, callAfmJson will deny with structured error.
export function modelAllowedTargets(): string[] {
  return [process.env.MINNI_AFM_ALLOWED_TARGETS, process.env.MINNI_MODEL_ALLOWED_TARGETS]
    .flatMap((value) => (value ?? "").split(","))
    .map((s) => s.trim())
    .filter(Boolean);
}

export const AFM_ALLOWED_TARGETS: string[] = modelAllowedTargets();
export const MODEL_ALLOWED_TARGETS: string[] = AFM_ALLOWED_TARGETS;

// --- Model provider chain config (P3) ---------------------------------------
// ~/.minni/providers.json configures the provider chain and per-operation
// routing policy. MINNI_AFM_* env vars keep precedence over file values.
// Secrets are NEVER stored in providers.json: cloud credentials come only from
// apiKeyEnv (env var name) or apiKeyFile (0600 file under ~/.minni/secrets/).

export interface CloudProviderConfig {
  enabled?: boolean;
  vendor?: string;
  model?: string;
  apiKeyEnv?: string;
  apiKeyFile?: string;
  privacyMax?: boolean;
}

export interface ProvidersConfig {
  chain: string[];
  operations: Record<string, { localOnly?: boolean }>;
  providers: {
    mlx?: { baseUrl?: string; model?: string };
    ollama?: { baseUrl?: string; model?: string };
    cloud?: CloudProviderConfig;
  };
}

const DEFAULT_PROVIDERS_CONFIG: ProvidersConfig = {
  chain: ["afm"],
  operations: { retrieval: { localOnly: true } },
  providers: {},
};

export const PROVIDERS_CONFIG_PATH = expandTilde(
  process.env.MINNI_PROVIDERS_CONFIG ?? path.join(os.homedir(), ".minni", "providers.json"),
);

export function loadProvidersConfig(filePath = PROVIDERS_CONFIG_PATH): ProvidersConfig {
  let raw: string;
  try {
    raw = readFileSync(filePath, "utf8");
  } catch {
    return { ...DEFAULT_PROVIDERS_CONFIG };
  }
  try {
    const parsed = JSON.parse(raw) as Partial<ProvidersConfig> & Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT_PROVIDERS_CONFIG };
    const chain = Array.isArray(parsed.chain)
      ? parsed.chain.filter((item): item is string => typeof item === "string" && item.length > 0)
      : DEFAULT_PROVIDERS_CONFIG.chain;
    const operations =
      parsed.operations && typeof parsed.operations === "object"
        ? (parsed.operations as ProvidersConfig["operations"])
        : DEFAULT_PROVIDERS_CONFIG.operations;
    const providers =
      parsed.providers && typeof parsed.providers === "object"
        ? (parsed.providers as ProvidersConfig["providers"])
        : {};
    // SEC: inline secrets are rejected outright — keys live in env or 0600 files only.
    const cloud = providers.cloud as (CloudProviderConfig & { apiKey?: unknown }) | undefined;
    if (cloud && "apiKey" in cloud) {
      console.warn(
        "[minni] providers.json: inline providers.cloud.apiKey is not allowed (use apiKeyEnv or apiKeyFile); cloud provider disabled",
      );
      providers.cloud = { ...cloud, apiKey: undefined, enabled: false } as CloudProviderConfig;
      delete (providers.cloud as Record<string, unknown>).apiKey;
    }
    return { chain: chain.length > 0 ? chain : ["afm"], operations, providers };
  } catch {
    console.warn("[minni] providers.json: invalid JSON; using default AFM-only chain");
    return { ...DEFAULT_PROVIDERS_CONFIG };
  }
}

export const PROVIDERS_CONFIG: ProvidersConfig = loadProvidersConfig();

export const MINNI_SECRETS_DIR = path.join(os.homedir(), ".minni", "secrets");

export interface CloudApiKeyResolution {
  key?: string;
  /** Structured, key-free reason when no usable secret was found. */
  error?: string;
}

/**
 * Resolve the cloud provider API key. Secrets come ONLY from:
 *   - apiKeyEnv: the named environment variable, or
 *   - apiKeyFile: a 0600 file under ~/.minni/secrets/
 * Never from providers.json itself. The resolved key must never be written to
 * status/audit/error output (safeError strips auth material as a backstop).
 */
export function resolveCloudApiKey(
  cloud: CloudProviderConfig | undefined,
  secretsDir = MINNI_SECRETS_DIR,
): CloudApiKeyResolution {
  if (!cloud || cloud.enabled !== true) return {};
  if (cloud.apiKeyEnv) {
    const key = process.env[cloud.apiKeyEnv];
    return key ? { key } : { error: `cloud_key_unavailable: env ${cloud.apiKeyEnv} is not set` };
  }
  if (cloud.apiKeyFile) {
    const resolved = path.resolve(expandTilde(cloud.apiKeyFile));
    const root = path.resolve(secretsDir) + path.sep;
    if (!resolved.startsWith(root)) {
      return { error: "cloud_key_denied: apiKeyFile must live under ~/.minni/secrets/" };
    }
    try {
      const st = statSync(resolved);
      if ((st.mode & 0o077) !== 0) {
        return { error: "cloud_key_denied: apiKeyFile must be mode 0600 (no group/other access)" };
      }
      const key = readFileSync(resolved, "utf8").trim();
      return key ? { key } : { error: "cloud_key_unavailable: apiKeyFile is empty" };
    } catch {
      return { error: "cloud_key_unavailable: apiKeyFile is not readable" };
    }
  }
  return { error: "cloud_key_unavailable: cloud provider enabled without apiKeyEnv or apiKeyFile" };
}

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

export const CLAUDECODE_VAULT_PATH = expandTilde(
  process.env.MINNI_CLAUDECODE_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "claudecode-vault"),
);

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

export const KILOCODE_VAULT_PATH = expandTilde(
  process.env.MINNI_KILOCODE_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "kilocode-vault"),
);

export const KILOCODE_HOOKS_ENABLED =
  (process.env.MINNI_KILOCODE_HOOKS ?? "on").toLowerCase() !== "off";

export const KILOCODE_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.KILO_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

// --- Grok-build agent defaults ---

export const GROK_AGENT_ID =
  process.env.MINNI_GROK_AGENT_ID ?? "grok-build";

export const GROK_WORKSPACE_ID =
  process.env.MINNI_GROK_WORKSPACE_ID ??
  `workspace-${path.basename(process.cwd())}`;

export const GROK_VAULT_PATH = expandTilde(
  process.env.MINNI_GROK_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "grok-build-vault"),
);

export const GROK_HOOKS_ENABLED =
  (process.env.MINNI_GROK_HOOKS ?? "on").toLowerCase() !== "off";

export const GROK_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.GROK_CONTEXT_WINDOW ?? process.env.MINNI_GROK_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 256_000;
})();
