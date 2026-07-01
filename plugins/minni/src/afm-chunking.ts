// AFM Chunked Calling — TypeScript mirror of engine/afm_chunking.py.
//
// Two-language mirrored boundary (same precedent as providers.ts mirroring
// model_provider.py): this module gives task.ts's prepare_task/prepare_outcome
// call site (the exact op that hit the reported 4096-token context-overflow
// incident) the same recovery the Python side gets, without inventing any
// new Swift-side operation.
//
// Only list-splitting is needed here (not a text splitter): the oversized
// field at this call site is relevantSources, already an array, not raw
// prose. AFM calls are free (on-device) — nothing here economizes on call
// count; MIN_CHUNK_TOKENS/group count is the only real floor.

export const AFM_INPUT_BUDGET_TOKENS = 3200;
export const MIN_CHUNK_TOKENS = 200;

/**
 * Estimate tokens for a payload via a chars/4 heuristic — the common
 * rule-of-thumb approximation for BPE-style tokenizers (~4 chars/token in
 * English) used when no tokenizer is available. engine/tokens.py's fallback
 * is words/0.75, but that's whitespace-dependent and compact
 * JSON.stringify output has no spaces between fields/array elements, so a
 * word split would collapse an entire serialized object into a single
 * "word" and silently under-count it. Python's fallback doesn't hit this in
 * practice because tiktoken (BPE, punctuation-aware) is normally present
 * there; this side has no tokenizer dependency at all, so the estimate
 * needs to work directly off the serialized string length instead.
 */
export function estimateNativePayloadTokens(payload: Record<string, unknown>): number {
  const serialized = JSON.stringify(payload) ?? "";
  return estimateTextTokens(serialized);
}

function estimateTextTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

/**
 * Greedy bin-packing of items into groups whose serialized total stays
 * within budgetTokens. A single item that alone exceeds budgetTokens still
 * gets its own group.
 */
export function splitListByTokenBudget<T>(
  items: T[],
  budgetTokens: number,
  serialize: (item: T) => string = (item) => JSON.stringify(item),
): T[][] {
  const groups: T[][] = [];
  let current: T[] = [];
  let currentTokens = 0;
  for (const item of items) {
    const itemTokens = estimateTextTokens(serialize(item));
    if (current.length > 0 && currentTokens + itemTokens > budgetTokens) {
      groups.push(current);
      current = [];
      currentTokens = 0;
    }
    current.push(item);
    currentTokens += itemTokens;
  }
  if (current.length > 0) groups.push(current);
  return groups.length > 0 ? groups : [[]];
}

export interface NativeOpResult {
  ok: boolean;
  data?: Record<string, unknown>;
  error?: string;
}

function errorKindOf(result: NativeOpResult): string {
  const kind = result.data?.["error_kind"];
  return typeof kind === "string" ? kind.trim().toLowerCase() : "";
}

/**
 * Call callOp(payload) once if the payload is under budget (byte-identical
 * to today's un-chunked behavior). If over budget (proactively, by
 * estimateNativePayloadTokens, or reactively after a surprise
 * context_overflow), splits payload[listField] via splitListByTokenBudget
 * and calls callOp once per group, sequentially.
 *
 * Does NOT reduce results — see reduceViaSameOp for that.
 */
export async function callNativeOpChunked(
  callOp: (payload: Record<string, unknown>) => Promise<NativeOpResult>,
  payload: Record<string, unknown>,
  listField: string,
  budgetTokens: number = AFM_INPUT_BUDGET_TOKENS,
): Promise<{ results: NativeOpResult[]; wasChunked: boolean }> {
  const estimated = estimateNativePayloadTokens(payload);
  if (estimated <= budgetTokens) {
    const result = await callOp(payload);
    if (result.ok || errorKindOf(result) !== "context_overflow") {
      return { results: [result], wasChunked: false };
    }
    // Reactive: estimate was wrong. Fall through to chunking below.
  }

  const items = Array.isArray(payload[listField]) ? (payload[listField] as unknown[]) : [];
  if (items.length <= 1) {
    // Nothing to split further — make the call once more as-is and let the
    // caller see the failure.
    return { results: [await callOp(payload)], wasChunked: false };
  }

  const { [listField]: _omitted, ...basePayload } = payload;
  const baseTokens = estimateNativePayloadTokens(basePayload);
  const groupBudget = Math.max(budgetTokens - baseTokens, MIN_CHUNK_TOKENS);
  const groups = splitListByTokenBudget(items, groupBudget);

  const results: NativeOpResult[] = [];
  for (const group of groups) {
    results.push(await callOp({ ...payload, [listField]: group }));
  }
  return { results, wasChunked: true };
}

/**
 * Reduce N successful per-chunk results into one, by calling callOp again
 * with a payload built from the chunks' .data (buildReducePayload decides
 * the shape). If the reduce payload is itself over budget, recurses (tree
 * reduction) — AFM calls are free, so there is no single-pass ceiling.
 *
 * Returns undefined if there are zero successful results to reduce. Returns
 * the sole result unreduced if there is exactly one success.
 */
export async function reduceViaSameOp(
  callOp: (payload: Record<string, unknown>) => Promise<NativeOpResult>,
  results: NativeOpResult[],
  buildReducePayload: (partials: Record<string, unknown>[]) => Record<string, unknown>,
  listField: string,
  budgetTokens: number = AFM_INPUT_BUDGET_TOKENS,
): Promise<NativeOpResult | undefined> {
  const successes = results.filter((r) => r.ok && r.data).map((r) => r.data as Record<string, unknown>);
  if (successes.length === 0) return undefined;
  if (successes.length === 1) return results.find((r) => r.ok);

  const reducePayload = buildReducePayload(successes);
  const { results: reduced } = await callNativeOpChunked(callOp, reducePayload, listField, budgetTokens);
  const reducedSuccesses = reduced.filter((r) => r.ok);
  if (reducedSuccesses.length === 1) return reducedSuccesses[0];
  if (reducedSuccesses.length > 1) {
    return reduceViaSameOp(callOp, reducedSuccesses, buildReducePayload, listField, budgetTokens);
  }
  // Final safety net: every reduce attempt failed — fall back to the first
  // successful chunk rather than nothing.
  return results.find((r) => r.ok);
}
