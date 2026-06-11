// Model provider protocol for Minni plugin call sites.
//
// Two-language mirrored boundary (precedent: engine/test_g03_contract_matrix.py):
// this module mirrors engine/model_provider.py with identical semantics. Call
// sites (task.ts, team-harvest.ts) route through a ProviderChain instead of
// talking to the AFM transport directly, so additional local providers (mlx,
// ollama) and an explicitly-gated cloud tier can be added later (P4-P6)
// without touching the call sites again.
//
// Deliberately daemon-free: these call sites must keep working when the daemon
// is down, so the chain wraps the same in-process AFM bridge+native transport
// that afm.ts already owns. With the default AFM-only chain the wire behavior
// is byte-identical to the P0 golden contracts.

import {
  callAfmJson,
  getAfmProviderHealth,
  resolveAfmMode,
  safeError,
  type AfmProviderMode,
  type ProviderHealth,
} from "./afm.js";
import { AFM_PREPARE_TASK_URL, loadProvidersConfig, type ProvidersConfig } from "./config.js";
import type { JsonResult } from "./sovereign.js";

export type OperationClass = "retrieval" | "prepare" | "extraction";
export type ProviderTier = "local" | "cloud";

const OPERATION_CLASSES = new Set<OperationClass>(["retrieval", "prepare", "extraction"]);

export interface ChatRequest {
  /** OpenAI-compatible chat-completions body — shape frozen by the P0 goldens. */
  payload: Record<string, unknown>;
  operation: OperationClass;
  url?: string;
  timeoutMs?: number;
  /** Optional JSON schema the caller expects the completion to satisfy. */
  responseSchema?: Record<string, unknown>;
  /**
   * AFM native-helper escape hatch: when the AFM provider runs in native mode
   * it sends nativePayload (default: payload) verbatim under this operation
   * instead of re-shaping the chat body.
   */
  nativeOperation?: string;
  nativePayload?: Record<string, unknown>;
  mode?: AfmProviderMode;
  transport?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult>;
}

export interface ProviderChatResult extends JsonResult {
  provider: string;
}

export interface ModelProvider {
  readonly name: string;
  readonly tier: ProviderTier;
  supports(operation: OperationClass): boolean;
  chat(request: ChatRequest): Promise<ProviderChatResult>;
  health?(): Promise<ProviderHealth>;
}

/**
 * Wraps the existing AFM bridge+native transport as the first provider.
 * Wire behavior is identical to calling callAfmJson directly (P0 goldens).
 */
export class AfmProvider implements ModelProvider {
  readonly name = "afm";
  readonly tier: ProviderTier = "local";

  constructor(private readonly mode?: AfmProviderMode) {}

  supports(operation: OperationClass): boolean {
    return OPERATION_CLASSES.has(operation);
  }

  async chat(request: ChatRequest): Promise<ProviderChatResult> {
    // Mode is resolved BEFORE choosing the payload so auto mode sends the
    // native envelope to the native helper (mirror of model_provider.py).
    // Unset modes consult MINNI_AFM_PROVIDER_MODE / MINNI_AFM_MODE exactly
    // like the Python mirror's resolve_afm_mode(None).
    const mode = resolveAfmMode(request.mode ?? this.mode);
    const url = request.url ?? AFM_PREPARE_TASK_URL;
    if (mode === "off") {
      return { ok: false, provider: this.name, error: "AFM mode is off" };
    }
    if (mode === "native" || mode === "auto") {
      // Identical native semantics to afm_provider.afm_chat_completion: the
      // operation defaults to chat_completion and a bare chat body is wrapped
      // as {payload: ...} for that default operation.
      const operation = request.nativeOperation ?? "chat_completion";
      const nativePayload =
        request.nativePayload
          ?? (request.nativeOperation === undefined ? { payload: request.payload } : request.payload);
      const native = await callAfmJson(url, nativePayload, {
        mode: "native",
        operation,
        timeoutMs: request.timeoutMs,
        transport: request.transport,
      });
      if (native.ok || mode === "native") {
        return { ...native, provider: this.name };
      }
    }
    const result = await callAfmJson(url, request.payload, {
      mode: "bridge",
      timeoutMs: request.timeoutMs,
      transport: request.transport,
    });
    return { ...result, provider: this.name };
  }

  async health(): Promise<ProviderHealth> {
    return getAfmProviderHealth({ mode: resolveAfmMode(this.mode) });
  }
}

export interface OperationPolicy {
  localOnly?: boolean;
}

/**
 * SEC (P3): secret hygiene is structural, not per-call-site. Every non-ok
 * result leaving the chain has its error passed through safeError so future
 * providers (P4-P6 cloud SDKs routinely embed auth headers / key-bearing URLs
 * in exception text) cannot leak secrets into audit/inbox/status surfaces.
 */
function sanitizeChainResult(result: ProviderChatResult): ProviderChatResult {
  if (result.ok || typeof result.error !== "string") return result;
  const sanitized = safeError(result.error);
  return sanitized === result.error ? result : { ...result, error: sanitized };
}

export class ProviderChain {
  // Public readonly like the Python mirror (model_provider.ProviderChain).
  constructor(
    readonly providers: ModelProvider[],
    readonly operations: Partial<Record<OperationClass, OperationPolicy>> = {},
  ) {}

  providersFor(operation: OperationClass): ModelProvider[] {
    const policy = this.operations[operation] ?? {};
    return this.providers.filter(
      (provider) => provider.supports(operation) && !(policy.localOnly && provider.tier !== "local"),
    );
  }

  async chat(request: ChatRequest): Promise<ProviderChatResult> {
    const eligible = this.providersFor(request.operation);
    if (eligible.length === 0) {
      return {
        ok: false,
        provider: "none",
        error: `no provider eligible for operation ${request.operation}`,
      };
    }
    let last: ProviderChatResult | undefined;
    for (const provider of eligible) {
      last = await provider.chat(request);
      if (last.ok) return last;
    }
    return sanitizeChainResult(last as ProviderChatResult);
  }
}

/**
 * Build the configured provider chain. P0-P3: the AFM provider is the only
 * implemented backend; configured mlx/ollama/cloud entries (P4-P6) are parsed
 * by config but skipped here until their transports exist. Retrieval defaults
 * to local-only.
 */
// Default config is read at CALL time (not the import-time PROVIDERS_CONFIG
// snapshot) so a long-lived plugin process observes providers.json edits —
// mirror of model_provider.default_provider_chain, which re-reads per call.
export function defaultProviderChain(config: ProvidersConfig = loadProvidersConfig()): ProviderChain {
  const operations: Partial<Record<OperationClass, OperationPolicy>> = {
    retrieval: { localOnly: true },
  };
  for (const [name, policy] of Object.entries(config.operations ?? {})) {
    if (OPERATION_CLASSES.has(name as OperationClass) && policy && typeof policy === "object") {
      // Secure default: retrieval stays localOnly unless EXPLICITLY set false.
      // {"operations":{"retrieval":{}}} must not flip retrieval cloud-eligible.
      const localOnly =
        name === "retrieval"
          ? (policy as OperationPolicy).localOnly !== false
          : Boolean((policy as OperationPolicy).localOnly);
      operations[name as OperationClass] = { localOnly };
    }
  }
  const providers: ModelProvider[] = [];
  for (const name of config.chain ?? ["afm"]) {
    if (name === "afm") providers.push(new AfmProvider());
    // mlx/ollama/cloud transports land in P4-P6; until then they are skipped.
  }
  if (providers.length === 0) providers.push(new AfmProvider());
  return new ProviderChain(providers, operations);
}
