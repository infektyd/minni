# Goal: Native AFM Wiring

Wire Sovereign Memory's AFM layer into a real native Apple Foundation Models integration, iteratively and safely, until the repo no longer treats AFM only as an optional OpenAI-compatible localhost bridge.

## Context

Sovereign Memory's thesis is local-first, LLM-first, agent-swarm-native. AFM means Apple Foundation Models: Apple's on-device + Private Cloud Compute model family accessed through the Foundation Models framework on iOS 26 / macOS 26. The desired direction is native Foundation Models usage for summarization, classification, and structured Generable output, with future adapter awareness. Cloud frontier models are consumers, not dependencies.

## Current State To Verify

- `engine/query_expand.py` and `engine/hyde.py` call an optional local OpenAI-compatible AFM bridge at `127.0.0.1:11437`.
- `plugins/sovereign-memory/src/task.ts` uses the same bridge for compact task/outcome distillation.
- `engine/afm_passes/`, `engine/afm_prompts/`, `engine/afm_writer.py`, and scheduler/compile entry points already provide deterministic review-first AFM pass discipline.
- AFM is currently a contract/socket/scheduler/draft pipeline, not yet native Foundation Models framework integration, not yet Generable structured output, and not yet adapter-aware.

## Hard Constraints

- Read live repo files first. Trust filesystem over prior summaries.
- Use existing architecture and naming where possible.
- Keep all AFM behavior opt-in or explicitly configured.
- Preserve privacy boundaries: do not expose raw sessions, raw vault material, local DB contents, adapter files, launchd plists, secrets, or private machine-local paths.
- Do not run adapter training or create private training datasets unless explicitly asked.
- Keep deterministic fallbacks for machines without macOS 26 / Foundation Models SDK.
- Review-first draft discipline stays intact: AFM may propose, classify, summarize, or structure, but must not silently accept durable memory changes.
- Tests must pass before calling the goal complete.

## Implementation Direction

1. Map current AFM surfaces.
   - Inspect `README.md`, `index.md`, `engine/query_expand.py`, `engine/hyde.py`, `engine/afm_passes/`, `engine/afm_prompts/`, `engine/afm_writer.py`, `engine/sovrd.py`, `engine/sovereign_memory.py`, `plugins/sovereign-memory/src/config.ts`, `plugins/sovereign-memory/src/task.ts`, and related tests.
   - Produce a short implementation note before editing: current call sites, desired provider boundary, and verification plan.

2. Introduce an AFM provider abstraction.
   - Add one conceptual interface for AFM operations used by the repo: query expansion, neighborhood summary, HyDE generation, prepare-task distillation, prepare-outcome distillation, and structured draft/pass output where appropriate.
   - Support modes: `off`, `bridge`, `native`, and `auto`.
   - Existing OpenAI-compatible bridge remains supported.
   - Native provider should be preferred when available and configured.

3. Add a native Foundation Models helper.
   - If the local SDK supports Foundation Models, implement a small Swift helper or service inside the repo that uses the Foundation Models framework directly.
   - If the SDK is unavailable, add a compile-safe stub and tests that prove graceful downgrade.
   - Keep the boundary local-only.
   - Expose stable JSON contracts to Python and Node callers.
   - Do not depend on cloud OpenAI-compatible semantics for native AFM output.

4. Add structured Generable-style contracts.
   - Define schemas for query expansion, neighborhood summary, task preparation distillation, outcome draft suggestions, and AFM compile-pass draft proposal.
   - Native AFM should return structured JSON matching these contracts.
   - Bridge fallback may continue to parse JSON from chat-completion style responses, but all call sites should consume the same normalized contract.

5. Wire call sites incrementally.
   - Update Python retrieval helpers first.
   - Then update plugin task/outcome preparation.
   - Then update compile-pass paths if needed.
   - Keep behavior unchanged when AFM is off or unavailable.
   - Add clear status/health reporting so `sovereign_status` can distinguish `off`, `bridge`, `native_available`, `native_unavailable`, and `fallback_used`.

6. Add adapter-awareness as metadata, not training.
   - Introduce config/status fields for future AFM adapters.
   - Detect/report adapter configuration safely if present.
   - Do not train, load private adapter files, or expose adapter paths unless sanitized.
   - Document what is implemented now versus future adapter training.

7. Update docs.
   - Update README/plugin docs to explain AFM modes.
   - Clearly state the native Foundation Models path, bridge fallback, environment variables, privacy behavior, and graceful degradation.
   - Avoid overstating: if native compile cannot run on this machine, document the stub/fallback and exact verification performed.

## Verification Required

- Run existing Python tests related to AFM/query expansion/compile passes.
- Run existing plugin tests related to task/outcome AFM behavior.
- Add new tests for provider selection, native-unavailable fallback, structured output normalization, status reporting, and privacy redaction.
- If native Foundation Models SDK is available, run a live smoke test against the native helper.
- If unavailable, prove the repo degrades cleanly and reports the reason.
- Do not finish until tests pass or every remaining failure is documented with exact cause and next step.

## Definition Of Done

- AFM is represented as a native-provider architecture, not only hardcoded `127.0.0.1:11437` bridge calls.
- Existing bridge behavior still works.
- Native Foundation Models integration is implemented where the SDK is available, with safe fallback where it is not.
- Call sites consume normalized structured AFM contracts.
- Status clearly reports native vs bridge vs unavailable.
- Compile/draft discipline remains review-first.
- Docs explain the wiring honestly.
- Tests cover the new behavior.
