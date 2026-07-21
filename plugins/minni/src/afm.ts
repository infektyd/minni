import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { URL } from "node:url";

import type { JsonResult } from "./sovereign.js";
// G13: single source for allowlist (fixes duplication noted in review)
import { AFM_PREPARE_TASK_MODEL, AFM_PREPARE_TASK_URL, minniHome, modelAllowedTargets } from "./config.js";

export type AfmProviderMode = "auto" | "bridge" | "native" | "off";
export type AfmProvider = "bridge" | "native" | "off";

const VALID_AFM_MODES = new Set<AfmProviderMode>(["auto", "bridge", "native", "off"]);

/**
 * Resolve an AFM provider mode exactly like engine/afm_provider.resolve_afm_mode:
 * an explicit value wins, otherwise MINNI_AFM_PROVIDER_MODE then MINNI_AFM_MODE,
 * defaulting to "bridge". Unknown values fall back to "bridge".
 */
export function resolveAfmMode(value?: string | null): AfmProviderMode {
  const raw = (
    value ?? process.env.MINNI_AFM_PROVIDER_MODE ?? process.env.MINNI_AFM_MODE ?? "bridge"
  )
    .trim()
    .toLowerCase();
  return VALID_AFM_MODES.has(raw as AfmProviderMode) ? (raw as AfmProviderMode) : "bridge";
}

export interface AfmProviderResolution {
  mode: AfmProviderMode;
  provider: AfmProvider;
  status: "off" | "bridge" | "native_available" | "native_unavailable";
  available: boolean;
  backend?: string;
  availability?: string;
  fallbackUsed?: boolean;
  adapterConfigured?: boolean;
  reason?: string;
}

export interface AfmProviderOptions {
  nativeHelperPath?: string;
  health?: JsonResult;
}

export interface CallAfmJsonOptions extends AfmProviderOptions {
  mode?: AfmProviderMode;
  operation?: string;
  timeoutMs?: number;
  transport?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult>;
}

function nativeHelperAvailable(path: string | undefined): boolean {
  return Boolean(path && existsSync(path));
}

export function defaultNativeHelperPath(): string | undefined {
  const here = path.dirname(fileURLToPath(import.meta.url));
  // v0.2 rename: the engine package lives at src/minni/; keep the legacy
  // engine/ location as a fallback for un-migrated checkouts.
  for (const segments of [["src", "minni"], ["engine"]]) {
    const repoHelper = path.resolve(here, "..", "..", "..", ...segments, "native_afm_helper");
    if (existsSync(repoHelper)) return repoHelper;
  }
  return undefined;
}

export function resolvedNativeHelperPath(options: AfmProviderOptions = {}): string | undefined {
  return Object.prototype.hasOwnProperty.call(options, "nativeHelperPath")
    ? options.nativeHelperPath
    : process.env.MINNI_AFM_NATIVE_HELPER ?? defaultNativeHelperPath();
}

function adapterConfigured(): boolean {
  return Boolean(process.env.MINNI_AFM_ADAPTER_PATH || process.env.MINNI_AFM_ADAPTER_ID);
}

function stringField(data: unknown, key: string): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const value = (data as Record<string, unknown>)[key];
  return typeof value === "string" ? value : undefined;
}

function booleanField(data: unknown, key: string): boolean | undefined {
  if (!data || typeof data !== "object") return undefined;
  const value = (data as Record<string, unknown>)[key];
  return typeof value === "boolean" ? value : undefined;
}

function healthAdapterConfigured(health: JsonResult | undefined): boolean {
  if (!health) return false;
  return Boolean(
    stringField(health.data, "adapter")
      || stringField(health.data, "adapterPath")
      || booleanField(health.data, "adapterConfigured") === true,
  );
}

function nativeHealthAvailable(health: JsonResult | undefined): boolean {
  return Boolean(
    health?.ok
      && stringField(health.data, "backend") === "apple-foundation-models"
      && stringField(health.data, "availability") === "available"
      && (stringField(health.data, "status") ?? "ok") === "ok",
  );
}

function nativeHealthReported(health: JsonResult | undefined): boolean {
  return Boolean(
    stringField(health?.data, "backend") === "apple-foundation-models"
      || stringField(health?.data, "provider") === "native",
  );
}

