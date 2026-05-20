# Architecture & Separations Audit - Claude Findings
**Date:** 2026-05-19
**Dimension:** Architecture & Separations
**Model:** Claude 4.6 Sonnet

## Executive Summary
The Sovereign Memory architecture exhibits a high degree of modularity but suffers from fragmented public surfaces and hardcoded agent-specific logic in the core daemon. The current "EffectivePrincipal" implementation (G11) provides a strong foundation for identity, but its rollout is incomplete, and many legacy paths still rely on unauthenticated claims.

## Findings

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| ARC-01 | P1 | engine/sovrd.py:287-293 | Hardcoded agent aliases in vault resolution logic. | [sovrd.py:L287-293](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py#L287-L293) | Move agent-to-vault mapping to an external configuration file or the principal JSON schema. |
| ARC-02 | P2 | engine/sovrd.py:1867 | Status API leaks absolute system paths (db_path, faiss_path). | [sovrd.py:L1867-1920](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py#L1867-L1920) | Redact absolute paths in public status reports; use relative paths from $HOME or $SOVEREIGN_HOME. |
| ARC-03 | P1 | engine/principal.py:376 | Hardcoded "main" and "operator" identities as having root-level governance capabilities. | [principal.py:L376](file:///Users/hansaxelsson/projects/sovereignMemory/engine/principal.py#L376) | Enforce explicit "govern" or "resolve_candidate" capabilities in principals/*.json instead of hardcoding names. |
| ARC-04 | P2 | plugins/sovereign-memory/src/server.ts | Overlapping and inconsistent tool surfaces (Daemon RPC vs MCP tools). | [server.ts](file:///Users/hansaxelsson/projects/sovereignMemory/plugins/sovereign-memory/src/server.ts) | Define a single canonical schema for Sovereign Memory operations and derive both RPC and MCP surfaces from it. |
| ARC-05 | P3 | engine/agent_api.py:3 | Legacy mentions of "Hermes" and "OpenClaw" in core logic documentation. | [agent_api.py:L3](file:///Users/hansaxelsson/projects/sovereignMemory/engine/agent_api.py#L3) | Update documentation to reflect the agent-agnostic "Sovereign Memory" branding. |

## Separation Cut Proposals
1. **Principal Authority**: Fully deprecate the `agent_id` parameter in all RPC methods where it is currently optional or unvalidated. Rely solely on the UDS-stamped `EffectivePrincipal`.
2. **Vault Isolation**: Move the `_agent_vault` resolution logic out of `sovrd.py` and into `principal.py`, making it a property of the `EffectivePrincipal`.
3. **Schema Unification**: Extract all JSON-RPC and MCP schemas into a shared `schema/` directory (e.g. as JSON Schema or Protobuf) to ensure parity between plugin and daemon.
