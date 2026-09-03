import os from "node:os";

/** Drain-queue default. Omitting status used to SELECT every status. */
export const LIST_CANDIDATES_DEFAULT_STATUS = "proposed";

/** Terminal statuses whose content is not cleared on resolve. The hide rule
 *  is any non-proposed status — this set is the documented subset. */
export const MODEL_HIDDEN_CANDIDATE_STATUSES = new Set([
  "redacted",
  "rejected",
  "log_only",
  "do_not_store",
  "accepted",
  "merged",
  "superseded",
  "expired",
]);

export function isModelHiddenCandidateStatus(status: string | undefined): boolean {
  return (
    typeof status === "string" &&
    (MODEL_HIDDEN_CANDIDATE_STATUSES.has(status) ||
      status !== LIST_CANDIDATES_DEFAULT_STATUS)
  );
}

const MODEL_CANDIDATE_KEYS = [
  "candidate_id",
  "status",
  "proposed_at",
  "instruction_like",
  "layer",
  "privacy_level",
  "workspace_id",
  "content",
] as const;

export function drainStatusForModel(status: string | undefined): string {
  const trimmed = typeof status === "string" ? status.trim() : "";
  return trimmed || LIST_CANDIDATES_DEFAULT_STATUS;
}

/** Model-surface redaction. Ports the daemon `redact_value` secret classes
 *  (JSON-quoted credentials, bare provider tokens, PEM blocks) so MCP output
 *  to cloud models is covered the same way. Naming split is deliberate and
 *  covered by tests: this surface emits `[local-path]` / `key=[REDACTED]` /
 *  `[REDACTED]` where the daemon emits `[REDACTED_PATH]` / `key=[REDACTED]` /
 *  `[REDACTED]` — same redaction, different path token. */
export function redactLocalValue(value: unknown): unknown {
  if (typeof value === "string") {
    const home = os.homedir().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return value
      .replace(/\/Users\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/home\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/Volumes\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/private\/[^\s"',)]+/g, "[local-path]")
      .replace(new RegExp(home + "[^\\s\"',)]*", "g"), "[local-path]")
      .replace(/\/tmp\/sov(?:ereign|rd)\.sock\b/g, "[local-path]")
      .replace(/[^\s"',)]+\.fmadapter\b/g, "[local-path]")
      .replace(
        /\b(api[_-]?key|password|secret|credential|private[_ -]?key|bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*[^\s,;<>"']+/gi,
        (_match, key: string) => `${key}=[REDACTED]`,
      )
      .replace(
        /("?)(api[_-]?key|password|secret|credential|private[_ -]?key)\1\s*:\s*"[^"]+"/gi,
        (_match, _quote: string, key: string) => `${key}=[REDACTED]`,
      )
      .replace(
        /("?)(bearer|access[_-]?token|refresh[_-]?token|token)\1\s*:\s*"[^"]+"/gi,
        (_match, _quote: string, key: string) => `${key}=[REDACTED]`,
      )
      .replace(/\bsk-[A-Za-z0-9_-]{16,}\b/g, "[REDACTED]")
      .replace(/\bgh[pousr]_[A-Za-z0-9_]{20,}\b/g, "[REDACTED]")
      .replace(/\bgithub_pat_[A-Za-z0-9_]{20,}\b/g, "[REDACTED]")
      .replace(/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED]")
      .replace(/\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g, "[REDACTED]")
      .replace(
        /-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----/gs,
        "[REDACTED]",
      );
  }
  if (Array.isArray(value)) return value.map(redactLocalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, redactLocalValue(item)]),
    );
  }
  return value;
}

/** Shared-gate failures return from MCP before jsonRpc. Redact the same
 *  as daemon JsonResult so socket/db paths never reach the model. */
export function modelSharedGatePayload(
  payload: Record<string, unknown>,
): Record<string, unknown> {
  return redactLocalValue(payload) as Record<string, unknown>;
}

function projectModelCandidate(row: unknown): Record<string, unknown> {
  const obj = row && typeof row === "object" ? (row as Record<string, unknown>) : {};
  const projected: Record<string, unknown> = {};
  for (const key of MODEL_CANDIDATE_KEYS) {
    if (key in obj) projected[key] = obj[key];
  }
  return projected;
}

function candidatesFromRpc(rpc: unknown): { envelope: Record<string, unknown>; candidates: unknown[] } {
  const root = rpc && typeof rpc === "object" ? (rpc as Record<string, unknown>) : {};
  const data =
    root.data && typeof root.data === "object" ? (root.data as Record<string, unknown>) : root;
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  return { envelope: data, candidates };
}

/**
 * Model-facing list_candidates view: redact, drop SELECT * internals
 * (evidence_refs / derived_from / paths), and never return non-proposed
 * packet content even if the caller asked for those statuses.
 */
export function modelListCandidatesPayload(rpc: unknown, requestedStatus: string): Record<string, unknown> {
  const root = rpc && typeof rpc === "object" ? (rpc as Record<string, unknown>) : {};
  if (root.ok === false) {
    return redactLocalValue({
      ok: false,
      error: root.error,
    }) as Record<string, unknown>;
  }

  if (isModelHiddenCandidateStatus(requestedStatus)) {
    return {
      ok: true,
      hidden: true,
      status: requestedStatus,
      candidates: [],
      count: 0,
      total: 0,
      has_more: false,
      reason: "only proposed candidate content is returned to the model",
    };
  }

  const { envelope, candidates: raw } = candidatesFromRpc(rpc);
  const candidates = raw
    .filter((row) => {
      const status =
        row && typeof row === "object" ? (row as Record<string, unknown>).status : undefined;
      return status === LIST_CANDIDATES_DEFAULT_STATUS;
    })
    .map(projectModelCandidate);

  return redactLocalValue({
    ok: root.ok !== false,
    error: root.error,
    principal: envelope.principal,
    status: envelope.status ?? requestedStatus,
    candidates,
    count: candidates.length,
    total: envelope.total,
    has_more: envelope.has_more,
    limit: envelope.limit,
  }) as Record<string, unknown>;
}
