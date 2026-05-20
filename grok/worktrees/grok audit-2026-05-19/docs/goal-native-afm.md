# Native AFM Wiring

Sovereign Memory no longer treats AFM only as an optional
OpenAI-compatible localhost bridge. The repo now has a native-provider
architecture with bridge compatibility, explicit opt-out, and graceful
downgrade.

## Completed Scope

- Added `engine/afm_provider.py` as the Python provider boundary.
- Added provider modes: `off`, `bridge`, `native`, and `auto`.
- Added `engine/native_afm_helper` plus `engine/native_afm_helper.swift`, a
  JSON stdin/stdout helper for Apple Foundation Models when the Foundation
  Models framework is available.
- Wired Python retrieval helpers for query expansion, neighborhood summaries,
  and HyDE generation through normalized provider results.
- Wired `session_distillation` to accept structured native compile proposals
  while preserving review-only draft discipline.
- Kept the existing localhost OpenAI-compatible bridge as the default
  compatibility path.
- Added status reporting for AFM mode, native availability, backend,
  availability, fallback state, and adapter configuration.
- Added adapter-awareness as metadata only. Adapter paths are sanitized and are
  not emitted in status packets or model-facing payloads.

## Provider Contracts

Native AFM operations exchange JSON only:

- `health` -> provider availability metadata.
- `query_expansion` -> `{ "queries": string[] }`
- `neighborhood_summary` -> `{ "summary": string }`
- `hyde_generation` -> `{ "answer": string }`
- `prepare_task` -> `{ "brief": string, "recommendedNextActions": string[], "risks": string[] }`
- `prepare_outcome` -> `{ "outcomeDraft": { "learnCandidates": string[], "logOnly": string[], "expires": string[], "doNotStore": string[] } }`
- `compile_pass_proposals` -> `{ "drafts": [{ "kind": string, "section": string, "title": string, "body": string, "sources": string[] }] }`

Bridge mode may still parse OpenAI-compatible chat-completion responses, but
call sites consume normalized data after the provider boundary.

## Safety Rules

- AFM behavior remains opt-in or explicitly configured.
- `bridge` is the default compatibility mode.
- `native` never silently falls back; it reports unavailable if the helper or
  Foundation Models backend is unavailable.
- `auto` prefers native when available and falls back to the bridge.
- `off` skips AFM calls entirely.
- AFM compile outputs remain review-only drafts. Native proposals cannot accept,
  endorse, or write durable memory by themselves.
- Private adapter paths, DB paths, vault material, logs, and raw sessions must
  stay out of public git and out of model-facing provider metadata.

## Verification

This work was verified with:

- Native helper smoke test returning an available Apple Foundation Models
  backend on the development machine.
- Focused Python AFM and compile tests.
- Full engine suite: `333 passed`.
- Plugin suite: `121 passed`.

Machines without the Foundation Models framework should still compile the helper
stub, report a native-unavailable status, and keep retrieval/compile workflows
operational through deterministic fallback or bridge mode.
