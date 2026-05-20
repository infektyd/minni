# Code Quality & Syntax Audit: Sovereign Memory (Agent A Findings)
**Dimension:** Code Quality & Syntax
**Model:** Gemini 3.5 Flash (High)
**Date:** 2026-05-19

## 1. Complexity Hotspots (Cyclomatic Complexity > 10)
| File | Method | Estimated Complexity | Issue |
|------|--------|----------------------|-------|
| `retrieval.py` | `retrieve` | 25+ | Massive "God method" handling FTS, Semantic, RRF, Re-ranking, HyDE, and Access Control in one block. |
| `retrieval.py` | `_apply_depth` | 15+ | Heavy branching on string literals for depth tiers. |
| `agent_api.py` | `startup_context` | 12+ | Orchestrates multiple DB queries and markdown formatting in a single block. |
| `sovrd.py` | `_handle_daemon_handoff` | 15+ | Deep nested validation and dual-write logic. |

## 2. Type Hinting Gaps
- **Public APIs**: `RetrievalEngine.retrieve` lacks return type hints for several inner functions.
- **Daemon Handlers**: Most `_handle_*` methods in `sovrd.py` use `params: dict` instead of `Dict[str, Any]` and lack `-> dict` return annotations.
- **Optional types**: Missing `Optional[]` wrappers for nullable database fields in `db.py` and `retrieval.py`.

## 3. Style & Maintenance
- **Mutable Defaults**: Found `allowed_vault_roots=[]` in `EffectivePrincipal` constructor (engine/principal.py). While shadowed by a factory, it's a risky pattern.
- **Docstrings**: Daemon handlers (`sovrd.py`) almost entirely lack docstrings explaining their JSON-RPC contract.
- **Raw String Exceptions**: Widespread use of `raise ValueError("raw string")` instead of defined `SovereignError` subclasses.
