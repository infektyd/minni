# Native AFM Implementation Note

## Implemented Surfaces

- `src/minni/afm_provider.py` owns shared AFM mode resolution, native helper
  invocation, bridge fallback, status reporting, and path/error sanitization.
- `src/minni/query_expand.py` uses native provider operations for query expansion
  and neighborhood summaries, with bridge compatibility and off-mode downgrade.
- `src/minni/hyde.py` uses native provider operations for HyDE probes, with bridge
  compatibility and native-unavailable downgrade.
- `src/minni/afm_passes/session_distillation.py` can request structured native
  compile proposals and normalizes them into review-only drafts with exact
  source citations.
- `src/minni/native_afm_helper.swift` implements the Foundation Models helper when
  available and returns structured JSON. The wrapper script compiles and caches
  the helper locally.
- `plugins/minni/src/afm.ts` and task preparation paths expose the
  same provider modes for prepare-task and prepare-outcome distillation.

## Provider Modes

| Mode | Result |
| --- | --- |
| `off` | AFM calls are skipped. |
| `bridge` | Calls the existing localhost OpenAI-compatible bridge. |
| `native` | Calls the configured JSON helper and reports unavailable on failure. |
| `auto` | Tries native first, then falls back to bridge. |

`MINNI_AFM_PROVIDER_MODE` is the preferred environment variable.
`MINNI_AFM_MODE` remains the Python helper alias. Unset, both resolvers
(`resolve_afm_mode` in `src/minni/afm_provider.py`, `resolveAfmMode` in
`plugins/minni/src/afm.ts`) default to `bridge` — but the daemon's launchd
plist (`src/minni/launchd/com.minni.minnid.plist.example`) sets
`MINNI_AFM_PROVIDER_MODE=native` in its `EnvironmentVariables`, so a
deployed daemon runs native mode day to day and the bridge is the
fallback/compat path. Hook, MCP, and shell processes spawned outside that
launchd environment do not inherit the variable; since PR #200, an
unconfigured caller defers to the daemon's own AFM verdict instead of
defaulting to a cold bridge probe (see `plugins/minni/src/sovereign.ts`).

## Normalized Contracts

- query expansion: `{ "queries": string[] }`
- neighborhood summary: `{ "summary": string }`
- HyDE generation: `{ "answer": string }`
- prepare-task distillation: `{ "brief": string, "recommendedNextActions": string[], "risks": string[] }`
- prepare-outcome distillation: `{ "outcomeDraft": { "learnCandidates": string[], "logOnly": string[], "expires": string[], "doNotStore": string[] } }`
- compile-pass proposals: `{ "drafts": [{ "kind": string, "section": string, "title": string, "body": string, "sources": string[] }] }`

Call sites should consume these normalized shapes rather than provider-specific
chat-completion envelopes.

## Privacy and Public Repo Boundary

Status and provider metadata may report:

- mode
- provider/backend
- availability
- native availability
- fallback status
- whether an adapter is configured

Status and provider metadata must not report:

- private adapter paths
- raw vault content
- local database paths
- session logs
- launchd plists with machine paths
- private datasets or model artifacts

## Verification Notes

The initial native AFM implementation was verified with the full engine suite,
the full plugin suite, focused provider tests, and a live native-helper smoke
test on a machine where Apple Foundation Models were available. The helper is
designed to degrade cleanly on machines without the framework.
