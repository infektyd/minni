# Inventory: Public Surfaces (Sovereign Memory RC Audit 2026-05-19)

**Source:** Aggregated from Phase 1a architecture.md (11 surfaces, 101 tool calls, executive table + per-surface drift) + Phase 2e security.md (attack surface on MCP/plugin layer) + cross-refs from all other reports.

## Canonical List (11 surfaces)

1. **Daemon API (JSON-RPC line-delimited + minimal HTTP fallback)**
   - Entry: sovrd.py:2478 (UDS 0700/0600), 2612 (HTTP --port 9900)
   - Registry: sovrd.py:2445-2472 (_METHODS, 25+ including sovereign_* aliases)
   - Documented: CAPABILITIES.md:22-39 (v1.0.0)
   - Leaks: status db_path/faiss_path (1913-1922), provenance backend/doc/chunk
   - Agent-agnostic: Partial (EffectivePrincipal stamping good; _agent_vault aliases + "main"/unknown branches remain)

2. **Python Agent API (SovereignAgent + CLI)**
   - agent_api.py:29 (class), 57 (identity_context), 94 (recall), 138 (learn), 190 (startup_context)
   - sovereign_memory.py:468 (CLI dispatch, 14 cmd_*)
   - Leaks: internal columns (whole_document, decay_score, sigil) in queries/responses
   - Zero direct test coverage (P1 from code-quality.md)

3. **MCP Tool Surface (26 sovereign_* tools)**
   - server.ts:48-926 (registerTool + zod inputSchemas)
   - Tools: sovereign_status, recall, prepare_task/outcome, learn, learning_quality, vault_write, audit_report/tail, negotiate/await/ack_handoff, ping_agent_*, team_*, route, drill, export_pack, resolve_candidate, subscribe_contradictions, compile_vault
   - Documented: SKILL.md:73-89 (~15 listed); actual 26 in code + connected surface
   - Drift + leaks: vaultPath still accepted in prepare/audit/negotiate (SEC-P0-01); afmProvider details exposed

4. **Claude Code Plugin Contract (SKILL + 4 hooks + 10 commands)**
   - SKILL.md:10-22 (spine integration), hooks/hooks.json:3-43, commands/*.md
   - Envelopes: agent_envelope.ts:54-81 (`<sovereign:context version="1" ...>`)
   - Hardcoded: CLAUDECODE_* constants throughout hook.ts (non-agnostic)

5. **Codex Plugin Contract (hooks + CLI)**
   - hooks/hooks-codex.json:1-38, codex-hook.ts:1-340
   - DEFAULT_AGENT_ID fallback "codex" (config.ts:44)

6. **KiloCode / Gemini / OpenClaw Extension Surfaces**
   - kilocode-hook.ts + package manifests; openclaw-extension/plugin.json + src/ (deprecated)
   - openclaw-extension/sovrd.py + engine/openclaw-tool.sh (direct backend imports — 3 violations)

7. **Vault File Schema (layout + frontmatter + rules)**
   - VAULT.md:12-36 (v1.0.0 canonical: raw/wiki/schema/logs/inbox/index/log)
   - Implemented: vault.ts:103-119 (VAULT_DIRS + extras "outbox"/".obsidian"), 309-334 (ensureVault)
   - Drift: VAULT.md omits outbox/.obsidian present in code

8. **Handoff Envelope Schema**
   - sovrd.py:429-471 (_validate_handoff_packet — required from/to_agent, kind, task, envelope, wikilink_refs, trace/lease)
   - task.ts:807-818 (HandoffPacket), 820 (build)
   - server.ts:638+ (negotiate/await/ack tools)
   - G23 containment only on daemon send side (plugin resolveVaultRef lacks it — SEC-P0-04)

9. **Identity Envelope Schema (Layer 1)**
   - AGENT.md:38-51 (reserved 'identity:<agent_id>', seed_identity.py example)
   - seed_identity.py:22-88 (hardcoded AGENTS dict only), 239 (INSERT agent= + whole_document=1)
   - agent_api.py:67-90 + sovrd.py:1330-1347 (identity_context + startup read)
   - Not open for arbitrary custom agents despite AGENT.md:28 claim

10. **OpenClaw Tool Wrapper Surface**
    - engine/openclaw-tool.sh + openclaw-extension/ (deprecated direct exec + HTTP bridge)

11. **Local Console / UI surfaces** (secondary)
    - sovrd.py HTTP status + ui-server.ts deep-research + frontend

## Cross-Cutting Observations
- Strong G11 stamping (server.ts:93,370+ "model can no longer supply agentId") mitigates some caller spoofing.
- Plugin FS layer (vault.ts ensure/search/list) has no EffectivePrincipal or G12/G23 equivalent on model-supplied paths (root cause of SEC-P0-01 and SEC-P0-04).
- Contracts (AGENT.md, VAULT.md, CAPABILITIES.md) are version-stamped v1.0.0 (2026-04-26) but lag implementation on shipped features and leak details.

**See also:** architecture.md (full per-surface drift + agent-specific table), security.md (attack surface on 1,3,4,7,8,9), proposals/separation-cuts.md (recommended cuts).