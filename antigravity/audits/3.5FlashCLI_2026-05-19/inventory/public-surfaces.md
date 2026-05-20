# Public Surfaces Inventory
**Date:** 2026-05-19
**Dimension:** Architecture & Separations
**Auditor:** Gemini 3.5 Flash

| Surface | Location | Status | Documented? | Impl Detail Leaks? | Agent-Agnostic? |
|---------|----------|--------|-------------|--------------------|-----------------|
| Daemon API (JSON-RPC) | [sovrd.py](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py) | Stable (V3.1) | Yes (Docstrings) | Yes (DB IDs, backend names) | Yes (mostly) |
| Agent API (Python) | [agent_api.py](file:///Users/hansaxelsson/projects/sovereignMemory/engine/agent_api.py) | Stable | Yes | No | Yes |
| MCP Tool Surface | `plugins/sovereign-memory/src/server.ts` | Stable | Yes (Schemas) | Yes (AFM params, tokens) | No (hardcoded default) |
| Plugin Hook Contract | `plugins/sovereign-memory/src/hook.ts` | Stable | Yes | No | No (agent-specific hooks) |
| Vault File Schema | `~/.sovereign-memory/` | Stable | No (Implicit) | No | No (vault per agent) |
| Handoff Envelope | [sovrd.py](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py) | Stable | Yes (PRs) | No | Yes |
| Identity Envelope | [agent_api.py](file:///Users/hansaxelsson/projects/sovereignMemory/engine/agent_api.py) | Stable | Yes | No | Yes |
