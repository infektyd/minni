/**
 * AFM semantic tier for #147 — classifies unquoted multi-word tails after
 * high-risk credential keywords when the regex gate is inconclusive.
 *
 * Kept out of policy.ts so the sync regex path stays dependency-light and
 * unit-testable without spinning up AFM. Fail-OPEN on AFM off/error: the
 * regex tier remains authoritative; this pass only ADDS blocks when AFM
 * positively classifies a span as credential material. That means the #147
 * gap remains when AFM is off/unreachable — intentional enhancement, not a
 * hard replacement of the regex gate.
 */
import { callAfmJson, type CallAfmJsonOptions } from "./afm.js";
import { AFM_PREPARE_TASK_MODEL, AFM_PREPARE_TASK_URL, AFM_PROVIDER_MODE } from "./config.js";
import type {
  InconclusiveCredentialClassifier,
  InconclusiveCredentialVerdict,
  InconclusiveHighRiskAssignment,
} from "./policy.js";
import type { JsonResult } from "./sovereign.js";

const AFM_TIMEOUT_MS = 8_000;

type AfmCaller = (
  url: string,
  payload: Record<string, unknown>,
  options?: CallAfmJsonOptions,
) => Promise<JsonResult>;

function extractJsonObject(raw: string): unknown | undefined {
  const trimmed = raw.trim();
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1]?.trim();
  const candidates = [trimmed, fenced].filter((c): c is string => Boolean(c));
  const objectMatch = trimmed.match(/\{[\s\S]*\}/)?.[0];
  if (objectMatch) candidates.push(objectMatch);
  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch {
      // try next
    }
  }
  return undefined;
}

function contentFromResponse(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const record = data as Record<string, unknown>;
  const choices = record.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = choices[0];
    if (first && typeof first === "object") {
      const message = (first as { message?: unknown }).message;
      if (message && typeof message === "object") {
        const content = (message as { content?: unknown }).content;
        if (typeof content === "string") return content;
      }
    }
  }
  for (const key of ["answer", "content", "text"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

/** Exported for unit tests — normalize AFM JSON into a verdict. */
export function parseInconclusiveAfmVerdict(
  data: unknown,
): InconclusiveCredentialVerdict | undefined {
  const chat = contentFromResponse(data);
  const parsed = (chat ? extractJsonObject(chat) : undefined) ?? data;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
  const raw = (parsed as { verdict?: unknown }).verdict;
  if (typeof raw !== "string") return undefined;
  const verdict = raw.trim().toLowerCase();
  if (verdict === "credential" || verdict === "prose") return verdict;
  return undefined;
}

function buildMessages(spans: InconclusiveHighRiskAssignment[]): Array<{ role: string; content: string }> {
  const listed = spans
    .map(
      (s, i) =>
        `${i + 1}. keyword=${JSON.stringify(s.keyword)} value=${JSON.stringify(s.tail)}`,
    )
    .join("\n");
  return [
    {
      role: "system",
      content: [
        "Classify whether each assigned value is a real credential/passphrase being stored,",
        "or ordinary prose ABOUT credentials (advice, procedure, permission names).",
        "Examples of credential: diceware/passphrase word lists, actual secret values.",
        'Examples of prose: "use a manager", "was rotated", "belongs in the keychain".',
        "Reply with ONLY JSON: {\"verdict\":\"credential\"} if ANY value is credential material,",
        "or {\"verdict\":\"prose\"} if ALL are prose. Do not echo the values back.",
      ].join(" "),
    },
    {
      role: "user",
      content: listed,
    },
  ];
}

export interface ClassifyInconclusiveAfmOptions {
  /** Test seam — defaults to live `callAfmJson`. */
  callAfm?: AfmCaller;
  providerMode?: typeof AFM_PROVIDER_MODE;
}

/**
 * Default AFM classifier for inconclusive high-risk assignments.
 * Injectable seam lives on `assessLearningQualityAsync` for gate tests;
 * `options.callAfm` covers the production classifier path in unit tests.
 */
export async function classifyInconclusiveWithAfmImpl(
  spans: InconclusiveHighRiskAssignment[],
  options: ClassifyInconclusiveAfmOptions = {},
): Promise<InconclusiveCredentialVerdict> {
  if (spans.length === 0) return "prose";
  const mode = options.providerMode ?? AFM_PROVIDER_MODE;
  if (mode === "off") return "unavailable";

  const call = options.callAfm ?? callAfmJson;
  const result = await call(
    AFM_PREPARE_TASK_URL,
    {
      model: AFM_PREPARE_TASK_MODEL,
      temperature: 0,
      max_tokens: 64,
      messages: buildMessages(spans),
    },
    { mode, timeoutMs: AFM_TIMEOUT_MS },
  );
  if (!result.ok) return "unavailable";
  return parseInconclusiveAfmVerdict(result.data) ?? "unavailable";
}

export const classifyInconclusiveWithAfm: InconclusiveCredentialClassifier = (spans) =>
  classifyInconclusiveWithAfmImpl(spans);
