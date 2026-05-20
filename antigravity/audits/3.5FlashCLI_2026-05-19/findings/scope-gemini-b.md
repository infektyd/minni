# Audit Findings: Scope Creep & Dead Code (Agent B Findings)
**Dimension:** Scope Creep & Dead Code
**Model:** Gemini 3.5 Flash (High)
**Date:** 2026-05-19

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| SCOPE-B01 | P1 | `engine/migrations/` | **Duplicate Migration Version (007)** | `007_candidate_packets.sql` and `007_handoff_contradiction_events.sql` collision. | Rename and re-sequence. |
| SCOPE-B02 | P2 | `engine/db.py` | **Duplicate Schema Initialization Logic** | `db.py` manually initializes tables also defined in `.sql` migrations. | Rely on migrations for all schema changes. |
| SCOPE-B03 | P2 | `engine/migrate_v3_to_v3_1.py` | **Legacy Migration Script** | Manual precursor to SQL migrations; redundant and risky. | Delete and verify SQL coverage. |
| SCOPE-B04 | P2 | `engine/backends/` | **Stub Vector Backends (Feature Creep)** | `qdrant.py` and `lance.py` are non-functional placeholders. | Remove or move to experimental. |
| SCOPE-B05 | P3 | `test_candidate_lifecycle.py:90` | **Environment-Dependent Skipped Tests** | Tests skipped based on file presence (`server.ts`, etc.). | Ensure artifact parity in CI. |
| SCOPE-B06 | P3 | `docs/ARCHITECTURAL-REVIEW-ROADMAP.md` | **Stale Historical Documentation** | Explicitly historical reference; superseded by README. | Move to `_archive`. |
| SCOPE-B07 | P2 | `_archive/` | **Zombie Backup Directories** | April 2026 backups still in root. | Purge old backups. |
