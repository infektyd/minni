# Separation Cuts Proposal (Draft)
**Date:** 2026-05-19
**Dimension:** Architecture & Separations
**Auditor:** Gemini 3.5 Flash

1.  **Dynamic Principal Mapping**: Externalize the `agent_id` to slug mapping from `sovrd.py` to a JSON configuration file.
2.  **Generic Hook Provider**: Refactor `hook.ts` to be a single, environment-aware entry point that loads agent configuration at runtime.
3.  **Transport-Based Identity**: Remove `DEFAULT_AGENT_ID` from the MCP server and force it to resolve identity from the transport-stamped `EffectivePrincipal`.
