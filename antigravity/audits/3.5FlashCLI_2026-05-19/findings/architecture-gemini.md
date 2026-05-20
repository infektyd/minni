# Architecture & Separations Findings (Gemini 3.5 Flash)
**Date:** 2026-05-19

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| ARCH-001 | P2 | [sovrd.py:286](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py#L286) | Hardcoded agent aliases in daemon. | `aliases = {"claude-code": "claudecode", ...}` | Move to config file. |
| ARCH-002 | P1 | `plugins/.../hook.ts:2` | Agent-specific constants in plugin contract. | `import { CLAUDECODE_AGENT_ID, ... }" | Use env vars or registry. |
| ARCH-003 | P2 | `plugins/.../server.ts:93` | Hardcoded agent ID in MCP tools. | `agentId: DEFAULT_AGENT_ID` | Resolve from principal. |
| ARCH-004 | P3 | [sovrd.py:1218](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py#L1218) | Leaking internal ID in public URI. | `return f"sm://doc/{doc_id}/chunk/{chunk_id}"` | Use content hashes for anchors. |
