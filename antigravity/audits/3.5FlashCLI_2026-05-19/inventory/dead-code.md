# Dead Code & Scope Creep Inventory
**Date:** 2026-05-19
**Auditors:** Gemini 3.5 Flash High (A & B)

## Orphaned / Legacy Files
| Path | Type | Status | Reason |
|------|------|--------|--------|
| `openclaw-extension/sovrd.py` | Python Script | Dead | Superseded by `engine/sovrd.py` (JSON-RPC). |
| `engine/migrate_v3_to_v3_1.py` | Python Script | Dead | Superseded by `engine/migrations/`. |
| `docs/ARCHITECTURAL-REVIEW-ROADMAP.md` | Documentation | Stale | Historical reference only; superseded by README.md. |
| `assets/hermes/` | Assets | Legacy | Branding for legacy Hermes agent. |
| `_archive/` | Directory | Backup | Old backups from April cleanup. |
| `_cleanup-quarantine/` | Directory | Backup | Old backups from April cleanup. |

## Feature Stubs ("In Case")
| Feature | Location | Status | Impact |
|---------|----------|--------|--------|
| Qdrant Vector Backend | `engine/backends/qdrant.py` | Stub | Placeholder only; requires manual implementation. |
| Lance Vector Backend | `engine/backends/lance.py` | Stub | Placeholder only; requires manual implementation. |
| Multi-Backend Fan-out | `engine/backends/multi.py` | Partial | Logic exists but relies on stubs. |
| Experimental Team Tools | `server.ts:808+` | Untested | Tools registered but explicitly labeled "untested". |

## Debt Markers (TODOs/FIXMEs)
| File | Line | Type | Summary |
|------|------|------|---------|
| `plugins/sovereign-memory/src/team-harvest.ts` | 88 | TODO | Extract shared postJson helper. |
| `engine/sovrd.py` | 188 | Legacy | Legacy "dual-write" logic preserved for MEMORY.md. |

## Redundancy & Collisions
| Primitive | File A | File B | Conflict |
|-----------|--------|--------|----------|
| Migration 007 | `007_candidate_packets.sql` | `007_handoff_contradiction_events.sql` | Version number collision. |
| Schema (layer) | `engine/db.py` | `004_layer_column.sql` | Redundant definition in base schema. |
| Redaction Logic | `engine/sovrd.py` | `team-harvest.ts` | Triple-duplication of logic. |
