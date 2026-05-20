# Agent-Specific Leaks Inventory
**Date:** 2026-05-19
**Dimension:** Architecture & Separations
**Auditor:** Gemini 3.5 Flash

| Location | Severity | Leak Description | Impact |
|----------|----------|------------------|--------|
| [sovrd.py:286](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py#L286) | P2 | Hardcoded aliases for `claude-code`, `codex`, `hermes`. | Hinders adding new agents without backend changes. |
| `engine/seed_identity.py:91-98` | P3 | Hardcoded sigil map for specific agents (forge, syntra, etc.). | Coupling between core engine and personas. |
| `plugins/.../hook.ts` | P1 | Hardcoded `CLAUDECODE_AGENT_ID` and vault paths. | Plugin is tied to a specific agent's environment. |
| `plugins/.../src/codex-hook.ts` | P2 | Agent-specific implementation of the hook contract. | Code duplication; lack of generic abstraction. |
