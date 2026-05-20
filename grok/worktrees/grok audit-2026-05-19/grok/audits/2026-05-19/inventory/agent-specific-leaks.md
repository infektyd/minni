# Inventory: Agent-Specific Leaks & Backend Import Violations (Sovereign Memory RC Audit 2026-05-19)

**Source:** Phase 1a architecture.md (15+ sites table with risk, 8 leaks, 3 violations, 101 tool calls) + Phase 2e security.md (how leaks + non-strict principal + per-agent wiring enable P0 bypasses) + scope-creep.md (legacy surfaces).

## Agent-Specific Logic Sites (15+ — highest risk first)

| file:line | Condition / Branch | Risk (from architecture + security) | Cross-ref |
|-----------|--------------------|-------------------------------------|---------|
| sovrd.py:285-294 | _default_agent_vault explicit aliases map (claude-code→claudecode, codex, hermes, openclaw, ...) + slugify fallback | High — core daemon now couples to 5 known names; custom agents get generic "-vault" | P1 ARC-P1-01 |
| sovrd.py:297-310 | _agent_vault: SOVEREIGN_{UPPER}_VAULT_PATH env + SOVEREIGN_AGENT_VAULTS JSON per agent_id | Medium — powerful but fragments "single daemon" model | security.md principal spoof compounds |
| sovrd.py:356-369 | _known_agent_vaults: glob "*-vault" + JSON map (used for list_pending without agent) | Medium — discovery heuristic assumes naming convention from aliases | SEC-P0-03 (ping writes) |
| sovrd.py:640-644 | _iter_handoff_files: if agent_id then single _agent_vault else all _known | Medium — cross-agent listing behavior depends on agent-specific discovery | |
| principal.py:442 | can_read_document: if agent == principal.agent_id or agent == "unknown" | Low-Medium — special-cases "unknown" + self; later wiki/handoff exceptions | |
| principal.py:251+ / 323+ / 376 | resolve_effective_principal legacy "main" synthesis + CANONICAL + is_operator_principal treats synthesized "main" as operator | High (P0 when non-strict) — "main" treated as default/legacy operator | SEC-P0-02 (non-strict synthesis) |
| agent_api.py:81,236 | identity_context/startup_context: queries for 'identity:{self.agent_id}' + fallback (agent=='unknown' OR LIKE 'wiki:%') | Low — special tags for identity + shared wiki/unknown | |
| config.ts:44-52,62-100 | DEFAULT_AGENT_ID / DEFAULT_VAULT_PATH fallbacks to "codex" + separate CLAUDECODE_*/KILOCODE_* constants + envs | Medium — plugin (primary MCP surface) documents per-agent paths/IDs | |
| server.ts:93,370,384,475,484,663 | Every tool handler: agentId: DEFAULT_AGENT_ID (G11 stamping comment) + CLAUDECODE_AGENT_ID for negotiate | Medium — stamping improves security but still agent-aware logic | |
| hook.ts:101-262 (multiple) | Hardcoded CLAUDECODE_AGENT_ID / VAULT_PATH / CONTEXT_WINDOW for all 4 hooks + envelope agent= | High — Claude Code hook surface is entirely non-agnostic | |
| codex-hook.ts:116-331 (multiple) | Hardcoded DEFAULT_AGENT_ID ("codex") + "codex" strings in fallback_commands, kinds, audit | High — Codex-specific hook + CLI smoke paths | |
| agent_ping.ts:104 | ping map: kilocode: "kilocode" (plus others) | Low — mapping table | |
| seed_identity.py:22-88 | AGENTS dict only seeds 7 hardcoded agents (forge, syntra, recon, pulse, hermes, vidar, drift) with specific ~/.openclaw/... paths | High — identity bootstrap not open for arbitrary custom agents (despite AGENT.md:28 claim) | ARC-P1-01 |
| commands/recall.md + others | Hard "claude-code" in example arg + agentId | Low — doc examples | |

## 8 Implementation Detail Leaks (all P2)

1. db_path / faiss_path / faiss_cache_age_seconds in status/health (sovrd.py:1913-1915, 1922+, 1961)
2. "backend", "doc_id", "chunk_id" in provenance (AGENT.md:177 example + retrieval.py)
3. _agent_vault aliases + per-agent env derivation errors (sovrd.py:286+)
4. whole_document / identity: prefix + chunk_index=0 visible in read/identity queries (sovrd.py:1342, agent_api.py:82-84)
5. Direct FS paths in handoff page compilation frontmatter (sovrd.py:474+)
6. Python module names in some error paths and status
7. VAULT.md:12-36 omits "outbox"/".obsidian" present in vault.ts:117-118 + ensureVault
8. Legacy "main"/"unknown"/"wiki:*" tags in retrieval filters and can_read_document

## Backend Import Violations (exactly 3 — all in deprecated surfaces)

- openclaw-extension/sovrd.py:46 (`from agent_api import`), :57 (sqlite3 + DB_PATH + ENGINE_PATH hack)
- migrate_phase2.py:19 (direct sqlite3)
- engine/openclaw-tool.sh:32 (direct exec of agent_api.py, hardcoded old ~/.openclaw/... paths)

**Core sovereign-memory TS plugin src/ is clean** (no engine imports; only JSON-RPC via sovereign.ts or intended vault.ts helpers).

**See also:** proposals/separation-cuts.md (recommended deletion of openclaw surfaces + removal of non-canonical aliases), security.md (how these leaks + non-strict principal enable P0 spoofing and cross-agent writes), architecture.md (full 15+ table + risk ratings).