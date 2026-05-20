# Scope Creep & Dead Code Findings (Agent A & B Synthesis)
**Date:** 2026-05-19

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| SCOPE-001 | P1 | `engine/migrations/` | Duplicate migration ID (007). | Two files named `007_*.sql` exist. | Rename and sequence migrations correctly. |
| SCOPE-002 | P1 | `openclaw-extension/sovrd.py` | Entire deprecated HTTP daemon remains in repo. | Superseded by `engine/sovrd.py` (JSON-RPC). | Remove deprecated directory. |
| SCOPE-003 | P2 | `engine/db.py` | Redundant schema logic duplicated in code and migrations. | `CREATE TABLE` in `db.py` vs `.sql` migrations. | Standardize on SQL migrations only. |
| SCOPE-004 | P2 | `backends/` | Non-functional stub backends (Qdrant, Lance). | Placeholders found in `vector_backend.py`. | Remove or complete implementation. |
| SCOPE-005 | P2 | `team-harvest.ts` | Triple-duplication of redaction logic. | Found in Python core and TS plugins separately. | Centralize redaction in the daemon. |
| SCOPE-006 | P3 | `assets/hermes/` | Legacy branding and assets for "Hermes" agent. | Documented as legacy but files remain. | Move to `_archive`. |
| SCOPE-007 | P3 | `team-harvest.ts:88` | Stale TODO regarding shared helper extraction. | Commented-out roadmap item. | Address or move to ticket system. |

### Architectural Dissent
- **Migration Duplication**: Both agents flagged the 007 collision as P1, indicating a high risk of database corruption for new users.
- **Redaction Logic**: Agent A emphasized the architectural cost of triple-duplication, while Agent B focused on the maintenance burden.
