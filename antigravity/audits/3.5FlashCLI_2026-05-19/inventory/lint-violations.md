# Sovereign Memory Lint & Quality Inventory
**Dimension:** Code Quality & Syntax
**Date:** 2026-05-19

## 1. Complexity Violations (Target < 10)
| Function | File | Estimated Complexity | Priority |
|----------|------|----------------------|----------|
| `RetrievalEngine.retrieve` | `retrieval.py` | 28 | P0 (Refactor) |
| `indexer.index_vault` | `indexer.py` | 18 | P1 |
| `_handle_daemon_handoff` | `sovrd.py` | 15 | P1 |
| `SovereignAgent.startup_context`| `agent_api.py`| 12 | P2 |

## 2. Type Hint Gaps (High Priority)
- [ ] `engine/db.py`: Missing return types for all cursor-management helpers.
- [ ] `engine/vector_sync.py`: Argument typing missing in `_compute_delta`.
- [ ] `engine/sovrd.py`: JSON-RPC handlers return `dict` instead of structured `TypedDict` or `Response` objects.

## 3. Style & Standards
- **Mutable Default**: `engine/principal.py:255` (`allowed_vault_roots=[]`).
- **Naming Inconsistency**: Mix of `snake_case` and `camelCase` found in `app.js` (bridged to Python).
- **Hardcoded Paths**: Multiple references to `~/.openclaw` instead of using the config-driven `SOVEREIGN_ROOT`.

## 4. Documentation Coverage
- **Indexer**: 40% (Public methods lack JSDoc/Docstrings).
- **Daemon**: 20% (Handler contracts undocumented).
- **Core Engine**: 85% (Good coverage, but needs update for V3.1).
