# Dimension 2: Scope Creep & Dead Code (Model B - Claude 4.6 Sonnet)

## Conceptual Analysis of Code Scope and Design Decay

### Multi-Language Logic Duplication (Semantic Drift Risk)
Sovereign Memory duplicates core business logic across Python (daemon) and TypeScript (plugin/client). Key examples include:
- **Filename Slugification:** `_slugify` in `engine/sovrd.py` (line 332) uses basic character replacements, while `slugify` in `plugins/sovereign-memory/src/vault.ts` (line 129) uses normalization (`NFKD`). This divergence can cause the python indexer and typescript vault writer to produce different filenames for the same note title, leading to broken wikilinks.
- **Audit Log Escaping:** `_escape_audit_field` in `engine/sovrd.py` (line 380) and `escapeAuditField` in `plugins/sovereign-memory/src/vault.ts` (line 342) duplicate logic for preventing log forging.

This is architectural scope bloat: the daemon and the client should have a single source of truth for vault structural operations.

---

## Findings

### [P1] [Scope Creep] Deprecated HTTP Fallback Transport
* **File:** [engine/sovrd.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/sovrd.py#L36) and [plugins/sovereign-memory/src/sovereign.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/sovereign.ts)
* **Summary:** The daemon maintains an HTTP server fallback (`--port` flag) and the plugin contains client-side HTTP request fallback logic. While labeled as deprecated, this fallback remains in the codebase, introducing unnecessary surface area and potential security/socket-binding risks.
* **Evidence:** `engine/sovrd.py` line 36 and `plugins/sovereign-memory/src/sovereign.ts` `jsonRpcSocketRequestWithFallback`.
* **Recommendation:** Remove HTTP fallback logic entirely. Enforce UDS as the sole communication channel.

### [P1] [Design Gap] Duplicated Slugification Implementations (Friction & File Drift)
* **File:** [engine/sovrd.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/sovrd.py#L332) and [plugins/sovereign-memory/src/vault.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/vault.ts#L129)
* **Summary:** Slugification logic for vault page names is implemented twice in different languages with slightly different regexes and unicode normalization rules. This creates a high risk of semantic drift where the daemon cannot resolve files written by the plugin.
* **Evidence:** Difference in character replacements between `_slugify` in `engine/sovrd.py` and `slugify` in `plugins/sovereign-memory/src/vault.ts`.
* **Recommendation:** Move slugification entirely to the TS client layer, or enforce a strict parameter format where the daemon only accepts pre-slugified paths from the client.
