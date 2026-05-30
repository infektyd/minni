import { existsSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { spawn } from "node:child_process";
import { URL } from "node:url";

import type { JsonResult } from "./sovereign.js";
import { AFM_ALLOWED_TARGETS } from "./config.js";  // G13: single source for allowlist (fixes duplication noted in review)

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

async function defaultTransport(url: string, payload: Record<string, unknown>): Promise<JsonResult> {
  try {
    const parsed = await postJson<any>(url, payload, { timeoutMs: 45000 });
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

// G13 (SEC-004): AFM URL allowlist enforcement. Loopback always permitted (default in config).
// Non-loopback hosts only if listed in MINNI_AFM_ALLOWED_TARGETS (comma sep).
// Denial is structured, does not echo the attacker URL in the error payload (no secret leak).
function isAfmTargetAllowed(targetUrl: string): boolean {
  if (!targetUrl) return false;
  try {
    const u = new URL(targetUrl);
    const h = (u.hostname || "").toLowerCase();
    if (!h) return false;
    // Explicit loopback per G13 requirement (0.0.0.0 / *.localhost kept for dev/mDNS compat; see plan review note)
    if (h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "0.0.0.0" || h.endsWith(".localhost")) {
      return true;
    }
    // Use the canonical parsed list from config (fixes duplication / dead-code noted in reviews)
    const allowedLower = AFM_ALLOWED_TARGETS.map((s) => s.toLowerCase());
    return allowedLower.includes(h);
  } catch {
    return false;
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
    if (!isAfmTargetAllowed(url)) {
      const host = (() => {
        try {
          return new URL(url).hostname;
        } catch {
          return "invalid";
        }
      })();
      // Structured denial (no full URL in error to avoid leaking internal/attacker-controlled values)
      console.warn(`[minni] afm_target_denied host=${host} (not loopback and not in MINNI_AFM_ALLOWED_TARGETS)`);
      return { ok: false, error: "afm_target_denied: target is not loopback-only and not explicitly allowlisted by operator config" };
    }
  }
  if (provider.provider === "native") {
    const helperPath = resolvedNativeHelperPath(options);
    if (!helperPath) return { ok: false, error: provider.reason ?? "native helper unavailable" };
    return callNativeHelper(helperPath, options.operation ?? "json", payload, options.timeoutMs ?? 45000);
  }
  const transport = options.transport ?? defaultTransport;
  return transport(url, payload);
}
