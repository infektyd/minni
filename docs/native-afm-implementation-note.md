# Native AFM Implementation Note

## Current Surfaces

- `engine/query_expand.py` owns rule expansion, AFM query expansion, and neighborhood summaries. Its AFM path posts OpenAI-compatible chat-completions payloads to `http://127.0.0.1:11437/v1/chat/completions`.
- `engine/hyde.py` generates HyDE probes through the same chat-completions shape, with `SOVEREIGN_HYDE_AFM_URL` and `SOVEREIGN_HYDE_AFM_MODEL` overrides.
- `engine/retrieval.py` consumes `query_expand.expand`, `query_expand.summarize_with_afm`, and `hyde.generate_hypothetical_answer`; it already degrades when AFM returns no text.
- `engine/afm_passes/`, `engine/afm_prompts/`, `engine/afm_writer.py`, and the CLI compile path keep review-first deterministic draft discipline. Current pass runners do not require live model output.
- `plugins/sovereign-memory/src/task.ts` has a separate prepare-task/outcome AFM boundary. It posts either chat-completions payloads or raw JSON to a configured URL and normalizes partial packet fields.
- `plugins/sovereign-memory/src/sovereign.ts` reports AFM health by probing a configured bridge health URL.

## Provider Boundary

The first provider boundary should normalize AFM operations before call sites inspect results:

- query expansion: `{ "queries": string[] }`
- neighborhood summary: `{ "summary": string }`
- HyDE generation: `{ "answer": string }`
- prepare-task distillation: `{ "brief": string, "recommendedNextActions": string[], "risks": string[] }`
- prepare-outcome distillation: `{ "outcomeDraft": { "learnCandidates": string[], "logOnly": string[], "expires": string[], "doNotStore": string[] } }`
- compile-pass proposals: `{ "drafts": [{ "kind": string, "section": string, "title": string, "body": string, "sources": string[] }] }`

Provider modes are `off`, `bridge`, `native`, and `auto`. Bridge mode preserves existing localhost chat-completions behavior. Native mode talks to a local helper through JSON only. Auto mode prefers native when available and falls back to the bridge. All modes must degrade without throwing into retrieval or silently writing durable memory.

## Verification Plan

1. Add failing Python tests for provider mode selection, native-unavailable downgrade, bridge normalization, and call-site consumption.
2. Implement the minimal Python provider abstraction and wire `query_expand.py` and `hyde.py`.
3. Keep the Swift helper compile-safe and JSON-only; it now covers retrieval helper operations plus prepare-task/outcome distillation.
4. Extend the normalized compile-pass proposal shape beyond `session_distillation` if synthesis/procedure/reorganization/pruning need native proposals.
5. Run focused Python AFM tests first, then plugin AFM tests, then the broader suites before considering the goal complete.