export function safeError(error: unknown): string | undefined {
  if (typeof error !== "string" || error.length === 0) return undefined;
  return error
    // SEC (P3): strip auth headers / bearer tokens / API keys before anything
    // can reach status, audit, or error output. Key names and values may be
    // JSON-quoted (serialized header dumps), so quotes around the name and
    // separator are tolerated.
    .replace(/\b(authorization|proxy-authorization)\b["']?\s*[:=]\s*["']?(?:bearer\s+|basic\s+)?[^\s"',;)]+/gi, "$1=[redacted]")
    .replace(/\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, "bearer [redacted]")
    .replace(/\b(x-api-key|api[-_]?key|apikey|access[-_]?token|secret[-_]?key)\b["']?\s*[:=]\s*["']?[^\s"',;)]+/gi, "$1=[redacted]")
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[redacted-key]")
    .replace(/\/(?:Users|Volumes|private|var|tmp|Library)\/[^\s"',)]+/g, "[local-path]")
    .replace(/[^\s"',)]+\.fmadapter\b/g, "[adapter]")
    .replace(/[^\s"',)]+\.(?:db|sqlite|sqlite3|faiss|index|plist)\b/g, "[local-artifact]")
    .slice(0, 240);
}



export interface PostJsonOptions {
  timeoutMs?: number;
  headers?: Record<string, string>;
}

export async function postJson<T = unknown>(
  url: string,
  body: unknown,
  opts: PostJsonOptions = {}
): Promise<T> {
  const payload = JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const parsedUrl = new URL(url);
    const client = parsedUrl.protocol === "https:" ? httpsRequest : httpRequest;
    const req = client(
      parsedUrl,
      {
        method: "POST",
        timeout: opts.timeoutMs ?? 30000,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload).toString(),
          ...opts.headers,
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          if (res.statusCode && res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}`));
            return;
          }
          try {
            const parsed = JSON.parse(data) as T;
            resolve(parsed);
          } catch (error) {
            reject(error instanceof Error ? error : new Error(String(error)));
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy(new Error("AFM request timed out"));
    });
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

export function sanitizeAfmHealth(health: JsonResult): JsonResult<Record<string, unknown>> {
  const data: Record<string, unknown> = {};
  for (const key of ["provider", "backend", "availability", "status", "mode"]) {
    const value = stringField(health.data, key);
    if (value) data[key] = value;
  }
  if (healthAdapterConfigured(health) || adapterConfigured()) {
    data.adapterConfigured = true;
  }
  return {
    ok: health.ok,
    data,
    error: safeError(health.error),
  };
}

export function resolveAfmProvider(mode: AfmProviderMode, options: AfmProviderOptions = {}): AfmProviderResolution {
  const helperPath = resolvedNativeHelperPath(options);
  const helperAvailable = nativeHelperAvailable(helperPath);
  const hasAdapter = adapterConfigured() || healthAdapterConfigured(options.health);
  if (mode === "off") {
    return { mode, provider: "off", status: "off", available: false, adapterConfigured: hasAdapter };
  }
  if (mode === "bridge") {
    // Honest health: when the caller already probed the bridge /health endpoint,
    // a failed probe means the bridge is NOT available (was previously always true).
    if (options.health && !options.health.ok) {
      return {
        mode,
        provider: "bridge",
        status: "bridge",
        available: false,
        adapterConfigured: hasAdapter,
        reason: safeError(options.health.error) ?? "bridge health unavailable",
      };
    }
    return { mode, provider: "bridge", status: "bridge", available: true, adapterConfigured: hasAdapter };
  }
  if (options.health && nativeHealthAvailable(options.health)) {
    return {
      mode,
      provider: "native",
      status: "native_available",
      available: true,
      backend: stringField(options.health.data, "backend"),
      availability: stringField(options.health.data, "availability"),
      adapterConfigured: hasAdapter,
    };
  }
  if (options.health && nativeHealthReported(options.health)) {
    const unavailable = {
      mode,
      provider: mode === "auto" ? "bridge" as const : "native" as const,
      status: mode === "auto" ? "bridge" as const : "native_unavailable" as const,
      available: mode === "auto",
      backend: stringField(options.health.data, "backend"),
      availability: stringField(options.health.data, "availability"),
      fallbackUsed: mode === "auto" ? true : undefined,
      adapterConfigured: hasAdapter,
      reason: safeError(options.health.error) ?? "native helper health unavailable",
    };
    return unavailable;
  }
  if (helperAvailable) {
    return { mode, provider: "native", status: "native_available", available: true, adapterConfigured: hasAdapter };
  }
  if (mode === "auto") {
    return {
      mode,
      provider: "bridge",
      status: "bridge",
      available: true,
      fallbackUsed: true,
      adapterConfigured: hasAdapter,
      reason: "native helper unavailable",
    };
  }
  return {
    mode,
    provider: "native",
    status: "native_unavailable",
    available: false,
    adapterConfigured: hasAdapter,
    reason: "native helper unavailable",
  };
}

async function defaultTransport(url: string, payload: Record<string, unknown>, timeoutMs = 45000): Promise<JsonResult> {
  try {
    const parsed = await postJson<any>(url, payload, { timeoutMs });
    return { ok: true, data: parsed };
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("HTTP ")) {
      // The old transport sometimes parsed data even on 4xx, but it's okay to just return error
      return { ok: false, error: error.message };
    }
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

async function callNativeHelper(
  helperPath: string,
  operation: string,
  payload: Record<string, unknown>,
  timeoutMs: number,
): Promise<JsonResult> {
  return new Promise((resolve) => {
    const child = spawn(helperPath, [], { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGTERM");
      resolve({ ok: false, error: "native AFM helper timed out" });
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: false, error: error.message });
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) {
        resolve({ ok: false, error: stderr.trim() || "native AFM helper failed" });
        return;
      }
      try {
        const parsed = JSON.parse(stdout || "{}");
        resolve({ ok: parsed.ok !== false, data: parsed.data ?? parsed, error: parsed.error });
      } catch (error) {
        resolve({ ok: false, error: error instanceof Error ? error.message : String(error) });
      }
    });
    child.stdin.end(JSON.stringify({ schema_version: 1, operation, input: payload }));
  });
}

// G13 (SEC-004): model target allowlist enforcement. Loopback always permitted
// (default in config). Non-loopback hosts only if listed in
// MINNI_AFM_ALLOWED_TARGETS / MINNI_MODEL_ALLOWED_TARGETS (comma sep), and
// non-loopback targets additionally require HTTPS (P3).
// Denial is structured, does not echo the attacker URL in the error payload (no secret leak).
export type ModelTargetDecision =
  | { allowed: true }
  | { allowed: false; reason: "not_allowlisted" | "https_required" | "invalid_url" };

export function checkModelTarget(targetUrl: string): ModelTargetDecision {
  if (!targetUrl) return { allowed: false, reason: "invalid_url" };
  try {
    const u = new URL(targetUrl);
    // WHATWG URL keeps brackets on IPv6 hostnames ("[::1]"); strip them so the
    // loopback/allowlist comparison matches the Python mirror (urlparse strips them).
    const h = (u.hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
    if (!h) return { allowed: false, reason: "invalid_url" };
    // Explicit loopback per G13 requirement (0.0.0.0 / *.localhost kept for dev/mDNS compat; see plan review note)
    if (h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "0.0.0.0" || h.endsWith(".localhost")) {
      return { allowed: true };
    }
    // Call-time union of MINNI_AFM_ALLOWED_TARGETS and MINNI_MODEL_ALLOWED_TARGETS.
    const allowedLower = modelAllowedTargets().map((s) => s.toLowerCase());
    if (!allowedLower.includes(h)) return { allowed: false, reason: "not_allowlisted" };
    if (u.protocol !== "https:") return { allowed: false, reason: "https_required" };
    return { allowed: true };
  } catch {
    return { allowed: false, reason: "invalid_url" };
  }
}

export async function callAfmJson(
  url: string,
  payload: Record<string, unknown>,
  options: CallAfmJsonOptions = {},
): Promise<JsonResult> {
  const provider = resolveAfmProvider(options.mode ?? "bridge", {
    nativeHelperPath: resolvedNativeHelperPath(options),
  });
  if (provider.provider === "off") return { ok: false, error: "AFM mode is off" };
  if (provider.provider === "bridge") {
    const decision = checkModelTarget(url);
    if (!decision.allowed) {
      const host = (() => {
        try {
          return new URL(url).hostname;
        } catch {
          return "invalid";
        }
      })();
      // Structured denial (no full URL in error to avoid leaking internal/attacker-controlled values)
      console.warn(
        `[minni] afm_target_denied host=${host} reason=${decision.reason} (loopback only unless allowlisted via MINNI_AFM_ALLOWED_TARGETS/MINNI_MODEL_ALLOWED_TARGETS; non-loopback requires https)`,
      );
      if (decision.reason === "https_required") {
        return { ok: false, error: "afm_target_denied: non-loopback model targets require https" };
      }
      return { ok: false, error: "afm_target_denied: target is not loopback-only and not explicitly allowlisted by operator config" };
    }
  }
  if (provider.provider === "native") {
    const helperPath = resolvedNativeHelperPath(options);
    if (!helperPath) return { ok: false, error: provider.reason ?? "native helper unavailable" };
    const nativeResult = await callNativeHelper(helperPath, options.operation ?? "json", payload, options.timeoutMs ?? 45000);
    // Cache-poisoning guard: an ok response only counts as a generation proof
    // when it carries actual completion content — the same check the probe
    // applies. A contentless ok (e.g. a non-completion native op) is neutral.
    if (!nativeResult.ok) noteAfmGenerationFailure(url);
    else if (nativeCompletionContent(nativeResult.data) !== undefined) {
      noteAfmGenerationSuccess(url, options.mode ?? "bridge");
    }
    return nativeResult;
  }
  const transport = options.transport ?? ((targetUrl: string, body: Record<string, unknown>) => defaultTransport(targetUrl, body, options.timeoutMs ?? 45000));
  const result = await transport(url, payload);
  // Honest health: a failed live call invalidates the cached generation probe
  // so the next health read re-verifies instead of serving a stale "ok"; a
  // successful live call refreshes the cache (symmetric positive signal —
  // recovery must not wait out a negative TTL) but ONLY when the body carries
  // real completion content. A hollow 2xx ({"status":"ok"} with no choices)
  // proves nothing about generation and must not poison the probe cache.
  if (!result.ok) noteAfmGenerationFailure(url);
  else if (typeof chatCompletionContent(result.data) === "string") {
    noteAfmGenerationSuccess(url, options.mode ?? "bridge");
  }
  return result;
}

// --- Verified generation health (P1: honest health) -------------------------
//
// `ok` is only true when a real 1-token completion has been verified within the
// TTL. /health reachability alone is no longer sufficient (the bridge answered
// /health while generation was dead — the two health lies this replaces).

export interface ProviderHealth {
  ok: boolean;
  reachable: boolean;
  generationVerified: boolean;
  probeAgeMs: number;
  detail?: string;
}

export interface AfmGenerationProbeOptions {
  mode?: AfmProviderMode;
  chatUrl?: string;
  model?: string;
  timeoutMs?: number;
  ttlMs?: number;
  /** Pre-fetched /health result; a failed health probe skips the generation call. */
  health?: JsonResult;
  transport?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult>;
  nativeHelperPath?: string;
  now?: () => number;
}

interface GenerationProbeEntry {
  reachable: boolean;
  generationVerified: boolean;
  detail?: string;
  probedAt: number;
}

export const GENERATION_PROBE_TTL_MS = 5 * 60 * 1000;

/**
 * Mirror of afm_provider._generation_probe_timeout_seconds
 * (src/minni/afm_provider.py:99-106): read MINNI_AFM_PROBE_TIMEOUT at CALL
 * TIME, not bound once at module load. Native mode spawns a fresh helper that
 * cold-loads FoundationModels on the first call (~2-3s measured; warm calls
 * ~0.5s); the previous hardcoded 1500ms plugin-side budget (a quarter of the
 * daemon's matching 10s default) clipped that cold start and produced
 * structurally guaranteed false negatives even while the daemon's own probe,
 * run with its real budget, verified generation fine (punch-list §3).
 * Default 10.0s; unset/invalid/non-positive values fall back to 10.0s.
 */
export function generationProbeTimeoutMs(): number {
  const raw = process.env.MINNI_AFM_PROBE_TIMEOUT ?? "10.0";
  const seconds = Number(raw);
  const resolved = Number.isFinite(seconds) && seconds > 0 ? seconds : 10.0;
  return resolved * 1000;
}

const generationProbeCache = new Map<string, GenerationProbeEntry>();
const generationProbeInFlight = new Map<string, Promise<GenerationProbeEntry>>();

function generationProbeKey(options: AfmGenerationProbeOptions): string {
  return `${options.mode ?? "bridge"}|${options.chatUrl ?? AFM_PREPARE_TASK_URL}`;
}

// --- cross-process persistent probe cache (L2) -------------------------------
//
// SessionStart hooks (hook.ts, codex-hook.ts, grok-hook.ts, kilocode-hook.ts)
// run as fresh short-lived node processes, so the in-memory Maps above (L1)
// never help them: without an L2 every session boot pays a full live 1-token
// generation probe. A small atomic-write JSON file under ~/.minni/run/ (same
// schema in engine/afm_provider.py) lets a hook reuse a recent verified probe.
// Trivial versioned schema; any read/parse problem degrades to "no cache".
const PROBE_CACHE_FILE_VERSION = 1;

interface PersistedProbeEntry {
  reachable: boolean;
  generation_verified: boolean;
  detail?: string | null;
  probed_at_ms: number;
}

/** MINNI_AFM_PROBE_CACHE override exists so tests never touch live ~/.minni. */
function probeCacheFilePath(): string {
  return process.env.MINNI_AFM_PROBE_CACHE ?? path.join(minniHome(), "run", "afm-probe-cache.json");
}

function readPersistentProbeEntries(): Record<string, PersistedProbeEntry> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(probeCacheFilePath(), "utf8"));
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== "object") return {};
  const file = parsed as { version?: unknown; entries?: unknown };
  if (file.version !== PROBE_CACHE_FILE_VERSION || !file.entries || typeof file.entries !== "object") return {};
  const valid: Record<string, PersistedProbeEntry> = {};
  for (const [key, value] of Object.entries(file.entries as Record<string, unknown>)) {
    const entry = value as PersistedProbeEntry | undefined;
    if (
      entry
      && typeof entry === "object"
      && typeof entry.reachable === "boolean"
      && typeof entry.generation_verified === "boolean"
      && typeof entry.probed_at_ms === "number"
    ) {
      valid[key] = entry;
    }
  }
  return valid;
}

function writePersistentProbeEntries(entries: Record<string, PersistedProbeEntry>): void {
  // Best-effort: persistence must never break the health path.
  try {
    const filePath = probeCacheFilePath();
    mkdirSync(path.dirname(filePath), { recursive: true });
    const tempPath = `${filePath}.${process.pid}.${Math.random().toString(36).slice(2)}.tmp`;
    writeFileSync(tempPath, JSON.stringify({ version: PROBE_CACHE_FILE_VERSION, entries }), { mode: 0o600 });
    renameSync(tempPath, filePath);
  } catch {
    /* persistence is an optimization, never a failure */
  }
}

function persistProbeMutation(mutate: (entries: Record<string, PersistedProbeEntry>) => void): void {
  const entries = readPersistentProbeEntries();
  mutate(entries);
  writePersistentProbeEntries(entries);
}

function toPersistedEntry(entry: GenerationProbeEntry): PersistedProbeEntry {
  return {
    reachable: entry.reachable,
    generation_verified: entry.generationVerified,
    detail: entry.detail ?? null,
    probed_at_ms: entry.probedAt,
  };
}

function loadPersistedProbeEntry(key: string): GenerationProbeEntry | undefined {
  const entry = readPersistentProbeEntries()[key];
  if (!entry) return undefined;
  return {
    reachable: entry.reachable,
    generationVerified: entry.generation_verified,
    detail: typeof entry.detail === "string" ? entry.detail : undefined,
    probedAt: entry.probed_at_ms,
  };
}

export function resetAfmGenerationProbeCache(): void {
  generationProbeCache.clear();
  generationProbeInFlight.clear();
  try {
    rmSync(probeCacheFilePath(), { force: true });
  } catch {
    /* best-effort */
  }
}

/** Invalidate cached generation probes (all entries, or those for one chat URL). */
export function noteAfmGenerationFailure(chatUrl?: string): void {
  if (!chatUrl) {
    generationProbeCache.clear();
    persistProbeMutation((entries) => {
      for (const key of Object.keys(entries)) delete entries[key];
    });
    return;
  }
  for (const key of [...generationProbeCache.keys()]) {
    if (key.endsWith(`|${chatUrl}`)) generationProbeCache.delete(key);
  }
  persistProbeMutation((entries) => {
    for (const key of Object.keys(entries)) {
      if (key.endsWith(`|${chatUrl}`)) delete entries[key];
    }
  });
}

/**
 * Upsert a verified probe entry after a successful live call — the call IS a
 * generation proof. Also flips any cached negative entries for the same chat
 * URL so a recovered bridge does not stay afm_ok=false for the rest of the TTL
 * (symmetric counterpart of noteAfmGenerationFailure).
 */
export function noteAfmGenerationSuccess(chatUrl: string, mode: AfmProviderMode = "bridge"): void {
  const entry: GenerationProbeEntry = { reachable: true, generationVerified: true, probedAt: Date.now() };
  generationProbeCache.set(`${mode}|${chatUrl}`, entry);
  for (const key of [...generationProbeCache.keys()]) {
    if (key.endsWith(`|${chatUrl}`)) generationProbeCache.set(key, { ...entry });
  }
  persistProbeMutation((entries) => {
    entries[`${mode}|${chatUrl}`] = toPersistedEntry(entry);
    for (const key of Object.keys(entries)) {
      if (key.endsWith(`|${chatUrl}`)) entries[key] = toPersistedEntry(entry);
    }
  });
}

function chatCompletionContent(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const choices = (data as { choices?: unknown }).choices;
  if (!Array.isArray(choices) || choices.length === 0) return undefined;
  const message = (choices[0] as { message?: unknown } | undefined)?.message;
  if (!message || typeof message !== "object") return undefined;
  const content = (message as { content?: unknown }).content;
  return typeof content === "string" ? content : undefined;
}

/**
 * Native helpers answer chat_completion probes with either a chat-shaped body
 * or a flat {answer|content|text} field. An ok response with neither carries
 * no proof of generation and must not flip afm_ok=true.
 */
function nativeCompletionContent(data: unknown): string | undefined {
  const chat = chatCompletionContent(data);
  if (chat) return chat;
  for (const key of ["answer", "content", "text"]) {
    const value = stringField(data, key);
    if (value) return value;
  }
  return undefined;
}

async function runGenerationProbe(options: AfmGenerationProbeOptions, now: () => number): Promise<GenerationProbeEntry> {
  const mode = options.mode ?? "bridge";
  if (mode === "off") {
    return { reachable: false, generationVerified: false, detail: "AFM mode is off", probedAt: now() };
  }
  if (options.health && !options.health.ok) {
    return {
      reachable: false,
      generationVerified: false,
      detail: safeError(options.health.error) ?? "AFM health unreachable",
      probedAt: now(),
    };
  }
  const chatUrl = options.chatUrl ?? AFM_PREPARE_TASK_URL;
  const body = {
    model: options.model ?? AFM_PREPARE_TASK_MODEL,
    temperature: 0,
    max_tokens: 1,
    messages: [{ role: "user", content: "ok" }],
  };
  // Only forward nativeHelperPath when the caller set it, so an unset option
  // does not mask the MINNI_AFM_NATIVE_HELPER env fallback inside callAfmJson.
  const helperOptions = Object.prototype.hasOwnProperty.call(options, "nativeHelperPath")
    ? { nativeHelperPath: options.nativeHelperPath }
    : {};
  // Resolve the provider first so auto mode wraps the native envelope exactly
  // like an explicit native probe (mirror of afm_provider._run_generation_probe).
  const usesNative = resolveAfmProvider(mode, helperOptions).provider === "native";
  const payload = usesNative ? { payload: body } : body;
  const result = await callAfmJson(chatUrl, payload, {
    mode,
    operation: "chat_completion",
    timeoutMs: options.timeoutMs ?? generationProbeTimeoutMs(),
    transport: options.transport,
    ...helperOptions,
  });
  const content = usesNative ? nativeCompletionContent(result.data) : chatCompletionContent(result.data);
  // Native mode no longer auto-verifies on any ok response: both paths must
  // produce actual completion content before afm_ok may flip true.
  const generationVerified = result.ok && typeof content === "string";
  const reachable =
    result.ok || (typeof result.error === "string" && result.error.startsWith("HTTP ")) || options.health?.ok === true;
  return {
    reachable,
    generationVerified,
    detail: generationVerified
      ? undefined
      : safeError(result.error) ?? (result.ok ? "generation probe returned no completion content" : "generation probe failed"),
    probedAt: now(),
  };
}

function toProviderHealth(entry: GenerationProbeEntry, now: () => number): ProviderHealth {
  return {
    ok: entry.generationVerified,
    reachable: entry.reachable,
    generationVerified: entry.generationVerified,
    probeAgeMs: Math.max(0, now() - entry.probedAt),
    detail: entry.detail,
  };
}

/**
 * Convert a daemon-sourced `data.afm` block (the "status" RPC's embedded
 * afm_runtime_status() result — src/minni/afm_provider.py:376-421, wired in
 * via minnid_runtime/health.py:156-189) into a ProviderHealth the plugin can
 * reuse directly, instead of spawning a second, independently-timed probe
 * (Health Endpoint Aggregation: a downstream aggregator consumes the
 * dependency's own fresh health summary rather than re-probing it
 * transitively). This is what closes punch-list §3: the daemon already ran
 * afm_runtime_status() with its real (matching) probe budget, so the plugin
 * no longer needs its own cold, budget-mismatched subprocess spawn to answer
 * the same question the daemon just answered.
 *
 * The daemon RPC payload is UNTRUSTED input (same posture as sanitizeAfmHealth
 * / safeError applied elsewhere to raw AFM bodies): every field is validated
 * with strict `typeof` narrowing, never a truthy cast, so a malformed or
 * corrupted daemon response degrades to "no reuse" rather than being coerced
 * into a false verdict. Returns undefined (falls back to the plugin's own
 * probe) when:
 *   - `data` isn't a well-shaped afm_runtime_status object (also covers
 *     "afm field ABSENT in socket.data", which back-compat requires behave
 *     identically to "socket unreachable" — see afm-health.test.mjs);
 *   - `data.mode` doesn't match the plugin's own resolved provider (avoid
 *     adopting a native verdict when the plugin resolved bridge, or vice
 *     versa — the daemon and the plugin process can have different env/
 *     helper availability);
 *   - the verdict is bridge-effective but the daemon's declared probe target
 *     (`probe_url`/`probe_model`) is absent or differs from this plugin's own
 *     configured AFM_PREPARE_TASK_URL/MODEL — the daemon verified a chat-
 *     completions endpoint this process would not actually call, so its
 *     afm.ok says nothing about the target the plugin uses (not comparable →
 *     fall back to the plugin's own probe);
 *   - the daemon's probe is stale (probe_age_ms >= ttlMs). probe_age_ms is
 *     computed against the daemon's own clock; on a single local-machine
 *     daemon (today's deployment) that's directly comparable to the plugin's
 *     clock with no correction. A future networked-daemon change must not
 *     reuse this function unmodified without adding clock-skew handling.
 */
export function daemonAfmToProviderHealth(
  data: unknown,
  expectedProvider: AfmProvider,
  ttlMs: number,
  now: () => number,
  configuredMode?: AfmProviderMode,
  expectedProbeUrl: string = AFM_PREPARE_TASK_URL,
  expectedProbeModel: string = AFM_PREPARE_TASK_MODEL,
): ProviderHealth | undefined {
  if (!data || typeof data !== "object") return undefined;
  const record = data as Record<string, unknown>;
  // Review r1 (P2): afm_runtime_status() reports the RESOLVED MODE, which in
  // auto installs is the literal string "auto" — never "native"/"bridge".
  // Strict equality against the plugin's resolved provider would reject every
  // fresh auto-mode daemon verdict and reintroduce the redundant cold probe.
  // Derive the daemon's effective provider: explicit native/bridge modes map
  // to themselves; auto maps via `status` ("native_available" → native,
  // "bridge"/"fallback_used" → bridge). Anything else stays unmatched (fail
  // to "no reuse", the untrusted-input posture).
  let daemonEffective: AfmProvider | undefined;
  if (record.mode === "native" || record.mode === "bridge" || record.mode === "off") {
    daemonEffective = record.mode;
  } else if (record.mode === "auto") {
    if (record.status === "native_available") daemonEffective = "native";
    else if (record.status === "bridge" || record.status === "fallback_used") daemonEffective = "bridge";
  }
  // Review r2 (P2): in configured auto mode the DAEMON is the authority on the
  // effective provider — the plugin's local resolveAfmProvider() can guess
  // "native" from helper presence alone while the daemon has already verified
  // that FoundationModels is unavailable and fallen back to bridge
  // (mode:"auto", status:"fallback_used"). Rejecting that fresh verdict on a
  // strict provider match would re-run the redundant cold native probe every
  // status/SessionStart. So: when this process's configured mode is "auto",
  // any well-derived daemon effective provider is acceptable; explicit modes
  // still require an exact match (never adopt a bridge verdict when the
  // operator pinned native, or vice versa).
  if (daemonEffective === undefined) return undefined;
  if (configuredMode !== "auto" && daemonEffective !== expectedProvider) return undefined;
  // Review r4 (P2): reusing the daemon's verdict is only sound when the daemon
  // probed the SAME target this plugin will actually call. For a bridge-
  // effective verdict that target is an HTTP chat-completions endpoint the
  // operator can repoint via MINNI_AFM_PREPARE_TASK_URL / _MODEL; a daemon that
  // verified a DIFFERENT url/model says nothing about the endpoint this plugin
  // uses, so adopting its afm.ok would report a target this process cannot
  // reach as healthy. Require the daemon to declare its probe target and match
  // it exactly, else fall back to the plugin's own probe (native has no
  // configurable HTTP target, so this gate is bridge-only). A daemon that omits
  // the target (older build) is "not comparable" → also fall back.
  if (daemonEffective === "bridge") {
    if (typeof record.probe_url !== "string" || record.probe_url !== expectedProbeUrl) return undefined;
    if (typeof record.probe_model !== "string" || record.probe_model !== expectedProbeModel) return undefined;
  }
  const generationVerified = record.generation_verified;
  const reachable = record.reachable;
  const probeAgeMs = record.probe_age_ms;
  if (typeof generationVerified !== "boolean") return undefined;
  if (typeof reachable !== "boolean") return undefined;
  if (typeof probeAgeMs !== "number" || !Number.isFinite(probeAgeMs) || probeAgeMs < 0) return undefined;
  if (probeAgeMs >= ttlMs) return undefined;
  void now; // reserved for a future clock-skew correction; not needed for a local daemon.
  return {
    ok: generationVerified,
    reachable,
    generationVerified,
    probeAgeMs,
    detail: typeof record.error === "string" ? record.error : undefined,
  };
}

/**
 * Verified AFM provider health with a ~5 min probe cache.
 * - Fresh cache entry: served directly (SessionStart stays fast).
 * - Stale entry: served stale while a background re-probe refreshes the cache
 *   (stale-while-revalidate); failed live calls invalidate via
 *   noteAfmGenerationFailure().
 * - No entry: probes synchronously with a short timeout.
 */
export async function getAfmProviderHealth(options: AfmGenerationProbeOptions = {}): Promise<ProviderHealth> {
  const now = options.now ?? Date.now;
  const key = generationProbeKey(options);
  const ttlMs = options.ttlMs ?? GENERATION_PROBE_TTL_MS;
  let cached = generationProbeCache.get(key);
  if (!cached) {
    // L2: a fresh process (SessionStart hook) reuses a recent probe persisted
    // by another process instead of paying a live generation call every boot.
    // Stale or missing/corrupt file entries fall through to a normal probe.
    const persisted = loadPersistedProbeEntry(key);
    if (persisted && Math.max(0, now() - persisted.probedAt) < ttlMs) {
      generationProbeCache.set(key, persisted);
      cached = persisted;
    }
  }
  if (cached) {
    const ageMs = Math.max(0, now() - cached.probedAt);
    if (ageMs >= ttlMs && !generationProbeInFlight.has(key)) {
      const refresh = runGenerationProbe(options, now)
        .then((entry) => {
          generationProbeCache.set(key, entry);
          persistProbeMutation((entries) => {
            entries[key] = toPersistedEntry(entry);
          });
          return entry;
        })
        .catch(() => cached as GenerationProbeEntry)
        .finally(() => {
          generationProbeInFlight.delete(key);
        });
      generationProbeInFlight.set(key, refresh);
    }
    return toProviderHealth(cached, now);
  }
  let probe = generationProbeInFlight.get(key);
  if (!probe) {
    probe = runGenerationProbe(options, now)
      .then((entry) => {
        generationProbeCache.set(key, entry);
        persistProbeMutation((entries) => {
          entries[key] = toPersistedEntry(entry);
        });
        return entry;
      })
      .finally(() => {
        generationProbeInFlight.delete(key);
      });
    generationProbeInFlight.set(key, probe);
  }
  const entry = await probe;
  return toProviderHealth(entry, now);
}
