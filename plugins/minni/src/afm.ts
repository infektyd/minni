import { existsSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { spawn } from "node:child_process";
import { URL } from "node:url";

import type { JsonResult } from "./sovereign.js";
// G13: single source for allowlist (fixes duplication noted in review)
import { AFM_PREPARE_TASK_MODEL, AFM_PREPARE_TASK_URL, modelAllowedTargets } from "./config.js";

export type AfmProviderMode = "auto" | "bridge" | "native" | "off";
export type AfmProvider = "bridge" | "native" | "off";

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

function resolvedNativeHelperPath(options: AfmProviderOptions): string | undefined {
  return Object.prototype.hasOwnProperty.call(options, "nativeHelperPath")
    ? options.nativeHelperPath
    : process.env.MINNI_AFM_NATIVE_HELPER;
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

function safeError(error: unknown): string | undefined {
  if (typeof error !== "string" || error.length === 0) return undefined;
  return error
    // SEC (P3): strip auth headers / bearer tokens / API keys before anything
    // can reach status, audit, or error output.
    .replace(/\b(authorization|proxy-authorization)\b\s*[:=]\s*(?:bearer\s+|basic\s+)?[^\s"',;)]+/gi, "$1=[redacted]")
    .replace(/\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, "bearer [redacted]")
    .replace(/\b(x-api-key|api[-_]?key|apikey|access[-_]?token|secret[-_]?key)\b\s*[:=]\s*[^\s"',;)]+/gi, "$1=[redacted]")
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
  if (options.health) {
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
    const h = (u.hostname || "").toLowerCase();
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
    if (!nativeResult.ok) noteAfmGenerationFailure(url);
    return nativeResult;
  }
  const transport = options.transport ?? ((targetUrl: string, body: Record<string, unknown>) => defaultTransport(targetUrl, body, options.timeoutMs ?? 45000));
  const result = await transport(url, payload);
  // Honest health: a failed live call invalidates the cached generation probe
  // so the next health read re-verifies instead of serving a stale "ok".
  if (!result.ok) noteAfmGenerationFailure(url);
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

const GENERATION_PROBE_TTL_MS = 5 * 60 * 1000;
const GENERATION_PROBE_TIMEOUT_MS = 1500;
const generationProbeCache = new Map<string, GenerationProbeEntry>();
const generationProbeInFlight = new Map<string, Promise<GenerationProbeEntry>>();

function generationProbeKey(options: AfmGenerationProbeOptions): string {
  return `${options.mode ?? "bridge"}|${options.chatUrl ?? AFM_PREPARE_TASK_URL}`;
}

export function resetAfmGenerationProbeCache(): void {
  generationProbeCache.clear();
  generationProbeInFlight.clear();
}

/** Invalidate cached generation probes (all entries, or those for one chat URL). */
export function noteAfmGenerationFailure(chatUrl?: string): void {
  if (!chatUrl) {
    generationProbeCache.clear();
    return;
  }
  for (const key of [...generationProbeCache.keys()]) {
    if (key.endsWith(`|${chatUrl}`)) generationProbeCache.delete(key);
  }
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
  const payload = mode === "native" ? { payload: body } : body;
  const result = await callAfmJson(chatUrl, payload, {
    mode,
    operation: "chat_completion",
    timeoutMs: options.timeoutMs ?? GENERATION_PROBE_TIMEOUT_MS,
    transport: options.transport,
    nativeHelperPath: options.nativeHelperPath,
  });
  const content = chatCompletionContent(result.data);
  const generationVerified = result.ok && (mode === "native" ? true : typeof content === "string");
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
  const cached = generationProbeCache.get(key);
  if (cached) {
    const ageMs = Math.max(0, now() - cached.probedAt);
    if (ageMs >= ttlMs && !generationProbeInFlight.has(key)) {
      const refresh = runGenerationProbe(options, now)
        .then((entry) => {
          generationProbeCache.set(key, entry);
          return entry;
        })
        .catch(() => cached)
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
