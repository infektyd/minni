# Audit Findings: Scope Creep & Dead Code (Agent A Findings)
**Dimension:** Scope Creep & Dead Code
**Model:** Gemini 3.5 Flash (High)
**Date:** 2026-05-19

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| SCOPE-A01 | P1 | `server.ts:808+` | Experimental "Team" tools registered in public MCP. | `sovereign_team_runtime`, `evidence`, etc. registered but "untested". | Move to experimental flag or remove. |
| SCOPE-A02 | P1 | Multiple | Triple-duplication of redaction logic. | Found in `sovrd.py`, `team-harvest.ts`, `agent_ping.ts`, and `afm.ts`. | Centralize to shared utility. |
| SCOPE-A03 | P2 | `openclaw-extension/` | Deprecated bridge code remains in tree. | Uses old "Phase 2" architecture. | Decommission and remove directory. |
| SCOPE-A04 | P2 | `engine/sovrd.py:188` | Legacy "dual-write" logic preserved "in case". | `_flatfile_append` writes to `~/.openclaw/MEMORY.md`. | Remove if Hermes is truly legacy. |
| SCOPE-A05 | P2 | `test_pr2_envelope.py:38` | Incomplete FAISS module skip in tests. | "faiss module is incomplete; numpy fallback path tested separately". | Complete module or remove stubs. |
| SCOPE-A06 | P3 | `team-harvest.ts:88` | Stale TODO (>30 days) for shared helper extraction. | `TODO: extract shared postJson helper`. | Extract helper to shared utils. |
