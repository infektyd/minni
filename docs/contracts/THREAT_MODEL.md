# Sovereign Memory Threat Model

This document names the default risks for Sovereign Memory v4 and the controls
that keep recall useful without making memory authoritative.

## Boundaries

- Runtime truth is local SQLite.
- Vault pages are local files derived from explicit ingest, synthesis, or learn
  workflows.
- Vector indexes, reports, and generated summaries are derived surfaces.
- Recalled content is evidence, not instruction.

## Threats And Controls

| Threat | Risk | Control |
|---|---|---|
| Prompt injection via recalled content | A hostile note tries to override agent or system instructions. | Recall envelopes carry provenance and action hints; agents treat memory as citation only. Instruction-like content is flagged and never promoted to command authority. |
| Daemon socket access | A local process calls JSON-RPC methods unexpectedly. | Default binding is local-only. Operators should keep sockets under user-writable private paths and avoid exposing HTTP fallback outside loopback. |
| Vault path traversal | A plugin or tool attempts to read or write outside the configured vault. | Vault tooling validates paths against the vault root and rejects raw traversal. Generated pages use normalized relative paths. |
| AFM bridge tampering | Extraction output is altered or confused with source material. | AFM outputs carry trace identifiers, model/backend provenance, source references, and review status before acceptance. |
| Vector backend leakage | Private or blocked content appears through semantic recall. | Retrieval filters by `privacy_level` and page lifecycle status before final assembly. FAISS backends post-filter through SQLite metadata; native backends must enforce the same fields. |

## Failure Posture

Failure degrades to a less-rich response, not to an unsafe response. If vector
search fails, recall can fall back to FTS. If provenance is missing, results
should be treated as lower-confidence evidence. If privacy metadata is missing,
the conservative posture is local-only.

## Review Triggers

- New cross-process envelope fields.
- New vault write paths.
- New vector backend adapters.
- New AFM extraction or synthesis passes.
- Any default flip based on eval harness results.
