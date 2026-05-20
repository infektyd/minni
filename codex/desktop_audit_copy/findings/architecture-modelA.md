# Dimension 1: Architecture & Separations (Model A - Gemini 3.5 Flash)

## Public Surfaces Inventory & Evaluation

### 1. Daemon JSON-RPC API
* **Location:** `engine/sovrd.py` (specifically `_METHODS` registry at lines 2445-2472)
* **Documented?** Partially. Described conceptually in `docs/contracts/AGENT.md` but parameter schemas are not formalized.
* **Stable?** No. New endpoints like `resolve_candidate`, `stage_candidate`, and `daemon.endorse` have been recently added.
* **Leaks Implementation Details?** Yes. `_handle_status` (lines 1866-1919) returns absolute host filesystem paths for `socket_path`, `db_path`, and `faiss_path`, which can reveal the host username (e.g., `hansaxelsson`).

### 2. MCP Tool Surface
* **Location:** `plugins/sovereign-memory/src/server.ts`
* **Documented?** Yes, in `README.md` and inline schemas.
* **Stable?** Yes, provides a standardized bridge for 26 tools.
* **Leaks Implementation Details?** No. Sanitizes output before returning it to the agent.

### 3. Plugin Contract & Hooks
* **Location:** `plugins/sovereign-memory/src/hook.ts`, `codex-hook.ts`, `kilocode-hook.ts`
* **Documented?** Stale. `docs/runtime-integration.md` still documents the legacy `openclaw-extension` and refers to deprecated environment variables like `SOVEREIGN_DEFAULT_AGENT_ID` instead of `SOVEREIGN_AGENT_ID`.
* **Stable?** Transitional. Refactoring from OpenClaw to Multi-Plugin is incomplete.
* **Leaks Implementation Details?** No.

### 4. Vault File Schema
* **Location:** Obsidian vault folders (`wiki/`, `logs/`, `inbox/`, `outbox/`, `schema/`)
* **Documented?** Yes, in `docs/contracts/VAULT.md` and `docs/contracts/PAGE_TYPES.md`.
* **Stable?** Yes.
* **Leaks Implementation Details?** No.

### 5. Handoff Envelope Schema
* **Location:** `engine/sovrd.py` (`_validate_handoff_packet` at lines 429-471)
* **Documented?** No. The JSON wire format (e.g. `lease_id`, `requires_ack`, `kind`, `envelope`, `wikilink_refs`) is completely undocumented in public contracts.
* **Stable?** Yes, but changes would break compatibility without versioning.
* **Leaks Implementation Details?** No.

### 6. Identity Envelope Schema
* **Location:** `engine/principal.py`
* **Documented?** No. The `EffectivePrincipal` layout and Principal metadata JSON format (`principals/*.json`) are undocumented.
* **Stable?** Internal only.
* **Leaks Implementation Details?** No.

---

## Findings

### [P1] [Quality] Stale Integration Documentation & Deprecated Directory
* **File:** [docs/runtime-integration.md](file:///Users/hansaxelsson/Projects/sovereignMemory/docs/runtime-integration.md) and [openclaw-extension/](file:///Users/hansaxelsson/Projects/sovereignMemory/openclaw-extension)
* **Summary:** The integration docs reference the legacy `openclaw-extension` directory and stale environment variables (`SOVEREIGN_DEFAULT_AGENT_ID`), while the actual plugin uses `plugins/sovereign-memory` and `SOVEREIGN_AGENT_ID`. The `openclaw-extension` directory is deprecated but still exists in the root.
* **Evidence:** `docs/runtime-integration.md` lines 8-10, 31-38; existence of `openclaw-extension/` folder.
* **Recommendation:** Delete the `openclaw-extension/` directory. Update `docs/runtime-integration.md` to reference the `plugins/sovereign-memory` path and correct environment variables.

### [P2] [Info Leak] Absolute Path Leak in RPC Status Handler
* **File:** [engine/sovrd.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/sovrd.py#L1907-L1915)
* **Summary:** The `_handle_status` method returns absolute host paths (`socket_path`, `db_path`, `faiss_path`) to clients via the JSON-RPC interface. This exposes host directory layout and username details.
* **Evidence:** `engine/sovrd.py` lines 1907, 1913, 1915.
* **Recommendation:** Redact absolute paths or return them relative to the user's home directory (e.g. using `~`).
